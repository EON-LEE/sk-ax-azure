"""A model-only, OpenAI-compatible API in front of the GPU link: the backend for a Foundry agent's own model.

This is the bring-your-own-model (BYOM) path of a pre-demo, not a Model Catalog registration. Foundry Agent
Service calls `POST {base}/chat/completions` on a gateway (Azure API Management) and names the model in the
body; the gateway forwards here. Per profile (FP8 at the root, NVFP4 under /models/nvfp4) the API is
  GET  /v1/models, /v1/models/{id}   a static list, independent of the GPU
  POST /v1/chat/completions          chat, tools and streaming
  GET  /v1/health                    whether the GPU link is ready (200 or 503)
and every route needs the API key: `Authorization: Bearer <key>` or an `api-key` header. The settings hold only
the key's SHA-256; without one the routes do not exist (404). The demo's own cookies, passwords and admin token
do not open this API, and the key opens nothing under /api.

The hub's relay answers the browser with custom SSE events, so this module talks to the link itself. vLLM's
chunks pass through unchanged (minus reasoning and the usage chunk unless asked for), a non-stream answer is
built from the same chunks, and the upstream request is built from an allow-list: a caller cannot set vLLM
extensions such as chat_template_kwargs, guided_* or priority. Nothing in a request or response body, no header
and no key is ever logged: one JSON line per request carries counts and timings only.
"""
import asyncio
import collections
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from hub import Full, Gate, upstream_error

LOG = logging.getLogger("axk2.model_api")
SERVED_NAME = "axk2"  # --served-model-name of both vLLM profiles (aml/src/entry.sh)
UPSTREAM_PATH = "/v1/chat/completions"
CREATED = 1_735_689_600  # the static model list needs some timestamp
DEFAULT_TEMPERATURE, DEFAULT_TOP_P = 0.6, 0.95  # what the demo chat uses
REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
HEX64 = re.compile(r"[0-9a-f]{64}")
TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
CALL_ID = re.compile(r"[\x21-\x7e]{1,256}")
TEXT_PARTS = ("text", "input_text", "output_text")
REASONING = ("reasoning_content", "reasoning")
HANDLED = frozenset({"model", "messages", "tools", "tool_choice", "parallel_tool_calls", "max_tokens",
                     "max_completion_tokens", "temperature", "top_p", "stop", "seed", "stream", "stream_options",
                     "response_format", "n", "logprobs", "top_logprobs", "logit_bias", "functions", "modalities",
                     "audio"})
DONE = b"data: [DONE]\n\n"
KEEPALIVE = b": keepalive\n\n"
EVENT_LIMIT = 1 << 20  # an upstream event without its blank line stops being plausible here
MAX_DEPTH = 32


@dataclass(frozen=True)
class Settings:
    """AXK2_MODEL_API_* settings; the API exists only when at least one valid key digest is configured."""
    key_hashes: tuple = ()
    name_fp8: str = "ax-k2-fp8"
    name_nvfp4: str = "ax-k2-nvfp4"
    aliases_fp8: tuple = ("skt/A.X-K2",)
    aliases_nvfp4: tuple = ("skt/A.X-K2-NVFP4",)
    max_active: int = 4
    max_queue: int = 8
    rate_per_minute: int = 60
    max_tokens: int = 8192
    default_max_tokens: int = 4096
    max_body_bytes: int = 2 * 2**20
    max_messages: int = 400
    max_tools: int = 64
    max_schema_bytes: int = 128 * 2**10
    queue_timeout: float = 30.0
    timeout: float = 300.0

    @property
    def enabled(self):
        return bool(self.key_hashes)

    def names_for(self, profile):
        """The canonical model name first, then the aliases."""
        return (self.name_nvfp4, *self.aliases_nvfp4) if profile == "nvfp4" else (self.name_fp8, *self.aliases_fp8)

    @classmethod
    def from_env(cls, env):
        def number(name, default, low, high):
            try:
                value = type(default)(env.get(name) or default)
            except (TypeError, ValueError):
                return default
            if isinstance(value, float) and not math.isfinite(value):
                return default
            return min(max(value, low), high)

        def listed(name, default):
            found = tuple(dict.fromkeys(part.strip() for part in re.split(r"[,\n]", env.get(name) or "")
                                        if part.strip()))
            return found if found and all(len(item) <= 200 and item.isprintable() for item in found) else default

        return cls(key_hashes=digests(env.get("AXK2_MODEL_API_KEY_SHA256")),
                   name_fp8=listed("AXK2_MODEL_API_NAME_FP8", (cls.name_fp8,))[0],
                   name_nvfp4=listed("AXK2_MODEL_API_NAME_NVFP4", (cls.name_nvfp4,))[0],
                   aliases_fp8=listed("AXK2_MODEL_API_ALIASES_FP8", cls.aliases_fp8),
                   aliases_nvfp4=listed("AXK2_MODEL_API_ALIASES_NVFP4", cls.aliases_nvfp4),
                   max_active=number("AXK2_MODEL_API_MAX_ACTIVE", 4, 1, 256),
                   max_queue=number("AXK2_MODEL_API_MAX_QUEUE", 8, 0, 1024),
                   rate_per_minute=number("AXK2_MODEL_API_RATE_PER_MINUTE", 60, 1, 100_000),
                   max_tokens=number("AXK2_MODEL_API_MAX_TOKENS", 8192, 1, 131_072),
                   default_max_tokens=number("AXK2_MODEL_API_DEFAULT_MAX_TOKENS", 4096, 1, 131_072),
                   max_body_bytes=number("AXK2_MODEL_API_MAX_BODY_BYTES", 2 * 2**20, 1024, 64 * 2**20),
                   max_messages=number("AXK2_MODEL_API_MAX_MESSAGES", 400, 1, 10_000),
                   max_tools=number("AXK2_MODEL_API_MAX_TOOLS", 64, 0, 1024),
                   max_schema_bytes=number("AXK2_MODEL_API_MAX_SCHEMA_BYTES", 128 * 2**10, 1024, 8 * 2**20),
                   queue_timeout=number("AXK2_MODEL_API_QUEUE_TIMEOUT", 30.0, 0.0, 3600.0),
                   timeout=number("AXK2_MODEL_API_TIMEOUT", 300.0, 1.0, 3600.0))


