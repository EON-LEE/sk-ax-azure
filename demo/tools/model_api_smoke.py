#!/usr/bin/env python3
"""Smoke checks for the A.X K2 model-only API (BYOM pre-demo helper).

The model-only API is the OpenAI-compatible surface that a Foundry agent calls (chat completions with
streaming and tool calling). This tool exercises it on the demo web app directly or through a gateway such
as APIM and prints a JSON summary that contains no secrets.

    model_api_smoke.py --base-url https://HOST/v1 --key-file KEYFILE [--out RESULT.json]
    model_api_smoke.py --base-url https://GATEWAY/PATH --auth-header api-key --key-file KEYFILE

Checks (--only takes a comma-separated subset):
  models       GET /models lists the model
  chat         non-stream completion
  stream       SSE completion: [DONE], no event lines, usage chunk and timing evidence
  tool         a tool-call request is answered with structured tool_calls
  tool_result  the tool result is sent back and a final answer returns
  auth         no key and a wrong key are both rejected with HTTP 401 (status only, body not parsed)

At most four calls reach the GPU (chat, stream, tool, tool_result). Calls run one at a time with no retry,
the output limit is small and thinking is never requested. The key is read from --key-file (first line),
--key-stdin or an environment variable (--key-env) and is never printed.
Exit code: 0 all selected checks passed, 1 a check failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import secrets
import ssl
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

VERSION = 1
CHECKS = ("models", "chat", "stream", "tool", "tool_result", "auth")
GPU_CHECKS = ("chat", "stream", "tool", "tool_result")
STOP_STATUSES = (401, 403, 429, 503)
LOOPBACK = ("localhost", "127.0.0.1", "::1")
KEY_FORMAT = re.compile(r"[\x21-\x7e]{8,512}")
MODEL_FORMAT = re.compile(r"[A-Za-z0-9._:/-]{1,128}")
CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
FINISHES = ("stop", "length")
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
MAX_BODY = 1_000_000
MAX_LINE = 1_000_000

QUESTION = "What is the weather in Seoul right now? Use the get_weather tool."
WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}
WEATHER_RESULT = {"city": "Seoul", "temperature_c": 21, "condition": "clear"}


class Usage(Exception):
    """A command-line problem; the message never repeats a user-supplied value."""


def tidy(value, limit=128, secret=None):
    text = "" if value is None else str(value)
    if secret:
        text = text.replace(secret, "***")
    return CONTROL.sub(" ", text).strip()[:limit]


def rounded(value, places=3):
    return None if value is None else round(value, places)


def is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# ------------------------------------------------------------------------------------- command line
class Parser(argparse.ArgumentParser):
    sink = None

    def error(self, message):
        # argparse repeats the offending values; a key typed into a wrong option must not be echoed
        named = re.match(r"(argument [^:]+):", message)
        if named:
            detail = f"{named.group(1)} is invalid"
        elif message.startswith("the following arguments are required"):
            detail = message
        else:
            detail = "unrecognized or malformed arguments"
        (self.sink or sys.stderr).write(f"{self.prog}: error: {detail}\n")
        raise SystemExit(2)


def build_parser(sink):
    parser = Parser(
        prog="model_api_smoke",
        description="Smoke checks for the A.X K2 model-only API (BYOM pre-demo).",
        epilog="Checks: " + ", ".join(CHECKS) + ". The key is never printed.",
        allow_abbrev=False,
    )
    parser.sink = sink
    parser.add_argument("--base-url", required=True,
                        help="API base URL including /v1, e.g. https://HOST/v1 or https://HOST/models/nvfp4/v1")
    parser.add_argument("--model", default="ax-k2-fp8", help="model name (default: ax-k2-fp8)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--key-file", help="file whose first line is the API key")
    source.add_argument("--key-stdin", action="store_true", help="read the API key from the first line of stdin")
    parser.add_argument("--key-env", default="AXK2_MODEL_API_KEY",
                        help="environment variable holding the key (default: AXK2_MODEL_API_KEY)")
    parser.add_argument("--auth-header", choices=("bearer", "api-key"), default="bearer",
                        help="how the key is sent: 'Authorization: Bearer' (default) or 'api-key' (APIM)")
    parser.add_argument("--only", action="append", help="comma-separated subset of checks (default: all)")
    parser.add_argument("--max-tokens", type=int, default=64, help="output token limit per call (default 64)")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds per call (default 120)")
    parser.add_argument("--out", help="also write the JSON summary to this file")
    return parser


def pick_checks(values):
    if not values:
        return list(CHECKS)
    chosen = set()
    for value in values:
        for name in value.split(","):
            name = name.strip().lower()
            if not name:
                continue
            if name not in CHECKS:
                raise Usage("--only names an unknown check")
            chosen.add(name)
    if not chosen:
        raise Usage("--only is empty")
    return [name for name in CHECKS if name in chosen]


class Target:
    def __init__(self, scheme, host, port, path, shown_port):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.path = path
        self.shown_port = shown_port

    @property
    def label(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = f":{self.shown_port}" if self.shown_port else ""
        return f"{self.scheme}://{host}{port}{self.path}"


def parse_target(url):
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise Usage("--base-url is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not host:
        raise Usage("--base-url must be an http(s) URL with a host")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise Usage("--base-url must not contain credentials, a query or a fragment")
    if parts.scheme == "http" and host.lower() not in LOOPBACK:
        raise Usage("plain http is only allowed for loopback hosts; use https")
    default = 443 if parts.scheme == "https" else 80
    return Target(parts.scheme, host, port or default, parts.path.rstrip("/"), port)


def read_key(args, environ, stdin):
    if args.key_file:
        try:
            with open(args.key_file, encoding="utf-8-sig") as handle:
                raw = handle.readline()
        except (OSError, UnicodeError):
            raise Usage("the key file cannot be read") from None
    elif args.key_stdin:
        raw = stdin.readline()
    else:
        raw = environ.get(args.key_env, "")
    key = raw.strip()
    if not key:
        raise Usage("no API key was provided (use --key-file, --key-stdin or --key-env)")
    if not KEY_FORMAT.fullmatch(key):
        raise Usage("the API key has an unexpected format")
    return key


# ------------------------------------------------------------------------------------------ transport
class Stream:
    """Line-by-line reader of a server-sent-events reply that keeps the timing evidence."""

    def __init__(self, started):
        self.started = started
        self.chunks = 0
        self.content_chunks = 0
        self.comment_lines = 0
        self.event_lines = 0
        self.other_lines = 0
        self.bad_json = 0
        self.after_done = 0
        self.done = False
        self.first_at = None
        self.last_at = None
        self.first_content_at = None
        self.last_content_at = None
        self.previous_at = None
        self.max_gap = 0.0
        self.finish_reason = None
        self.usage = None
        self.model = None
        self.reasoning = False
        self.tool_deltas = False
        self.errors = []
        self.text = []
        self.size = 0
        self.late = False
        self.oversized = False

    def consume(self, response, timeout):
        deadline = self.started + timeout
        while True:
            raw = response.readline(MAX_LINE + 1)
            now = time.perf_counter()
            if not raw:
                return
            if len(raw) > MAX_LINE:
                self.oversized = True
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line:
                self.take(line, now)
            if now > deadline:
                self.late = True
                return

    def take(self, line, now):
        if line.startswith(":"):
            self.comment_lines += 1
        elif line.startswith("data:"):
            data = line[5:].strip()
            if self.done:
                self.after_done += 1
            elif data == "[DONE]":
                self.done = True
            else:
                self.chunk(data, now)
        elif line.startswith("event:"):
            self.event_lines += 1
        else:
            self.other_lines += 1

    def chunk(self, data, now):
        try:
            value = json.loads(data)
        except ValueError:
            value = None
        if not isinstance(value, dict):
            self.bad_json += 1
            return
        self.chunks += 1
        if self.first_at is None:
            self.first_at = now
        else:
            self.max_gap = max(self.max_gap, now - self.previous_at)
        self.previous_at = self.last_at = now
        if isinstance(value.get("model"), str):
            self.model = value["model"]
        failure = value.get("error")
        if isinstance(failure, dict):
            self.errors.append(tidy(failure.get("code") or failure.get("type") or "error", 64))
        if isinstance(value.get("usage"), dict):
            self.usage = value["usage"]
        choices = value.get("choices")
        if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
            return
        choice = choices[0]
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.content_chunks += 1
            if self.first_content_at is None:
                self.first_content_at = now
            self.last_content_at = now
            if self.size < 2000:
                self.text.append(content)
                self.size += len(content)
        if delta.get("reasoning_content") or delta.get("reasoning"):
            self.reasoning = True
        if delta.get("tool_calls"):
            self.tool_deltas = True
        if isinstance(choice.get("finish_reason"), str):
            self.finish_reason = choice["finish_reason"]

    def tokens(self):
        if isinstance(self.usage, dict) and is_count(self.usage.get("completion_tokens")):
            return self.usage["completion_tokens"]
        return self.content_chunks

    def tpot_ms(self):
        tokens = self.tokens()
        if self.first_content_at is None or tokens < 2 or self.last_content_at <= self.first_content_at:
            return None
        return (self.last_content_at - self.first_content_at) * 1000 / (tokens - 1)

    def spread(self):
        return None if self.first_at is None else self.last_at - self.first_at

    def buffering(self):
        """A proxy that buffers the reply delivers all chunks in one burst after generation ends."""
        spread = self.spread()
        if self.chunks < 8 or spread is None:
            return "inconclusive"
        if spread >= 0.25:
            return "unlikely"
        return "likely" if spread < 0.05 else "inconclusive"


class Reply:
    """What came back from one HTTP exchange; a streamed body is summarised in `stream`."""

    def __init__(self, request_id):
        self.sent_request_id = request_id
        self.status = None
        self.headers = {}
        self.body = b""
        self.error = None
        self.stream = None
        self.connect_s = None
        self.headers_s = None
        self.total_s = None


class Client:
    def __init__(self, target, style, timeout, run_id):
        self.target = target
        self.style = style
        self.timeout = timeout
        self.run_id = run_id

    def auth_headers(self, key):
        if key is None:
            return {}
        return {"api-key": key} if self.style == "api-key" else {"Authorization": f"Bearer {key}"}

    def open(self):
        target = self.target
        if target.scheme == "https":
            return http.client.HTTPSConnection(target.host, target.port, timeout=self.timeout,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(target.host, target.port, timeout=self.timeout)

    def send(self, label, method, suffix, payload=None, *, key=None, stream=False):
        reply = Reply(f"smoke-{self.run_id}-{label}")
        headers = {
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "axk2-model-api-smoke/1",
            "X-Request-Id": reply.sent_request_id,
        }
        headers.update(self.auth_headers(key))
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = self.open()
        started = time.perf_counter()
        try:
            connection.connect()
            reply.connect_s = time.perf_counter() - started
            connection.request(method, self.target.path + suffix, body=body, headers=headers)
            response = connection.getresponse()
            reply.headers_s = time.perf_counter() - started
            reply.status = response.status
            reply.headers = {name.lower(): value for name, value in response.getheaders()}
            if stream and response.status == 200:
                reply.stream = Stream(started)
                reply.stream.consume(response, self.timeout)
            else:
                reply.body = response.read(MAX_BODY)
        except (OSError, http.client.HTTPException) as exc:
            reply.error = type(exc).__name__
        finally:
            reply.total_s = time.perf_counter() - started
            connection.close()
        return reply


# ---------------------------------------------------------------------------------------------- checks
class Run:
    def __init__(self, client, model, key, max_tokens):
        self.client = client
        self.model = model
        self.key = key
        self.max_tokens = max_tokens
        self.tool_call = None

    def text(self, value, limit=128):
        return tidy(value, limit, self.key)


def parse_json(body):
    try:
        value = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def begin(run, reply):
    seen = reply.headers.get("x-request-id")
    entry = {
        "ok": False,
        "status": reply.status,
        "x_request_id": run.text(seen) if seen else None,
        "sent_request_id": reply.sent_request_id,
        "request_id_echoed": seen == reply.sent_request_id,
        "elapsed_s": rounded(reply.total_s),
    }
    if reply.headers.get("x-axk2-profile"):
        entry["served_profile"] = run.text(reply.headers["x-axk2-profile"], 32)
    if reply.headers.get("retry-after"):
        entry["retry_after"] = run.text(reply.headers["retry-after"], 16)
    if reply.error:
        entry["error"] = reply.error
    entry["problems"] = []
    return entry


def finish(entry):
    entry["ok"] = not entry["problems"]
    return entry


def explain(run, reply, entry):
    body = parse_json(reply.body)
    failure = body.get("error") if body else None
    if not isinstance(failure, dict):
        return
    for field in ("code", "type", "param"):
        if isinstance(failure.get(field), str):
            entry[f"error_{field}"] = run.text(failure[field], 64)
    if isinstance(failure.get("message"), str):
        entry["error_message"] = run.text(failure["message"], 200)


def need_json(run, reply, entry):
    problems = entry["problems"]
    if reply.error:
        problems.append(f"request failed: {reply.error}")
        return None
    if reply.status != 200:
        problems.append(f"HTTP {reply.status}")
        explain(run, reply, entry)
        return None
    body = parse_json(reply.body)
    if body is None:
        problems.append("the reply is not a JSON object")
    return body


def inspect_completion(run, body, entry):
    """Check the chat.completion envelope; returns the assistant message (or None)."""
    problems = entry["problems"]
    if body.get("object") != "chat.completion":
        problems.append("object is not chat.completion")
    if not isinstance(body.get("id"), str) or not body["id"]:
        problems.append("id is missing")
    if not is_count(body.get("created")):
        problems.append("created is missing")
    if isinstance(body.get("model"), str) and body["model"]:
        entry["response_model"] = run.text(body["model"], 64)
    else:
        problems.append("model is missing")
    usage = body.get("usage")
    if isinstance(usage, dict) and all(is_count(usage.get(name)) for name in TOKEN_FIELDS):
        entry["usage"] = {name: usage[name] for name in TOKEN_FIELDS}
    else:
        problems.append("usage is missing or not integers")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        problems.append("choices is missing")
        return None
    choice = choices[0]
    if choice.get("index") != 0:
        problems.append("the choice index is not 0")
    finish_reason = choice.get("finish_reason")
    entry["finish_reason"] = run.text(finish_reason, 32) if isinstance(finish_reason, str) else None
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        problems.append("the choice has no assistant message")
        return None
    if message.get("reasoning_content") or message.get("reasoning"):
        problems.append("reasoning text is exposed")
    return message


def expect_finish(entry, allowed):
    if entry.get("finish_reason") not in allowed:
        entry["problems"].append(f"finish_reason is {entry.get('finish_reason')!r}, expected {' or '.join(allowed)}")


def check_models(run):
    reply = run.client.send("models", "GET", "/models", key=run.key)
    entry = begin(run, reply)
    body = need_json(run, reply, entry)
    if body is not None:
        data = body.get("data")
        if body.get("object") != "list" or not isinstance(data, list):
            entry["problems"].append("the reply is not an OpenAI model list")
        else:
            ids = [item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)]
            entry["model_ids"] = [run.text(item, 64) for item in ids[:20]]
            if run.model not in ids:
                entry["problems"].append("the requested model is not in the list (use the canonical name)")
    return finish(entry)


def check_chat(run):
    payload = {
        "model": run.model,
        "messages": [
            {"role": "system", "content": "You are a concise assistant."},
            {"role": "user", "content": "In one short sentence, what is the capital of France?"},
        ],
        "max_tokens": run.max_tokens,
        "temperature": 0,
    }
    reply = run.client.send("chat", "POST", "/chat/completions", payload, key=run.key)
    entry = begin(run, reply)
    body = need_json(run, reply, entry)
    if body is not None:
        message = inspect_completion(run, body, entry)
        if message is not None:
            answer = message.get("content")
            if isinstance(answer, str) and answer.strip():
                entry["answer_preview"] = run.text(answer, 120)
            else:
                entry["problems"].append("the answer text is empty")
            if message.get("tool_calls"):
                entry["problems"].append("tool_calls appeared although no tool was offered")
        expect_finish(entry, FINISHES)
    return finish(entry)


def check_stream(run):
    payload = {
        "model": run.model,
        "messages": [{"role": "user", "content": "Count from 1 to 30 separated by single spaces. Output only the numbers."}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": run.max_tokens,
        "temperature": 0,
    }
    reply = run.client.send("stream", "POST", "/chat/completions", payload, key=run.key, stream=True)
    entry = begin(run, reply)
    problems = entry["problems"]
    if reply.error and reply.stream is None:
        problems.append(f"request failed: {reply.error}")
    elif reply.status != 200:
        problems.append(f"HTTP {reply.status}")
        explain(run, reply, entry)
    if reply.stream is None:
        return finish(entry)
    stream = reply.stream
    kind = reply.headers.get("content-type", "")
    entry.update({
        "content_type": run.text(kind, 64),
        "connect_s": rounded(reply.connect_s),
        "headers_s": rounded(reply.headers_s),
        "first_chunk_s": rounded(None if stream.first_at is None else stream.first_at - stream.started),
        "ttft_s": rounded(None if stream.first_content_at is None else stream.first_content_at - stream.started),
        "first_to_last_chunk_s": rounded(stream.spread()),
        "max_chunk_gap_s": rounded(stream.max_gap),
        "tpot_ms": rounded(stream.tpot_ms(), 1),
        "total_s": rounded(reply.total_s),
        "chunks": stream.chunks,
        "content_chunks": stream.content_chunks,
        "comment_lines": stream.comment_lines,
        "event_lines": stream.event_lines,
        "other_lines": stream.other_lines,
        "done": stream.done,
        "finish_reason": run.text(stream.finish_reason, 32) if stream.finish_reason else None,
        "buffering": stream.buffering(),
        "answer_preview": run.text("".join(stream.text), 120),
    })
    if isinstance(stream.usage, dict):
        entry["usage"] = {name: stream.usage[name] for name in TOKEN_FIELDS if is_count(stream.usage.get(name))}
    if stream.model:
        entry["response_model"] = run.text(stream.model, 64)
    if reply.error:
        problems.append(f"the stream was interrupted: {reply.error}")
    if "text/event-stream" not in kind.lower():
        problems.append("content-type is not text/event-stream")
    if stream.late:
        problems.append("the stream did not finish within --timeout")
    if stream.oversized:
        problems.append("a stream line was oversized")
    if stream.bad_json:
        problems.append("a data line was not a JSON object")
    if stream.event_lines:
        problems.append("the stream contains event: lines")
    if stream.other_lines:
        problems.append("the stream contains lines that are neither data nor comments")
    if stream.errors:
        problems.append("the stream carries an error object: " + ", ".join(stream.errors[:3]))
    if not stream.done:
        problems.append("the stream did not end with data: [DONE]")
    if stream.after_done:
        problems.append("data followed [DONE]")
    if not stream.content_chunks:
        problems.append("no content chunk arrived")
    if entry["finish_reason"] not in FINISHES:
        problems.append(f"finish_reason is {entry['finish_reason']!r}, expected stop or length")
    if stream.usage is None:
        problems.append("no usage chunk although stream_options.include_usage was requested")
    if stream.reasoning:
        problems.append("reasoning text is exposed")
    if stream.tool_deltas:
        problems.append("tool_calls appeared although no tool was offered")
    return finish(entry)


def inspect_call(run, call, declared):
    """Returns (problems, the call as the follow-up turn needs it or None, a short summary for the report)."""
    if not isinstance(call, dict):
        return ["a tool call is not an object"], None, None
    problems = []
    ident = call.get("id")
    if not isinstance(ident, str) or not ident:
        problems.append("a tool call has no id")
    if call.get("type") != "function":
        problems.append("a tool call type is not function")
    function = call.get("function")
    name = arguments = None
    if not isinstance(function, dict):
        problems.append("a tool call has no function")
    else:
        name, arguments = function.get("name"), function.get("arguments")
        if name not in declared:
            problems.append("a tool call names a function that was not offered")
        if not isinstance(arguments, str):
            problems.append("function.arguments is not a JSON string")
        else:
            try:
                parsed = json.loads(arguments)
            except ValueError:
                parsed = None
                problems.append("function.arguments is not valid JSON")
            else:
                if not isinstance(parsed, dict):
                    problems.append("function.arguments is not a JSON object")
    seen = {"id": run.text(ident, 64), "name": run.text(name, 64), "arguments": run.text(arguments, 200)}
    return problems, ({"id": ident, "name": name, "arguments": arguments} if not problems else None), seen


def check_tool(run):
    payload = {
        "model": run.model,
        "messages": [{"role": "user", "content": QUESTION}],
        "tools": [WEATHER],
        "tool_choice": "auto",
        "max_tokens": run.max_tokens,
        "temperature": 0,
    }
    reply = run.client.send("tool", "POST", "/chat/completions", payload, key=run.key)
    entry = begin(run, reply)
    problems = entry["problems"]
    body = need_json(run, reply, entry)
    if body is None:
        return finish(entry)
    message = inspect_completion(run, body, entry)
    if message is not None:
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            if entry.get("finish_reason") == "length":
                problems.append("the output limit cut the reply before a tool call (raise --max-tokens)")
            else:
                problems.append("the model answered in text instead of calling the tool")
        else:
            entry["tool_calls"] = []
            for index, call in enumerate(calls):
                found, usable, seen = inspect_call(run, call, {WEATHER["function"]["name"]})
                problems.extend(found)
                if seen is not None:
                    entry["tool_calls"].append(seen)
                if index == 0 and usable is not None:
                    run.tool_call = usable
            if entry.get("finish_reason") != "tool_calls":
                problems.append(f"finish_reason is {entry.get('finish_reason')!r}, expected 'tool_calls'")
    return finish(entry)


def check_tool_result(run):
    call, history = run.tool_call, "model_call"
    if call is None:
        call = {"id": "call_smoke_1", "name": WEATHER["function"]["name"], "arguments": json.dumps({"city": "Seoul"})}
        history = "synthetic"
    payload = {
        "model": run.model,
        "messages": [
            {"role": "user", "content": QUESTION},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": call["id"], "type": "function",
                 "function": {"name": call["name"], "arguments": call["arguments"]}}]},
            {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(WEATHER_RESULT)},
        ],
        "tools": [WEATHER],
        "tool_choice": "auto",
        "max_tokens": run.max_tokens,
        "temperature": 0,
    }
    reply = run.client.send("tool_result", "POST", "/chat/completions", payload, key=run.key)
    entry = begin(run, reply)
    entry["history"] = history
    body = need_json(run, reply, entry)
    if body is not None:
        message = inspect_completion(run, body, entry)
        if message is not None:
            answer = message.get("content")
            if isinstance(answer, str) and answer.strip():
                entry["answer_preview"] = run.text(answer, 120)
                entry["mentions_result"] = any(token in answer.lower() for token in ("21", "clear"))
            else:
                entry["problems"].append("the final answer text is empty")
            if message.get("tool_calls"):
                entry["problems"].append("the model called a tool again instead of answering")
        expect_finish(entry, FINISHES)
    return finish(entry)


def check_auth(run):
    """Only the status code is judged: a gateway may answer 401 with a body that is not OpenAI-shaped."""
    payload = {"model": run.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    entry = {"ok": False, "problems": []}
    for label, key in (("no_key", None), ("wrong_key", "wrong-" + secrets.token_hex(12))):
        reply = run.client.send(f"auth-{label}", "POST", "/chat/completions", payload, key=key)
        seen = reply.headers.get("x-request-id")
        entry[label] = {
            "status": reply.status,
            "x_request_id": run.text(seen) if seen else None,
            "sent_request_id": reply.sent_request_id,
            "request_id_echoed": seen == reply.sent_request_id,
            "elapsed_s": rounded(reply.total_s),
        }
        if reply.error:
            entry[label]["error"] = reply.error
        if reply.status != 401:
            got = reply.status if reply.status is not None else reply.error
            entry["problems"].append(f"{label}: expected HTTP 401, got {got}")
    entry["status"] = entry["no_key"]["status"]
    entry["x_request_id"] = entry["no_key"]["x_request_id"]
    return finish(entry)


CHECK_FUNCTIONS = {
    "models": check_models,
    "chat": check_chat,
    "stream": check_stream,
    "tool": check_tool,
    "tool_result": check_tool_result,
    "auth": check_auth,
}


def run_checks(run, selected, progress):
    results, admissions, stop = {}, 0, None
    for name in selected:
        if stop is not None:
            results[name] = {"ok": False, "skipped": stop}
            continue
        entry = CHECK_FUNCTIONS[name](run)
        results[name] = entry
        if name in GPU_CHECKS:
            admissions += 1
        progress(name, entry)
        if name != "auth" and (entry.get("error") or entry.get("status") in STOP_STATUSES):
            stop = f"stopped after {name}: " + (entry.get("error") or f"HTTP {entry['status']}")
    return results, admissions


def main(argv=None, *, environ=None, stdin=None, stdout=None, stderr=None):
    environ = os.environ if environ is None else environ
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        args = build_parser(stderr).parse_args(argv)
        selected = pick_checks(args.only)
        if not 1 <= args.max_tokens <= 1024:
            raise Usage("--max-tokens must be between 1 and 1024")
        if not 1 <= args.timeout <= 3600:
            raise Usage("--timeout must be between 1 and 3600 seconds")
        if not MODEL_FORMAT.fullmatch(args.model):
            raise Usage("--model is not a valid model name")
        target = parse_target(args.base_url)
        key = read_key(args, environ, stdin) if selected != ["auth"] else None
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    except Usage as exc:
        stderr.write(f"model_api_smoke: error: {exc}\n")
        return 2

    run_id = secrets.token_hex(4)
    run = Run(Client(target, args.auth_header, args.timeout, run_id), args.model, key, args.max_tokens)
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    began = time.perf_counter()

    def progress(name, entry):
        verdict = "ok" if entry["ok"] else "FAILED: " + "; ".join(entry["problems"][:3])
        stderr.write(f"{name}: {verdict} ({entry.get('elapsed_s', '-')}s)\n")

    results, admissions = run_checks(run, selected, progress)
    summary = {
        "tool": "model_api_smoke",
        "version": VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "target": target.label,
        "auth_header": args.auth_header,
        "model": args.model,
        "max_tokens": args.max_tokens,
        "admissions": admissions,
        "ok": all(entry["ok"] for entry in results.values()),
        "total_s": rounded(time.perf_counter() - began),
        "checks": results,
    }
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if key:
        text = text.replace(key, "***")
    stdout.write(text + "\n")
    code = 0 if summary["ok"] else 1
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text + "\n")
        except OSError:
            stderr.write("model_api_smoke: error: the --out file cannot be written\n")
            return code or 2
    return code


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
