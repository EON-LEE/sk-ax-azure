"""Shared helpers for the Foundry BYOM agent scripts (agent creation, prompt runs, evidence).

Importing this module never touches the network. Everything printed or written goes through
redact(): subscription/tenant/object IDs, full resource IDs, keys, tokens, e-mail addresses and
resource-specific Azure hostnames are replaced before they leave the process, and service IDs
(resp_..., conv_...) are shortened to their first characters. With BYOM_PRIVATE_DIR set, evidence files are
mirrored there with whole IDs (same masking otherwise), outside the repository.
"""
from __future__ import annotations

import dataclasses
import datetime
import enum
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping
import urllib.parse

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_DIR = ROOT / "evidence" / "foundry-byom"
SCOPE_NOTE = "BYOM external-model connection verification; not a Model Catalog registration"
ENDPOINT_HOST_SUFFIX = ".ai.azure.com"

DEFAULT_AGENT = "byom-demo-agent"
DEFAULT_MODEL = "ax-k2-nvfp4"
DEFAULT_INSTRUCTIONS = (
    "간결한 한국어로 답한다. 최신 정보나 확인이 필요한 사실은 제공된 도구를 먼저 호출하고, "
    "도구 결과에 있는 내용만 근거로 답한다. 도구 결과에 근거가 없으면 모른다고 답한다."
)

AGENT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
CONNECTION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
LABEL_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
EVIDENCE_FILES = {"evidence": "{label}.json", "run": "{label}.run.json", "create": "{label}.create.json",
                  "connections": "{label}.connections.json", "series": "{label}.series.json"}
RESERVED_LABEL_SUFFIXES = (".run", ".create", ".connections", ".series")
MCP_LABEL_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
VISIBLE_ASCII_RE = re.compile(r"[\x21-\x7e]+")
TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
SCAN_MIN_LENGTH = 8
AUTH_CHOICES = ("cli", "default", "browser")

MCP_PRESETS = {
    "learn": {"url": "https://learn.microsoft.com/api/mcp", "label": "microsoft_learn",
              "allowed_tools": ["microsoft_docs_search"]},
    "webiq": {"url": None, "label": "web_iq", "allowed_tools": ["web"]},
}
DEMO_FUNCTION = {
    "type": "function",
    "name": "lookup_demo_stock",
    "description": "Return the demo stock quantity for a SKU code.",
    "parameters": {
        "type": "object",
        "properties": {"sku": {"type": "string", "description": "SKU code such as AX-100"}},
        "required": ["sku"],
        "additionalProperties": False,
    },
    "strict": False,
}

PATTERNS = {
    "arm_id": re.compile(r"/subscriptions/[^\s\"'<>,;)]*", re.I),
    "guid": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),
    "jwt": re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}\b"),
    "auth_header": re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    "azure_host": re.compile(
        r"\b(?:[a-z0-9][a-z0-9-]*\.)+(?:services\.ai\.azure\.com|ai\.azure\.com|openai\.azure\.com"
        r"|cognitiveservices\.azure\.com|azure-api\.net|azurewebsites\.net|azurecontainerapps\.io"
        r"|azurefd\.net|azureedge\.net|applicationinsights\.azure\.com|windows\.net"
        r"|vault\.azure\.net|onmicrosoft\.com)\b", re.I),
    "ipv4": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
}
# The project name is deployment-specific, so the /api/projects/<name> path segment is generalized too.
PROJECT_PATH = re.compile(r"(/api/projects/)(?!<)[^/\s\"'<>?#,;)]+", re.I)
KV_SECRET = re.compile(
    r"(?i)\b(x[-_]?api[-_]?key|api[-_]?key|authorization|client[-_]?secret|password|secret"
    r"|access[-_]?token)\b([\"']?\s*[:=]\s*[\"']?)(?!<redacted)[^\s\"',;}]+")