def digests(text):
    """Valid SHA-256 hex digests in a comma, semicolon or whitespace separated list (two while a key rotates)."""
    found = []
    for part in re.split(r"[\s,;]+", text or ""):
        part = part.strip().lower().removeprefix("sha256:")
        if HEX64.fullmatch(part) and part not in found:
            found.append(part)
    return tuple(found)


def key_digest(key):
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------------------------------------- errors
class ApiError(Exception):
    """An OpenAI-style error: the HTTP status and the {"error": {message, type, param, code}} body."""

    def __init__(self, status, code, message, *, param=None, headers=None):
        super().__init__(message)
        self.status, self.code, self.message, self.param = status, code, message, param
        self.headers = headers or {}

    @property
    def kind(self):
        if self.status == 429:
            return "rate_limit_error"
        return "server_error" if self.status >= 500 else "invalid_request_error"

    def body(self):
        return {"error": {"message": self.message, "type": self.kind, "param": self.param, "code": self.code}}

    def response(self):
        return JSONResponse(self.body(), status_code=self.status, headers=self.headers)


class Gone(Exception):
    """The caller went away; whatever it was waiting for is cleaned up and nothing is sent."""


def invalid(param, message):
    return ApiError(400, "invalid_value", message, param=param)


def unsupported(param, message):
    return ApiError(400, "unsupported_value", message, param=param)


def unavailable():
    return ApiError(503, "backend_unavailable", "The model backend is not ready. Retry later.",
                    headers={"Retry-After": "30"})


def interrupted():
    return ApiError(503, "backend_unavailable", "The model backend connection was interrupted. Retry later.",
                    headers={"Retry-After": "5"})


def busy():
    return ApiError(429, "capacity_exceeded", "The model is busy. Retry shortly.", headers={"Retry-After": "5"})


def timed_out():
    return ApiError(504, "timeout", "The model did not finish within the time limit.")


def broken(message="The model backend returned an invalid response."):
    return ApiError(502, "upstream_error", message)


def internal_error():
    return ApiError(500, "internal_error", "The server had an error while processing your request.")


def printable(text, limit=64):
    return "".join(char for char in str(text) if char.isprintable())[:limit]


def encodable(value):
    """Whether a parsed JSON value is text that UTF-8, and so a WebSocket text frame, can carry. A JSON escape such
    as \\ud800 parses to an unpaired surrogate, which it cannot: such text would fail the hub's writer and close
    the one GPU link for every request in flight. A value nested beyond the encoder's depth fails the same way."""
    try:
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError):  # UnicodeEncodeError is a ValueError
        return False
    return True


def upstream_failure(status, text):
    """vLLM's non-200 answer as an API error; only validation problems keep vLLM's own words."""
    info = upstream_error(status, text)
    detail = printable(info.get("detail", ""), 300)
    if status in (400, 422):
        if info["code"] == "too_long":
            return ApiError(400, "context_length_exceeded", detail, param="messages")
        return ApiError(400, "invalid_request", detail or "The model backend rejected the request.")
    if status in (429, 503):
        return ApiError(503, "backend_unavailable", "The model backend is busy. Retry later.",
                        headers={"Retry-After": "5"})
    return broken("The model backend failed to process the request.")


# ---------------------------------------------------------------------------------------- the request
@dataclass
class Spec:
    """A validated request: the body for vLLM and what the response needs to know about the caller's wishes."""
    body: dict
    stream: bool
    include_usage: bool
    dropped: list
    model: str


