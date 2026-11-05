"""Offline signed-token and durable quota tests; no cloud/model calls."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo" / "frontend"))
from hosted_model import Trial, install
from hub import Gate
import app as frontend
sys.path.pop(0)

TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "22222222-2222-2222-2222-222222222222"
CALLER = "33333333-3333-3333-3333-333333333333"


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "hosted-trial.sqlite"
        self.trial = Trial(self.path, tenant=TENANT, audience=AUDIENCE, caller=CALLER,
                           deadline=time.time() + 3600)
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.key = jwt.PyJWK.from_json(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key()))
        self.patch = patch.object(self.trial.keys, "get_signing_key_from_jwt", return_value=self.key)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def token(self, **changes):
        now = int(time.time())
        claims = dict(iss=self.trial.issuer, aud=AUDIENCE, tid=TENANT, oid=CALLER,
                      azp=CALLER, ver="2.0", roles=["Model.Invoke"], iat=now, nbf=now, exp=now + 60)
        claims.update(changes)
        return jwt.encode(claims, self.private, algorithm="RS256")

    async def test_exact_signed_app_identity(self):
        await self.trial.authorize(self.token())
        for changes in ({"aud": "https://ai.azure.com"}, {"aud": "https://management.azure.com"},
                        {"aud": "https://graph.microsoft.com"}, {"tid": AUDIENCE}, {"oid": AUDIENCE},
                        {"roles": []}, {"roles": "Model.Invoke"}, {"scp": "user_impersonation"},
                        {"exp": 1}, {"nbf": int(time.time()) + 3600}, {"ver": "1.0"},
                        {"iss": "https://other.example/v2.0"}):
            with self.subTest(changes=changes), self.assertRaises(jwt.InvalidTokenError):
                await self.trial.authorize(self.token(**changes))
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        signed = jwt.encode(jwt.decode(self.token(), options={"verify_signature": False}), other, algorithm="RS256")
        with self.assertRaises(jwt.InvalidSignatureError):
            await self.trial.authorize(signed)

    def test_budget_is_durable_and_concurrency_is_one(self):
        self.trial.reserve("a" * 32)
        with self.assertRaisesRegex(ValueError, "trial_busy"):
            self.trial.reserve("b" * 32)
        restarted = Trial(self.path, tenant=TENANT, audience=AUDIENCE, caller=CALLER,
                          deadline=time.time() + 3600)
        with self.assertRaisesRegex(ValueError, "trial_busy"):
            restarted.reserve("b" * 32)
        self.trial.finish("a" * 32)
        with self.assertRaisesRegex(ValueError, "duplicate_request"):
            restarted.reserve("a" * 32)
        for key in ("b" * 32, "c" * 32):
            restarted.reserve(key)
            restarted.finish(key)
        with self.assertRaisesRegex(ValueError, "trial_budget_exhausted"):
            self.trial.reserve("d" * 32)
        self.trial.deadline = 0
        with self.assertRaisesRegex(ValueError, "trial_expired"):
            self.trial.reserve("e" * 32)

    async def test_route_disabled_and_bad_contract_never_reaches_model(self):
        app = FastAPI()

        @app.exception_handler(frontend.Problem)
        async def handle_problem(request, exc):
            return JSONResponse({"error": exc.code}, status_code=exc.status)

        config = frontend.Config(data=Path(self.temp.name))
        async def read_json(request, limit):
            return await request.json()

        with patch.dict("os.environ", {}, clear=True):
            install(app, config, "nvfp4", read_json=read_json, chat_body=frontend.chat_body,
                    is_ready=lambda: True, hub=None, gate=None, problem=frontend.Problem)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as api:
            response = await api.post("/api/hosted/chat", json={})
        self.assertEqual(response.status_code, 404)

        app = FastAPI()
        app.add_exception_handler(frontend.Problem, handle_problem)
        env = {"AX_HOSTED_TENANT": TENANT, "AX_HOSTED_AUDIENCE": AUDIENCE,
               "AX_HOSTED_CALLER": CALLER, "AX_HOSTED_DEADLINE": str(time.time() + 3600)}
        with patch.dict("os.environ", env), patch.object(Trial, "authorize"):
            install(app, config, "nvfp4", read_json=read_json, chat_body=frontend.chat_body,
                    is_ready=lambda: True, hub=None, gate=None, problem=frontend.Problem)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as api:
                for body in ({}, {"messages": [{"role": "tool", "content": "x"}], "max_tokens": 64, "thinking": False},
                             {"messages": [{"role": "user", "content": "x"}], "max_tokens": 65, "thinking": False},
                             {"messages": [{"role": "user", "content": "x"}], "max_tokens": 64, "thinking": False, "tools": []}):
                    response = await api.post("/api/hosted/chat", json=body,
                                              headers={"x-ax-request-id": "a" * 32})
                    self.assertEqual(response.status_code, 400, response.text)
        with self.trial.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 0)

    async def test_route_relays_model_only_and_closes_durable_admission(self):
        app = FastAPI()
        requests = []

        class Connection:
            streams = {}

            def send(self, message):
                requests.append(message)
                if message["type"] == "req":
                    queue = self.streams[message["id"]]
                    queue.put_nowait({"type": "head", "status": 200})
                    chunk = {"choices": [{"delta": {"content": "fixture"}, "finish_reason": "stop"}]}
                    queue.put_nowait({"type": "data", "chunk": f"data: {json.dumps(chunk)}\n\n"})
                    queue.put_nowait({"type": "end"})

        connection = Connection()
        class Hub:
            def ready_link(self):
                return connection

        async def read_json(request, limit):
            return await request.json()

        env = {"AX_HOSTED_TENANT": TENANT, "AX_HOSTED_AUDIENCE": AUDIENCE,
               "AX_HOSTED_CALLER": CALLER, "AX_HOSTED_DEADLINE": str(time.time() + 3600)}
        with patch.dict("os.environ", env), patch.object(Trial, "authorize"):
            install(app, frontend.Config(data=Path(self.temp.name)), "nvfp4", read_json=read_json,
                    chat_body=frontend.chat_body, is_ready=lambda: True, hub=Hub(), gate=Gate(1, 1),
                    problem=frontend.Problem)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as api:
                response = await api.post("/api/hosted/chat", headers={"x-ax-request-id": "a" * 32},
                                          json={"messages": [{"role": "user", "content": "fixture"}],
                                                "thinking": False, "max_tokens": 64})
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: end", response.text)
        self.assertEqual(requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(requests[0]["body"]["max_tokens"], 64)
        self.assertNotIn("tools", requests[0]["body"])
        self.assertEqual(connection.streams, {})
        with self.trial.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM calls").fetchall(), [("closed",)])
