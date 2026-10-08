"""Fail-closed, bounded model-only route for the approved Hosted A.X trial."""
import asyncio
import contextlib
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from uuid import UUID

import jwt
from jwt import PyJWKClient
from fastapi import Request

from hub import ChatRelay, Full

LOG = logging.getLogger("ax.hosted_model")


class Trial:
    def __init__(self, path: Path, *, tenant: str, audience: str, caller: str, deadline: float):
        self.tenant, self.audience, self.caller = (str(UUID(value)) for value in (tenant, audience, caller))
        self.deadline = deadline
        self.issuer = f"https://login.microsoftonline.com/{self.tenant}/v2.0"
        self.keys = PyJWKClient(f"https://login.microsoftonline.com/{self.tenant}/discovery/v2.0/keys",
                               timeout=5, lifespan=300)
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (request_id TEXT PRIMARY KEY, state TEXT NOT NULL)")

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    async def authorize(self, token: str):
        if not token or len(token) > 16384:
            raise jwt.InvalidTokenError("missing or oversized bearer")
        key = await asyncio.to_thread(self.keys.get_signing_key_from_jwt, token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.audience, issuer=self.issuer,
                            options={"require": ["exp", "nbf", "iat", "iss", "aud", "tid", "oid", "azp"]})
        if (claims["tid"] != self.tenant or claims["oid"] != self.caller or claims.get("ver") != "2.0"
                or claims.get("scp") or not isinstance(claims.get("roles"), list)
                or "Model.Invoke" not in claims["roles"]):
            raise jwt.InvalidTokenError("caller or role is not authorized")

    def reserve(self, request_id: str):
        if time.time() >= self.deadline:
            raise ValueError("trial_expired")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM calls WHERE request_id=?", (request_id,)).fetchone():
                raise ValueError("duplicate_request")
            if db.execute("SELECT 1 FROM calls WHERE state='active'").fetchone():
                raise ValueError("trial_busy")
            if db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] >= 3:
                raise ValueError("trial_budget_exhausted")
            db.execute("INSERT INTO calls VALUES (?, 'active')", (request_id,))
        LOG.info("hosted_gateway admitted request_id=%s profile=nvfp4 max_tokens=64", request_id)

    def finish(self, request_id: str):
        with self.connect() as db:
            db.execute("UPDATE calls SET state='closed' WHERE request_id=?", (request_id,))
        LOG.info("hosted_gateway closed request_id=%s", request_id)


def install(app, config, model_id, *, read_json, chat_body, is_ready, hub, gate, problem):
    values = [os.environ.get(name, "") for name in (
        "AX_HOSTED_TENANT", "AX_HOSTED_AUDIENCE", "AX_HOSTED_CALLER", "AX_HOSTED_DEADLINE")]
    trial = None
    if model_id == "nvfp4" and any(values):
        if not all(values):
            raise ValueError("Hosted trial configuration is incomplete")
        trial = Trial(config.data / "hosted-trial.sqlite", tenant=values[0], audience=values[1],
                      caller=values[2], deadline=float(values[3]))

    @app.post("/api/hosted/chat")
    async def hosted_chat(request: Request):
        if trial is None:
            raise problem(404, "hosted_disabled", "Hosted model trial is not enabled")
        header = request.headers.get("authorization", "")
        token = header[7:] if header[:7].lower() == "bearer " else ""
        try:
            await trial.authorize(token)
        except jwt.PyJWKClientConnectionError:
            LOG.warning("hosted_gateway key service unavailable")
            raise problem(503, "identity_unavailable", "Identity validation is temporarily unavailable")
        except (jwt.InvalidTokenError, jwt.PyJWKClientError):
            LOG.warning("hosted_gateway rejected bearer")
            raise problem(401, "hosted_identity", "Hosted model identity is not authorized")
        request_id = request.headers.get("x-ax-request-id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise problem(400, "request_id", "A 32-character request correlation ID is required")
        body = await read_json(request, 16384)
        messages = body.get("messages")
        if (set(body) != {"messages", "thinking", "max_tokens"} or body["thinking"] is not False
                or type(body["max_tokens"]) is not int or not 16 <= body["max_tokens"] <= 64
                or not isinstance(messages, list) or not 1 <= len(messages) <= 12
                or any(not isinstance(row, dict) or set(row) != {"role", "content"}
                       or row["role"] not in ("system", "user", "assistant")
                       or not isinstance(row["content"], str) or len(row["content"]) > 4096 for row in messages)):
            raise problem(400, "hosted_contract", "Only bounded text-only Hosted requests are supported")
        if not is_ready():
            raise problem(503, "not_ready", "A.X NVFP4 is not ready")
        model_body = chat_body(body)
        try:
            trial.reserve(request_id)
        except ValueError as exc:
            code = str(exc)
            raise problem(429 if code in ("trial_busy", "trial_budget_exhausted") else 409, code, code)

        class LimitedRelay(ChatRelay):
            released = False

            def close(self):
                super().close()
                if not self.released:
                    self.released = True
                    trial.finish(request_id)

            async def events(self):
                dispatched = False
                async with asyncio.timeout(45):
                    async for event in super().events():
                        if self.rid and not dispatched:
                            dispatched = True
                            LOG.info("hosted_gateway dispatched request_id=%s relay_id=%s profile=nvfp4",
                                     request_id, self.rid)
                        yield event

        try:
            relay = LimitedRelay(hub, gate, model_body)
        except Full:
            trial.finish(request_id)
            raise problem(503, "busy", "A.X relay queue is full")
        return relay.response()