def refuse_unsupported(payload):
    n = payload.get("n")
    if n is not None and not (type(n) is int and n == 1):
        raise unsupported("n", "Only n=1 is supported.")
    if payload.get("logprobs") not in (None, False):
        raise unsupported("logprobs", "logprobs are not supported.")
    top = payload.get("top_logprobs")
    if top is not None and not (type(top) is int and top == 0):  # SDKs send top_logprobs: 0 next to no logprobs
        raise unsupported("top_logprobs", "Only top_logprobs=0 is supported.")
    if payload.get("logit_bias"):
        raise unsupported("logit_bias", "logit_bias is not supported.")
    if payload.get("functions"):
        raise unsupported("functions", "Use 'tools' instead of 'functions'.")
    modalities = payload.get("modalities")
    if modalities is not None and (not isinstance(modalities, list) or any(item != "text" for item in modalities)):
        raise unsupported("modalities", "Only text output is supported.")
    if payload.get("audio") is not None:
        raise unsupported("audio", "Audio output is not supported.")
    response_format = payload.get("response_format")
    if response_format is not None and not (isinstance(response_format, dict)
                                            and response_format.get("type") == "text"):
        raise unsupported("response_format", "Only response_format {\"type\": \"text\"} is supported.")


def text_of(content, where):
    """A message's content as one string: text or an array of text parts (other part types are refused)."""
    if content is None or isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise invalid(where, "content must be a string or an array of text parts.")
    parts = []
    for index, part in enumerate(content):
        if not isinstance(part, dict) or part.get("type") not in TEXT_PARTS or not isinstance(part.get("text"), str):
            raise unsupported(f"{where}[{index}]", "Only text content parts are supported (no images, audio or files).")
        parts.append(part["text"])
    return "\n".join(parts)


def clean_calls(calls, where):
    if not isinstance(calls, list) or len(calls) > 64:
        raise invalid(f"{where}.tool_calls", "tool_calls must be an array of at most 64 tool calls.")
    clean = []
    for index, call in enumerate(calls):
        place = f"{where}.tool_calls[{index}]"
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or call.get("type", "function") != "function":
            raise invalid(place, "Only function tool calls are supported.")
        if not isinstance(call.get("id"), str) or not CALL_ID.fullmatch(call["id"]):
            raise invalid(f"{place}.id", "A tool call needs an id.")
        if not isinstance(function.get("name"), str) or not TOOL_NAME.fullmatch(function["name"]):
            raise invalid(f"{place}.function.name", "Invalid function name.")
        arguments = function.get("arguments")
        if isinstance(arguments, (dict, list)):
            arguments = json.dumps(arguments, ensure_ascii=False)
        if arguments is None or arguments == "":
            arguments = "{}"
        if not isinstance(arguments, str) or len(arguments) > 256 * 1024:
            raise invalid(f"{place}.function.arguments", "arguments must be a JSON string.")
        try:
            parsed = json.loads(arguments)
        except (ValueError, RecursionError):
            raise invalid(f"{place}.function.arguments", "arguments must be valid JSON.")
        if not encodable(parsed):  # the arguments are JSON text: a \ud800 escape in it only becomes a surrogate here
            raise invalid(f"{place}.function.arguments", "arguments must not contain an unpaired UTF-16 surrogate.")
        clean.append({"id": call["id"], "type": "function", "function": {"name": function["name"],
                                                                         "arguments": arguments}})
    return clean


def clean_messages(messages, limit):
    if not isinstance(messages, list) or not messages:
        raise ApiError(400, "missing_required_parameter", "'messages' must be a non-empty array.", param="messages")
    if len(messages) > limit:
        raise invalid("messages", f"Too many messages (the limit is {limit}).")
    clean = []
    for index, message in enumerate(messages):
        where = f"messages[{index}]"
        if not isinstance(message, dict):
            raise invalid(where, "Each message must be an object.")
        role = message.get("role")
        role = "system" if role == "developer" else role
        if role not in ("system", "user", "assistant", "tool"):
            raise invalid(f"{where}.role", "Unsupported role.")
        content = text_of(message.get("content"), f"{where}.content")
        item = {"role": role}
        if role == "assistant":
            if message.get("tool_calls"):
                item["tool_calls"] = clean_calls(message["tool_calls"], where)
            item["content"] = content or ""
        elif role == "tool":
            if not isinstance(message.get("tool_call_id"), str) or not CALL_ID.fullmatch(message["tool_call_id"]):
                raise invalid(f"{where}.tool_call_id", "A tool message needs the tool_call_id it answers.")
            item["tool_call_id"], item["content"] = message["tool_call_id"], content or ""
        else:
            if content is None:
                raise invalid(f"{where}.content", "content is required.")
            item["content"] = content
        clean.append(item)
    return clean


def depth(value, limit=MAX_DEPTH):
    stack = [(value, 1)]
    while stack:
        item, level = stack.pop()
        if level > limit:
            return False
        if isinstance(item, dict):
            stack.extend((child, level + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, level + 1) for child in item)
    return True


