"""Read-only view of what the gateway received in a time window and how it answered each request.

Run it after a caller (for example a Foundry agent) has used the gateway. From Application Insights alone it shows,
for every request: the route and query names, the request shape the policy traced (body field names, model, stream,
token-limit field, message roles, tool names and count, the numeric and boolean body parameters such as temperature
and max_completion_tokens; never message text, string values or tool schemas), which side produced the status code,
the gateway and backend durations, whether a client credential survived to the backend call, and the gaps between
requests (retries).
Nothing is sent to the gateway or the web app. Calls of the verifier's smoke tool are left out unless
--include-smoke is given. Times are UTC. Logs arrive a few minutes late, so a window that ends now can be incomplete.
With --label the redacted result is saved under evidence/foundry-byom/gateway-requests-<label>.json.
Run it under WSL/Linux next to byom.py.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import byom
from verify_gateway import CLIENT_CREDENTIAL_FLAGS, FORWARDED_KIND, INJECTED_FLAG, known_secrets, parse_trace

# What the smoke tool sends (demo/tools/model_api_smoke.py): a request-id prefix and a user agent.
SMOKE_ID_PREFIX = "smoke-"
SMOKE_AGENT = "axk2-model-api-smoke"
SHAPE_UNAVAILABLE = "request-shape-unavailable"
PARAMETERS_UNAVAILABLE = "unavailable"
MAX_WINDOW = timedelta(hours=48)
INGEST_LAG = timedelta(minutes=5)
SPAN_SLACK = timedelta(milliseconds=50)
REQUEST_CAP, DEPENDENCY_CAP, TRACE_CAP = 500, 1000, 1000
RELATIVE = re.compile(r"(\d{1,4})([mh])")
VERSION = re.compile(r"[A-Za-z0-9._-]{1,40}")
SPAN_KIND = re.compile(r"[A-Za-z0-9_.-]{1,40}")
BACKEND_CALL = re.compile(r"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) /")


def moment(text, clock):
    """A UTC time from `30m` or `2h` (that long ago) or an ISO time that carries its own offset (Z, +09:00)."""
    found = RELATIVE.fullmatch(text)
    if found:
        amount = int(found[1])
        return clock() - (timedelta(minutes=amount) if found[2] == "m" else timedelta(hours=amount))
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Cannot read the time {text!r}: use 30m, 2h or an ISO time with an offset") from None
    if value.tzinfo is None:
        raise ValueError(f"The time {text!r} needs an explicit offset such as Z or +09:00")
    return value.astimezone(timezone.utc)


def kql_time(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def queries(start, end):
    """The three read-only log queries. Only validated datetimes are interpolated."""
    window = f"TimeGenerated between (datetime({kql_time(start)}) .. datetime({kql_time(end)}))"
    return {
        "requests": (f"AppRequests | where {window} | extend p = parse_json(Properties) "
                     "| project TimeGenerated, OperationId, Name, ResultCode, DurationMs, Success, Url, "
                     "Subscription = tostring(p['Subscription Name']) "
                     f"| order by TimeGenerated asc | take {REQUEST_CAP}"),
        "dependencies": (f"AppDependencies | where {window} | project TimeGenerated, OperationId, Name, ResultCode, "
                         f"DurationMs, Success | order by TimeGenerated asc | take {DEPENDENCY_CAP}"),
        "traces": (f"AppTraces | where {window} | project TimeGenerated, OperationId, Message "
                   f"| order by TimeGenerated asc | take {TRACE_CAP}")}


def stamp(text):
    """Log Analytics times carry up to seven fractional digits; Python reads six."""
    parsed = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", str(text)).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def url_shape(url):
    """Path, query-parameter names and the api-version value of a request URL; no host and no other values."""
    parts = urlsplit(url or "")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    version = next((value for key, value in pairs if key.lower() == "api-version"), None)
    if version is not None and not VERSION.fullmatch(version):
        version = "<other>"
    return {"path": parts.path, "queryNames": sorted({key for key, _ in pairs}), "apiVersion": version}


def flag(value):
    """The policy writes booleans, but a value that went through ToString() arrives as text such as "True"."""
    return value if isinstance(value, bool) else str(value).lower() == "true"


def names(value):
    return [item for item in str(value or "").split(",") if item]


def scalar(value):
    return isinstance(value, (bool, int, float))


def parameters(value):
    """The numeric and boolean body parameters the policy copied, in name order (stream_options one level deeper).

    Anything else is dropped again here, so evidence can never hold text. None: the trace predates the parameters.
    """
    if value == PARAMETERS_UNAVAILABLE:
        return value
    if not isinstance(value, dict):
        return None
    found = {}
    for name, item in sorted(value.items()):
        if scalar(item):
            found[name] = item
        elif isinstance(item, dict):
            found[name] = {inner: nested for inner, nested in sorted(item.items()) if scalar(nested)}
    return found


def render(found):
    """One line of name=value pairs; a nested object goes in braces."""
    return ", ".join(f"{name}={{{render(item)}}}" if isinstance(item, dict) else f"{name}={json.dumps(item)}"
                     for name, item in found.items())


def shape_of(record):
    if record is None:
        return None
    stream = record.get("stream")
    limits = [name for name, present in (("max_tokens", flag(record.get("has_max_tokens"))),
                                         ("max_completion_tokens", flag(record.get("has_max_completion_tokens"))))
              if present]
    return {"backendPath": record.get("path"), "userAgent": record.get("user_agent") or "",
            "model": record.get("model"), "stream": stream if stream in (None, "absent") else flag(stream),
            "fields": names(record.get("keys")), "tokenLimit": limits or ["none"],
            "messages": record.get("message_count"), "roles": names(record.get("roles")),
            "tools": names(record.get("tool_names")), "toolCount": record.get("tool_count"),
            "toolChoice": record.get("tool_choice"),
            "hasToolChoice": flag(record["has_tool_choice"]) if "has_tool_choice" in record else None,
            "parameters": parameters(record.get("params"))}


def classify(shape, forwarded, row):
    """smoke: sent by the verifier's smoke tool; other: any other caller; the rest never reached the policy."""
    records = [record for record in (shape, forwarded) if record]
    if not records:
        return "unknown" if row.get("Subscription") else "unauthenticated"
    agent = (shape or {}).get("user_agent") or ""
    smoke = agent.startswith(SMOKE_AGENT) or any(record["request_id"].startswith(SMOKE_ID_PREFIX) for record in records)
    return "smoke" if smoke else "other"