SENSITIVE_KEYS = {
    "authorization", "proxy_authorization", "api_key", "apikey", "x_api_key", "x_apikey",
    "access_token", "refresh_token", "id_token", "token", "secret", "client_secret", "password",
    "credential", "credentials", "cookie", "set_cookie", "connection_string", "sas_token",
    "signature", "headers", "consent_link",
}
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)
URL_RE = re.compile(r"https?://[^\s)\]>\"'<\\]+")
ERROR_MESSAGE_LIMIT = 1500
ERROR_HINTS = {
    401: "Token rejected: check the signed-in account (az account show) and the https://ai.azure.com/.default audience.",
    403: "Forbidden: the identity needs a data-plane role on the project (Foundry User, formerly Azure AI User, or higher).",
    404: "Not found: check the project endpoint, agent name/version, connection and model name.",
    429: "Rate limited: retry later and keep concurrency at 1.",
}

REDACT_ENTRY_RE = re.compile(r"(?P<literal>.+?)=(?P<alias><[a-z0-9][a-z0-9:_-]{0,30}>)")

# Service IDs such as resp_<hex> or conv_<hex>. Public output keeps their first ID_KEEP characters only;
# the full values stay in the private copy kept under BYOM_PRIVATE_DIR (outside the repository).
SERVICE_ID_RE = re.compile(r"(?<![A-Za-z0-9])([a-z]{2,8})_(?=[A-Za-z0-9]*\d)([A-Za-z0-9]{12,})(?![A-Za-z0-9])")
# Correlation IDs in error bodies and headers (request_id, x-request-id, trace_id, correlation id: <hex>) and the
# id of a JSON-RPC error body ("id": "<hex>") get the same treatment. Quotes may be backslash-escaped (JSON text).
REQUEST_ID_RE = re.compile(
    r"""(
        (?:request|trace|correlation)[ _-]?id(?:\\?['"])?\s*[:=]\s*(?:\\?['"])?
        |
        \\?['"]id\\?['"]\s*:\s*\\?['"]
    )([0-9a-f]{16,})(?![0-9a-f])""", re.I | re.X)
ID_KEEP = 8
PRIVATE_DIR_ENV = "BYOM_PRIVATE_DIR"

_runtime_secrets: set[str] = set()


class ConfigError(ValueError):
    """Invalid or missing configuration; reported without touching the cloud."""


class StageFailure(Exception):
    """An SDK call failed; the stage names the call so that an error report is unambiguous."""

    def __init__(self, stage, original):
        super().__init__(stage)
        self.stage = stage
        self.original = original


