"""Verify the APIM model gateway with the web app's smoke tool, then correlate the calls with telemetry.

The smoke tool (demo/tools/model_api_smoke.py) is not reimplemented here. This wrapper runs it against the
gateway APIs with the APIM subscription key, reads the request ids it reports, and looks each id up in
Application Insights (through Log Analytics): the gateway request row, the request-shape trace and the
backend call. Evidence is redacted before it is written.

Phases: `smoke` sends the requests and keeps each raw smoke result in the private directory
(apim-<api>-smoke.json); `correlate` reads those files and only queries the logs; `all` (the default) does both.
Requests are sent only with --go. Run it under WSL/Linux next to byom.py. The subscription key stays in the
private directory (written by `byom.py subkey`) and reaches the smoke tool by file name only.
The smoke tool has no concurrency or retry option: it sends one request at a time and never retries. It reports
how many calls reached the model (admissions); this wrapper holds that number, and the trace count, to the cap.
Once a run fails, no further run is started.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import byom

TOOL = byom.ROOT / "demo" / "tools" / "model_api_smoke.py"
# Flags of the smoke tool that this wrapper passes; the preflight checks each one against `--help`.
FLAGS = {"base_url": "--base-url", "model": "--model", "auth_header": "--auth-header", "key_file": "--key-file",
         "only": "--only", "max_tokens": "--max-tokens", "out": "--out"}
RUNS = (("nvfp4", "ax-k2-nvfp4", ("models", "chat", "stream", "tool", "tool_result", "auth")),
        ("fp8", "ax-k2-fp8", ("models", "auth")))
ADMISSIONS = {"chat", "stream", "tool", "tool_result"}
MAX_ADMISSIONS = 4
MAX_TOKENS = 64
SMOKE_TIMEOUT = 600
POLL_SECONDS = 30
SCAN_ROWS = 2000
PHASES = ("all", "smoke", "correlate")
RAW_NAME = "apim-{api}-smoke.json"
FORWARDED_KIND = "forwarded-headers"
# Client credentials the gateway removes; the backend must see only the gateway's own Authorization header.
CLIENT_CREDENTIAL_FLAGS = ("api_key_header", "subscription_key_header", "api_key_query", "subscription_key_query")
INJECTED_FLAG = "authorization_header"
# Prompt, tool-argument and answer text of the smoke tool's calls. None of it may reach the logs.
BODY_PHRASES = ("capital of France", "Count from 1", "right now", "concise assistant", "temperature_c",
                "weather in Seoul", "Paris")
REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
ID_KEYS = {"request_id": "request", "requestid": "request", "x_request_id": "request", "sent_request_id": "request",
           "apim_request_id": "apim", "apimrequestid": "apim", "x_apim_request_id": "apim"}


def show(client, text, secrets=()):
    for secret in secrets:
        text = text.replace(secret, "<KEY>")
    print(byom.scrub(client.redact(text), client.name))


def build_runs():
    runs = [{"api": api, "model": model, "checks": checks, "admissions": len(ADMISSIONS.intersection(checks))}
            for api, model, checks in RUNS]
    if sum(run["admissions"] for run in runs) > MAX_ADMISSIONS:
        raise ValueError(f"The plan sends more than {MAX_ADMISSIONS} model calls")
    return runs


def raw_path(client, api):
    return client.directory / RAW_NAME.format(api=api)


def command(python, tool, base_url, model, checks, key_file, out):
    """Smoke tool command line. The key travels as a file name, never as a value."""
    return [python, str(tool), FLAGS["base_url"], base_url, FLAGS["model"], model, FLAGS["auth_header"], "api-key",
            FLAGS["key_file"], str(key_file), FLAGS["only"], ",".join(checks), FLAGS["max_tokens"], str(MAX_TOKENS),
            FLAGS["out"], str(out)]


def preflight(python, tool):
    """Local only: the smoke tool must exist and list every flag this wrapper passes to it."""
    if not tool.is_file():
        raise FileNotFoundError("Smoke tool not found (this wrapper does not replace it); pass --smoke-tool")
    result = subprocess.run([python, str(tool), "--help"], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("Smoke tool --help failed: " + (result.stderr or result.stdout).strip()[-400:])
    missing = [flag for flag in FLAGS.values() if not re.search(re.escape(flag) + r"(?![\w-])", result.stdout)]
    if not re.search(r"(?<![\w-])api-key(?![\w-])", result.stdout):
        missing.append("the api-key choice of --auth-header")
    if missing:
        raise RuntimeError("Smoke tool does not list these flags: " + ", ".join(missing))


def resolve_tool(client, options):
    """The tool named on the command line, else the private copy extracted from G's commit, else the checkout's."""
    if options.smoke_tool is not None:
        return options.smoke_tool
    private = client.directory / "tools" / TOOL.name
    return private if private.is_file() else TOOL


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def brief(entry, depth=0):
    """Scalar fields of one smoke check with long text shortened; nested records one level down."""
    kept = {}
    for key, item in entry.items():
        if key == "target":
            continue
        if isinstance(item, str):
            kept[key] = item if len(item) <= 90 else item[:90] + "..."
        elif item is None or isinstance(item, (bool, int, float)):
            kept[key] = item
        elif isinstance(item, dict) and depth < 1:
            kept[key] = brief(item, depth + 1)
        elif isinstance(item, list) and item and all(isinstance(part, str) for part in item):
            kept[key] = item[:5]
    return kept


def summary_lines(output):
    if not isinstance(output, dict):
        return ["  the smoke tool wrote no JSON summary"]
    lines = [f"  admissions={output.get('admissions')} ok={output.get('ok')} total_s={output.get('total_s')}"]
    for name, entry in (output.get("checks") or {}).items():
        if isinstance(entry, dict):
            lines.append(f"  {name}: {json.dumps(brief(entry), ensure_ascii=False)}")
    return lines


def observations(value, text=""):
    """Request ids reported anywhere in the smoke output, each with the scalar fields recorded beside it."""
    found = []

    def walk(node, path):
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, path + [str(index)])
        elif isinstance(node, dict):
            fields = {key: (item[:200] if isinstance(item, str) else item) for key, item in node.items()
                      if item is None or isinstance(item, (str, int, float, bool))}
            fields["source"] = "/".join(path)
            for key, item in node.items():
                kind = ID_KEYS.get(re.sub(r"[^a-z]+", "_", str(key).lower()))
                if kind and isinstance(item, str) and REQUEST_ID.fullmatch(item):
                    found.append({"kind": kind, "id": item, "client": fields})
                elif isinstance(item, (dict, list)):
                    walk(item, path + [str(key)])

    walk(value, [])
    for match in re.finditer(r"x-(apim-)?request-id\s*[:=]\s*([A-Za-z0-9._:-]{1,128})", text, re.IGNORECASE):
        found.append({"kind": "apim" if match.group(1) else "request", "id": match.group(2), "client": {}})
    unique = {}
    for item in found:
        unique.setdefault((item["kind"], item["id"]), item)
    return list(unique.values())


def kql_list(values):
    """A KQL dynamic list; every value is checked first so nothing can break out of the string."""
    if not all(REQUEST_ID.fullmatch(item) for item in values):
        raise ValueError("Identifier outside the allowed character set")
    return "dynamic(" + json.dumps(sorted(set(values))) + ")"


def trace_query(since, request_ids):
    return (f"AppTraces | where TimeGenerated >= datetime({since}) | extend shape = parse_json(Message) "
            f"| where tostring(shape.request_id) in ({kql_list(request_ids)}) "
            "| project TimeGenerated, OperationId, Message")


def request_query(since, operation_ids, apim_ids):
    return (f"AppRequests | where TimeGenerated >= datetime({since}) | extend p = parse_json(Properties) "
            "| extend ApimRequestId = tostring(p['Request Id']) "
            f"| where OperationId in ({kql_list(operation_ids)}) or ApimRequestId in ({kql_list(apim_ids)}) "
            "| project TimeGenerated, OperationId, Name, ResultCode, DurationMs, Success, "
            "Api = tostring(p['API Name']), Operation = tostring(p['Operation Name']), "
            "Subscription = tostring(p['Subscription Name']), ApimRequestId")


def dependency_query(since, operation_ids):
    return (f"AppDependencies | where TimeGenerated >= datetime({since}) "
            f"| where OperationId in ({kql_list(operation_ids)}) "
            "| project TimeGenerated, OperationId, Name, ResultCode, DurationMs, Success")


def rejected_query(since):
    """Calls the gateway itself turned away (no or wrong subscription key): no subscription, HTTP 401."""
    return (f"AppRequests | where TimeGenerated >= datetime({since}) | where ResultCode == '401' "
            "| extend p = parse_json(Properties) | where tostring(p['Subscription Name']) == '' "
            "| summarize Count = count() by Name")


def window_query(since):
    """Every request the gateway logged in the window, counted by call and result."""
    return (f"AppRequests | where TimeGenerated >= datetime({since}) | extend p = parse_json(Properties) "
            "| summarize Count = count(), AvgMs = round(avg(DurationMs), 1), MaxMs = round(max(DurationMs), 1) "
            "by Name, ResultCode, Subscription = tostring(p['Subscription Name']) | order by Name asc, ResultCode asc")


def scan_query(since):
    return (f"union AppRequests, AppTraces, AppDependencies, AppExceptions | where TimeGenerated >= datetime({since}) "
            f"| take {SCAN_ROWS}")


def forwarded_query(since):
    """Presence flags the gateway traced for each request it sent to the backend (every method, GET included)."""
    return (f"AppTraces | where TimeGenerated >= datetime({since}) | extend f = parse_json(Message) "
            f"| where tostring(f.kind) == '{FORWARDED_KIND}' "
            "| project TimeGenerated, OperationId, request_id = tostring(f.request_id), method = tostring(f.method), "
            + ", ".join(f"{flag} = tobool(f.{flag})" for flag in (*CLIENT_CREDENTIAL_FLAGS, INJECTED_FLAG))
            + " | order by TimeGenerated asc | take 200")


def parse_trace(row):
    """The JSON object a policy trace logged, or None when the message is anything else."""
    try:
        record = json.loads(row["Message"])
    except (ValueError, TypeError, KeyError):
        return None
    return record if isinstance(record, dict) and isinstance(record.get("request_id"), str) else None


def forwarded_summary(rows):
    """What the gateway itself saw on the requests it passed to the backend: flags only, never header values."""
    flags = (*CLIENT_CREDENTIAL_FLAGS, INJECTED_FLAG)
    methods = {}
    for row in rows:
        methods[row.get("method")] = methods.get(row.get("method"), 0) + 1
    return {"rows": len(rows), "methods": dict(sorted(methods.items(), key=lambda item: str(item[0]))),
            "present": {flag: sum(1 for row in rows if row.get(flag) is True) for flag in flags},
            "requestIds": sorted({row["request_id"] for row in rows if row.get("request_id")})[:40]}


def credentials_dropped(summary):
    """At least one traced request, and none of them still carried a client credential."""
    return summary["rows"] >= 1 and not any(summary["present"][flag] for flag in CLIENT_CREDENTIAL_FLAGS)


def authorization_injected(summary):
    return summary["rows"] >= 1 and summary["present"][INJECTED_FLAG] == summary["rows"]


def correlate(found, traces, requests, dependencies):
    """Tie each reported id to its trace, gateway request row and backend call."""
    shapes, forwarded = {}, {}
    for row in traces:
        record = parse_trace(row)
        if record is not None:
            (forwarded if record.get("kind") == FORWARDED_KIND else shapes)[record["request_id"]] = (
                row["OperationId"], record)
    by_operation = {row["OperationId"]: row for row in requests}
    by_apim = {row["ApimRequestId"]: row for row in requests if row.get("ApimRequestId")}
    backend = {row["OperationId"]: row for row in dependencies}
    entries = []
    for item in found:
        operation, shape, flags = None, None, None
        if item["kind"] == "request":
            operation, shape = shapes.get(item["id"], (None, None))
            other, flags = forwarded.get(item["id"], (None, None))
            operation = operation or other
        row, via = by_operation.get(operation), "trace"
        if row is None:
            row, via = by_apim.get(item["id"]), "apim-request-id"
        call = backend.get(row["OperationId"]) if row else None
        entries.append({
            "kind": item["kind"], "id": item["id"], "client": item["client"], "trace": shape, "forwarded": flags,
            "apim": None if row is None else {
                "name": row["Name"], "resultCode": row["ResultCode"], "durationMs": round(row["DurationMs"], 1),
                "subscription": row["Subscription"]},
            "backend": None if call is None else {
                "name": call["Name"], "resultCode": call["ResultCode"], "durationMs": round(call["DurationMs"], 1)},
            "matchedBy": via if row else None, "correlated": row is not None})
    return entries


def operation_ids(rows):
    return [row["OperationId"] for row in rows if row.get("OperationId")]


def gather(client, since, found, expected, options, sleep, clock):
    """Poll Log Analytics until every expected trace is visible or the time runs out."""
    ids = [item["id"] for item in found]
    deadline = clock() + options.telemetry_timeout
    sleep(options.ingest_wait)
    while True:
        traces = client.log_query(trace_query(since, ids))
        requests = client.log_query(request_query(since, operation_ids(traces), ids))
        operations = operation_ids(traces + requests)
        dependencies = client.log_query(dependency_query(since, operations)) if operations else []
        entries = correlate(found, traces, requests, dependencies)
        if sum(1 for entry in entries if entry["trace"]) >= expected or clock() >= deadline:
            shape_rows = sum(1 for row in traces if (parse_trace(row) or {}).get("kind") != FORWARDED_KIND)
            counts = {"traceRows": shape_rows, "backendCalls": len(dependencies)}
            return entries, client.log_query(rejected_query(since)), client.log_query(window_query(since)), counts
        sleep(POLL_SECONDS)


def known_secrets(client):
    """Keys the logs must never hold. They are read into memory only and never sent in a query."""
    found = {}
    for name, path in (("subscriptionKey", client.directory / byom.SUBSCRIPTION_KEY_FILE),
                       ("backendKey", client.core / byom.BACKEND_KEY_FILE)):
        if path.exists():
            found[name] = byom.read_secret_file(path)
    return found


def leak_scan(client, since, secrets):
    """Search every log row of the window in memory for the keys and for the smoke tool's prompt text."""
    rows = client.log_query(scan_query(since))
    text = json.dumps(rows, default=str)
    hits = {}
    for row in rows:
        for column, value in row.items():
            lowered = json.dumps(value, default=str).lower()
            for phrase in BODY_PHRASES:
                count = lowered.count(phrase.lower())
                if count:
                    entry = hits.setdefault(phrase, {"occurrences": 0, "columns": {}})
                    entry["occurrences"] += count
                    entry["columns"][column] = entry["columns"].get(column, 0) + count
    return {"rowsScanned": len(rows), "truncated": len(rows) >= SCAN_ROWS, "keysChecked": sorted(secrets),
            "keysFound": sorted(name for name, value in secrets.items() if value in text),
            "promptTextHits": hits}