def answered_by(request, call):
    """backend: the backend's status was passed through; gateway: no backend call; gateway-translated: they differ."""
    if call is None:
        return "gateway"
    return "backend" if str(call["ResultCode"]) == str(request["ResultCode"]) else "gateway-translated"


def span_kind(name):
    first = str(name or "").split(" ")[0]
    return first if SPAN_KIND.fullmatch(first) else "other"


def enclosing(started, spans):
    """Kind of the caller-side span (for example an agent invocation) that was running when the request arrived."""
    for span in spans:
        begin = stamp(span["TimeGenerated"])
        if begin - SPAN_SLACK <= started <= begin + timedelta(milliseconds=span["DurationMs"]) + SPAN_SLACK:
            return span_kind(span["Name"])
    return None


def build(requests, dependencies, traces):
    """Join the gateway's request rows with their backend call and policy traces by operation id."""
    shapes, flags, unavailable, other_traces = {}, {}, set(), 0
    for row in traces:
        record = parse_trace(row)
        if record is not None:
            (flags if record.get("kind") == FORWARDED_KIND else shapes)[row["OperationId"]] = record
        elif row.get("Message") == SHAPE_UNAVAILABLE:
            unavailable.add(row["OperationId"])
        else:
            other_traces += 1
    gateway_operations = {row["OperationId"] for row in requests}
    calls, spans = {}, []
    for row in dependencies:
        if row["OperationId"] in gateway_operations:
            calls.setdefault(row["OperationId"], row)
        elif not BACKEND_CALL.match(str(row.get("Name"))):
            spans.append(row)
    entries = []
    for row in requests:
        operation = row["OperationId"]
        call, shape, forwarded = calls.get(operation), shapes.get(operation), flags.get(operation)
        started = stamp(row["TimeGenerated"])
        entries.append({
            "at": started.strftime("%Y-%m-%dT%H:%M:%S.") + f"{started.microsecond // 1000:03d}Z",
            "operation": str(operation)[:8], "call": row["Name"], "url": url_shape(row.get("Url")),
            "subscription": row.get("Subscription") or None, "caller": classify(shape, forwarded, row),
            "status": str(row["ResultCode"]), "gatewayMs": round(row["DurationMs"], 1),
            "backend": None if call is None else {
                "call": call["Name"], "status": str(call["ResultCode"]), "ms": round(call["DurationMs"], 1)},
            "gatewayOverheadMs": None if call is None else round(row["DurationMs"] - call["DurationMs"], 1),
            "answeredBy": answered_by(row, call),
            "shape": {"unavailable": True} if shape is None and operation in unavailable else shape_of(shape),
            "clientCredentialsForwarded": None if forwarded is None else [
                name for name in CLIENT_CREDENTIAL_FLAGS if flag(forwarded.get(name))],
            "authorizationInjected": None if forwarded is None else flag(forwarded.get(INJECTED_FLAG)),
            "duringSpan": enclosing(started, spans), "_started": started})
    return entries, spans, other_traces