def clean_tools(tools, settings):
    if tools is None or tools == []:
        return []
    if not isinstance(tools, list) or len(tools) > settings.max_tools:
        raise invalid("tools", f"tools must be an array of at most {settings.max_tools} function tools.")
    clean = []
    for index, tool in enumerate(tools):
        place = f"tools[{index}]"
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or tool.get("type", "function") != "function":
            raise unsupported(place, "Only function tools are supported.")
        if not isinstance(function.get("name"), str) or not TOOL_NAME.fullmatch(function["name"]):
            raise invalid(f"{place}.function.name", "Invalid function name.")
        description, parameters = function.get("description"), function.get("parameters")
        if description is not None and not isinstance(description, str):
            raise invalid(f"{place}.function.description", "description must be a string.")
        if parameters is None:
            parameters = {"type": "object", "properties": {}}
        if not isinstance(parameters, dict) or not depth(parameters):
            raise invalid(f"{place}.function.parameters", "parameters must be a JSON schema object.")
        entry = {"name": function["name"], "parameters": parameters}
        if description:
            entry["description"] = description
        clean.append({"type": "function", "function": entry})
    if len(json.dumps(clean, ensure_ascii=False).encode("utf-8")) > settings.max_schema_bytes:
        raise invalid("tools", f"The tool definitions exceed {settings.max_schema_bytes} bytes.")
    return clean


def tool_choice_of(choice, tools):
    if choice is None:
        return "auto"
    if isinstance(choice, str) and choice in ("none", "auto", "required"):
        return choice
    function = choice.get("function") if isinstance(choice, dict) else None
    name = function.get("name") if isinstance(function, dict) else None
    if (isinstance(choice, dict) and choice.get("type", "function") == "function"
            and any(tool["function"]["name"] == name for tool in tools)):
        return {"type": "function", "function": {"name": name}}
    raise invalid("tool_choice", "tool_choice must be none, auto, required or one of the given functions.")


def number(payload, name, low, high, default=None):
    value = payload.get(name)
    if value is None:
        return default
    if type(value) not in (int, float) or not low <= value <= high:
        raise invalid(name, f"'{name}' must be a number between {low} and {high}.")
    return value


def token_limit(payload, settings):
    for name in ("max_completion_tokens", "max_tokens"):
        value = payload.get(name)
        if value is None:
            continue
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if type(value) is not int or value < 1:
            raise invalid(name, f"'{name}' must be a positive integer.")
        return min(value, settings.max_tokens)
    return min(settings.default_max_tokens, settings.max_tokens)


def build_request(payload, settings, profile):
    """The allow-listed vLLM request for a caller's JSON object, or an ApiError."""
    names = {name.casefold() for name in settings.names_for(profile)}
    model = payload.get("model")
    if model is None:
        raise ApiError(400, "missing_required_parameter", "'model' is required.", param="model")
    if not isinstance(model, str):
        raise invalid("model", "'model' must be a string.")
    if model.strip().casefold() not in names:
        raise ApiError(404, "model_not_found", f"The model `{printable(model)}` does not exist or is not served "
                       "at this URL.", param="model")
    refuse_unsupported(payload)
    stream, options = payload.get("stream"), payload.get("stream_options")
    if stream is not None and not isinstance(stream, bool):
        raise invalid("stream", "'stream' must be a boolean.")
    if options is not None and not isinstance(options, dict):
        raise invalid("stream_options", "'stream_options' must be an object.")
    include_usage = bool(stream and (options or {}).get("include_usage") is True)
    body = {"model": SERVED_NAME, "messages": clean_messages(payload.get("messages"), settings.max_messages),
            "stream": True, "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False}, "max_tokens": token_limit(payload, settings),
            "temperature": number(payload, "temperature", 0, 2, DEFAULT_TEMPERATURE),
            "top_p": max(number(payload, "top_p", 0, 1, DEFAULT_TOP_P), 1e-6)}
    tools = clean_tools(payload.get("tools"), settings)
    if tools:
        parallel = payload.get("parallel_tool_calls")
        if parallel is not None and not isinstance(parallel, bool):
            raise invalid("parallel_tool_calls", "'parallel_tool_calls' must be a boolean.")
        body["tools"], body["tool_choice"] = tools, tool_choice_of(payload.get("tool_choice"), tools)
        if parallel is not None:
            body["parallel_tool_calls"] = parallel
    stop = payload.get("stop")
    if stop is not None:
        items = [stop] if isinstance(stop, str) else stop
        if not isinstance(items, list) or len(items) > 4 or not all(isinstance(i, str) and len(i) <= 256
                                                                    for i in items):
            raise invalid("stop", "'stop' must be a string or an array of up to 4 strings.")
        if any(items):
            body["stop"] = [item for item in items if item]
    seed = payload.get("seed")
    if seed is not None:
        if type(seed) is not int or not -2**63 <= seed < 2**63:
            raise invalid("seed", "'seed' must be an integer.")
        body["seed"] = seed
    dropped = sorted(printable(key, 40) for key in payload if key not in HANDLED)[:20]
    return Spec(body, bool(stream), include_usage, dropped, settings.names_for(profile)[0])


