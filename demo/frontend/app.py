"""The A.X K2 demo frontend on Azure App Service.

It serves the chat UI behind a shared password, relays chat requests to the GPU job's link (hub.py), runs
the supervisor that keeps one GPU job alive while the demo is on (supervisor.py), stores eval uploads, and
serves the results page and the admin console. Start it with:
  python -m uvicorn app:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers
Settings come from the environment (Config.from_env). The GPU job dials in at /ws/link with its token, so
the cluster needs no inbound port.
"""
import asyncio
import collections
import contextlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from hub import ChatRelay, Full, Gate, Hub
from store import RUN, Store, check_password, hash_password, new_password, token_digest
from supervisor import Supervisor, load_module
from workspace import LIMIT as FILE_LIMIT, Workspaces, safe_preview
from web_iq import WebIQ
from comparison import install as install_comparison

HERE = Path(__file__).resolve().parent
COOKIES = {"demo": ("axk2_demo", 24 * 3600), "admin": ("axk2_admin", 12 * 3600)}
STATIC_FILES = frozenset({"common.js", "markdown.js", "tools.js", "chat.js", "app.js", "admin.js", "style.css",
                          "chat.css"})
SUITES = ("aime", "kobalt", "click", "ifbench", "niah")
REPEATS = re.compile(r"[a-z0-9=,]{0,80}")
FIGURE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.png")
TOOL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
CHAT_LIMIT = 4 * 2**20
EVAL_LIMIT = 20 * 2**20
MAX_TOKENS = {True: 16384, False: 8192}  # thinking on / off
TOOLS = [
    {"type": "function", "function": {
        "name": "calculator",
        "description": "Evaluate an arithmetic expression exactly as written: + - * / ^ %, parentheses, sqrt, abs, "
                       "sin, cos, tan, log (base 10), ln, pi and e.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "for example (3 + 4) * 2 ^ 10 / sqrt(16)"}},
            "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "get_current_time",
        "description": "The current date and time in an IANA time zone.",
        "parameters": {"type": "object", "properties": {
            "timezone": {"type": "string", "description": "IANA time zone such as Asia/Seoul (the default)"}},
            "required": []}}},
]
TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}
SECURITY = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
            (b"x-frame-options", b"DENY"), (b"strict-transport-security", b"max-age=31536000"),
            (b"content-security-policy",
             b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
             b"connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]


def pairs(text, convert=str):
    out = {}
    for part in filter(None, (p.strip() for p in (text or "").split(","))):
        key, _, value = part.partition("=")
        if key.strip() and value.strip():
            out[key.strip()] = convert(value.strip())
    return out


@dataclass
class Config:
    data: Path = Path("/home/data")
    public_url: str = ""
    subscription: str = ""
    resource_group: str = ""
    workspaces: dict = field(default_factory=dict)  # region -> workspace name, in race order
    compute: str = "a100-nd96-lp"
    template: str = "demo-fp8-nd96.yml"
    aml_dir: Path = HERE / "aml"
    results_dir: Path = HERE / "static" / "results"
    session_secret: str = ""
    demo_hash: str = ""
    admin_hash: str = ""
    open_demo: bool = False  # internal demo: the chat page needs no password (admin still does)
    price: float = 8.192  # USD per node-hour (ND96amsr_A100_v4, low priority)
    prices: dict = field(default_factory=dict)
    max_active: int = 24
    max_queue: int = 50
    interval: float = 45
    idle_interval: float = 300
    nodes: int = 2
    partial_limit: float = 900
    retry_window: float = 300
    cooldown: float = 600
    stale_limit: float = 1200

    @property
    def link_url(self):
        parts = urlsplit(self.public_url)
        prefix = parts.path.rstrip("/")
        return f"{'wss' if parts.scheme == 'https' else 'ws'}://{parts.netloc}{prefix}/ws/link" if parts.netloc else ""

    def node_price(self, region):
        return self.prices.get(region, self.price)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        host = env.get("WEBSITE_HOSTNAME", "")
        number = lambda name, default: type(default)(env.get(name) or default)  # noqa: E731
        return cls(data=Path(env.get("AXK2_DATA") or "/home/data"),
                   public_url=env.get("AXK2_PUBLIC_URL") or (f"https://{host}" if host else ""),
                   subscription=env.get("AXK2_SUBSCRIPTION", ""), resource_group=env.get("AXK2_RESOURCE_GROUP", ""),
                   workspaces=pairs(env.get("AXK2_WORKSPACES")), compute=env.get("AXK2_COMPUTE") or "a100-nd96-lp",
                   template=env.get("AXK2_TEMPLATE") or "demo-fp8-nd96.yml",
                   aml_dir=Path(env.get("AXK2_AML_DIR") or HERE / "aml"),
                   results_dir=Path(env.get("AXK2_RESULTS_DIR") or HERE / "static" / "results"),
                   session_secret=env.get("AXK2_SESSION_SECRET", ""),
                   demo_hash=env.get("AXK2_DEMO_PASSWORD_HASH", ""), admin_hash=env.get("AXK2_ADMIN_PASSWORD_HASH", ""),
                   open_demo=env.get("AXK2_OPEN_DEMO", "").lower() in ("1", "true", "yes"),
                   price=number("AXK2_PRICE", 8.192), prices=pairs(env.get("AXK2_PRICES"), float),
                   max_active=number("AXK2_MAX_ACTIVE", 24), max_queue=number("AXK2_MAX_QUEUE", 50),
                   interval=number("AXK2_INTERVAL", 45.0), idle_interval=number("AXK2_IDLE_INTERVAL", 300.0),
                   nodes=number("AXK2_NODES", 2), partial_limit=number("AXK2_PARTIAL_LIMIT", 900.0),
                   retry_window=number("AXK2_RETRY_WINDOW", 300.0), cooldown=number("AXK2_COOLDOWN", 600.0),
                   stale_limit=number("AXK2_STALE_LIMIT", 1200.0))


class Problem(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def client_ip(request):
    """App Service appends the caller's address (ip:port) to X-Forwarded-For, so the rightmost entry is the one
    a client cannot forge; uvicorn's proxy-header handling trusts the leftmost one."""
    value = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
    if value.startswith("["):
        value = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value or (request.client.host if request.client else "unknown")


async def read_json(request, limit):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise Problem(415, "content_type", "Content-Type must be application/json")
    try:
        if int(request.headers.get("content-length") or 0) > limit:
            raise Problem(413, "too_large", "요청이 너무 큽니다.")
    except ValueError:
        raise Problem(400, "bad_request", "invalid Content-Length")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise Problem(413, "too_large", "요청이 너무 큽니다.")
    try:
        value = json.loads(body)
    except ValueError:
        raise Problem(400, "bad_json", "요청 형식(JSON)이 올바르지 않습니다.")
    if not isinstance(value, dict):
        raise Problem(400, "bad_json", "요청 형식(JSON)이 올바르지 않습니다.")
    return value


class Limiter:
    """Failed password attempts: per client address, and in total so that many addresses cannot add up."""

    def __init__(self, per_ip=8, total=1000, window=600, clock=time.time):
        self.per_ip, self.total, self.window, self.clock = per_ip, total, window, clock
        self.ips, self.all = {}, collections.deque()

    def prune(self):
        horizon = self.clock() - self.window
        while self.all and self.all[0] < horizon:
            self.all.popleft()
        for ip in list(self.ips):
            times = self.ips[ip]
            while times and times[0] < horizon:
                times.popleft()
            if not times:
                del self.ips[ip]

    def blocked(self, ip):
        self.prune()
        return len(self.ips.get(ip, ())) >= self.per_ip or len(self.all) >= self.total

    def failed(self, ip):
        now = self.clock()
        self.ips.setdefault(ip, collections.deque()).append(now)
        self.all.append(now)

    def succeeded(self, ip):
        self.ips.pop(ip, None)


class Auth:
    """Shared passwords (PBKDF2 hashes in app settings, or a rotated one in the store) and signed cookies.

    A cookie carries its role and the role's generation; rotating a password bumps the generation, which
    signs everyone out. A rotated password stays in force while the app setting it replaced is unchanged,
    so setting a new hash in the app settings takes over again.
    """

    def __init__(self, config, store):
        self.config, self.store = config, store
        self.signer = URLSafeTimedSerializer(config.session_secret or store.secret(), salt="axk2-demo-cookie")
        self.limiter = Limiter()

    def env_hash(self, role):
        return self.config.admin_hash if role == "admin" else self.config.demo_hash

    def stored_hash(self, role):
        override = self.store.data["passwords"].get(role)
        if isinstance(override, dict) and override.get("hash") and override.get("env", "") == self.env_hash(role):
            return override["hash"]
        return self.env_hash(role)

    async def check(self, role, password):
        stored = self.stored_hash(role)
        if not stored or not isinstance(password, str) or not 0 < len(password) <= 256:
            return False
        return await asyncio.to_thread(check_password, password, stored)

    def generation(self, role):
        return self.store.data["gen"].get(role, 0)

    def issue(self, role):
        return self.signer.dumps({"r": role, "g": self.generation(role)})

    def has(self, request, role):
        name, age = COOKIES[role]
        token = request.cookies.get(name)
        if not token:
            return False
        try:
            data = self.signer.loads(token, max_age=age)
        except BadSignature:
            return False
        return isinstance(data, dict) and data.get("r") == role and data.get("g") == self.generation(role)

    def set_cookie(self, response, request, role):
        name, age = COOKIES[role]
        response.set_cookie(name, self.issue(role), max_age=age, path="/", httponly=True, samesite="lax",
                            secure=request.url.scheme == "https")

    async def rotate(self, role):
        password = new_password()
        hashed = await asyncio.to_thread(hash_password, password)
        self.store.data["passwords"][role] = {"hash": hashed, "env": self.env_hash(role), "t": round(time.time())}
        self.store.data["gen"][role] = self.generation(role) + 1
        self.store.event("password", f"the {role} password was rotated; existing {role} sessions are signed out")
        return password


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        api = scope["path"].startswith("/api/")

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", [])) + SECURITY
                if api:
                    headers.append((b"cache-control", b"no-store"))
                message = dict(message, headers=headers)
            await send(message)

        await self.app(scope, receive, wrapped)


def chat_body(body):
    """The request sent to vLLM, built from an allow-list; sampling and tools are fixed here."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not 0 < len(messages) <= 400:
        raise Problem(400, "bad_messages", "messages must be a non-empty list")
    clean = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant", "tool"):
            raise Problem(400, "bad_messages", "unsupported message")
        role, content = message["role"], message.get("content")
        item = {"role": role, "content": content}
        if role == "assistant" and message.get("tool_calls"):
            calls = message["tool_calls"]
            if not isinstance(calls, list) or len(calls) > 8:
                raise Problem(400, "bad_messages", "invalid tool_calls")
            item["tool_calls"] = []
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                if (not isinstance(function, dict) or not TOOL_ID.fullmatch(str(call.get("id", "")))
                        or function.get("name") not in TOOL_NAMES or not isinstance(function.get("arguments"), str)
                        or len(function["arguments"]) > 4096):
                    raise Problem(400, "bad_messages", "invalid tool call")
                item["tool_calls"].append({"id": call["id"], "type": "function",
                                           "function": {"name": function["name"], "arguments": function["arguments"]}})
            if content is not None and not isinstance(content, str):
                raise Problem(400, "bad_messages", "content must be text")
            item["content"] = content or ""
        elif not isinstance(content, str):
            raise Problem(400, "bad_messages", "content must be text")
        if role == "assistant" and message.get("reasoning_content") is not None:
            # The chat template replays the reasoning of tool-call turns after the last user message.
            reasoning = message["reasoning_content"]
            if not isinstance(reasoning, str) or len(reasoning) > 200_000:
                raise Problem(400, "bad_messages", "invalid reasoning_content")
            item["reasoning_content"] = reasoning
        if role == "tool":
            if not TOOL_ID.fullmatch(str(message.get("tool_call_id", ""))):
                raise Problem(400, "bad_messages", "invalid tool_call_id")
            item["tool_call_id"] = message["tool_call_id"]
        clean.append(item)
    thinking = body.get("thinking", True) is not False
    try:
        wanted = int(body.get("max_tokens") or MAX_TOKENS[thinking])
    except (TypeError, ValueError):
        raise Problem(400, "bad_request", "max_tokens must be a number")
    out = {"model": "axk2", "messages": clean, "stream": True, "stream_options": {"include_usage": True},
           "chat_template_kwargs": {"enable_thinking": thinking}, "temperature": 0.6, "top_p": 0.95,
           "max_tokens": max(16, min(wanted, MAX_TOKENS[thinking]))}
    if body.get("tools"):
        out["tools"], out["tool_choice"] = TOOLS, "auto"
    return out


def agent_body(raw, space):
    if "max_tokens" in raw:
        value = raw["max_tokens"]
        maximum = MAX_TOKENS[raw.get("thinking", True) is not False]
        if isinstance(value, bool) or not isinstance(value, int) or not 16 <= value <= maximum:
            raise Problem(400, "generation", f"출력 상한은 16부터 {maximum}까지의 정수여야 합니다.")
    body = chat_body(raw)
    generation = raw.get("generation", {})
    if not isinstance(generation, dict) or set(generation) - {"temperature", "top_p"}:
        raise Problem(400, "generation", "지원하지 않는 생성 설정입니다.")
    for key, lower, upper in (("temperature", 0, 2), ("top_p", 0.000001, 1)):
        value = generation.get(key, body[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
            raise Problem(400, "generation", f"{key} 범위를 확인해 주세요.")
        body[key] = value
    if not isinstance(raw.get("tools", False), bool):
        raise Problem(400, "tools", "tools must be a boolean")
    attachments = raw.get("attachments", [])
    if (not isinstance(attachments, list) or len(attachments) > 100
            or any(not isinstance(p, str) for p in attachments)):
        raise Problem(400, "attachments", "attachments must be a bounded list of workspace file paths")
    try:
        for path in attachments:
            space.get(path)
    except ValueError as exc:
        raise Problem(400, "attachments", str(exc)) from exc
    pdfs = {p for p in attachments if p.lower().endswith(".pdf")}
    if len(pdfs) > 3:
        raise Problem(400, "attachments", "한 번에 PDF는 3개까지 읽을 수 있습니다.")
    if pdfs and not raw.get("tools"):
        raise Problem(400, "tools_disabled", "PDF를 읽으려면 도구를 켜 주세요. 실제 판독은 아직 하지 않았습니다.")
    if any(m["role"] not in ("user", "assistant") or m.get("tool_calls") for m in body["messages"]):
        raise Problem(400, "bad_messages", "Agent input accepts only user/assistant text, not client tool results")
    if body["messages"][-1]["role"] != "user":
        raise Problem(400, "bad_messages", "Agent input must end with the current user request")
    return body, attachments


def eval_spec(body):
    suites = body.get("suites") or ",".join(SUITES)
    repeats, limit = str(body.get("repeats") or ""), body.get("limit") or 0
    if (not isinstance(suites, str) or not set(suites.split(",")) <= set(SUITES)
            or not REPEATS.fullmatch(repeats) or not isinstance(limit, int) or not 0 <= limit <= 100000):
        raise Problem(400, "bad_eval", "invalid suites, repeats or limit")
    run = body.get("run") or time.strftime("ev%Y%m%d-%H%M%S", time.gmtime())
    if not isinstance(run, str) or not RUN.fullmatch(run):
        raise Problem(400, "bad_eval", "invalid run name")
    return {"run": run, "suites": suites, "repeats": repeats, "limit": limit}


def read_file_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def create_app(config=None, azure=None, start_supervisor=True, *, shared_auth=None, archives=None,
               profiles=True, model_id="fp8", shared_tool_gate=None, shared_web_iq=None):
    if model_id not in {"fp8", "nvfp4"}:
        raise ValueError("Unsupported model profile")
    config = config or Config.from_env()
    store = Store(config.data)
    gate = Gate(config.max_active, config.max_queue)
    hub = Hub(active=lambda: store.data["active"])
    supervisor = Supervisor(config, store, hub, azure=azure, archives=archives)
    auth = shared_auth or Auth(config, store)
    evals = load_module("axk2_evals", config.aml_dir / "src" / "evals.py")  # stdlib-only imports
    summaries = {}  # run -> (stamp, live summary)

    # ----------------------------------------------------------------------------------- link callbacks
    def note_eval(info, final=False):
        """Track the link's eval run; a run that finished or failed for good no longer needs to be wished."""
        data = store.data
        if not isinstance(info, dict) or not isinstance(info.get("run"), str) or not RUN.fullmatch(info["run"]):
            return
        run, state = info["run"], str(info.get("state", ""))
        entry = data["eval_runs"].setdefault(run, {"run": run, "created": round(time.time())})
        changed = entry.get("state") != state
        entry.update({key: info.get(key) for key in ("suites", "repeats", "limit", "started", "ended", "rc")
                      if info.get(key) is not None})
        entry["state"] = state
        if info.get("tail"):
            entry["tail"] = str(info["tail"])[-1500:]
        wish = data["eval_wish"]
        if wish and wish.get("run") == run and state in ("done", "failed"):
            data["eval_wish"] = None
        if changed and (final or state in ("done", "failed", "stopped")):
            store.event("eval", f"eval {run}: {state}" + (f" (exit {info.get('rc')})" if info.get("rc") else ""))

    def reconcile_eval(conn, force=False):
        """Send the wished eval to the active link unless it is already running it or another run."""
        data, wish = store.data, store.data["eval_wish"]
        if not wish or data["active"] != conn.digest:
            return
        status = conn.status or {}
        info = status.get("eval") or {}
        busy = info.get("state") in ("running", "stopping") or status.get("eval_pending")
        if not force and (busy or info.get("run") == wish["run"]
                          or time.time() - getattr(conn, "eval_sent", 0) < 60):
            return
        conn.eval_sent = time.time()
        conn.send(dict(wish, type="eval", action="start"))

    def on_hello(conn):
        data = store.data
        job = data["jobs"].get(conn.digest)
        if job is None:
            return
        job.update(link_seen=round(time.time(), 1), host=str(conn.hello.get("host", ""))[:80],
                   run_id=str(conn.hello.get("job", ""))[:120])
        if supervisor.promote(conn.digest, "its link connected"):
            supervisor.poke()  # cancel the other regions' jobs now
        if data["active"] == conn.digest:
            reconcile_eval(conn, force=True)

    def on_status(conn):
        if store.data["active"] == conn.digest:
            note_eval((conn.status or {}).get("eval"))
            reconcile_eval(conn)

    def on_eval(conn, message):
        if message.get("state") == "rejected":
            wish = store.data["eval_wish"]
            if wish and store.data["active"] == conn.digest:
                store.data["eval_wish"] = None
                store.data["eval_runs"].setdefault(wish["run"], {"run": wish["run"]})["state"] = "rejected"
            store.event("eval", f"the link rejected the eval request: {str(message.get('message', ''))[:200]}")
        else:
            note_eval(message, final=True)

    def on_close(conn):
        job = store.data["jobs"].get(conn.digest)
        if job is not None:
            job["link_seen"] = round(time.time(), 1)
            if store.data["active"] == conn.digest:
                store.event("link", f"{job['region']} {job.get('name')}: link closed", save=False)

    hub.on_hello, hub.on_status, hub.on_eval, hub.on_close = on_hello, on_status, on_eval, on_close

    # ------------------------------------------------------------------------------------------ views
    def is_profile_ready():
        conn = hub.ready_link()
        if conn is None:
            return False
        if model_id == "fp8":
            return True  # The already-running legacy link is retained without requiring a GPU restart.
        evidence = (conn.status or {}).get("provenance") or {}
        job = store.data["jobs"].get(store.data["active"]) or {}
        return (evidence.get("checkpoint") == "skt/A.X-K2-NVFP4"
                and evidence.get("revision") == "9e2e804e80f8d1b3afba5d7938173cec1ed46b49"
                and evidence.get("tp") == 8 and evidence.get("pp") == 1 and evidence.get("nodes") == 1
                and bool(job.get("source_sha")) and evidence.get("source_sha") == job["source_sha"])

    def power():
        data = store.data
        nodes = sum((n or {}).get("total", 0) for n in data["nodes"].values())
        if data["desired"] != "on":
            return "stopping" if data["jobs"] or nodes else "off"
        conn = hub.active_link()
        if conn and conn.ready:
            return "ready" if is_profile_ready() else "configuration_error"
        if conn:
            return "booting"
        if data["active"]:
            return "starting"
        return "waiting_capacity" if data["jobs"] else "starting"

    def public_status():
        data = store.data
        conn = hub.active_link()
        status = (conn.status or {}) if conn else {}
        job = data["jobs"].get(data["active"]) if data["active"] else None
        info = status.get("eval") or {}
        regions = {}
        for region in config.workspaces:
            nodes = data["nodes"].get(region) or {}
            jobs = [j for j in data["jobs"].values() if j["region"] == region]
            regions[region] = {"nodes": nodes.get("total", 0), "usable": nodes.get("usable", 0),
                               "job": jobs[0].get("status") if jobs else None,
                               "active": bool(job and job["region"] == region)}
        return {"power": power(), "desired": data["desired"], "region": job["region"] if job else None,
                "active_since": data["active_since"],
                "phase": status.get("phase"), "phase_since": status.get("since"), "note": status.get("note"),
                "download_gb": status.get("download_gb"), "download_of_gb": status.get("download_of_gb"),
                "load_step": status.get("load_step"), "load_pct": status.get("load_pct"),
                "healthy": bool(status.get("healthy")) and is_profile_ready(), "metrics": status.get("metrics"),
                "runtime_provenance":status.get("provenance"), "provenance_error":status.get("provenance_error"),
                "sampled": status.get("t"), "uptime": status.get("uptime"), "regions": regions,
                "nodes_per_job": config.nodes,
                "queue": {"active": gate.active, "waiting": len(gate.waiting), "max_active": gate.max_active,
                          "max_queue": gate.max_queue},
                "eval": {"run": info.get("run"), "state": info.get("state")} if info else None,
                "t": round(time.time(), 1)}

    def cost():
        seconds = store.data["cost"]["node_seconds"]
        regions = {region: {"node_hours": round(value / 3600, 3), "price": config.node_price(region),
                            "usd": round(value / 3600 * config.node_price(region), 2)}
                   for region, value in seconds.items()}
        return {"regions": regions, "usd": round(sum(r["usd"] for r in regions.values()), 2)}

    def admin_state():
        data = store.data
        jobs = []
        for digest, job in data["jobs"].items():
            conn = hub.links.get(digest)
            status = (conn.status or {}) if conn else {}
            jobs.append(dict(job, id=digest[:10], active=digest == data["active"], link=conn is not None,
                             ready=bool(conn and conn.ready), phase=status.get("phase"),
                             status_age=round(time.time() - conn.status_at, 1) if conn and conn.status_at else None,
                             inflight=status.get("inflight"), eval=status.get("eval"),
                             eval_pending=status.get("eval_pending"), eval_packages=status.get("eval_packages")))
        now = time.time()
        return {"status": public_status(), "jobs": jobs, "mode": data["mode"], "mode_since": data["mode_since"],
                "last_region": data["last_region"],
                "cooldown": {r: round(t - now) for r, t in data["cooldown"].items() if t > now},
                "nodes": data["nodes"], "cost": cost(), "events": data["events"][::-1],
                "eval_wish": data["eval_wish"], "eval_current": data["eval_current"],
                "eval_runs": sorted(data["eval_runs"].values(), key=lambda r: r.get("created") or 0, reverse=True),
                "passwords": {role: {"rotated": bool(auth.stored_hash(role) != auth.env_hash(role)),
                                     "configured": bool(auth.stored_hash(role)), "generation": auth.generation(role)}
                              for role in COOKIES},
                "config": {"regions": list(config.workspaces), "compute": config.compute, "nodes": config.nodes,
                           "price": config.price, "prices": config.prices, "link_url": config.link_url,
                           "max_active": config.max_active, "max_queue": config.max_queue},
                "links": len(hub.links), "t": round(now, 1)}

    def live_summary(run, posted):
        plan = (posted or {}).get("plan")
        if not isinstance(plan, dict):
            return None
        stamp = store.stamp(run)
        cached = summaries.get(run)
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            summary = evals.summarize(store.records(run), plan)
        except (KeyError, TypeError, ValueError, AttributeError):
            summary = None
        summaries[run] = (stamp, summary)
        return summary

    def results(meta, current):
        runs = []
        for run in store.runs():
            posted = store.summary(run)
            final = bool(posted and posted.get("state") == "done" and isinstance(posted.get("summary"), dict))
            runs.append({"run": run, "state": (meta.get(run) or {}).get("state") or (posted or {}).get("state"),
                         "posted_state": (posted or {}).get("state"), "plan": (posted or {}).get("plan"),
                         "seconds": (posted or {}).get("seconds"),
                         "summary": posted["summary"] if final else live_summary(run, posted)})
        folder = config.results_dir
        figures = sorted(p.name for p in folder.glob("*.png")) if folder.is_dir() else []
        return {"published": read_file_json(folder / "skt_published.json"),
                "throughput": read_file_json(folder / "throughput.json"), "figures": figures,
                "runs": runs[::-1], "current": current}

    # -------------------------------------------------------------------------------------------- app
    @contextlib.asynccontextmanager
    async def lifespan(app):
        store.event("frontend", "the frontend started")
        task = asyncio.create_task(supervisor.run()) if start_supervisor else None
        async def cleanup():
            while True:
                await asyncio.sleep(60)
                workspaces.prune()
        cleanup_task = asyncio.create_task(cleanup())
        try:
            async with contextlib.AsyncExitStack() as stack:
                for child in children.values():
                    await stack.enter_async_context(child.router.lifespan_context(child))
                yield
        finally:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for conn in list(hub.links.values()):
                conn.close()
            store.save()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityHeaders)
    app.state.config, app.state.store, app.state.hub, app.state.gate = config, store, hub, gate
    app.state.supervisor, app.state.auth = supervisor, auth
    app.state.public_status = public_status
    app.state.is_ready = is_profile_ready
    workspaces = Workspaces()
    app.state.workspaces = workspaces
    web_iq = shared_web_iq or WebIQ()
    app.state.web_iq = web_iq
    upload_gate = shared_tool_gate or asyncio.Semaphore(2)
    app.state.tool_gate = upload_gate
    children = {}
    if profiles:
        from dataclasses import replace
        nv_config = replace(config, data=config.data / "models" / "nvfp4", nodes=1,
                            template="demo-nvfp4-nd96.yml",
                            public_url=config.public_url.rstrip("/") + "/models/nvfp4",
                            workspaces={r: w for r, w in config.workspaces.items() if r == "uksouth"})
        children["nvfp4"] = create_app(nv_config, start_supervisor=start_supervisor,
                                      shared_auth=auth, archives=supervisor.archives, profiles=False,
                                      model_id="nvfp4", shared_tool_gate=upload_gate, shared_web_iq=web_iq)
        app.mount("/models/nvfp4", children["nvfp4"])
    app.state.profiles = {model_id:app, **children}

    @app.exception_handler(Problem)
    async def problem(request, exc):
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)

    def is_demo(request):
        return config.open_demo or auth.has(request, "demo") or auth.has(request, "admin")

    def require_demo(request):
        if not is_demo(request):
            raise Problem(401, "login", "로그인이 필요합니다.")

    async def require_admin(request):
        if auth.has(request, "admin"):
            return
        header = request.headers.get("authorization", "")
        if header[:7].lower() == "bearer ":
            ip = client_ip(request)
            if auth.limiter.blocked(ip):
                raise Problem(429, "rate_limited", "too many failed attempts; try again in 10 minutes")
            if await auth.check("admin", header[7:]):
                return
            auth.limiter.failed(ip)
        raise Problem(401, "login", "관리자 로그인이 필요합니다.")

    def require_job(request):
        header = request.headers.get("authorization", "")
        token = header[7:] if header[:7].lower() == "bearer " else ""
        if not token or token_digest(token) not in store.data["jobs"]:
            raise Problem(403, "forbidden", "unknown job token")
        return store.data["jobs"][token_digest(token)]

    def page(name):
        return FileResponse(HERE / "static" / name, headers={"Cache-Control": "no-cache"})

    @app.get("/")
    async def index():
        return page("index.html")

    @app.get("/admin")
    async def admin_page():
        return page("admin.html")

    @app.get("/static/{name}")
    async def static(name: str):
        if name not in STATIC_FILES:
            raise Problem(404, "not_found", "not found")
        return FileResponse(HERE / "static" / name, headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "agent": "maf-1.20.0", "web_iq": web_iq.status()["state"],
                "sandbox": "not_configured"}

    @app.get("/api/capabilities")
    async def capabilities(request: Request):
        require_demo(request)
        await web_iq.prepare()
        return {"web_iq": web_iq.status(), "ocr": "not_configured", "sandbox": "not_configured"}

    async def login(request, roles, cookie):
        body = await read_json(request, 4096)
        ip = client_ip(request)
        if auth.limiter.blocked(ip):
            raise Problem(429, "rate_limited", "로그인 시도가 너무 많습니다. 10분 뒤에 다시 시도해 주세요.")
        for role in roles:
            if await auth.check(role, body.get("password")):
                auth.limiter.succeeded(ip)
                response = JSONResponse({"ok": True})
                auth.set_cookie(response, request, cookie)
                return response
        auth.limiter.failed(ip)
        raise Problem(401, "bad_password", "비밀번호가 맞지 않습니다.")

    @app.post("/api/login")
    async def demo_login(request: Request):
        return await login(request, ("demo", "admin"), "demo")  # the admin password opens the demo too

    @app.post("/api/admin/login")
    async def admin_login(request: Request):
        return await login(request, ("admin",), "admin")

    @app.post("/api/logout")
    async def logout(request: Request):
        response = JSONResponse({"ok": True})
        for name, _ in COOKIES.values():
            response.delete_cookie(name, path="/")
        return response

    @app.get("/api/me")
    async def me(request: Request):
        return {"demo": is_demo(request), "admin": auth.has(request, "admin"), "open": config.open_demo}

    @app.get("/api/status")
    async def status(request: Request):
        require_demo(request)
        return public_status()

    @app.get("/api/models")
    async def models(request: Request):
        require_demo(request)
        runtime = await asyncio.gather(*(profile.state.hub.model_info() for profile in app.state.profiles.values()))
        limits = [info.get("max_context_tokens") for info in runtime]
        common_limit = min(limits) if limits and all(type(value) is int and value > 0 for value in limits) else None
        return {"common_context_tokens":common_limit,
                "models": [{"id":name, "api_prefix":"" if name == "fp8" else "/models/nvfp4",
                            "label":"A.X K2 FP8" if name == "fp8" else "A.X K2 NVFP4",
                            "checkpoint":"skt/A.X-K2" if name == "fp8" else "skt/A.X-K2-NVFP4",
                            "tp":8,"pp":2 if name == "fp8" else 1,
                            "runtime":info,
                            "job_provenance":(profile.state.store.data["jobs"].get(
                                profile.state.store.data["active"]) or {}).get("facts"),
                            "status":profile.state.public_status()}
                           for (name, profile), info in zip(app.state.profiles.items(), runtime)]}

    @app.post("/api/chat")
    async def chat(request: Request):
        require_demo(request)
        body = chat_body(await read_json(request, CHAT_LIMIT))
        if not is_profile_ready():
            raise Problem(503, "not_ready", "모델 서버가 아직 준비되지 않았습니다. 잠시 후 다시 시도해 주세요.")
        try:
            relay = ChatRelay(hub, gate, body)
        except Full:
            raise Problem(503, "busy", "지금 사용자가 많아 대기열이 가득 찼습니다. 잠시 후 다시 시도해 주세요.")
        return relay.response()

    def workspace_for(request, allow_busy=False):
        require_demo(request)
        try:
            space = workspaces.get(request.headers.get("x-ax-workspace", ""))
        except ValueError as exc:
            raise Problem(410, "workspace", str(exc))
        if space.busy and not allow_busy:
            raise Problem(409, "workspace_busy", "This chat is running; wait or cancel before modifying files")
        if getattr(space, "comparison_snapshot", None) and not allow_busy:
            raise Problem(409, "comparison_input_locked", "비교 입력은 고정돼 있습니다. 공유 첨부에서 새 비교를 시작해 주세요.")
        return space

    @app.post("/api/workspace")
    async def new_workspace(request: Request):
        require_demo(request)
        try:
            space = workspaces.create()
        except ValueError as exc:
            raise Problem(503, "workspace_capacity", str(exc))
        return {"token": space.token, "expires_after_seconds": workspaces.ttl}

    @app.post("/api/workspace/cancel")
    async def cancel_turn(request: Request):
        space = workspace_for(request, allow_busy=True)
        await finish_workspace(space, close=False)
        return {"ok": True, "state": "cancelled"}

    async def finish_workspace(space, close):
        tasks = []
        owned = [(app, space)]
        for record in getattr(space, "comparisons", {}).values():
            record["cancelled"] = True
            owned.extend((a["profile"], a["space"]) for a in record["actors"].values())
        for profile, item in owned:
            item.cancel_requested = True
            if close:
                item.closed = True
            if item.active_task:
                item.active_task.cancel()
                tasks.append(item.active_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if close:
            for profile, item in owned:
                item.expire()
                profile.state.workspaces.items.pop(item.token, None)

    @app.post("/api/workspace/close")
    async def close_workspace(request: Request):
        space = workspace_for(request, allow_busy=True)
        await finish_workspace(space, close=True)
        return {"ok": True}

    @app.post("/api/workspace/upload")
    async def upload(request: Request):
        space = workspace_for(request)
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > FILE_LIMIT:
                raise Problem(413, "too_large", "Upload limit is 8 MiB")
        # Reserve the chat across parsing so concurrent uploads cannot bypass combined limits.
        if space.busy:
            raise Problem(409, "workspace_busy", "Chat is busy")
        space.busy = True
        try:
            async with upload_gate:
                names = await asyncio.to_thread(space.upload, request.query_params.get("name", ""), bytes(data))
            return {"files": names, "bytes": len(data)}
        except (ValueError, UnicodeError, OSError) as exc:
            raise Problem(400, "upload", str(exc)[:500])
        finally:
            space.busy = False

    @app.get("/api/workspace/download")
    async def download(request: Request):
        space = workspace_for(request, allow_busy=True)
        path = request.query_params.get("path", "")
        try:
            data = space.get(path)
        except ValueError as exc:
            raise Problem(404, "file", str(exc))
        name = path.rsplit("/", 1)[-1]
        fallback = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"
        return Response(data, media_type="application/octet-stream", headers={"Content-Disposition": disposition})

    @app.post("/api/workspace/remove")
    async def remove_attachment(request: Request):
        space = workspace_for(request)
        names = (await read_json(request, 30_000)).get("files")
        if space.busy:
            raise Problem(409, "workspace_busy", "Chat is busy")
        if not isinstance(names, list) or len(names) > 100 or any(not isinstance(n, str) for n in names):
            raise Problem(400, "files", "files must be a bounded list of names")
        for name in names:
            space.files.pop(name, None)
            space.original.pop(name, None)
            space.upload_versions.pop(name, None)
            space.pdf_checked.discard(name)
            space.pdf_outcomes.pop(name, None)
        return {"ok": True}

    @app.post("/api/agent")
    async def run_agent(request: Request):
        from agent import agent_response
        space = workspace_for(request)
        raw = await read_json(request, CHAT_LIMIT)
        space = workspace_for(request)
        body, attachments = agent_body(raw, space)
        if getattr(space, "comparison_snapshot", None):
            raise Problem(409, "comparison", "비교 응답은 서버가 고정한 입력으로만 실행할 수 있습니다.")
        if not is_profile_ready():
            raise Problem(503, "not_ready", "모델 서버가 아직 준비되지 않았습니다.")
        if space.busy:
            raise Problem(409, "workspace_busy", "This chat is already running")
        space.busy = True
        return agent_response(hub, gate, body, space, raw.get("tools") is True, tool_gate=upload_gate,
                              attachment_paths=attachments, web_iq=web_iq)

    if profiles:
        install_comparison(app, workspace_for, read_json, agent_body, Problem, CHAT_LIMIT)

    @app.get("/api/workspace/preview")
    async def preview(request: Request):
        space = workspace_for(request, allow_busy=True)
        path = request.query_params.get("path", "")
        if not path.lower().endswith(".html"):
            raise Problem(400, "preview", "HTML files only")
        try:
            text = safe_preview(space.get(path))
        except (ValueError, UnicodeError) as exc:
            raise Problem(400, "preview", str(exc))
        return Response(text, media_type="text/plain")

    @app.get("/api/results")
    async def results_view(request: Request):
        require_demo(request)
        meta = {run: dict(entry) for run, entry in store.data["eval_runs"].items()}
        return await asyncio.to_thread(results, meta, store.data["eval_current"])

    @app.get("/api/results/figure/{name}")
    async def figure(name: str, request: Request):
        require_demo(request)
        path = config.results_dir / name
        if not FIGURE.fullmatch(name) or not path.is_file():
            raise Problem(404, "not_found", "not found")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=600"})

    # ---------------------------------------------------------------------------------------- admin
    @app.get("/api/admin/state")
    async def admin_view(request: Request):
        await require_admin(request)
        return admin_state()

    @app.post("/api/admin/power")
    async def admin_power(request: Request):
        await require_admin(request)
        wanted = (await read_json(request, 4096)).get("state")
        if wanted not in ("on", "off"):
            raise Problem(400, "bad_request", "state must be on or off")
        data = store.data
        if data["desired"] != wanted:
            data["desired"] = wanted
            if wanted == "on":
                data["cooldown"] = {}
            store.event("power", f"switched {wanted}")
        supervisor.poke()
        return admin_state()

    @app.post("/api/admin/restart")
    async def admin_restart(request: Request):
        await require_admin(request)
        await read_json(request, 4096)
        if store.data["desired"] != "on" or not store.data["active"]:
            raise Problem(409, "no_job", "there is no active job to restart")
        store.data["restart"] = True
        store.event("power", "restart requested")
        supervisor.poke()
        return admin_state()

    @app.post("/api/admin/eval")
    async def admin_eval(request: Request):
        await require_admin(request)
        body = await read_json(request, 4096)
        data, conn = store.data, hub.active_link()
        if body.get("action") == "stop":
            data["eval_wish"] = None
            if conn:
                conn.send({"type": "eval", "action": "stop"})
            store.event("eval", "eval stop requested")
        elif body.get("action") == "start":
            spec = eval_spec(body)
            data["eval_wish"], data["eval_current"] = spec, spec["run"]
            entry = data["eval_runs"].setdefault(spec["run"], {"run": spec["run"], "created": round(time.time())})
            entry.update(spec, state="requested")
            store.event("eval", f"eval {spec['run']} requested: suites {spec['suites']}, "
                                f"repeats {spec['repeats'] or 'default'}, limit {spec['limit'] or 'none'}")
            if conn:
                reconcile_eval(conn, force=True)
        else:
            raise Problem(400, "bad_request", "action must be start or stop")
        return admin_state()

    @app.post("/api/admin/password")
    async def admin_password(request: Request):
        await require_admin(request)
        role = (await read_json(request, 4096)).get("role")
        if role not in COOKIES:
            raise Problem(400, "bad_request", "role must be demo or admin")
        password = await auth.rotate(role)
        response = JSONResponse({"role": role, "password": password})
        if role == "admin":
            auth.set_cookie(response, request, "admin")  # keep the rotating admin signed in
        return response

    # ----------------------------------------------------------------------------------------- link
    @app.websocket("/ws/link")
    async def link(ws: WebSocket):
        header = ws.headers.get("authorization", "")
        token = header[7:] if header[:7].lower() == "bearer " else ""
        digest = token_digest(token) if token else ""
        if not token or digest not in store.data["jobs"]:
            await ws.close(code=4403)  # before accept: the handshake is refused with HTTP 403
            return
        await ws.accept()
        await hub.serve(ws, digest)

    @app.get("/api/link/src")
    async def link_source(request: Request):
        job = require_job(request)
        try:
            source = await asyncio.to_thread(supervisor.archives.for_job, job)
        except (OSError, ValueError) as exc:
            store.event("error", f"immutable source unavailable: {exc}", save=False)
            raise Problem(503, "source_unavailable", "The job's verified source archive is unavailable") from exc
        return Response(source, media_type="application/gzip", headers={"Cache-Control": "no-store"})

    @app.get("/api/link/evals")
    async def link_records(request: Request, run: str = ""):
        require_job(request)
        try:
            records = await asyncio.to_thread(store.records, run)
        except ValueError:
            raise Problem(400, "bad_run", "invalid run name")
        return {"records": records}

    @app.post("/api/link/evals")
    async def link_add(request: Request):
        require_job(request)
        body = await read_json(request, EVAL_LIMIT)
        run, records = body.get("run"), body.get("records")
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise Problem(400, "bad_records", "records must be a list of objects")
        try:
            await asyncio.to_thread(store.add_records, run, records)
        except ValueError:
            raise Problem(400, "bad_run", "invalid run name")
        store.data["eval_runs"].setdefault(run, {"run": run, "created": round(time.time()), "state": "running"})
        return {"ok": True, "records": len(records)}

    @app.post("/api/link/evals/summary")
    async def link_summary(request: Request):
        require_job(request)
        body = await read_json(request, EVAL_LIMIT)
        run = body.get("run")
        if not isinstance(body.get("plan"), dict) or not isinstance(body.get("summary"), dict):
            raise Problem(400, "bad_summary", "plan and summary must be objects")
        try:
            await asyncio.to_thread(store.put_summary, run, body)
        except ValueError:
            raise Problem(400, "bad_run", "invalid run name")
        entry = store.data["eval_runs"].setdefault(run, {"run": run, "created": round(time.time())})
        entry.update(summary_state=str(body.get("state", ""))[:20], summary_t=round(time.time()))
        return {"ok": True}

    return app
