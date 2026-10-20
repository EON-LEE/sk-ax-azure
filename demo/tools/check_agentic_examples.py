#!/usr/bin/env python3
"""Run the demo chat's agent examples against a running demo web app and record what every run did.

    check_agentic_examples.py --list
    check_agentic_examples.py --write-fixtures DIR
    AXK2_DEMO_URL=https://HOST check_agentic_examples.py [--only ID[,ID]] [--runs fp8=2,nvfp4=1] [--gap 10]
                                                         [--dns doh|system] [--out RESULT.json] [--table RESULT.md]
                                                         [--go]

Without --go nothing is sent: the plan (examples, runs, planned model calls) is printed. With --go every run is one
chat turn made the way the web page makes it: create a workspace, upload the example's fixture if it has one,
POST /api/agent (tools on, thinking off), read the event stream, download the files the turn wrote, open the HTML
ones through the preview route and close the workspace. Runs are sequential, at least 10 s apart; the NVFP4 profile
is the same app under /models/nvfp4.

The demo must be in open mode (this tool does not log in). The site root comes from --base-url or AXK2_DEMO_URL; it
is never printed. The summary holds only what the turn did: tools and errors, sources, files, timing, tokens.

A run passes when the turn completed with an answer, the tools the example needs ran in order without a tool error,
the first tool is the forced one, links in the answer come from tool results, the files exist (HTML opens in the
preview), the total time is within --max-seconds and no HTTP 5xx came back. Content checks (expected numbers,
rounded calculator result, files rewritten) are reported as flags and do not fail a run. Model-written code is
parsed, never run.

Name lookup: the resolver of a PC can fail for seconds at a time. With --dns doh (the default) an https host name is
looked up once through Google's DNS-over-HTTPS service, and every request of the run goes to that address; the TLS
server name, the certificate check and the Host header stay those of the real name. The address is not printed or
saved. If the lookup fails the system resolver is used. --dns system skips the lookup. The system DNS settings and
the hosts file are never touched.

Connection errors: a request that gets no HTTP answer (name lookup, connect, TLS, a stream that breaks, a download
that breaks) makes the run a CONNECTION ERROR. It says nothing about the example, so it is recorded apart from
failures and the example is run once more after the gap; a candidate gets one such retry in all, and the first
attempt stays in the summary marked "superseded". A run that only went over its deadline is a failure, not a
connection error. Three attempts in a row with a connection error stop the plan. /api/me is asked again twice when
the server cannot be reached.

Exit code: 0 every run passed, 1 a run failed or was skipped, 2 usage error.
"""

from __future__ import annotations

import argparse
import ast
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from fractions import Fraction
from urllib.parse import quote, unquote, urlsplit

from agentic_examples import EXAMPLES, FIXTURES

VERSION = 1
PROFILES = {"fp8": "", "nvfp4": "/models/nvfp4"}
LABELS = {"fp8": "FP8", "nvfp4": "NVFP4"}
DEFAULT_RUNS = "fp8=2,nvfp4=1"
MIN_GAP = 10.0
STOP_STATUSES = (401, 403, 429)
MAX_5XX_STREAK = 3
MAX_LINK_STREAK = 3
PREFLIGHT_RETRIES = 2
CONNECT_TIMEOUT = 20.0
STALLS = ("deadline", "oversized line")
DOH_NAME = "dns.google"
DOH_ADDRESSES = ("8.8.8.8", "8.8.4.4")
DOH_TRIES, DOH_PAUSE, DOH_TIMEOUT, MAX_DNS_BODY = 3, 3.0, 10.0, 65536
LOOPBACK = ("localhost", "127.0.0.1", "::1")
CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
NUMBER = re.compile(r"\d{1,30}(?:\.\d{1,12})?")
URL = re.compile(r"https?://[^\s<>\"'`)\]}\\]+")
HANGUL_TAIL = re.compile(r"[\u3131-\u318e\uac00-\ud7a3]+$")
PREVIEW_TAG = re.compile(r"<(?:h[1-4]|table|p|ul|ol|div|section|article)\b", re.I)
TEXT_FILES = (".md", ".html", ".csv", ".txt", ".json")
MAX_LINE = 2_000_000
MAX_BODY = 2_000_000
MAX_FILES = 12


class Usage(Exception):
    """A command-line problem; the message never repeats a user-supplied value."""


class Stalled(Exception):
    """The turn went past its deadline."""


def tidy(value, limit=128):
    return CONTROL.sub(" ", "" if value is None else str(value)).strip()[:limit]


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def result_text(result):
    """A tool result as searchable text; diff_file returns a plain string whose line breaks must stay real."""
    return result if isinstance(result, str) else dumps(result)


def plain(text):
    """The text without thousands separators, so that 1,188,000 and 1188000 compare equal."""
    return THOUSANDS.sub("", text)


def numbers(text):
    return {Fraction(token) for token in NUMBER.findall(plain(text))}


def cell(value):
    return "-" if value is None or value == "" else str(value)


