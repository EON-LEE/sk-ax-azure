"""Run a fixed schedule of prompt calls against Foundry agents, one at a time, and record every call.

Each call is one run_prompt execution: one conversation, one Responses request per round, SDK retries off.
The series stops at the first call that does not end "completed" (error, 5xx, timeout, failed, incomplete)
and never retries it. With --continue-on-model-error, a call that fails with a model_error is recorded and the
next call follows after the gap; every other failure still stops the series. A completed call in which the model
did not use the MCP tool is a normal result. --request-budget stops before a call that could exceed an estimated
number of model requests, and --stop-on-mcp-error stops after a completed call in which an MCP item failed.
Before the first model call, every agent version in the schedule is read once (GET), so a wrong name or
version fails without any model traffic. --precheck-only stops after that read. --dry-run prints the schedule
without SDK import, credentials or network use.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import re
import secrets
import sys
import time
from typing import NamedTuple

import byom_common as common
import run_prompt

SCHEMA = "foundry-byom-series/1"
CLIPPED_RE = re.compile(r"(.*)\.\.\.\(\+(\d+) chars\)", re.S)
MIN_GAP_FLOOR_S = 15.0
MAX_GAP_S = 3600.0
MAX_TIMEOUT_S = 600.0
MAX_CALLS = 30
REQUESTS_PER_CALL = 2
STOP_RULE =  ("stop at the first call that does not end completed (error, 5xx, timeout, failed, incomplete); "
             "no retry and no further call")
CONTINUE_RULE = ("stop at the first call that does not end completed (error, 5xx, timeout, failed, incomplete) "
                 "except a model_error, which is recorded and followed by the next call after the gap; "
                 "no retry of any call and no further call after any other failure")
NOTES = (
    "Timestamps are UTC with millisecond precision from the client clock; match them with vLLM, APIM and span "
    "clocks by time window.",
    "client_http counts the HTTP requests the OpenAI client sent for that call only: requests = conversation "
    "creation plus Responses POSTs, responses_posts = Responses POSTs. The client is created with max_retries=0. "
    "responses_statuses lists the HTTP status of each Responses POST that got an answer, in order; it is absent "
    "when none did, and fewer statuses than responses_posts means a POST without an answer.",
    "request_budget is a client-side safety net, not a measurement: each call is counted as its Responses POSTs "
    f"(at least 1) plus its mcp_call items (one more model request each), and a call is not started when the "
    f"estimate so far plus {REQUESTS_PER_CALL} would exceed the budget. Gateway counts stay authoritative.",
    "Foundry spans (invoke_agent, chat, execute_tool) are not collected here; read them from the telemetry side "
    "by call window and nonce.",
    "model_error is true when the call failed with HTTP 500 (or as a failed response) and the service error type "
    "or code is model_error. A 503 'model backend not ready' can carry the same code; it is not counted.",
    "contaminated and contamination_note stay null until someone marks a call by hand because other traffic "
    "overlapped it; summary does not account for those marks.",
    "response_model echoes the agent reference; it does not show which model served the call.",
)


class Entry(NamedTuple):
    index: int
    name: str
    version: str
    args: argparse.Namespace
    prompt: str
    run_label: str


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common.add_connection_args(parser)
    parser.add_argument("--call", action="append", required=True, metavar="NAME:VERSION",
                        help="agent name and explicit version (repeatable); the calls run in the given order")
    parser.add_argument("--repeat", type=int, default=1, help="run the whole --call list this many times")
    parser.add_argument("--label", required=True,
                        help="writes evidence/foundry-byom/<label>.series.json and <label>-NN.run.json (one per call)")
    parser.add_argument("--expect-calls", type=int,
                        help="the number of calls you expect; required without --dry-run, must equal the schedule length")
    parser.add_argument("--min-gap-s", type=float, default=60.0,
                        help=f"seconds from the end of one call to the start of the next (at least {MIN_GAP_FLOOR_S:g})")
    parser.add_argument("--scenario", choices=tuple(run_prompt.SCENARIO_PROMPTS), default="mcp")
    prompt = parser.add_mutually_exclusive_group()
    prompt.add_argument("--prompt", help="custom prompt (default: the scenario prompt)")
    prompt.add_argument("--prompt-file", help="UTF-8 file with the prompt")
    parser.add_argument("--nonce", help="one trace code for every call, to repeat an earlier run's prompt "
                                        "(default: a random code per call)")
    parser.add_argument("--max-output-tokens", type=int, default=384, help="output cap per response (at least 1)")
    parser.add_argument("--max-tool-calls", type=int, default=0, help="tool-call cap per response; 0 omits it")
    parser.add_argument("--max-rounds", type=int, default=4, help="request rounds per call including follow-ups")
    parser.add_argument("--timeout", type=float, default=180, help=f"seconds per request (above 0, at most {MAX_TIMEOUT_S:g})")
    parser.add_argument("--continue-on-model-error", action="store_true",
                        help="record a call that ends with a model_error and go on with the next call; "
                             "every other call that does not complete still stops the series")
    parser.add_argument("--request-budget", type=int, metavar="N",
                        help="client-side cap on the estimated model requests of the whole series (at least "
                             f"{REQUESTS_PER_CALL}); the series stops before a call that could exceed it")
    parser.add_argument("--stop-on-mcp-error", action="store_true",
                        help="stop after a completed call in which an MCP call or tool listing failed "
                             "(error set or status failed)")
    parser.add_argument("--precheck-only", action="store_true",
                        help="connect, read every agent version once (GET), print the definitions and exit; "
                             "no model request is sent and no file is written")
    return parser


def parse_call(text):
    name, separator, version = text.rpartition(":")
    if not separator or not run_prompt.VERSION_RE.fullmatch(version):
        raise common.ConfigError("--call expects NAME:VERSION with an explicit version, for example my-agent:1")
    return common.check_agent_name(name), version


def call_args(args, name, version):
    """The run_prompt arguments of one call: its own agent version and trace code, the shared prompt and limits."""
    call = run_prompt.build_parser().parse_args([])
    call.project_endpoint, call.auth = args.project_endpoint, args.auth
    call.agent_name, call.agent_version = name, version
    call.scenario, call.prompt, call.prompt_file = args.scenario, args.prompt, args.prompt_file
    call.nonce = f"r-{secrets.token_hex(4)}" if args.nonce is None else args.nonce
    call.max_output_tokens, call.max_tool_calls = args.max_output_tokens, args.max_tool_calls
    call.max_rounds, call.timeout = args.max_rounds, args.timeout
    return call


def build_schedule(args):
    """Validate everything that can be checked offline and return the calls in running order."""
    scheduled = [parse_call(text) for text in args.call]
    if not 1 <= args.repeat <= MAX_CALLS:
        raise common.ConfigError(f"--repeat must be between 1 and {MAX_CALLS}")
    scheduled = scheduled * args.repeat
    if len(scheduled) > MAX_CALLS:
        raise common.ConfigError(f"the schedule has {len(scheduled)} calls; at most {MAX_CALLS} are allowed")
    if args.expect_calls is not None and args.expect_calls != len(scheduled):
        raise common.ConfigError(f"--expect-calls {args.expect_calls} does not match the {len(scheduled)} scheduled calls")
    if not MIN_GAP_FLOOR_S <= args.min_gap_s <= MAX_GAP_S:
        raise common.ConfigError(f"--min-gap-s must be between {MIN_GAP_FLOOR_S:g} and {MAX_GAP_S:g}")
    if args.max_output_tokens < 1:
        raise common.ConfigError("--max-output-tokens must be at least 1: every call runs with an output cap")
    if not 0 < args.timeout <= MAX_TIMEOUT_S:
        raise common.ConfigError(f"--timeout must be above 0 and at most {MAX_TIMEOUT_S:g}")
    if args.nonce is not None and not run_prompt.NONCE_RE.fullmatch(args.nonce):
        raise common.ConfigError("--nonce must match [A-Za-z0-9._-]{1,40}")
    if args.request_budget is not None and args.request_budget < REQUESTS_PER_CALL:
        raise common.ConfigError(f"--request-budget must be at least {REQUESTS_PER_CALL}: one call can use that many")
    if args.precheck_only and args.dry_run:
        raise common.ConfigError("--precheck-only reads from the project and cannot be combined with --dry-run")
    common.evidence_path(args.label, "series")
    entries = []
    for index, (name, version) in enumerate(scheduled, start=1):
        run_label = f"{args.label}-{index:02d}"
        common.evidence_path(run_label, "run")
        call = call_args(args, name, version)
        prompt, _ = run_prompt.prepare(call)
        entries.append(Entry(index, name, version, call, prompt, run_label))
    return entries


def stop_rule(args):
    return CONTINUE_RULE if args.continue_on_model_error else STOP_RULE


def describe_plan(args, entries):
    calls = []
    for entry in entries:
        single = run_prompt.plan(entry.args, entry.prompt, [])["plan"]
        calls.append({"index": entry.index, "run_file": common.display_path(common.evidence_path(entry.run_label, "run")),
                      "nonce": entry.args.nonce,
                      **{key: single[key] for key in ("agent_reference", "input", "use_conversation",
                                                      "request_options", "max_rounds")}})
    return {"action": "dry-run", "plan": {
        "label": args.label,
        "series_file": common.display_path(common.evidence_path(args.label, "series")),
        "planned_calls": len(entries),
        "min_gap_s": args.min_gap_s,
        "continue_on_model_error": args.continue_on_model_error,
        "request_budget": args.request_budget,
        "stop_on_mcp_error": args.stop_on_mcp_error,
        "stop_rule": stop_rule(args),
        "precheck":  "GET agents.get_version once per distinct name:version, before the first model call",
        "calls": calls,
    }}


def definition_view(definition):
    canonical = common.canonical_definition(definition)
    text = canonical.get("instructions") or ""
    return {"model": canonical.get("model"), "temperature": canonical.get("temperature"),
            "tools": canonical.get("tools"), "instructions_chars": len(text),
            "instructions_sha256_12": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text else None,
            "instructions": common.clip(text, 400) if text else None}


def inspect_agents(clients, entries):
    """GET every distinct agent version once; a wrong name or version fails here, before any model call."""
    found = {}
    for entry in entries:
        key = f"{entry.name}:{entry.version}"
        if key not in found:
            detail = common.run_stage("agents.get_version", clients.project.agents.get_version,
                                      agent_name=entry.name, agent_version=entry.version)
            found[key] = definition_view(common.to_plain(detail).get("definition") or {})
    return found


def wait_for_gap(previous_end, min_gap_s, clock, sleep):
    """Sleep until min_gap_s have passed since the previous call ended; returns the measured gap (None before call 1)."""
    if previous_end is None:
        return None
    while (remaining := min_gap_s - (clock() - previous_end)) > 0:
        sleep(remaining)
    return round(clock() - previous_end, 1)


def chars_of(text):
    """Length of a string that shrink() may have clipped to 'head...(+N chars)'."""
    match = CLIPPED_RE.fullmatch(text)
    return len(match[1]) + int(match[2]) if match else len(text)


def mcp_view(event):
    arguments = event.get("arguments")
    query = arguments.get("query") if isinstance(arguments, Mapping) else None
    view = {"name": event.get("name"), "status": event.get("status"), "error": event.get("error"),
            "arguments_object": isinstance(arguments, Mapping),
            "query_chars": chars_of(query) if isinstance(query, str) else None,
            "output_chars": event.get("output_chars")}
    return {key: value for key, value in view.items() if value is not None}


def brief_error(error):
    if not error:
        return None
    if not isinstance(error, Mapping):
        return {"message": common.clip(str(error), 300)}
    keys = ("type", "status_code", "code", "api_type", "param", "request_id")
    brief = {key: error[key] for key in keys if error.get(key) is not None}
    brief["message"] = common.clip(error.get("message"), 300)
    return brief


def is_model_error(error):
    """True for the service error model_error that arrived as HTTP 500 or as a failed response.

    A 503 "model backend not ready" can carry the same code, but it is a backend outage and not a model_error.
    """
    if not isinstance(error, Mapping) or error.get("status_code") not in (None, 500):
        return False
    return "model_error" in (error.get("code"), error.get("api_type"))


def failure_labels(error):
    """status_code, code and api_type of an error in this order, each value once."""
    labels = []
    for key in ("status_code", "code", "api_type"):
        value = error.get(key) if isinstance(error, Mapping) else None
        if value is not None and str(value) not in labels:
            labels.append(str(value))
    return labels


def round_view(item):
    keys = ("requested_at", "responded_at", "elapsed_s", "response_id", "request_id", "status", "temperature",
            "top_p", "usage", "output_item_types")
    return {key: item[key] for key in keys if item.get(key) is not None}


def call_record(entry, result, gap_s, duration_ms):
    rounds = result["rounds"]
    last = rounds[-1] if rounds else {}
    mcp_calls = [mcp_view(event) for event in result["tool_events"] if event.get("type") == "mcp_call"]
    error = result["error"]
    return {
        "index": entry.index,
        "contaminated": None,
        "contamination_note": None,
        "agent": entry.name,
        "version": entry.version,
        "nonce": entry.args.nonce,
        "started_at": result["started_at"],
        "first_request_at": rounds[0].get("requested_at") if rounds else result["last_request_at"],
        "finished_at": result["finished_at"],
        "duration_ms": duration_ms,
        "gap_before_s": gap_s,
        "ok": result["ok"],
        "stopped": result["stopped"],
        "error": brief_error(error),
        "model_error": is_model_error(error),
        "item_sequence": result["item_sequence"],
        "mcp_called": bool(mcp_calls),
        "mcp_calls": mcp_calls,
        "answer_chars": last.get("answer_chars"),
        "final_answer_contains_nonce": result["final_answer_contains_nonce"],
        "think_tag": any(item.get("think_tag") for item in rounds),
        "response_model": last.get("model"),
        "response_temperature": last.get("temperature"),
        "response_top_p": last.get("top_p"),
        "response_id": last.get("response_id"),
        "conversation_id": result["conversation_id"],
        "usage": last.get("usage"),
        "rounds": [round_view(item) for item in rounds],
        "client_http": result["client_http"],
    }


def summarize(calls):
    def count(items):
        return {"calls": len(items),
                "ok": sum(1 for call in items if call["ok"]),
                "mcp_called": sum(1 for call in items if call["mcp_called"]),
                "ok_without_mcp": sum(1 for call in items if call["ok"] and not call["mcp_called"]),
                "model_error": sum(1 for call in items if call["model_error"])}

    by_agent = {}
    for call in calls:
        by_agent.setdefault(f"{call['agent']}:{call['version']}", []).append(call)
    return {**count(calls), "by_agent": {key: count(items) for key, items in by_agent.items()}}


def new_series(args, entries):
    return {
        "schema": SCHEMA,
        "scope": common.SCOPE_NOTE,
        "generated_at": None,
        "label": args.label,
        "plan": {
            "calls": [f"{entry.name}:{entry.version}" for entry in entries],
            "scenario": args.scenario,
            "custom_prompt": bool(args.prompt or args.prompt_file),
            "min_gap_s": args.min_gap_s,
            "expect_calls": args.expect_calls,
            "limits": {"max_output_tokens": args.max_output_tokens, "max_tool_calls": args.max_tool_calls,
                       "max_rounds": args.max_rounds, "timeout_s": args.timeout},
            "continue_on_model_error": args.continue_on_model_error,
            "request_budget": args.request_budget,
            "stop_on_mcp_error": args.stop_on_mcp_error,
            "stop_rule": stop_rule(args),
        },
        "auth": common.credential_info(args),
        "agents": {},
        "planned_calls": len(entries),
        "calls": [],
        "stopped_early": False,
        "stop_reason": None,
        "error": None,
        "summary": None,
        "notes": list(NOTES),
    }


def save(series):
    series["generated_at"] = common.now_iso("milliseconds")
    series["summary"] = summarize(series["calls"])
    common.write_json(common.evidence_path(series["label"], "series"), series)


def stop_reason(entry, result):
    detail = " ".join(failure_labels(result["error"]))
    return f"call {entry.index} did not complete: {result['stopped']}" + (f" ({detail})" if detail else "")


def request_estimate(record):
    """Estimated model requests of one call: its Responses POSTs (at least 1) plus one more per mcp_call item."""
    posts = (record["client_http"] or {}).get("responses_posts") or 0
    return max(1, posts) + len(record["mcp_calls"])


def mcp_failure(result):
    """Content-free description of the first MCP item that failed inside a call, or None."""
    for event in result["tool_events"]:
        kind = event.get("type")
        if kind in ("mcp_call", "mcp_list_tools") and (event.get("error") or event.get("status") == "failed"):
            name = event.get("name")
            return f"an {kind} item" + (f" ({name})" if name else "") + " failed"
    return None


def progress(entry, total, record):
    usage = record["usage"] or {}
    error = record["error"] or {}
    labels = ([str(error["type"])] if error.get("type") is not None else []) + failure_labels(error)
    failure = "/".join(labels) or "-"
    print(f"call {entry.index}/{total} {entry.name}:{entry.version} ok={record['ok']} start={record['started_at']} "
          f"end={record['finished_at']} duration_ms={record['duration_ms']} "
          f"items={'>'.join(record['item_sequence']) or '-'} usage={usage.get('input_tokens')}/{usage.get('output_tokens')} "
          f"client_http={record['client_http']} error={failure}", file=sys.stderr)


def execute_series(args, clients, entries, clock=time.monotonic, sleep=time.sleep):
    """Run the schedule; returns (series, exit code). The series file is saved before the next call starts."""
    series = new_series(args, entries)
    try:
        series["agents"] = inspect_agents(clients, entries)
    except common.StageFailure as exc:
        series.update(stopped_early=True, stop_reason=f"precheck failed at {exc.stage}",
                      error=brief_error(common.describe_error(exc.original)))
        save(series)
        return series, 1
    save(series)
    print(f"precheck ok: {', '.join(series['agents'])}", file=sys.stderr)
    code, previous_end, estimated = 0, None, 0
    for entry in entries:
        if args.request_budget is not None and estimated + REQUESTS_PER_CALL > args.request_budget:
            series["stopped_early"] = True
            series["stop_reason"] = (f"request budget {args.request_budget} reached before call {entry.index}: "
                                     f"{estimated} estimated model requests so far, "
                                     f"one call can use {REQUESTS_PER_CALL}")
            code = 1
            save(series)
            break
        if previous_end is not None:
            print(f"call {entry.index}/{len(entries)}: waiting for the {args.min_gap_s:g} s gap", file=sys.stderr)
        gap = wait_for_gap(previous_end, args.min_gap_s, clock, sleep)
        reset = getattr(getattr(clients, "http", None), "reset", None)
        if reset:
            reset()
        began = clock()
        result = run_prompt.execute(entry.args, clients, entry.prompt, None, [])
        previous_end = clock()
        record = call_record(entry, result, gap, round((previous_end - began) * 1000))
        series["calls"].append(record)
        estimated += request_estimate(record)
        common.write_json(common.evidence_path(entry.run_label, "run"), result)
        progress(entry, len(entries), record)
        if not result["ok"] and not (args.continue_on_model_error and record["model_error"]):
            series["stopped_early"] = True
            series["stop_reason"] = stop_reason(entry, result)
            code = run_prompt.exit_code(entry.args, result)
            save(series)
            break
        failure = mcp_failure(result) if args.stop_on_mcp_error and result["ok"] else None
        if failure:
            series["stopped_early"] = True
            series["stop_reason"] = f"call {entry.index} completed but {failure}"
            code = 1
            save(series)
            break
        save(series)
    return series, code


def precheck(clients, entries):
    """GET every agent version of the schedule once and print the definitions; no model request, no file."""
    try:
        agents = inspect_agents(clients, entries)
    except common.StageFailure as exc:
        common.emit({"action": "precheck", "ok": False, "stage": exc.stage,
                     "error": brief_error(common.describe_error(exc.original))})
        return 1
    common.emit({"action": "precheck", "ok": True, "agents": agents})
    return 0


def main(argv=None, clients=None, *, clock=time.monotonic, sleep=time.sleep):
    common.configure_output()
    args = build_parser().parse_args(argv)
    try:
        entries = build_schedule(args)
        if args.dry_run:
            common.emit(describe_plan(args, entries))
            return 0
        common.require_live(args)
        if args.precheck_only:
            clients = common.connect(args, clients, with_openai=False)
        else:
            if args.expect_calls is None:
                raise common.ConfigError("--expect-calls is required without --dry-run")
            clients = common.connect(args, clients)
    except common.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - client construction failed before any request
        common.emit({"action": "error", "error": common.describe_error(exc)})
        return 1
    if args.precheck_only:
        return precheck(clients, entries)
    series, code = execute_series(args, clients, entries, clock, sleep)
    common.emit(series)
    print(f"saved {common.display_path(common.evidence_path(args.label, 'series'))}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