def parse_json(raw):
    def reject(constant):
        raise ValueError(constant)

    try:
        value = json.loads(raw, parse_constant=reject)
    except (ValueError, RecursionError):
        raise ApiError(400, "invalid_json", "The request body is not valid JSON.")
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_json", "The request body must be a JSON object.")
    if not encodable(value):
        raise ApiError(400, "invalid_json", "The request body has text that cannot be encoded as UTF-8 (an unpaired "
                       "UTF-16 surrogate) or is nested too deeply.")
    return value


# --------------------------------------------------------------------------------------- the response
class Events:
    """Whole SSE events out of the link's text, plus what the log and the non-stream answer need."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.buffer, self.done, self.finish, self.usage = "", False, None, None

    def feed(self, text, keep_usage):
        """[(bytes to send on or None, parsed chunk or None)] for every event completed by `text`."""
        self.buffer = (self.buffer + text).replace("\r\n", "\n")
        *blocks, self.buffer = self.buffer.split("\n\n")
        if len(self.buffer) > EVENT_LIMIT:
            raise broken()
        return [self.event(block, keep_usage) for block in blocks if block.strip() and not self.done]

    def event(self, block, keep_usage):
        data, kept = [], []
        for line in block.split("\n"):
            if line.startswith("data:"):
                data.append(line[6:] if line.startswith("data: ") else line[5:])
                kept.append(line)
            elif line.startswith(":"):
                kept.append(line)  # event:, id: and retry: lines are not passed on
        raw = ("\n".join(kept) + "\n\n").encode("utf-8") if kept else None
        if not data:
            return raw, None
        payload = "\n".join(data).strip()
        if payload == "[DONE]":
            self.done = True
            return raw, None
        try:
            chunk = json.loads(payload)
        except ValueError:
            raise broken()
        if not isinstance(chunk, dict):
            raise broken()
        if chunk.get("error") and not chunk.get("choices"):
            raise broken("The model backend reported an error.")
        return self.chunk(chunk, raw, keep_usage), chunk

    def chunk(self, chunk, raw, keep_usage):
        usage, choices = chunk.get("usage"), chunk.get("choices")
        if isinstance(usage, dict):
            self.usage = usage
            counted = lambda value: value if type(value) is int else None  # noqa: E731
            self.ctx.prompt_tokens = counted(usage.get("prompt_tokens"))
            self.ctx.completion_tokens = counted(usage.get("completion_tokens"))
        trimmed, quiet = False, True
        for choice in choices if isinstance(choices, list) else ():
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                for key in REASONING:
                    if key in delta:
                        del delta[key]
                        trimmed = True
                if delta.get("content") or delta.get("tool_calls"):
                    self.ctx.token()
                quiet = quiet and not delta
            if choice.get("finish_reason"):
                self.finish = self.ctx.finish_reason = choice["finish_reason"]
                quiet = False
        if isinstance(usage, dict) and not choices and not keep_usage:
            return None
        if trimmed:
            if quiet and not isinstance(usage, dict):
                return None
            return ("data: " + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")
        return raw


class Synth:
    """The one chat.completion a non-stream caller expects, put together from the stream's chunks."""

    def __init__(self):
        self.parts, self.calls, self.meta = [], {}, {}

    def add(self, chunk):
        for key in ("id", "created", "model", "system_fingerprint"):
            if chunk.get(key) is not None and key not in self.meta:
                self.meta[key] = chunk[key]
        for choice in chunk.get("choices") or ():
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                self.parts.append(delta["content"])
            for call in delta.get("tool_calls") or ():
                self.call(call)

    def call(self, call):
        if not isinstance(call, dict):
            return
        index = call.get("index")
        entry = self.calls.setdefault(index if type(index) is int else 0, {"id": None, "name": "", "arguments": ""})
        if isinstance(call.get("id"), str) and call["id"]:
            entry["id"] = call["id"]
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name, arguments = function.get("name"), function.get("arguments")
        if isinstance(name, str) and name and name != entry["name"]:
            entry["name"] += name
        if isinstance(arguments, str):
            entry["arguments"] += arguments

    def completion(self, events, fallback_model):
        calls = [self.calls[index] for index in sorted(self.calls)]
        text = "".join(self.parts)
        message = {"role": "assistant", "content": text if text or not calls else None}
        if calls:
            message["tool_calls"] = [
                {"id": call["id"] or "call_" + secrets.token_hex(12), "type": "function",
                 "function": {"name": call["name"], "arguments": call["arguments"] or "{}"}} for call in calls]
        finish = events.finish
        if calls and finish == "stop":
            finish = "tool_calls"
        body = {"id": self.meta.get("id") or "chatcmpl-" + secrets.token_hex(12), "object": "chat.completion",
                "created": self.meta.get("created") if type(self.meta.get("created")) is int else int(time.time()),
                "model": self.meta.get("model") or fallback_model,
                "choices": [{"index": 0, "message": message, "logprobs": None, "finish_reason": finish}]}
        if self.meta.get("system_fingerprint"):
            body["system_fingerprint"] = self.meta["system_fingerprint"]
        usage = events.usage or {}
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if type(prompt) is int and type(completion) is int:
            body["usage"] = {"prompt_tokens": prompt, "completion_tokens": completion,
                             "total_tokens": prompt + completion}
        return body