def strings(value):
    """Every string inside a JSON-like value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings(item)


# ------------------------------------------------------------------------------------- command line
class Parser(argparse.ArgumentParser):
    sink = None

    def error(self, message):
        # argparse repeats the offending values; they are not echoed
        named = re.match(r"(argument [^:]+):", message)
        detail = f"{named.group(1)} is invalid" if named else "unrecognized or malformed arguments"
        (self.sink or sys.stderr).write(f"{self.prog}: error: {detail}\n")
        raise SystemExit(2)


def build_parser(sink):
    parser = Parser(prog="check_agentic_examples", allow_abbrev=False,
                    description="Run the demo chat's agent examples against a running demo web app.",
                    epilog="Without --go nothing is sent. The site root is never printed.")
    parser.sink = sink
    parser.add_argument("--base-url", help="site root of the demo web app, e.g. https://HOST (or AXK2_DEMO_URL)")
    parser.add_argument("--only", action="append", help="comma-separated example ids (default: all; see --list)")
    parser.add_argument("--runs", default=DEFAULT_RUNS, help="runs per profile (default: %(default)s)")
    parser.add_argument("--gap", type=float, default=MIN_GAP, help="seconds between runs, at least 10 (default 10)")
    parser.add_argument("--dns", choices=("doh", "system"), default="doh",
                        help="doh: look the https host up once through DNS-over-HTTPS and use that address; "
                             "system: use the system resolver (default: %(default)s)")
    parser.add_argument("--max-seconds", type=float, default=45.0,
                        help="a turn passes when its total time is within this many seconds (default 45)")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="cancel a turn that is still running after this many seconds (default 120)")
    parser.add_argument("--out", help="also write the JSON summary to this file")
    parser.add_argument("--table", help="also write the result table (markdown) to this file")
    parser.add_argument("--go", action="store_true", help="send the requests (default: print the plan only)")
    parser.add_argument("--list", action="store_true", help="list the examples and exit")
    parser.add_argument("--write-fixtures", metavar="DIR",
                        help="write the synthetic contract PDF and script the examples attach, then exit")
    return parser


def parse_runs(text):
    runs = {}
    for part in text.split(","):
        name, sep, count = part.strip().partition("=")
        if name not in PROFILES or name in runs or not sep or not count.isdigit() or int(count) > 5:
            raise Usage("--runs must look like fp8=2,nvfp4=1 (profiles fp8 and nvfp4, 0 to 5 runs each)")
        runs[name] = int(count)
    if not sum(runs.values()):
        raise Usage("--runs selects no run")
    return {name: runs[name] for name in PROFILES if name in runs}


def pick_examples(values):
    if not values:
        return list(EXAMPLES)
    known = {example.id: example for example in EXAMPLES}
    chosen = []
    for value in values:
        for name in value.split(","):
            name = name.strip()
            if name not in known:
                raise Usage("--only names an unknown example (see --list)")
            if known[name] not in chosen:
                chosen.append(known[name])
    return chosen


class Target:
    def __init__(self, scheme, host, port, address=None):
        self.scheme, self.host, self.port, self.address = scheme, host, port, address


def parse_target(url):
    try:
        parts = urlsplit((url or "").strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise Usage("the base URL is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not host:
        raise Usage("the base URL must be an http(s) URL with a host (--base-url or AXK2_DEMO_URL)")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment \
            or parts.path.strip("/"):
        raise Usage("the base URL must be the site root: no credentials, path, query or fragment")
    if parts.scheme == "http" and host.lower() not in LOOPBACK:
        raise Usage("plain http is only allowed for loopback hosts; use https")
    return Target(parts.scheme, host, port or (443 if parts.scheme == "https" else 80))


# ------------------------------------------------------------------------------------------ transport
def json_object(body):
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


class Reply:
    def __init__(self):
        self.status, self.body, self.error = None, b"", None

    def json(self):
        return json_object(self.body) or {}


def events(response, deadline):
    """The (event name, data) pairs of a server-sent-events body."""
    name, data = None, []
    while True:
        raw = response.readline(MAX_LINE + 1)
        if not raw:
            break
        if len(raw) > MAX_LINE:
            raise Stalled("oversized line")
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                yield name or "message", "\n".join(data)
            name, data = None, []
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[6:] if line[5:6] == " " else line[5:])
        if time.monotonic() > deadline:
            raise Stalled("deadline")
    if data:
        yield name or "message", "\n".join(data)


class PinnedConnection(http.client.HTTPSConnection):
    """An HTTPS connection to a fixed address. Only the name lookup is replaced: the TLS server name, the certificate
    check and the Host header stay those of the host name."""

    def __init__(self, host, port, address, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self.address, self.tls = address, context

    def connect(self):
        raw = socket.create_connection((self.address, self.port), min(CONNECT_TIMEOUT, self.timeout))
        self.sock = raw  # close() reaches the raw socket when the handshake fails
        try:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        raw.settimeout(self.timeout)
        self.sock = self.tls.wrap_socket(raw, server_hostname=self.host)


class Server:
    def __init__(self, target, timeout):
        self.target, self.timeout, self.statuses = target, timeout, []
        self.headers = {"User-Agent": "axk2-agentic-examples-check/1"}

    def connect(self):
        target = self.target
        if target.scheme == "https":
            context = ssl.create_default_context()
            if target.address:
                return PinnedConnection(target.host, target.port, target.address, self.timeout, context)
            return http.client.HTTPSConnection(target.host, target.port, timeout=self.timeout, context=context)
        return http.client.HTTPConnection(target.host, target.port, timeout=self.timeout)

    def request(self, method, path, body=None, headers=None):
        reply, connection, response = Reply(), self.connect(), None
        try:
            connection.request(method, path, body=body, headers={**self.headers, **(headers or {})})
            response = connection.getresponse()
            reply.status = response.status
            reply.body = response.read(MAX_BODY)
        except (OSError, http.client.HTTPException) as exc:
            reply.error = type(exc).__name__
        finally:
            if response is not None:  # the connection has handed its socket to the response
                response.close()
            connection.close()
        self.statuses.append(reply.status)
        return reply

    def stream(self, path, payload, token, deadline, turn):
        """POST the agent request and give every event of the answer to the turn; returns (status, error name)."""
        connection, response, status, error = self.connect(), None, None, None
        try:
            connection.request("POST", path, body=dumps(payload).encode("utf-8"), headers={
                **self.headers, "Content-Type": "application/json", "Accept": "text/event-stream",
                "X-AX-Workspace": token})
            response = connection.getresponse()
            status = response.status
            if status == 200:
                for name, data in events(response, deadline):
                    turn.feed(name, data)
            else:
                turn.refused = json_object(response.read(MAX_BODY)) or {}
        except Stalled as exc:
            error = str(exc)
        except (OSError, http.client.HTTPException) as exc:
            error = type(exc).__name__
        finally:
            if response is not None:
                response.close()
            connection.close()
        self.statuses.append(status)
        return status, error


# ------------------------------------------------------------------------------------------ name lookup
def public_ipv4(text):
    try:
        address = ipaddress.IPv4Address(text) if isinstance(text, str) else None
    except ValueError:
        return None
    # is_global alone lets multicast addresses through
    return str(address) if address is not None and address.is_global and not address.is_multicast else None


def doh_query(connection, host):
    """The first public IPv4 address that the DNS-over-HTTPS service answers for the host, or None."""
    response = None
    try:
        connection.request("GET", "/resolve?name=" + quote(host, safe="") + "&type=A",
                           headers={"Accept": "application/dns-json", "User-Agent": "axk2-agentic-examples-check/1"})
        response = connection.getresponse()
        body = response.read(MAX_DNS_BODY) if response.status == 200 else b""
    except (OSError, http.client.HTTPException):
        return None
    finally:
        if response is not None:
            response.close()
        connection.close()
    answers = (json_object(body) or {}).get("Answer")
    for answer in answers if isinstance(answers, list) else ():
        if isinstance(answer, dict) and answer.get("type") == 1:  # type 1 is an A record; CNAME links are skipped
            address = public_ipv4(answer.get("data"))
            if address:
                return address
    return None


def doh_connections():
    """The ways to reach the service: straight to its well-known addresses first, then by name."""
    context = ssl.create_default_context()
    for address in DOH_ADDRESSES:
        yield PinnedConnection(DOH_NAME, 443, address, DOH_TIMEOUT, context)
    yield http.client.HTTPSConnection(DOH_NAME, 443, timeout=DOH_TIMEOUT, context=context)


def doh_address(host, sleep, connections=doh_connections):
    """One public IPv4 address of the host from the DNS-over-HTTPS service; raises LookupError after a few tries."""
    for attempt in range(DOH_TRIES):
        if attempt:
            sleep(DOH_PAUSE)
        for connection in connections():
            address = doh_query(connection, host)
            if address:
                return address
    raise LookupError("no DNS-over-HTTPS answer")


def pinnable(target):
    if target.scheme != "https" or target.host.lower() in LOOPBACK:
        return False
    try:
        ipaddress.ip_address(target.host)
    except ValueError:
        return True
    return False


def pin(target, mode, resolve):
    """Make the target go to one address, asked once. Returns what the summary says; it never holds the address."""
    if mode == "system":
        return {"mode": mode, "pinned": False}
    if not pinnable(target):
        return {"mode": mode, "pinned": False, "note": "no lookup needed: plain http, loopback or an IP address"}
    try:
        target.address = resolve(target.host)
    except (LookupError, OSError):  # OSError: the lookup could not start, e.g. no CA file
        return {"mode": mode, "pinned": False, "note": "the DNS-over-HTTPS lookup failed; the system resolver is used"}
    return {"mode": mode, "pinned": True}


class Turn:
    """What the event stream of one chat turn said."""

    def __init__(self):
        self.round, self.texts, self.actions = 0, {}, {}
        self.policy = self.metrics = self.end = self.error = self.refused = None
        self.notices, self.reasoning = [], False

    def feed(self, name, data):
        try:
            payload = json.loads(data)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        if name == "round":
            self.round = payload["index"] if isinstance(payload.get("index"), int) else self.round + 1
        elif name == "action":
            self.actions.setdefault(payload.get("id") or len(self.actions), {}).update(payload)
        elif name == "notice":
            self.notices.append(tidy(payload.get("code"), 40))
        elif name in ("policy", "metrics", "end", "error"):
            setattr(self, name, payload)
        elif name == "message":
            delta = ((payload.get("choices") or [{}])[0] or {}).get("delta") or {}
            if isinstance(delta.get("content"), str):
                self.texts[self.round] = self.texts.get(self.round, "") + delta["content"]
            if delta.get("reasoning_content") or delta.get("reasoning"):
                self.reasoning = True

    def ok(self, name=None):
        return [a for a in self.actions.values() if a.get("state") == "success" and name in (None, a.get("name"))]

    def answer(self):
        """The text of the last round: the answer after the tools. Words said before a tool call do not count."""
        return self.texts.get(self.round, "").strip()


# --------------------------------------------------------------------------------------------- one run
def refusal(reply):
    if reply.status is None:
        return "transport " + str(reply.error)
    code = reply.json().get("error")
    return f"http {reply.status}" + (f" {tidy(code, 40)}" if code else "")


def lost(broke, step, reply):
    """Note a request that got no HTTP answer at all: name lookup, connect, TLS or a broken connection."""
    if reply.status is None:
        broke.append((step, reply.error))


def collect_files(server, prefix, auth, turn, broke):
    """Download what write_file reported and open the HTML files in the preview, as the page does. Once a request
    has lost its connection the other files are not asked for."""
    paths = []
    for action in turn.ok("write_file"):
        path = (action.get("result") or {}).get("file") if isinstance(action.get("result"), dict) else None
        if isinstance(path, str) and path not in paths:
            paths.append(path)
    files, texts = [], {}
    for path in paths[:MAX_FILES]:
        query = "?path=" + quote(path, safe="")
        entry = {"name": path.rsplit("/", 1)[-1], "download": None, "bytes": None}
        files.append(entry)
        if broke:
            continue
        got = server.request("GET", prefix + "/api/workspace/download" + query, headers=auth)
        entry["download"] = got.status
        lost(broke, "download", got)
        if got.status == 200:
            entry["bytes"] = len(got.body)
            texts[entry["name"]] = got.body.decode("utf-8", "replace")
            if path.lower().endswith(".html"):
                shown = server.request("GET", prefix + "/api/workspace/preview" + query, headers=auth)
                lost(broke, "preview", shown)
                entry["preview"] = None if shown.status is None else (
                    shown.status == 200 and bool(PREVIEW_TAG.search(shown.body.decode("utf-8", "replace"))))
    return files, texts


def run_one(server, prefix, example, profile, number, limit, timeout, attempt=1):
    rec = {"id": example.id, "profile": profile, "run": number, "attempt": attempt, "ok": False, "state": "",
           "http": {}}
    mark, turn, files, texts, token, broke = len(server.statuses), Turn(), [], {}, None, []
    began = time.monotonic()
    try:
        reply = server.request("POST", prefix + "/api/workspace")
        rec["http"]["workspace"] = reply.status
        lost(broke, "workspace", reply)
        token = reply.json().get("token") if reply.status == 200 else None
        if not isinstance(token, str):
            token, rec["state"] = None, refusal(reply)
        else:
            auth, attachments = {"X-AX-Workspace": token}, []
            if example.upload:
                reply = server.request("POST", prefix + "/api/workspace/upload?name=" + quote(example.upload, safe=""),
                                       body=FIXTURES[example.upload](),
                                       headers={**auth, "Content-Type": "application/octet-stream"})
                rec["http"]["upload"] = reply.status
                lost(broke, "upload", reply)
                names = reply.json().get("files")
                attachments = [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
                if reply.status != 200 or not attachments:
                    rec["state"] = "upload " + refusal(reply)
            if not rec["state"]:
                payload = {"messages": [{"role": "user", "content": example.prompt}], "thinking": False, "tools": True}
                if attachments:
                    payload["attachments"] = attachments
                started = time.monotonic()
                status, error = server.stream(prefix + "/api/agent", payload, token, started + timeout, turn)
                rec["http"]["agent"], rec["wall_seconds"] = status, round(time.monotonic() - started, 3)
                if error:
                    server.request("POST", prefix + "/api/workspace/cancel", headers=auth)
                rec["state"] = outcome(turn, status, error)
                if error and error not in STALLS and not turn.error and status in (None, 200):
                    broke.append(("agent", error))
                files, texts = collect_files(server, prefix, auth, turn, broke)
    finally:
        if token:
            rec["http"]["close"] = server.request("POST", prefix + "/api/workspace/close",
                                                  headers={"X-AX-Workspace": token}).status
    rec["http_5xx"] = sum(1 for status in server.statuses[mark:] if status is not None and status >= 500)
    rec["stop"] = next((status for status in server.statuses[mark:] if status in STOP_STATUSES), None)
    describe(rec, turn, files)
    rec["failed"], rec["flags"] = evaluate(example, rec, turn, turn.answer(), files, texts, limit)
    rec["connection_error"] = None
    if broke:
        link = rec["connection_error"] = {"error": tidy(broke[0][1], 60), "step": broke[0][0]}
        rec["failed"] = {"connection": f"{link['error']} at {link['step']}", **rec["failed"]}
    rec["ok"] = not rec["failed"]
    rec["elapsed_seconds"] = round(time.monotonic() - began, 3)
    return rec


def outcome(turn, status, error):
    """How the turn ended, in a few words."""
    if status is None:
        return "transport " + tidy(error, 40)
    if status != 200:
        code = (turn.refused or {}).get("error")
        return f"http {status}" + (f" {tidy(code, 40)}" if code else "")
    if turn.error:
        return "error " + tidy(turn.error.get("code"), 40)
    if error:
        return "deadline" if error == "deadline" else "transport " + tidy(error, 40)
    if (turn.end or {}).get("state") == "completed" and turn.metrics:
        return "completed"
    return "ended " + tidy((turn.end or {}).get("state") or "without end", 40)


def describe(rec, turn, files):
    actions = list(turn.actions.values())
    metrics = turn.metrics or {}
    rec["tools"] = [{"name": a.get("name"), "state": a.get("state"), "ms": a.get("milliseconds"),
                     **({"error": tidy(failure_text(a), 100)} if a.get("state") in ("error", "cancelled") else {})}
                    for a in actions]
    rec["first_tool"] = actions[0].get("name") if actions else None
    rec["web_iq_calls"] = sum(1 for a in actions if str(a.get("name")).startswith("web_iq_"))
    rec["files"] = files
    for key in ("ttft_seconds", "total_seconds", "input_tokens", "output_tokens", "model_calls", "rounds"):
        rec[key] = metrics.get(key)
    rec["notices"] = turn.notices
    if turn.error:
        rec["error"] = {"code": tidy(turn.error.get("code"), 40), "message": tidy(turn.error.get("message"), 200)}
    rec["answer"] = tidy(turn.answer(), 1200)


def failure_text(action):
    result = action.get("result")
    if isinstance(result, dict):
        return result.get("error") or result.get("message") or result.get("status") or dumps(result)
    return result or action.get("state")


# ------------------------------------------------------------------------------------------- checking
def norm(url):
    """A link as host + path, so that the scheme, query, fragment, case of the host and a final / do not matter."""
    try:
        parts = urlsplit(url.rstrip(".,;:!?*"))
    except ValueError:
        return ""
    return (parts.hostname or "").lower() + unquote(parts.path).rstrip("/")


def links_in(text):
    return {link for link in map(norm, URL.findall(text)) if link}


def known_links(turn):
    """The links the tools returned, plus the pages the model asked web_iq_browse to read."""
    links = set()
    for action in turn.ok():
        for text in strings(action.get("result")):
            links |= links_in(text)
        arguments = action.get("arguments")
        if action.get("name") == "web_iq_browse" and isinstance(arguments, dict) \
                and isinstance(arguments.get("url"), str):
            links |= links_in(arguments["url"])
    return links


def cited_links(answer, texts):
    found = links_in(answer)
    for name, text in texts.items():
        if name.lower().endswith(TEXT_FILES):
            found |= links_in(text)
    return found


def came_from_tools(link, known):
    """The cited link as a tool knows it, or "" when no tool returned it. A link is real when a tool returned it or
    returned a page below it (a shortened link, cut at a path segment; a bare host never counts). A Korean particle
    written right after the link (URL에서, **URL**에서) is not part of it, so the same page cited with and without
    a particle is one source."""
    for candidate in (link, HANGUL_TAIL.sub("", link).rstrip(".,;:!?*")):
        if candidate in known or ("/" in candidate and any(item.startswith(candidate + "/") for item in known)):
            return candidate
    return ""


def tool_problems(example, turn):
    ok = turn.ok()
    names = [a.get("name") for a in ok]
    problems, position, missing = [], 0, []
    for tool in example.tools:
        if tool in names[position:]:
            position = names.index(tool, position) + 1
        else:
            missing.append(tool)
    if missing:
        problems.append("not run (in order): " + ", ".join(missing))
    for group in example.any_of:
        if not any(name in names for name in group):
            problems.append("none of " + "/".join(group))
    for tool, pattern in example.args:
        if not any(a.get("name") == tool and re.search(pattern, dumps(a.get("arguments"))) for a in ok):
            problems.append(f"{tool} was not called with /{pattern}/")
    return problems


def artifact_problems(example, turn):
    problems = []
    for kind in example.artifacts:
        found = [a["result"]["artifact"] for a in turn.ok()
                 if isinstance(a.get("result"), dict) and isinstance(a["result"].get("artifact"), dict)
                 and a["result"]["artifact"].get("kind") == kind]
        if not found:
            problems.append(f"no {kind} artifact")
        elif kind == "chart" and not any(str(item.get("svg", "")).lstrip().startswith("<svg") for item in found):
            problems.append("the chart has no svg")
    return problems


def file_problems(example, files):
    by_name = {f["name"]: f for f in files}
    problems = []
    for name in example.files:
        entry = by_name.get(name)
        if entry is not None and entry["download"] is None:
            problems.append(f"{name} could not be downloaded (connection error)")
        elif entry is None or entry["download"] != 200:
            problems.append(f"{name} was not written")
    if example.preview:
        entry = by_name.get(example.preview, {})
        if "preview" in entry and entry["preview"] is None:
            problems.append(f"{example.preview} could not be opened in the preview (connection error)")
        elif not entry.get("preview") and entry.get("download", 0) is not None:  # None: the download message says why
            problems.append(f"{example.preview} does not open in the preview")
    return problems


def source_problems(example, turn, answer, texts):
    cited, known = cited_links(answer, texts), known_links(turn)
    matched = {link: came_from_tools(link, known) for link in cited}
    real = {link for link in matched.values() if link}
    problems = []
    if len(real) < example.sources:
        problems.append(f"{len(real)} of {example.sources} source links come from tool results")
    invented = sorted(link for link, link_real in matched.items() if not link_real)
    if invented:
        problems.append("links that no tool returned: " + ", ".join(invented[:3]))
    if example.hosts and not any(h == want or h.endswith("." + want)
                                 for h in (link.partition("/")[0] for link in real) for want in example.hosts):
        problems.append("no source on " + "/".join(example.hosts))
    return problems, sorted({link.partition("/")[0] for link in real})


def evaluate(example, rec, turn, answer, files, texts, limit):
    """(failed, flags): what broke the pass criteria and what looks wrong in the content."""
    failed, flags = {}, {}
    actions = list(turn.actions.values())
    done = rec["state"] == "completed"
    if not done:
        failed["completed"] = rec["state"]
    elif not answer:
        failed["answer"] = "the turn ended without answer text"
    broken = [a for a in actions if a.get("state") in ("error", "cancelled")]
    if broken:
        failed["tool_errors"] = f"{len(broken)}: " + ", ".join(
            f"{a.get('name')} ({tidy(failure_text(a), 60)})" for a in broken[:3])
    if rec["http_5xx"]:
        failed["http_5xx"] = str(rec["http_5xx"])
    if not done:
        return failed, flags
    if example.forced and (actions[0].get("name") if actions else None) != example.forced:
        failed["first_tool"] = f"expected {example.forced}, got {rec['first_tool']}"
    for key, problems in (("tools", tool_problems(example, turn)), ("artifacts", artifact_problems(example, turn)),
                          ("files", file_problems(example, files))):
        if problems:
            failed[key] = "; ".join(problems)
    if example.external or example.sources:
        problems, hosts = source_problems(example, turn, answer, texts)
        rec["source_hosts"] = hosts
        if problems:
            failed["sources"] = "; ".join(problems)
    seconds = rec.get("total_seconds") or rec.get("wall_seconds")
    if seconds is None or seconds > limit:
        failed["time"] = f"{seconds} s is over {limit:g} s"
    flags.update(content_flags(example, turn, answer, texts))
    calls = rec.get("model_calls")
    if isinstance(calls, int) and not isinstance(calls, bool) and calls > example.calls:
        flags["calls"] = f"{calls} model calls, {example.calls} planned"
    if turn.reasoning:
        flags["reasoning"] = "reasoning text arrived although thinking was off"
    if turn.notices:
        flags["notice"] = ", ".join(turn.notices)
    return failed, flags


def content_flags(example, turn, answer, texts):
    flags = {}
    for source, pattern in example.contains:
        text = answer if source == "answer" else texts.get(source)
        if text is None:
            flags["contains " + source] = "file was not written"
        elif not re.search(pattern, plain(text)):
            flags["contains " + source] = f"no match for /{pattern}/"
    for tool, pattern in example.results:
        if not any(a.get("name") == tool and re.search(pattern, plain(result_text(a.get("result"))))
                   for a in turn.ok()):
            flags["result " + tool] = f"no match for /{pattern}/"
    if example.inputs:
        stray = stray_inputs(example, turn)
        if stray:
            flags["inputs"] = "calculator numbers not from the prompt or a tool result: " + ", ".join(stray[:4])
    if example.shown and not calculation_shown(turn, [answer, *texts.values()]):
        flags["shown"] = "the last calculator result is not in the answer or the files"
    if example.changes:
        before = FIXTURES[example.upload]().decode("utf-8", "replace")
        if texts.get(example.changes, before) == before:
            flags["changes"] = f"{example.changes} was not rewritten"
    for name, text in texts.items():
        if name.lower().endswith(".py"):
            try:
                ast.parse(text)
            except (SyntaxError, ValueError) as exc:
                flags["python " + name] = type(exc).__name__
    return flags


def stray_inputs(example, turn):
    known = numbers(example.prompt) | {Fraction(1), Fraction(100)}
    known |= {n / 100 for n in known}
    stray = []
    for action in turn.ok():
        if action.get("name") == "calculator":
            expression = str((action.get("arguments") or {}).get("expression", ""))
            stray += [t for t in NUMBER.findall(plain(expression)) if Fraction(t) not in known]
        found = numbers(dumps(action.get("result")))
        known |= found | {n / 100 for n in found}
    return stray


def calculation_shown(turn, texts):
    for action in reversed(turn.ok("calculator")):
        try:
            value = Fraction(str((action.get("result") or {}).get("result")))
        except (AttributeError, ValueError, ZeroDivisionError):
            return True
        targets = (value, value * 100)
        return any(abs(found - target) <= abs(target) / 100 for text in texts for found in numbers(text)
                   for target in targets)
    return True


# -------------------------------------------------------------------------------------------- reports
def render_table(runs):
    head = ("example", "model", "run", "result", "total s", "ttft s", "tokens in/out", "calls", "tools", "web iq",
            "files", "hosts", "failed", "flags")
    rows = [head]
    for rec in runs:
        who = (rec["id"], LABELS[rec["profile"]], str(rec["run"]) + (" (retry)" if rec.get("attempt", 1) > 1 else ""))
        if rec.get("skipped"):
            rows.append((*who, "skipped", "", "", "", "", "", "", "", "", rec["skipped"], ""))
            continue
        verdict = "CONNECTION ERROR" if rec.get("connection_error") else "PASS" if rec["ok"] else "FAIL"
        failed = [f"{k}: {v}" for k, v in rec["failed"].items()] + (["retried"] if rec.get("superseded") else [])
        rows.append((*who, verdict,
                     cell(rec.get("total_seconds") or rec.get("wall_seconds")), cell(rec.get("ttft_seconds")),
                     f"{cell(rec.get('input_tokens'))}/{cell(rec.get('output_tokens'))}", cell(rec.get("model_calls")),
                     cell(">".join(t["name"] or "?" for t in rec["tools"])), str(rec["web_iq_calls"]),
                     cell(",".join(f["name"] for f in rec["files"])), cell(",".join(rec.get("source_hosts") or [])),
                     "; ".join(failed), "; ".join(f"{k}: {v}" for k, v in rec["flags"].items())))
    lines = ["| " + " | ".join(item.replace("|", "/") for item in row) + " |" for row in rows]
    lines.insert(1, "|" + " --- |" * len(head))
    return "\n".join(lines) + "\n"


def plan_of(examples, runs):
    return [(example, profile, number) for example in examples for profile, count in runs.items()
            for number in range(1, count + 1)]


def write_text(path, text, stderr, name):
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError:
        stderr.write(f"check_agentic_examples: error: the {name} file cannot be written\n")
        return False
    return True


def preflight(server, prefix, sleep=time.sleep, pause=MIN_GAP):
    """(reason the profile cannot be used or None, Web IQ state). A request that gets no HTTP answer is made again."""
    for attempt in range(PREFLIGHT_RETRIES + 1):
        if attempt:
            sleep(pause)
        me = server.request("GET", prefix + "/api/me")
        if me.status is not None:
            break
    if me.status != 200:
        return "api/me " + refusal(me), "unknown"
    if me.json().get("open") is not True:
        return "the demo is not in open mode and this tool does not log in", "unknown"
    caps = server.request("GET", prefix + "/api/capabilities")
    state = (caps.json().get("web_iq") or {}).get("state") if caps.status == 200 else None
    return None, state if state in ("ready", "unavailable") else "unknown"


def progress_line(position, total, rec, stderr):
    label = (f"[{position}/{total}] {LABELS[rec['profile']]} {rec['id']} #{rec['run']}"
             + (" (retry)" if rec.get("attempt", 1) > 1 else ""))
    if rec.get("skipped"):
        stderr.write(f"{label}: skipped ({rec['skipped']})\n")
        return
    link = rec.get("connection_error")
    if link:
        verdict = f"CONNECTION ERROR {link['error']} at {link['step']}" + (", retrying once" if rec.get("superseded")
                                                                          else "")
    else:
        verdict = "PASS" if rec["ok"] else "FAIL " + ",".join(rec["failed"])
    seconds = rec.get("total_seconds") or rec.get("wall_seconds") or rec.get("elapsed_seconds")
    stderr.write(f"{label}: {verdict} {cell(seconds)}s calls {cell(rec.get('model_calls'))} "
                 f"tools {'>'.join(t['name'] or '?' for t in rec['tools']) or '-'}\n")


def skipped(example, profile, number, reason):
    return {"id": example.id, "profile": profile, "run": number, "ok": False, "skipped": reason}


class Course:
    """What the finished attempts say about going on: the stop reason, the two streaks, the candidates run again."""

    def __init__(self):
        self.stop, self.errors, self.broken, self.retried = None, 0, 0, set()

    def judge(self, rec):
        """Take one attempt into account. Returns why its profile can no longer be used, or None."""
        self.errors = self.errors + 1 if rec["http_5xx"] else 0
        self.broken = self.broken + 1 if rec["connection_error"] else 0
        if rec["stop"]:
            self.stop = f"stopped after HTTP {rec['stop']}"
        elif self.errors >= MAX_5XX_STREAK:
            self.stop = f"stopped after {MAX_5XX_STREAK} runs in a row with HTTP 5xx"
        elif self.broken >= MAX_LINK_STREAK:
            self.stop = f"stopped after {MAX_LINK_STREAK} attempts in a row with connection errors"
        elif 503 in (rec["http"].get("workspace"), rec["http"].get("agent")):
            return "the profile answered HTTP 503"
        return None

    def again(self, rec):
        """Whether an attempt that lost its connection is made once more. A candidate has one such retry in all."""
        if not rec["connection_error"] or self.stop or rec["id"] in self.retried:
            return False
        self.retried.add(rec["id"])
        return True


def main(argv=None, *, environ=None, stdout=None, stderr=None, sleep=time.sleep, resolve=None):
    environ = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        args = build_parser(stderr).parse_args(argv)
        if args.write_fixtures is not None:
            os.makedirs(args.write_fixtures, exist_ok=True)
            for name, build in FIXTURES.items():
                with open(os.path.join(args.write_fixtures, name), "wb") as handle:
                    handle.write(build())
            stdout.write("\n".join(FIXTURES) + "\n")
            return 0
        if args.list:
            stdout.write("".join(f"{e.id}\t{e.group}\t{e.label}\n" for e in EXAMPLES))
            return 0
        examples, runs = pick_examples(args.only), parse_runs(args.runs)
        if not MIN_GAP <= args.gap <= 3600:
            raise Usage("--gap must be between 10 and 3600 seconds")
        if not 1 <= args.max_seconds <= 900 or not 1 <= args.timeout <= 3600:
            raise Usage("--max-seconds must be 1 to 900 and --timeout 1 to 3600")
        url = (args.base_url or environ.get("AXK2_DEMO_URL", "")).strip()
        target = parse_target(url) if args.go or url else None
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    except (Usage, OSError) as exc:
        stderr.write(f"check_agentic_examples: error: {exc if isinstance(exc, Usage) else 'files cannot be written'}\n")
        return 2

    plan = plan_of(examples, runs)
    summary = {"tool": "check_agentic_examples", "version": VERSION, "go": args.go, "gap_seconds": args.gap,
               "max_seconds": args.max_seconds, "planned_runs": len(plan),
               "planned_model_calls": sum(example.calls for example, _, _ in plan)}
    if not args.go:
        summary["plan"] = [{"id": e.id, "profile": p, "run": n, "calls": e.calls, "external": e.external}
                           for e, p, n in plan]
        stdout.write(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        stderr.write("nothing was sent: add --go to run this plan\n")
        return 0

    server, results, states, course, began, finished = Server(target, args.timeout), [], {}, Course(), False, 0
    summary.update(started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), scheme=target.scheme,
                   dns={"mode": args.dns, "pinned": False})
    resolver = resolve or (lambda host: doh_address(host, sleep))
    try:
        summary["dns"] = pin(target, args.dns, resolver)
        if args.dns == "doh":
            stderr.write("dns: " + ("looked up once through DNS-over-HTTPS; the address is not shown"
                                    if summary["dns"]["pinned"] else summary["dns"]["note"]) + "\n")
        for position, (example, profile, number) in enumerate(plan, 1):
            if course.stop:
                rec = skipped(example, profile, number, course.stop)
            else:
                if profile not in states:
                    states[profile] = preflight(server, PROFILES[profile], sleep, args.gap)
                reason, web_iq = states[profile]
                if reason:
                    rec = skipped(example, profile, number, reason)
                elif example.external and web_iq == "unavailable":
                    rec = skipped(example, profile, number, "Web IQ is unavailable on this profile")
                else:
                    if began:
                        sleep(args.gap)
                    began = True
                    rec = run_one(server, PROFILES[profile], example, profile, number, args.max_seconds, args.timeout)
                    problem = course.judge(rec)
                    if problem:
                        states[profile] = (problem, "unknown")
                    elif course.again(rec):
                        rec["superseded"] = True
                        results.append(rec)
                        progress_line(position, len(plan), rec, stderr)
                        sleep(args.gap)
                        rec = run_one(server, PROFILES[profile], example, profile, number, args.max_seconds,
                                      args.timeout, attempt=2)
                        problem = course.judge(rec)
                        if problem:
                            states[profile] = (problem, "unknown")
            results.append(rec)
            progress_line(position, len(plan), rec, stderr)
            finished = position
    except KeyboardInterrupt:
        course.stop = "interrupted"
    results += [skipped(example, profile, number, "interrupted") for example, profile, number in plan[finished:]]
    final = [r for r in results if not r.get("superseded")]
    summary.update(profiles={p: {"web_iq": s[1], "problem": s[0]} for p, s in states.items()},
                   ok=all(r["ok"] for r in final), interrupted=course.stop == "interrupted", runs=results,
                   totals={"passed": sum(1 for r in final if r["ok"]),
                           "failed": sum(1 for r in final if not r["ok"] and not r.get("skipped")
                                         and not r.get("connection_error")),
                           "skipped": sum(1 for r in final if r.get("skipped")),
                           "http_5xx": sum(r.get("http_5xx", 0) for r in results),
                           "web_iq_calls": sum(r.get("web_iq_calls", 0) for r in results)},
                   connection_errors={"attempts": sum(1 for r in results if r.get("connection_error")),
                                      "retried": sum(1 for r in results if r.get("superseded")),
                                      "final": sum(1 for r in final if r.get("connection_error"))})

    def scrub(text):
        text = text.replace(target.host, "<host>")
        return text.replace(target.address, "<ip>") if target.address else text

    table = scrub(render_table(results))
    text = scrub(json.dumps(summary, indent=2, ensure_ascii=False))
    stdout.write(text + "\n")
    stderr.write(table)
    code = 0 if summary["ok"] else 1
    if args.out and not write_text(args.out, text + "\n", stderr, "--out"):
        code = code or 2
    if args.table and not write_text(args.table, table, stderr, "--table"):
        code = code or 2
    return code


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