def run_stage(stage, function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported with the stage name and a redacted message
        raise StageFailure(stage, exc) from exc


class HttpRequestCounter:
    """httpx request hook that counts what the OpenAI client really sent, so any retry shows up in the evidence."""

    def __init__(self):
        self.requests = 0
        self.responses_posts = 0

    def __call__(self, request):
        self.requests += 1
        if request.method == "POST" and request.url.path.rstrip("/").endswith("/responses"):
            self.responses_posts += 1

    def summary(self):
        return {"requests": self.requests, "responses_posts": self.responses_posts}

    def reset(self):
        """Start a new count, so that a series of calls over one client reports its HTTP requests call by call."""
        self.requests = 0
        self.responses_posts = 0


@dataclasses.dataclass
class Clients:
    project: Any
    openai: Any
    models: Any
    http: Any = None


def register_secret(value):
    if value and len(value) >= 4:
        _runtime_secrets.add(value)


@functools.lru_cache(maxsize=8)
def _parse_redact_entries(raw):
    entries = []
    for part in raw.split(","):
        part = part.strip()
        match = REDACT_ENTRY_RE.fullmatch(part)
        literal, alias = (match["literal"].strip(), match["alias"]) if match else (part, None)
        if len(literal) >= 4:
            entries.append((literal, alias))
    return tuple(entries)


def redact_entries():
    """Deployment-specific literals from BYOM_REDACT_LITERALS as (literal, alias) pairs.

    An entry is either a literal or literal=<alias>; the alias replaces the literal so that evidence
    stays readable without naming the deployment. Entries come from the environment only and are never committed.
    """
    return _parse_redact_entries(os.environ.get("BYOM_REDACT_LITERALS", ""))


def secret_literals():
    found = set(_runtime_secrets)
    found.update(value for name, value in os.environ.items() if SECRET_ENV.search(name) and len(value) >= 8)
    found.update(literal for literal, _ in redact_entries())
    return sorted(found, key=len, reverse=True)


def secret_from_env(variable):
    """Read a secret from the environment and register it for redaction; the value is never printed."""
    if not ENV_NAME_RE.fullmatch(variable or ""):
        raise ConfigError("invalid environment variable name")
    value = os.environ.get(variable)
    if not value:
        raise ConfigError(f"environment variable {variable} is empty or unset")
    if len(value) < SCAN_MIN_LENGTH:
        raise ConfigError(f"environment variable {variable} holds fewer than {SCAN_MIN_LENGTH} characters; "
                          "too short to track reliably")
    register_secret(value)
    return value


def scan_for_secrets(value, literals):
    """Whether any literal of at least SCAN_MIN_LENGTH characters appears in value (None: nothing to scan for).

    Redaction hides an echoed secret, so this check runs on the raw record before anything is summarized.
    Only the boolean leaves this function.
    """
    eligible = [literal for literal in literals if literal and len(literal) >= SCAN_MIN_LENGTH]
    if not eligible:
        return None
    text = json.dumps(to_plain(value), ensure_ascii=False, default=str)
    return any(literal in text or json.dumps(literal, ensure_ascii=False)[1:-1] in text for literal in eligible)


def shorten_ids(text):
    """resp_0a1b2c3d4e5f... -> resp_0a1b2c3d…, request_id/trace_id: <32 hex> -> <8 hex>…; the rest stays (idempotent)."""
    text = SERVICE_ID_RE.sub(lambda match: f"{match[1]}_{match[2][:ID_KEEP]}…", text)
    return REQUEST_ID_RE.sub(lambda match: f"{match[1]}{match[2][:ID_KEEP]}…", text)


def redact_text(text, literals=None, *, keep_ids=False):
    aliases = {literal.lower(): alias for literal, alias in redact_entries() if alias}
    for literal in secret_literals() if literals is None else literals:
        text = re.sub(re.escape(literal), aliases.get(literal.lower(), "<redacted>"), text, flags=re.I)
    for name, pattern in PATTERNS.items():
        text = pattern.sub(f"<redacted:{name}>", text)
    text = PROJECT_PATH.sub(r"\1<project>", text)
    text = KV_SECRET.sub(r"\1\2<redacted>", text)
    return text if keep_ids else shorten_ids(text)


def redact(value, literals=None, *, keep_ids=False):
    """Return a copy with sensitive keys, identifiers and secret literals replaced.

    Service IDs (resp_..., conv_...) are shortened unless keep_ids is set; only the private copy sets it.
    """
    literals = secret_literals() if literals is None else literals
    if isinstance(value, str):
        return redact_text(value, literals, keep_ids=keep_ids)
    if isinstance(value, Mapping):
        return {redact_text(str(key), literals, keep_ids=keep_ids):
                "<redacted>" if value[key] is not None and str(key).lower().replace("-", "_") in SENSITIVE_KEYS
                else redact(value[key], literals, keep_ids=keep_ids) for key in value}
    if isinstance(value, (list, tuple)):
        return [redact(item, literals, keep_ids=keep_ids) for item in value]
    return value


def to_plain(value, _depth=0):
    """Convert SDK models, enums and mappings to plain JSON-compatible data."""
    if _depth > 20:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, enum.Enum):
        return to_plain(value.value, _depth + 1)
    if isinstance(value, Mapping):
        return {str(key): to_plain(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_plain(item, _depth + 1) for item in value]
    for method, kwargs in (("model_dump", {"mode": "json"}), ("model_dump", {}), ("as_dict", {}), ("to_dict", {})):
        function = getattr(value, method, None)
        if callable(function):
            try:
                return to_plain(function(**kwargs), _depth + 1)
            except Exception:  # noqa: BLE001 - fall through to the next serializer
                continue
    return str(value)


def clip(text, limit=400):
    if text is None:
        return None
    if not isinstance(text, str):
        text = json.dumps(to_plain(text), ensure_ascii=False)
    return text if len(text) <= limit else f"{text[:limit]}...(+{len(text) - limit} chars)"


def shrink(value, limit=200):
    if isinstance(value, str):
        return clip(value, limit)
    if isinstance(value, Mapping):
        return {key: shrink(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [shrink(item, limit) for item in value[:20]]
    return value


def urls_in(text, limit=10):
    found = []
    for match in URL_RE.findall(text or ""):
        url = match.rstrip(".,;:!?")
        if url not in found:
            found.append(url)
    return found[:limit]


def now_iso(timespec="seconds"):
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec=timespec)


def dumps(payload, *, keep_ids=False):
    return json.dumps(redact(payload, keep_ids=keep_ids), indent=2, ensure_ascii=False, default=str)


def emit(payload):
    sys.stdout.write(dumps(payload) + "\n")


def configure_output():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def evidence_path(label, kind="evidence"):
    if not LABEL_RE.fullmatch(label or "") or label.endswith(RESERVED_LABEL_SUFFIXES):
        raise ConfigError("label must match [a-z0-9][a-z0-9._-]{0,63} and must not end with "
                          ".run, .create, .connections or .series")
    return EVIDENCE_DIR / EVIDENCE_FILES[kind].format(label=label)


def display_path(path):
    """Repo-relative POSIX path for messages, so local user names never leak into output."""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return Path(path).name


def private_directory():
    """The directory for full-ID copies named by BYOM_PRIVATE_DIR, or None when unset.

    It must be absolute and outside the repository; on POSIX an existing directory must belong to the current
    user and carry mode 0700 (a missing one is created that way).
    """
    raw = os.environ.get(PRIVATE_DIR_ENV)
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{PRIVATE_DIR_ENV} must be an absolute path")
    path = path.resolve()
    if path == ROOT or ROOT in path.parents:
        raise ConfigError(f"{PRIVATE_DIR_ENV} must be outside the repository")
    if path.exists():
        if not path.is_dir():
            raise ConfigError(f"{PRIVATE_DIR_ENV} is not a directory")
        if os.name == "posix" and (path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
            raise ConfigError(f"{PRIVATE_DIR_ENV} must belong to the current user and have mode 0700")
    return path


def private_copy(path):
    """The full-ID copy of an evidence file, when BYOM_PRIVATE_DIR holds one."""
    directory = private_directory()
    candidate = directory / Path(path).name if directory else None
    return candidate if candidate is not None and candidate.is_file() else None


def write_private(directory, name, payload):
    if not directory.exists():
        directory.mkdir(parents=True, mode=0o700)
        if os.name == "posix":
            os.chmod(directory, 0o700)
    target = directory / name
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(dumps(payload, keep_ids=True) + "\n")
    if os.name == "posix":
        os.chmod(target, 0o600)


def write_json(path, payload):
    """Write the public evidence file (secrets masked, service IDs shortened).

    With BYOM_PRIVATE_DIR set, a second copy with the same masking but whole IDs goes there (mode 0600).
    """
    directory = private_directory()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(payload) + "\n", encoding="utf-8")
    if directory is not None:
        write_private(directory, path.name, payload)
        print(f"saved private copy {path.name}", file=sys.stderr)


def emit_result(result, label, kind, schema):
    """Print a result record; with an evidence label also save it as evidence/foundry-byom/<label>.<kind>.json."""
    emit(result)
    if label:
        path = evidence_path(label, kind)
        write_json(path, {"schema": schema, "scope": SCOPE_NOTE, "generated_at": now_iso(), **result})
        print(f"saved {display_path(path)}", file=sys.stderr)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def status_code(exc):
    value = getattr(exc, "status_code", None)
    return value if isinstance(value, int) else None


def short_request_id(value):
    """The first ID_KEEP characters of a request correlation ID (a GUID keeps its first group); None without one."""
    return value[:ID_KEEP] if isinstance(value, str) and value else None


def describe_error(exc):
    """Summarize an SDK exception: the service message stays verbatim (redacted on output); headers and request bodies are never echoed."""
    inner = getattr(exc, "error", None)
    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        code = getattr(inner, "code", None)
    message = getattr(exc, "message", None)
    summary = {
        "type": type(exc).__name__,
        "status_code": status_code(exc),
        "code": code if isinstance(code, str) else None,
        "message": clip(message if isinstance(message, str) else str(exc), ERROR_MESSAGE_LIMIT),
    }
    param = getattr(exc, "param", None)
    if isinstance(param, str):
        summary["param"] = clip(param, 100)
    api_type = getattr(exc, "type", None)
    if isinstance(api_type, str):
        summary["api_type"] = clip(api_type, 100)
    request_id = short_request_id(getattr(exc, "request_id", None))
    if request_id:
        summary["request_id"] = request_id
    target = getattr(inner, "target", None)
    if isinstance(target, str):
        summary["target"] = clip(target, 200)
    details = getattr(inner, "details", None)
    if isinstance(details, (list, tuple)) and details:
        summary["details"] = [{"code": getattr(item, "code", None), "message": clip(getattr(item, "message", None), 400)}
                              for item in details[:5]]
    if summary["status_code"] in ERROR_HINTS:
        summary["hint"] = ERROR_HINTS[summary["status_code"]]
    return summary


def comma_list(value):
    return [part.strip() for part in value.split(",") if part.strip()]


def env(name, default=None):
    return os.environ.get(name) or default


def add_connection_args(parser):
    parser.add_argument("--project-endpoint", default=env("FOUNDRY_PROJECT_ENDPOINT"),
                        help="Foundry project endpoint (env FOUNDRY_PROJECT_ENDPOINT)")
    parser.add_argument("--auth", choices=AUTH_CHOICES, default=env("FOUNDRY_BYOM_AUTH", "cli"),
                        help="credential: cli = az login session (default), default = DefaultAzureCredential, "
                             "browser = interactive sign-in")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan only; no SDK import, credential or network use")


def add_mcp_args(parser):
    parser.add_argument("--mcp-preset", choices=sorted(MCP_PRESETS), default="learn",
                        help="learn = public Microsoft Learn MCP (no secret); webiq needs --mcp-url")
    parser.add_argument("--mcp-url", default=env("FOUNDRY_BYOM_MCP_URL"), help="MCP server URL (https, no query)")
    parser.add_argument("--mcp-label", help="MCP server label (default: from preset)")
    parser.add_argument("--mcp-allowed-tools", type=comma_list, help="comma-separated tool names (default: from preset)")
    parser.add_argument("--mcp-connection", default=env("FOUNDRY_BYOM_MCP_CONNECTION"),
                        help="project connection NAME that stores the MCP credential (never a secret value)")
    parser.add_argument("--require-approval", choices=("never", "always"), default="never",
                        help="MCP approval policy stored in the agent definition")


def add_agent_args(parser):
    parser.add_argument("--agent-name", default=env("FOUNDRY_BYOM_AGENT", DEFAULT_AGENT))
    parser.add_argument("--connection", default=env("FOUNDRY_BYOM_CONNECTION"),
                        help="Admin-connected models connection name (env FOUNDRY_BYOM_CONNECTION)")
    parser.add_argument("--model", default=env("FOUNDRY_BYOM_MODEL", DEFAULT_MODEL),
                        help="model name behind the connection (default ax-k2-nvfp4)")
    parser.add_argument("--instructions", help="agent instructions (default: concise Korean, tool-grounded)")
    parser.add_argument("--instructions-file", help="UTF-8 file with the agent instructions")
    parser.add_argument("--tool", choices=("none", "function", "mcp"), default="none")
    parser.add_argument("--temperature", type=float)
    add_mcp_args(parser)


def check_agent_name(name):
    if not AGENT_RE.fullmatch(name or ""):
        raise ConfigError("agent name: alphanumerics and inner hyphens only, at most 63 characters")
    return name


def model_reference(connection, model, allow_placeholder=False):
    if not MODEL_RE.fullmatch(model or ""):
        raise ConfigError("invalid model name")
    if not connection and allow_placeholder:
        return f"<connection>/{model}"
    if not connection:
        raise ConfigError("--connection (or FOUNDRY_BYOM_CONNECTION) is required")
    if not CONNECTION_RE.fullmatch(connection):
        raise ConfigError("invalid connection name")
    return f"{connection}/{model}"


def require_visible_ascii(value, what):
    # urlsplit silently drops CR, LF and TAB, so a value read from a CRLF file would pass the URL checks
    # and then fail at the first request (or be stored in the agent definition) with the character intact.
    if value and not VISIBLE_ASCII_RE.fullmatch(value):
        raise ConfigError(f"{what} must be visible ASCII without spaces or control characters "
                          "(a trailing carriage return from a CRLF file is a common cause)")


def check_endpoint(value):
    require_visible_ascii(value, "project endpoint")
    parts = urllib.parse.urlsplit(value or "")
    host = parts.hostname or ""
    if (parts.scheme != "https" or not host.endswith(ENDPOINT_HOST_SUFFIX) or parts.username
            or parts.password or parts.query or parts.fragment or "/api/projects/" not in parts.path):
        raise ConfigError("project endpoint must look like https://<resource>.services.ai.azure.com/api/projects/<name> "
                          "(no credentials, query or fragment)")
    return value.rstrip("/")


def require_live(args):
    if not args.project_endpoint:
        raise ConfigError("--project-endpoint (or FOUNDRY_PROJECT_ENDPOINT) is required unless --dry-run")
    args.project_endpoint = check_endpoint(args.project_endpoint)
    private_directory()


def resolve_instructions(args):
    if args.instructions_file:
        try:
            return Path(args.instructions_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read instructions file: {type(exc).__name__}") from exc
    return args.instructions or DEFAULT_INSTRUCTIONS


def mcp_tool_spec(args):
    preset = MCP_PRESETS[args.mcp_preset]
    url = args.mcp_url or preset["url"]
    if not url:
        raise ConfigError(f"--mcp-url is required for preset {args.mcp_preset!r}")
    require_visible_ascii(url, "MCP server URL")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("MCP server URL must be https without credentials, query or fragment")
    label = args.mcp_label or preset["label"]
    allowed = args.mcp_allowed_tools or list(preset["allowed_tools"])
    if not MCP_LABEL_RE.fullmatch(label):
        raise ConfigError("invalid MCP server label")
    if not all(TOOL_NAME_RE.fullmatch(name) for name in allowed):
        raise ConfigError("invalid MCP tool name in --mcp-allowed-tools")
    spec = {"type": "mcp", "server_label": label, "server_url": url, "allowed_tools": allowed,
            "require_approval": args.require_approval}
    if args.mcp_connection:
        if not CONNECTION_RE.fullmatch(args.mcp_connection):
            raise ConfigError("invalid MCP connection name")
        spec["project_connection_id"] = args.mcp_connection
    return spec


def definition_spec(args):
    """Plain-dict agent definition; never contains headers or other secrets."""
    check_agent_name(args.agent_name)
    spec = {"kind": "prompt",
            "model": model_reference(args.connection, args.model, allow_placeholder=args.dry_run),
            "instructions": resolve_instructions(args)}
    if args.temperature is not None:
        spec["temperature"] = args.temperature
    tools = []
    if args.tool == "function":
        tools.append(json.loads(json.dumps(DEMO_FUNCTION)))
    elif args.tool == "mcp":
        tools.append(mcp_tool_spec(args))
    spec["tools"] = tools
    return spec


def to_sdk_definition(spec, models):
    tools = []
    for tool in spec.get("tools", []):
        body = {key: value for key, value in tool.items() if key != "type"}
        if tool["type"] == "mcp":
            tools.append(models.MCPTool(**body))
        elif tool["type"] == "function":
            tools.append(models.FunctionTool(**body))
        else:
            raise ConfigError(f"unsupported tool type {tool['type']!r}")
    fields = {key: value for key, value in spec.items() if key not in ("kind", "tools")}
    return models.PromptAgentDefinition(**fields, tools=tools)


def canonical_tool(tool):
    keys = {"mcp": ("server_label", "server_url", "allowed_tools", "require_approval", "project_connection_id"),
            "function": ("name", "description", "parameters", "strict")}.get(tool.get("type"), ())
    result = {"type": tool.get("type")}
    for key in keys:
        value = tool.get(key)
        if key == "allowed_tools" and isinstance(value, Mapping):
            value = value.get("tool_names")
        if value is not None:
            result[key] = value
    return result


def canonical_definition(definition):
    result = {key: definition[key] for key in ("model", "instructions", "temperature") if definition.get(key) is not None}
    result["tools"] = [canonical_tool(tool) for tool in definition.get("tools") or [] if isinstance(tool, Mapping)]
    return result


def covers(actual, desired):
    """True when every value in desired is present and equal in actual (service defaults are ignored)."""
    if isinstance(desired, Mapping):
        return isinstance(actual, Mapping) and all(key in actual and covers(actual[key], value)
                                                   for key, value in desired.items())
    if isinstance(desired, list):
        return (isinstance(actual, list) and len(actual) == len(desired)
                and all(covers(a, d) for a, d in zip(actual, desired)))
    return actual == desired


AZ_SUBSCRIPTION_ENV = "FOUNDRY_BYOM_AZ_SUBSCRIPTION"


def pinned_subscription():
    """Subscription name or ID that az must use for tokens; supplied only at runtime and never printed."""
    value = os.environ.get(AZ_SUBSCRIPTION_ENV, "").strip()
    register_secret(value)
    return value or None


def credential_info(args):
    """Which identity mode ran, without the identity itself."""
    return {"mode": args.auth, "az_subscription_pinned": bool(pinned_subscription()) if args.auth == "cli" else False}


def make_credential(kind):
    """Credential factory; with --auth cli the az token request is pinned to FOUNDRY_BYOM_AZ_SUBSCRIPTION when set.

    The pin makes az fail instead of silently using another account when a shared az login changed.
    """
    from azure import identity
    if kind == "cli":
        subscription = pinned_subscription()
        return identity.AzureCliCredential(subscription=subscription) if subscription else identity.AzureCliCredential()
    factory = {"default": identity.DefaultAzureCredential, "browser": identity.InteractiveBrowserCredential}[kind]
    return factory()


def open_clients(args, with_openai=True):
    """Create the project client and, unless with_openai is False, the OpenAI-compatible client (imports the SDK lazily)."""
    from azure.ai.projects import AIProjectClient, models
    project = AIProjectClient(endpoint=args.project_endpoint, credential=make_credential(args.auth))
    counter = HttpRequestCounter()
    openai = None
    if with_openai:
        from openai import DefaultHttpxClient
        # max_retries=0: an SDK retry would be a hidden extra model call; the counter proves none happened.
        openai = project.get_openai_client(
            max_retries=0, http_client=DefaultHttpxClient(event_hooks={"request": [counter]}))
    return Clients(project=project, openai=openai, models=models, http=counter)


def connect(args, clients=None, with_openai=True):
    if clients is not None:
        return clients
    try:
        return open_clients(args, with_openai=with_openai)
    except ImportError as exc:
        raise ConfigError("Azure SDK packages are missing: pip install -r demo/foundry-byom/agent/requirements.txt") from exc


def demo_stock(nonce, sku):
    digest = hashlib.sha256(f"{nonce}:{sku}".encode()).hexdigest()
    return 1000 + int(digest[:8], 16) % 9000


def run_demo_function(name, arguments, nonce):
    """Execute the only whitelisted local function and return its JSON output."""
    if name != DEMO_FUNCTION["name"]:
        return json.dumps({"error": "unknown function"})
    try:
        sku = str(json.loads(arguments or "{}")["sku"])[:32]
    except (ValueError, KeyError, TypeError):
        return json.dumps({"error": "invalid arguments"})
    return json.dumps({"sku": sku, "stock": demo_stock(nonce, sku)})


def text_of(item):
    content = item.get("content") or []
    if isinstance(content, str):
        return content
    return "".join(part.get("text") or "" for part in content
                   if isinstance(part, Mapping) and part.get("type") in ("output_text", "input_text", "text"))


def parse_arguments(value):
    if isinstance(value, str):
        try:
            return shrink(json.loads(value))
        except ValueError:
            return clip(value, 200)
    return shrink(to_plain(value))


def summarize_item(item):
    """Structured, size-bounded view of one response/conversation item."""
    kind = item.get("type")
    summary = {"type": kind}
    if kind == "message":
        summary.update(role=item.get("role"), text=clip(text_of(item)))
    elif kind == "mcp_list_tools":
        summary.update(server_label=item.get("server_label"), error=shrink(item.get("error")),
                       tools=[tool.get("name") for tool in item.get("tools") or [] if isinstance(tool, Mapping)])
    elif kind == "mcp_call":
        output = item.get("output")
        summary.update(server_label=item.get("server_label"), name=item.get("name"),
                       arguments=parse_arguments(item.get("arguments")), status=item.get("status"),
                       error=shrink(item.get("error")),
                       output_chars=len(output) if isinstance(output, str) else None,
                       output_preview=clip(output, 200) if output is not None else None,
                       output_urls=urls_in(output) if isinstance(output, str) else None)
    elif kind == "mcp_approval_request":
        summary.update(id=item.get("id"), server_label=item.get("server_label"), name=item.get("name"),
                       arguments=parse_arguments(item.get("arguments")))
    elif kind == "function_call":
        summary.update(name=item.get("name"), arguments=parse_arguments(item.get("arguments")),
                       call_id=item.get("call_id"), status=item.get("status"))
    elif kind == "function_call_output":
        summary.update(call_id=item.get("call_id"), output=clip(item.get("output"), 200))
    elif kind == "oauth_consent_request":
        summary.update(needs_user_action=True)
    else:
        summary.update(status=item.get("status"))
    return {key: value for key, value in summary.items() if value is not None}


def summarize_usage(usage):
    usage = usage or {}
    fields = {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
              "total_tokens": usage.get("total_tokens"),
              "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
              "cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens")}
    return {key: value for key, value in fields.items() if value is not None} or None


def answer_text(items):
    return "".join(text_of(item) for item in items
                   if item.get("type") == "message" and item.get("role", "assistant") == "assistant")


def sampling_value(value):
    """temperature or top_p as the service echoed it; anything that is not a plain number is ignored."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def summarize_response(response):
    """Secret-free summary of a Responses API result (accepts SDK objects or dicts)."""
    data = to_plain(response)
    items = [item for item in data.get("output") or [] if isinstance(item, Mapping)]
    answer = answer_text(items)
    reference = data.get("agent_reference") if isinstance(data.get("agent_reference"), Mapping) else {}
    conversation = data.get("conversation")
    conversation_id = conversation.get("id") if isinstance(conversation, Mapping) else conversation
    incomplete = data.get("incomplete_details")
    error = data.get("error")
    return {
        "response_id": data.get("id"),
        "status": data.get("status"),
        "model": data.get("model"),
        "temperature": sampling_value(data.get("temperature")),
        "top_p": sampling_value(data.get("top_p")),
        "agent": {key: reference[key] for key in ("name", "version") if reference.get(key) is not None} or None,
        "conversation_id": conversation_id if isinstance(conversation_id, str) else None,
        "output_item_types": [item.get("type") for item in items],
        "reasoning_item_count": sum(1 for item in items if item.get("type") == "reasoning"),
        "tool_events": [summarize_item(item) for item in items if item.get("type") not in ("message", "reasoning")],
        "final_answer": clip(answer),
        "answer_chars": len(answer),
        "answer_urls": urls_in(answer),
        "think_tag": "<think" in answer.lower(),
        "usage": summarize_usage(data.get("usage")),
        "incomplete_reason": incomplete.get("reason") if isinstance(incomplete, Mapping) else None,
        "error": ({"code": error.get("code"), "message": clip(error.get("message"), 300)}
                  if isinstance(error, Mapping) else error),
    }