# ------------------------------------------------------------------------------------ one request
class Context:
    """What a request leaves in the log: counts and timings, never any content."""

    def __init__(self, request_id, profile):
        self.request_id, self.profile = request_id, profile
        self.route, self.stream, self.status, self.error = "other", None, None, None
        self.prompt_tokens = self.completion_tokens = self.finish_reason = self.queue = self.ttft = None
        self.dropped = []
        self.started, self.logged = time.monotonic(), False

    def token(self):
        if self.ttft is None:
            self.ttft = time.monotonic() - self.started

    def log(self):
        if self.logged:
            return
        self.logged = True
        ms = lambda value: None if value is None else round(value * 1000)  # noqa: E731
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": "model_api",
                  "request_id": self.request_id, "profile": self.profile, "route": self.route,
                  "stream": self.stream, "status": self.status, "prompt_tokens": self.prompt_tokens,
                  "completion_tokens": self.completion_tokens, "ttft_ms": ms(self.ttft), "queue_ms": ms(self.queue),
                  "total_ms": ms(time.monotonic() - self.started), "finish_reason": self.finish_reason,
                  "error": self.error}
        if self.dropped:
            record["dropped_fields"] = self.dropped
        try:
            LOG.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        except Exception:
            pass


class Frame:
    """Outermost layer of the /v1 app: request id, profile and cache headers on every response, one log line."""

    def __init__(self, app, profile):
        self.app, self.profile = app, profile

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        given = next((value.decode("latin-1") for name, value in scope["headers"] if name == b"x-request-id"), "")
        ctx = Context(given if REQUEST_ID.fullmatch(given) else "req_" + secrets.token_hex(16), self.profile)
        scope["axk2.context"] = ctx
        started = False

        async def stamp(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                ctx.status = message["status"]
                headers = list(message.get("headers", []))
                headers += [(b"x-request-id", ctx.request_id.encode("ascii")),
                            (b"x-axk2-profile", self.profile.encode("ascii"))]
                if not any(name.lower() == b"cache-control" for name, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message = dict(message, headers=headers)
            await send(message)

        try:
            await self.app(scope, receive, stamp)
        except Exception:
            # Handlers answer their own errors; this is the last resort, so that no traceback reaches a caller
            # and the response still carries the correlation headers.
            ctx.error = "internal_error"
            if not started:
                await internal_error().response()(scope, receive, stamp)
        finally:
            ctx.log()


class Job:
    """One admitted chat request: its place in this API's gate and its request stream on the link."""

    def __init__(self, api, ctx):
        self.api, self.ctx = api, ctx
        self.ticket = api.gate.join()  # raises Full
        self.conn = self.rid = self.queue = None
        self.finished = self.closed = False
        self.checked = time.monotonic()  # when the caller was last seen to be there

    async def admitted(self, request, limit):
        started = time.monotonic()
        end = started + limit
        while not self.ticket.granted:
            left = end - time.monotonic()
            if left <= 0:
                raise busy()
            await asyncio.wait({self.ticket.future}, timeout=min(left, 1.0))
            if not self.ticket.granted and await request.is_disconnected():
                raise Gone()
        self.ctx.queue = time.monotonic() - started

    def begin(self, conn, body):
        self.conn, self.rid = conn, secrets.token_hex(8)
        self.queue = conn.streams[self.rid] = asyncio.Queue()
        conn.send({"type": "req", "id": self.rid, "path": UPSTREAM_PATH, "body": body})

    async def poll(self, wait, request=None):
        """The next link message, or None after `wait` seconds. With a request it notices a vanished caller at
        least once a second, also while the upstream keeps sending."""
        end = time.monotonic() + wait
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return None
            message = None
            try:
                message = await asyncio.wait_for(self.queue.get(), left if request is None else min(left, 1.0))
            except asyncio.TimeoutError:
                pass
            if message is not None and message["type"] in ("end", "error", "lost"):
                self.finished = True
            if request is not None and (message is None or time.monotonic() - self.checked >= 1.0):
                self.checked = time.monotonic()
                if await request.is_disconnected():
                    raise Gone()
            if message is not None:
                return message

    async def head(self, request, until):
        """vLLM's status code, or None when `until` (a monotonic time) comes first."""
        message = await self.poll(until - time.monotonic(), request)
        if message is None:
            return None
        if message["type"] != "head":
            raise interrupted()
        return message.get("status")

    async def failure(self, request, deadline):
        """The text of a non-200 answer, up to its end."""
        text = ""
        while True:
            message = await self.poll(deadline - time.monotonic(), request)
            if message is None or message["type"] != "data":
                return text
            text = (text + message.get("chunk", ""))[:8192]

    def close(self):
        """Synchronous and idempotent: it runs from `finally` blocks, also under cancellation."""
        if self.closed:
            return
        self.closed = True
        if self.conn is not None and self.rid is not None:
            self.conn.streams.pop(self.rid, None)
            if not self.finished:
                self.conn.send({"type": "cancel", "id": self.rid})  # vLLM aborts the generation
        self.api.gate.leave(self.ticket)


class Streamed(StreamingResponse):
    def __init__(self, job, content):
        super().__init__(content, media_type="text/event-stream",
                         headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        self.job = job

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.job.close()


class Stdout(logging.StreamHandler):
    """Logs to whatever sys.stdout is at the moment: App Service shows the container's stdout."""

    def __init__(self):
        super().__init__(sys.stdout)

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, value):
        pass


class ModelAPI:
    keepalive = 10.0  # seconds without upstream data before an SSE comment goes out
    read_timeout = 30.0  # seconds for a caller to send its body

    def __init__(self, settings, *, profile, hub, is_ready, power, limiter, client_ip):
        self.settings, self.profile, self.hub = settings, profile, hub
        self.is_ready, self.power, self.limiter, self.client_ip = is_ready, power, limiter, client_ip
        self.names = settings.names_for(profile)
        self.accepted = {name.casefold() for name in self.names}
        self.gate = Gate(settings.max_active, settings.max_queue)
        self.recent = collections.deque()

    def app(self):
        routes = [Route("/models", self.guard(self.models), methods=["GET"]),
                  Route("/models/{name:path}", self.guard(self.model), methods=["GET"]),
                  Route("/chat/completions", self.guard(self.chat), methods=["POST"]),
                  Route("/health", self.guard(self.health), methods=["GET"])]
        app = Starlette(routes=routes, middleware=[Middleware(Frame, profile=self.profile)],
                        exception_handlers={HTTPException: self.http_error})
        app.router.redirect_slashes = False
        return app

    def guard(self, handler):
        async def run(request):
            ctx = request.scope["axk2.context"]
            try:
                return await handler(request, ctx)
            except ApiError as exc:
                ctx.error = exc.code
                return exc.response()
            except Gone:
                ctx.error = "client_gone"
                return Response(status_code=499)
            except Exception:
                ctx.error = "internal_error"
                return internal_error().response()
        return run

    async def http_error(self, request, exc):
        code, message = {404: ("not_found", "Unknown URL."),
                         405: ("method_not_allowed", "This method is not allowed for this URL.")}.get(
            exc.status_code, ("http_error", "The request could not be processed."))
        if "axk2.context" in request.scope:
            request.scope["axk2.context"].error = code
        return ApiError(exc.status_code, code, message, headers=dict(exc.headers or {})).response()

    # ---------------------------------------------------------------------------------- protection
    def authenticate(self, request):
        ip = self.client_ip(request)
        if self.limiter.blocked(ip):
            raise ApiError(429, "rate_limit_exceeded", "Too many failed authentication attempts. Retry later.",
                           headers={"Retry-After": "60"})
        keys = []
        header = request.headers.get("authorization", "").strip()
        if header:
            keys.append(header[7:].strip() if header[:7].lower() == "bearer " else header)
        if request.headers.get("api-key", "").strip():
            keys.append(request.headers["api-key"].strip())
        if not keys:
            raise ApiError(401, "missing_api_key", "No API key provided. Send it as 'Authorization: Bearer <key>' or "
                           "in the 'api-key' header.", headers={"WWW-Authenticate": "Bearer"})
        matched = False
        for key in keys:
            if 0 < len(key) <= 512:
                digest = key_digest(key)
                for wanted in self.settings.key_hashes:
                    matched |= hmac.compare_digest(digest, wanted)  # no early exit
        if not matched:
            self.limiter.failed(ip)
            raise ApiError(401, "invalid_api_key", "Incorrect API key provided.",
                           headers={"WWW-Authenticate": "Bearer"})

    def limit_rate(self):
        now = time.monotonic()
        while self.recent and now - self.recent[0] >= 60:
            self.recent.popleft()
        if len(self.recent) >= self.settings.rate_per_minute:
            raise ApiError(429, "rate_limit_exceeded", "Rate limit reached. Retry later.",
                           headers={"Retry-After": str(max(1, math.ceil(60 - (now - self.recent[0]))))})
        self.recent.append(now)

    async def read_body(self, request):
        limit = self.settings.max_body_bytes
        too_large = ApiError(413, "request_too_large", f"The request body exceeds {limit} bytes.")
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                raise ApiError(400, "invalid_request", "Invalid Content-Length header.")
            if size > limit:
                raise too_large
        body = bytearray()
        try:
            async with asyncio.timeout(self.read_timeout):
                async for chunk in request.stream():
                    body += chunk
                    if len(body) > limit:
                        raise too_large
        except TimeoutError:
            raise ApiError(408, "request_timeout", "The request body was not received in time.")
        except ClientDisconnect:
            raise Gone()
        return bytes(body)

    # ------------------------------------------------------------------------------------- routes
    def model_object(self):
        return {"id": self.names[0], "object": "model", "created": CREATED, "owned_by": "skt"}

    async def models(self, request, ctx):
        ctx.route = "models"
        self.authenticate(request)
        return JSONResponse({"object": "list", "data": [self.model_object()]})

    async def model(self, request, ctx):
        ctx.route = "models"
        self.authenticate(request)
        name = request.path_params["name"]
        if name.strip().casefold() not in self.accepted:
            raise ApiError(404, "model_not_found", f"The model `{printable(name)}` does not exist or is not served "
                           "at this URL.", param="model")
        return JSONResponse(self.model_object())

    async def health(self, request, ctx):
        ctx.route = "health"
        self.authenticate(request)
        ready = bool(self.is_ready())
        body = {"status": "ok" if ready else "unavailable", "ready": ready, "state": self.power(),
                "profile": self.profile, "model": self.names[0]}
        if not ready:
            ctx.error = "backend_unavailable"
        return JSONResponse(body, status_code=200 if ready else 503, headers={} if ready else {"Retry-After": "30"})

    async def chat(self, request, ctx):
        ctx.route = "chat"
        self.authenticate(request)
        self.limit_rate()
        spec = build_request(parse_json(await self.read_body(request)), self.settings, self.profile)
        ctx.stream, ctx.dropped = spec.stream, spec.dropped
        if not self.is_ready():
            raise unavailable()
        try:
            job = Job(self, ctx)
        except Full:
            raise busy()
        handed = False
        try:
            await job.admitted(request, self.settings.queue_timeout)
            conn = self.hub.ready_link()
            if conn is None:
                raise unavailable()
            job.begin(conn, spec.body)
            deadline = time.monotonic() + self.settings.timeout
            status = await job.head(request, min(deadline, time.monotonic() + self.keepalive)
                                    if spec.stream else deadline)
            if status is None and time.monotonic() >= deadline:
                raise timed_out()
            if status is not None and status != 200:
                raise upstream_failure(status, await job.failure(request, deadline))
            if spec.stream:
                response = Streamed(job, self.stream_events(job, ctx, spec, status, deadline))
                handed = True  # the response closes the job once it is over
                return response
            return JSONResponse(await self.collect(job, request, ctx, spec, deadline))
        finally:
            if not handed:
                job.close()

    async def collect(self, job, request, ctx, spec, deadline):
        """A non-stream answer: the whole stream, merged into one chat.completion."""
        events, synth = Events(ctx), Synth()
        while True:
            message = await job.poll(deadline - time.monotonic(), request)
            if message is None:
                raise timed_out()
            if message["type"] == "data":
                for _, chunk in events.feed(message.get("chunk", ""), True):
                    if chunk is not None:
                        synth.add(chunk)
            elif message["type"] == "end":
                break
            else:
                raise interrupted()
        for _, chunk in events.feed("\n\n", True):
            if chunk is not None:
                synth.add(chunk)
        if events.finish is None:
            raise broken("The model backend ended the response early.")
        return synth.completion(events, spec.model)

    async def stream_events(self, job, ctx, spec, status, deadline):
        """The SSE body: vLLM's events as they come, [DONE] at the end, and an error event if it breaks."""
        events, failure = Events(ctx), ""
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise timed_out()
                message = await job.poll(min(left, self.keepalive))
                if message is None:
                    yield KEEPALIVE  # a long prefill sends nothing for a while
                    continue
                kind = message["type"]
                if kind == "head":
                    status = message.get("status")
                elif kind == "data" and status == 200:
                    for raw, _ in events.feed(message.get("chunk", ""), spec.include_usage):
                        if raw is not None:
                            yield raw
                elif kind == "data":
                    failure = (failure + message.get("chunk", ""))[:8192]
                elif kind == "end":
                    if status != 200:
                        raise upstream_failure(status, failure)
                    for raw, _ in events.feed("\n\n", spec.include_usage):
                        if raw is not None:
                            yield raw
                    if not events.done:
                        if events.finish is None:
                            raise broken("The model backend ended the response early.")
                        yield DONE
                    return
                else:
                    raise interrupted()
        except ApiError as exc:
            ctx.error = exc.code
            yield b"data: " + json.dumps(exc.body(), ensure_ascii=False).encode("utf-8") + b"\n\n" + DONE
        except asyncio.CancelledError:
            ctx.error = ctx.error or "client_gone"
            raise
        except Exception:
            ctx.error = "internal_error"
            yield (b"data: " + json.dumps(internal_error().body(), ensure_ascii=False).encode("utf-8") + b"\n\n"
                   + DONE)
        finally:
            job.close()


def install(app, *, settings, model_id, hub, is_ready, power, limiter, client_ip):
    """Mount the API at /v1 of `app` (a profile's app); without a key digest nothing is mounted."""
    if not settings.enabled:
        return None
    if not LOG.handlers:
        handler = Stdout()
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOG.addHandler(handler)
        LOG.setLevel(logging.INFO)
        LOG.propagate = False
    api = ModelAPI(settings, profile=model_id, hub=hub, is_ready=is_ready, power=power, limiter=limiter,
                   client_ip=client_ip)
    app.mount("/v1", api.app(), name="model_api")
    app.state.model_api = api
    return api