def execute(client, options, run, key_file, clock):
    out = raw_path(client, run["api"])
    out.unlink(missing_ok=True)
    base_url = f"{client.gateway}/{byom.API_PATHS[run['api']]}"
    started = clock()
    try:
        result = subprocess.run(
            command(options.smoke_python, options.smoke_tool, base_url, run["model"], run["checks"], key_file, out),
            capture_output=True, text=True, timeout=SMOKE_TIMEOUT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    except subprocess.TimeoutExpired:
        return {"exitCode": None, "seconds": round(clock() - started, 1), "output": None, "stdout": "",
                "stderr": f"smoke tool timed out after {SMOKE_TIMEOUT}s"}
    output = None
    for source in (out.read_text() if out.exists() else "", result.stdout):
        try:
            output = json.loads(source)
            break
        except ValueError:
            continue
    if output is not None and not out.exists():
        byom.write_private(out, output)
    return {"exitCode": result.returncode, "seconds": round(clock() - started, 1), "output": output,
            "stdout": result.stdout, "stderr": result.stderr}


def smoke_phase(client, options, runs, key_file, secrets, clock):
    """One run after the other; after a failed run the rest are not started."""
    failed = False
    for run in runs:
        if failed:
            run.update(ran=False, exitCode=None, seconds=0.0, output=None, stdout="", stderr="")
            show(client, f"{run['api']}: not started, an earlier run failed")
            continue
        run.update(execute(client, options, run, key_file, clock), ran=True)
        show(client, f"{run['api']}: exit {run['exitCode']} in {run['seconds']}s", secrets)
        for line in summary_lines(run["output"]):
            show(client, line, secrets)
        if run["exitCode"] != 0:
            show(client, f"{run['api']} stderr tail: {run['stderr'][-600:]}", secrets)
            failed = True


def load_runs(client, runs):
    """Raw smoke results written by an earlier smoke phase; the tool's own ok flag stands in for its exit code."""
    for run in runs:
        path, output = raw_path(client, run["api"]), None
        if path.exists():
            try:
                output = json.loads(path.read_text())
            except ValueError:
                output = None
        valid = isinstance(output, dict)
        run.update(ran=valid, output=output if valid else None, stdout="", stderr="",
                   seconds=output.get("total_s") if valid else None,
                   exitCode=(0 if output.get("ok") is True else 1) if valid else None)


def window_start(runs):
    starts = []
    for run in runs:
        try:
            starts.append(datetime.strptime(run["output"]["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
        except (KeyError, TypeError, ValueError):
            continue
    if not starts:
        raise FileNotFoundError("No smoke result with a start time in the private directory; run --phase smoke first")
    return (min(starts) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")


def reported_admissions(runs):
    values = [run["output"].get("admissions") if isinstance(run["output"], dict) else None for run in runs]
    counted = values and all(isinstance(item, int) and not isinstance(item, bool) for item in values)
    return sum(values) if counted else None


def print_entries(client, entries):
    for entry in entries:
        apim, call, shape, seen = entry["apim"] or {}, entry["backend"] or {}, entry["trace"] or {}, entry["client"]
        flags = entry["forwarded"] or {}
        present = [key for key in (*CLIENT_CREDENTIAL_FLAGS, INJECTED_FLAG) if flags.get(key) is True]
        show(client, f"{seen.get('source') or '-':<22} traced={bool(entry['trace'])!s:<5} "
                     f"apim={apim.get('resultCode', '-')} {apim.get('durationMs', '-')}ms "
                     f"backend={call.get('resultCode', '-')} {call.get('durationMs', '-')}ms "
                     f"stream={shape.get('stream', '-')} max_tokens={shape.get('has_max_tokens', '-')} "
                     f"max_completion_tokens={shape.get('has_max_completion_tokens', '-')} "
                     f"ttft_s={seen.get('ttft_s', '-')} chunks={seen.get('chunks', '-')}"
                     + (f" forwarded_present={present}" if flags else ""))


def verify(client, options, sleep=time.sleep, clock=time.monotonic):
    runs = build_runs()
    sending, reading = options.phase != "correlate", options.phase != "smoke"
    key_file = client.directory / byom.SUBSCRIPTION_KEY_FILE
    if sending:
        if not key_file.exists():
            raise FileNotFoundError("APIM subscription key file is missing; run byom.py subkey first")
        options.smoke_tool = resolve_tool(client, options)
        preflight(options.smoke_python, options.smoke_tool)
        show(client, f"Plan: {sum(run['admissions'] for run in runs)} model call(s) in total, one request at a time, "
                     f"no retries, max_tokens {MAX_TOKENS}")
        for run in runs:
            show(client, f"  {run['api']}: {client.gateway}/{byom.API_PATHS[run['api']]} model={run['model']} "
                         f"checks={','.join(run['checks'])}")
        if not options.go:
            print("Dry run: the smoke tool accepts every planned flag and no request was sent. "
                  "Add --go only after the coordinator's go.")
            return 0
    client.verify_identity()
    secrets = known_secrets(client)
    forbidden = (client.sub, client.tenant, *secrets.values())
    if sending:
        since = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        smoke_phase(client, options, runs, key_file, tuple(secrets.values()), clock)
    else:
        load_runs(client, runs)
        since = window_start([run for run in runs if run["ran"]])
    ran = [run for run in runs if run["ran"]]
    tool = resolve_tool(client, options)
    found = []
    for run in ran:
        run["found"] = observations(run["output"], run["stdout"])
        found += run["found"]
        client.save_evidence(f"apim-smoke-{run['api']}-{options.label}", {
            "observedAt": byom.now(), "phase": options.phase, "smokeToolSha256": file_sha256(tool),
            "gateway": client.gateway, "model": run["model"], "exitCode": run["exitCode"], "seconds": run["seconds"],
            "result": run["output"] if run["output"] is not None else {
                "note": "The smoke tool wrote no JSON", "stdoutTail": run["stdout"][-1500:]}}, forbidden)
    admitted = reported_admissions(ran)
    expected = admitted if admitted is not None else sum(run["admissions"] for run in ran)
    entries, rejected, window, counts, scan, note = [], [], [], {"traceRows": None, "backendCalls": None}, None, None
    forwarded = None
    if not reading:
        note = "not requested (smoke phase)"
    elif not found:
        note = "no request ids in the smoke output; nothing to correlate"
    else:
        entries, rejected, window, counts = gather(client, since, found, expected, options, sleep, clock)
        scan = leak_scan(client, since, secrets)
        forwarded = forwarded_summary(client.log_query(forwarded_query(since)))
    traced = [entry for entry in entries if entry["trace"]]
    verdict = {"smokeExitCodesZero": bool(ran) and all(run["exitCode"] == 0 for run in ran),
               "admissionsReported": admitted,
               "admissionsWithinCap": admitted is not None and admitted <= MAX_ADMISSIONS,
               "tracesExpected": expected, "tracesFound": len(traced),
               "traceRows": counts["traceRows"],
               "traceRowsWithinCap": counts["traceRows"] is None or counts["traceRows"] <= MAX_ADMISSIONS,
               "backendCalls": counts["backendCalls"],
               "tracedRequestsCorrelated": all(entry["correlated"] for entry in traced),
               "backendAcceptedTracedCalls": None if not reading else bool(traced) and all(
                   (entry["backend"] or {}).get("resultCode") == "200" for entry in traced),
               "clientCredentialsNotForwarded": None if forwarded is None else credentials_dropped(forwarded),
               "gatewayAuthorizationOnEveryCall": None if forwarded is None else authorization_injected(forwarded),
               "logsFreeOfKeysAndPromptText": None if scan is None else not (scan["keysFound"] or scan["promptTextHits"])}
    passed = verdict["smokeExitCodesZero"] and verdict["admissionsWithinCap"] and (not reading or (
        verdict["tracesFound"] >= expected and verdict["traceRowsWithinCap"]
        and verdict["tracedRequestsCorrelated"] and verdict["backendAcceptedTracedCalls"] is True
        and verdict["clientCredentialsNotForwarded"] is True
        and verdict["gatewayAuthorizationOnEveryCall"] is True
        and verdict["logsFreeOfKeysAndPromptText"] is True))
    if reading:
        client.save_evidence(f"apim-smoke-correlation-{options.label}", {
            "observedAt": byom.now(), "label": options.label, "phase": options.phase, "gateway": client.gateway,
            "since": since, "smokeToolSha256": file_sha256(tool),
            "runs": [{field: run[field] for field in ("api", "model", "checks", "admissions", "exitCode", "seconds")}
                     | {"requestIds": [item["id"] for item in run["found"] if item["kind"] == "request"],
                        "apimRequestIds": [item["id"] for item in run["found"] if item["kind"] == "apim"]}
                     for run in ran],
            "correlation": {"note": note, "entries": entries, "rejectedAtGateway": rejected,
                            "requestsInWindow": window},
            "forwardedHeaders": forwarded,
            "logScan": scan, "verdict": verdict | {"passed": passed}}, forbidden)
    print_entries(client, entries)
    if forwarded is not None:
        show(client, f"forwarded-headers traces: {json.dumps({key: forwarded[key] for key in ('rows', 'methods', 'present')})}")
    saved = "apim-smoke-correlation" if reading else "apim-smoke-<api>"
    show(client, f"verdict: {'PASS' if passed else 'FAIL'} {json.dumps(verdict)}. "
                 f"Saved evidence/foundry-byom/{saved}-{options.label}.json")
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-directory", type=Path, default=os.environ.get("AXK2_CORE_DIRECTORY"),
                        help="defaults to the AXK2_CORE_DIRECTORY environment variable")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--label", default="post-go", type=byom.label)
    parser.add_argument("--phase", choices=PHASES, default="all")
    parser.add_argument("--smoke-tool", type=Path,
                        help="default: the copy in <private directory>/tools, else demo/tools/model_api_smoke.py")
    parser.add_argument("--smoke-python", default=sys.executable)
    parser.add_argument("--go", action="store_true", help="send requests (only after the coordinator's go)")
    parser.add_argument("--ingest-wait", type=int, help="seconds before the first log query (default 90, 0 for correlate)")
    parser.add_argument("--telemetry-timeout", type=int, default=420, help="seconds to keep polling the logs")
    options = parser.parse_args()
    if options.core_directory is None:
        parser.error("--core-directory or the AXK2_CORE_DIRECTORY environment variable is required")
    if options.ingest_wait is None:
        options.ingest_wait = 90 if options.phase == "all" else 0
    os.umask(0o077)
    return verify(byom.open_deployment(options.core_directory, options.directory), options)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, PermissionError, FileNotFoundError, TimeoutError,
            urllib.error.URLError, subprocess.SubprocessError) as exc:
        print("ERROR:", type(exc).__name__, exc, file=sys.stderr)
        sys.exit(1)