def summarize(shown, left_out, spans, other_traces):
    callers = [item for item in shown if item["caller"] == "other"]
    times = [item["_started"] for item in callers]
    shapes = [item["shape"] for item in callers if item["shape"] is not None]
    overhead = [item["gatewayOverheadMs"] for item in shown if item["gatewayOverheadMs"] is not None]
    traced = [item for item in shown if item["clientCredentialsForwarded"] is not None]
    kinds = {}
    for span in spans:
        kinds.setdefault(span_kind(span["Name"]), Counter())[str(span["ResultCode"])] += 1
    return {
        "requests": len(shown), "smokeLeftOut": left_out,
        "callers": dict(sorted(Counter(item["caller"] for item in shown).items())),
        "status": dict(sorted(Counter(item["status"] for item in shown).items())),
        "answeredBy": dict(sorted(Counter(item["answeredBy"] for item in shown).items())),
        "gapsSecondsBetweenCallerRequests": [round((later - earlier).total_seconds(), 2)
                                             for earlier, later in zip(times, times[1:])],
        "identicalShapes": None if not shapes else all(shape == shapes[0] for shape in shapes),
        "gatewayOverheadMs": {"min": min(overhead), "max": max(overhead)} if overhead else None,
        "tracedRequests": len(traced),
        "clientCredentialsForwarded": sum(1 for item in traced if item["clientCredentialsForwarded"]),
        "authorizationInjected": sum(1 for item in traced if item["authorizationInjected"]),
        "callerSideSpans": {kind: {"count": sum(counts.values()), "status": dict(sorted(counts.items()))}
                            for kind, counts in sorted(kinds.items())},
        "otherTraceRows": other_traces}


def describe(item):
    """Printable lines of one request."""
    backend = item["backend"]
    first = (f"+{item['tSeconds']:.2f}s {item['call']} -> {item['status']} in {item['gatewayMs']} ms; "
             + (f"backend {backend['status']} in {backend['ms']} ms (gateway overhead {item['gatewayOverheadMs']} ms)"
                if backend else "no backend call") + f"; answered by {item['answeredBy']}")
    query = item["url"]
    second = (f"    caller {item['caller']}; subscription {item['subscription'] or 'none'}; query names "
              f"{','.join(query['queryNames']) or 'none'}; api-version {query['apiVersion'] or 'none'}"
              + (f"; inside a {item['duringSpan']} span" if item["duringSpan"] else ""))
    lines = [first, second]
    shape = item["shape"]
    if shape and shape.get("unavailable"):
        lines.append("    shape: unavailable (the body could not be read as JSON)")
    elif shape:
        count = shape["toolCount"]
        lines.append(f"    shape: model {shape['model']}, stream {shape['stream']}, token limit "
                     f"{'+'.join(shape['tokenLimit'])}, messages {shape['messages']} ({','.join(shape['roles'])}), "
                     f"tools {'?' if count is None else count} ({','.join(shape['tools']) or 'none'}), "
                     f"tool_choice {shape['toolChoice']}, user agent {shape['userAgent'] or 'empty'}")
        lines.append(f"    body fields: {','.join(shape['fields'])}")
        if shape["parameters"] == PARAMETERS_UNAVAILABLE:
            lines.append("    parameters: unavailable (the policy could not read them)")
        elif shape["parameters"] is not None:
            lines.append(f"    parameters: {render(shape['parameters']) or 'none'}")
    sent = item["clientCredentialsForwarded"]
    if sent is not None:
        lines.append(f"    gateway: client credentials forwarded {','.join(sent) or 'none'}; "
                     f"authorization injected {'yes' if item['authorizationInjected'] else 'NO'}")
    return lines


def observe(client, options, clock=lambda: datetime.now(timezone.utc)):
    client.verify_identity()
    end = moment(options.until, clock) if options.until else clock()
    start = moment(options.since, clock)
    if not start < end <= start + MAX_WINDOW:
        raise ValueError("The window must end after it starts and span at most 48 hours")
    tables = {name: client.log_query(text) for name, text in queries(start, end).items()}
    entries, spans, other_traces = build(**tables)
    shown = [item for item in entries if options.include_smoke or item["caller"] != "smoke"]
    shown.sort(key=lambda item: item["_started"])
    summary = summarize(shown, len(entries) - len(shown), spans, other_traces)
    origin = shown[0]["_started"] if shown else None
    for item in shown:
        item["tSeconds"] = round((item.pop("_started") - origin).total_seconds(), 2)
    capped = [name for name, cap in (("requests", REQUEST_CAP), ("dependencies", DEPENDENCY_CAP), ("traces", TRACE_CAP))
              if len(tables[name]) >= cap]
    secrets = tuple(known_secrets(client).values())

    def show(text):
        for secret in secrets:
            text = text.replace(secret, "<KEY>")
        print(byom.scrub(client.redact(text), client.name))

    show(f"window {kql_time(start)} .. {kql_time(end)} (UTC): {summary['requests']} request(s), "
         f"{summary['smokeLeftOut']} smoke request(s) left out")
    if end > clock() - INGEST_LAG:
        show("note: the window reaches into the last few minutes; logs can arrive late, so run it again later")
    if capped:
        show(f"note: row cap reached for {', '.join(capped)}; narrow the window")
    for item in shown:
        for line in describe(item):
            show(line)
    show("summary: " + json.dumps({key: summary[key] for key in (
        "callers", "status", "answeredBy", "gapsSecondsBetweenCallerRequests", "identicalShapes", "gatewayOverheadMs",
        "clientCredentialsForwarded", "authorizationInjected", "callerSideSpans")}))
    if options.label:
        report = {"observedAt": byom.now(), "window": {"start": kql_time(start), "end": kql_time(end)},
                  "includeSmoke": options.include_smoke, "capped": capped, "summary": summary, "requests": shown}
        client.save_evidence(f"gateway-requests-{options.label}", report, (client.sub, client.tenant, *secrets))
        show(f"Saved evidence/foundry-byom/gateway-requests-{options.label}.json")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--core-directory", type=Path, default=os.environ.get("AXK2_CORE_DIRECTORY"),
                        help="defaults to the AXK2_CORE_DIRECTORY environment variable")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--since", required=True, help="30m, 2h, or an ISO time with an offset (Z, +09:00)")
    parser.add_argument("--until", help="same forms as --since; default: now")
    parser.add_argument("--include-smoke", action="store_true", help="also list the verifier's smoke-tool requests")
    parser.add_argument("--label", type=byom.label, help="save redacted evidence as gateway-requests-<label>.json")
    options = parser.parse_args()
    if options.core_directory is None:
        parser.error("--core-directory or the AXK2_CORE_DIRECTORY environment variable is required")
    os.umask(0o077)
    return observe(byom.open_deployment(options.core_directory, options.directory), options)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, PermissionError, FileNotFoundError, urllib.error.URLError) as exc:
        print("ERROR:", type(exc).__name__, exc, file=sys.stderr)
        sys.exit(1)
