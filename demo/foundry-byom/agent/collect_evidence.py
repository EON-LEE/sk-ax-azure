"""Collect secret-free evidence that the Foundry agent used the A.X K2 BYOM model.

Inputs: the run summary written by run_prompt.py (--evidence-label), optional server records (agent
version, response, conversation items) and operator marks for observations made outside the SDK (APIM
and hub logs, portal screens). A check is confirmed only when a record shows it; everything else stays
unverified. --scan-env VAR looks for a request-time secret in the retrieved records and stores only a
yes/no result. --dry-run and --offline make no network calls.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import itertools
import json
import re
import sys

import byom_common as common

SCHEMA = "foundry-byom-evidence/1"
STATUSES = ("confirmed", "failed", "unverified", "not_applicable")
MAX_ITEMS = 200
ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
TOOL_ITEMS = ("function_call", "mcp_call")
SCAN_SURFACES = {"agent_version": "agent version", "response": "retrieved response",
                 "conversation_items": "conversation items"}
NOT_TOOL_EVENTS = ("message", "reasoning", "function_call_output", "mcp_approval_response")
MANUAL_CHECKS = {
    "apim_gateway_log": "APIM log shows the Foundry call with the A.X K2 model name for this run",
    "backend_hub_log": "Web app/hub record shows the A.X K2 runtime for the same time window",
    "console_model_settings": "Foundry console shows the agent's model reference",
    "console_conversation": "Foundry console shows the conversation",
    "console_tool_history": "Foundry console shows the tool execution history",
    "tracing_visible": "Traces for this run are visible (Application Insights connected)",
}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common.add_connection_args(parser)
    parser.add_argument("--label", required=True, help="evidence/foundry-byom/<label>.json (reads <label>.run.json too)")
    parser.add_argument("--agent-name")
    parser.add_argument("--agent-version")
    parser.add_argument("--connection", default=common.env("FOUNDRY_BYOM_CONNECTION"),
                        help="expected connection name (env FOUNDRY_BYOM_CONNECTION)")
    parser.add_argument("--model", default=common.env("FOUNDRY_BYOM_MODEL", common.DEFAULT_MODEL),
                        help="expected model name (default ax-k2-nvfp4)")
    parser.add_argument("--response-id")
    parser.add_argument("--conversation-id")
    parser.add_argument("--scenario", choices=("plain", "function", "mcp"))
    parser.add_argument("--offline", action="store_true", help="no network: use the run file or existing evidence only")
    parser.add_argument("--mark", action="append", default=[], metavar="ID=STATUS[:NOTE]",
                        help="record a manual observation (APIM/hub log, console screen); repeatable")
    parser.add_argument("--scan-env", action="append", default=[], metavar="ENVVAR",
                        help="environment variable holding a request-time secret to look for in the retrieved "
                             "server records; only the yes/no result is stored (repeatable)")
    return parser


def parse_marks(values):
    marks = {}
    for value in values:
        check, _, rest = value.partition("=")
        status, _, note = rest.partition(":")
        if check not in MANUAL_CHECKS or status not in STATUSES:
            raise common.ConfigError(f"--mark expects ID=STATUS[:NOTE]; ids: {', '.join(MANUAL_CHECKS)}; "
                                     f"statuses: {', '.join(STATUSES)}")
        marks[check] = {"status": status, "note": common.clip(note.strip(), 300) or None, "marked_at": common.now_iso()}
    return marks


def manual_checks(existing, marks):
    previous = {row.get("id"): row for row in (existing or {}).get("manual_checks") or [] if isinstance(row, Mapping)}
    rows = []
    for check, description in MANUAL_CHECKS.items():
        old = previous.get(check) or {}
        row = {"id": check, "description": description, "status": old.get("status", "unverified"),
               "note": old.get("note"), "marked_at": old.get("marked_at")}
        row.update(marks.get(check, {}))
        rows.append(row)
    return rows


def load(path):
    source = common.private_copy(path) or path
    if not source.exists():
        return None
    try:
        data = common.read_json(source)
    except (OSError, ValueError) as exc:
        raise common.ConfigError(f"cannot read {common.display_path(source)}: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise common.ConfigError(f"{common.display_path(source)} is not a JSON object")
    return data


def attempt(errors, call, function):
    try:
        return common.to_plain(function())
    except Exception as exc:  # noqa: BLE001 - recorded in the evidence instead of aborting
        errors.append({"call": call, **common.describe_error(exc)})
        return None


def usable_id(value, call, errors):
    """IDs saved in public evidence are shortened (resp_0a1b2c3d…) and cannot be fetched; say so instead of calling."""
    if isinstance(value, str) and "…" in value:
        errors.append({"call": call, "type": "ShortenedId",
                       "message": "the saved run file holds a shortened ID; run with BYOM_PRIVATE_DIR set "
                                  "or pass the full ID with --response-id / --conversation-id"})
        return None
    return value


def fetch_records(args, clients, run, errors, literals=()):
    request = (run or {}).get("request") or {}
    rounds = (run or {}).get("rounds") or []
    last = rounds[-1] if rounds else {}
    name = args.agent_name or request.get("agent_name") or common.DEFAULT_AGENT
    version = args.agent_version or request.get("agent_version") or (last.get("agent") or {}).get("version")
    response_id = args.response_id or usable_id(last.get("response_id"), "responses.retrieve", errors)
    conversation_id = args.conversation_id or usable_id((run or {}).get("conversation_id"),
                                                        "conversations.items.list", errors)
    records = {"agent_version": None, "response": None, "conversation_items": None, "secret_scan": None}
    scan = {}
    if version is None:
        latest = attempt(errors, "agents.get", lambda: clients.project.agents.get(agent_name=name))
        version = (((latest or {}).get("versions") or {}).get("latest") or {}).get("version")
    if version is not None:
        detail = attempt(errors, "agents.get_version",
                         lambda: clients.project.agents.get_version(agent_name=name, agent_version=str(version)))
        if detail:
            scan["agent_version"] = common.scan_for_secrets(detail, literals)
            definition = common.canonical_definition(detail.get("definition") or {})
            if "instructions" in definition:
                definition["instructions"] = common.clip(definition["instructions"], 400)
            records["agent_version"] = {"name": detail.get("name") or name,
                                        "version": detail.get("version") or version, "definition": definition}
    if response_id:
        response = attempt(errors, "responses.retrieve", lambda: clients.openai.responses.retrieve(response_id))
        if response:
            scan["response"] = common.scan_for_secrets(response, literals)
            records["response"] = common.summarize_response(response)
    if conversation_id:
        items = attempt(errors, "conversations.items.list", lambda: list(itertools.islice(
            clients.openai.conversations.items.list(conversation_id, order="asc", limit=100), MAX_ITEMS)))
        if items is not None:
            scan["conversation_items"] = common.scan_for_secrets(items, literals)
            records["conversation_items"] = [common.summarize_item(item) for item in items if isinstance(item, Mapping)]
    if literals:
        records["secret_scan"] = {"values_checked": len(literals), **scan}
    return records


def pseudo_run(items):
    """Run-like view rebuilt from conversation items, for conversations created outside run_prompt.py."""
    visible = [item for item in items if not (item.get("type") == "message" and item.get("role") == "user")]
    answers = [item.get("text") or "" for item in visible if item.get("type") == "message"]
    return {
        "item_sequence": [item.get("type") for item in visible],
        "tool_events": [item for item in visible if item.get("type") not in NOT_TOOL_EVENTS],
        "function_results": [{"call_id": item.get("call_id"), "output": item.get("output")}
                             for item in items if item.get("type") == "function_call_output"],
        "final_answer": answers[-1] if answers else None,
    }


def infer_scenario(events):
    kinds = {event.get("type") for event in events}
    if kinds & {"mcp_call", "mcp_list_tools", "mcp_approval_request"}:
        return "mcp"
    return "function" if "function_call" in kinds else "plain"


def total_usage(rounds):
    totals = {}
    for item in rounds:
        for key, value in (item.get("usage") or {}).items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
    return totals or None


def build_view(args, run, records):
    request = (run or {}).get("request") or {}
    rounds = (run or {}).get("rounds") or []
    items = records["conversation_items"]
    response = records["response"] or (rounds[-1] if rounds else {})
    base = run or (pseudo_run(items) if items else {})
    events = base.get("tool_events") or response.get("tool_events") or []
    answer = base.get("final_answer") or response.get("final_answer") or ""
    reference = (response.get("agent") or {}) if response else {}
    agent_record = records["agent_version"] or {}
    version = args.agent_version or request.get("agent_version") or agent_record.get("version") or reference.get("version")
    return {
        "scenario": args.scenario or request.get("scenario") or infer_scenario(events),
        "expected_model": common.model_reference(args.connection, args.model) if args.connection else None,
        "agent": {"name": args.agent_name or request.get("agent_name") or common.DEFAULT_AGENT,
                  "version": str(version) if version is not None else None},
        "run": run,
        "known": bool(base) or bool(response),
        "response": response,
        "agent_record": records["agent_version"],
        "items": items,
        "secret_scan": records.get("secret_scan"),
        "events": events,
        "sequence": base.get("item_sequence") or response.get("output_item_types") or [],
        "function_results": base.get("function_results") or [],
        "answer": answer,
        "usage": total_usage(rounds) if rounds else response.get("usage"),
        "think_tag": any(item.get("think_tag") for item in rounds) or bool(response.get("think_tag"))
                     or "<think" in answer.lower(),
    }


def check(identifier, status, detail):
    return {"id": identifier, "status": status, "detail": detail}


def model_reference_check(view):
    name = "agent_definition_model_reference"
    observed = ((view["agent_record"] or {}).get("definition") or {}).get("model")
    if not observed:
        return check(name, "unverified", "agent definition was not retrieved")
    if not view["expected_model"]:
        return check(name, "unverified", f"observed {observed}; pass --connection to compare")
    if observed == view["expected_model"]:
        return check(name, "confirmed", f"agent definition model is {observed}")
    return check(name, "failed", f"agent definition model is {observed}; expected {view['expected_model']}")


def response_agent_check(view):
    name = "response_agent_reference"
    agent = view["response"].get("agent")
    if not agent:
        return check(name, "unverified", "response carries no agent reference")
    wanted = view["agent"]
    same = agent.get("name") == wanted["name"] and (
        not wanted["version"] or str(agent.get("version")) == wanted["version"])
    return check(name, "confirmed" if same else "failed",
                 f"response agent_reference is {agent.get('name')}:{agent.get('version')}")


def completed_check(view):
    name = "response_completed"
    status = view["response"].get("status")
    if status is None:
        return check(name, "unverified", "response status unknown")
    detail = f"status={status}"
    error = view["response"].get("error")
    if error:
        detail += f"; error={common.clip(error.get('code') or error, 120)}"
    return check(name, "confirmed" if status == "completed" else "failed", detail)


def tool_listed_check(view):
    name = "tool_listed"
    if view["scenario"] != "mcp":
        return check(name, "not_applicable", "no MCP tool in this scenario")
    if not view["known"]:
        return check(name, "unverified", "no run or response record")
    listings = [event for event in view["events"] if event.get("type") == "mcp_list_tools"]
    usable = [event for event in listings if event.get("tools") and not event.get("error")]
    if usable:
        return check(name, "confirmed", f"mcp_list_tools returned {len(usable[0]['tools'])} tool(s): "
                                        f"{', '.join(usable[0]['tools'][:5])}")
    if listings:
        return check(name, "failed", f"mcp_list_tools had no usable tools: "
                                     f"{common.clip(listings[0].get('error') or 'empty list', 200)}")
    return check(name, "failed", "no mcp_list_tools item in the output")


def tool_called_check(view):
    name = "tool_called"
    if view["scenario"] == "plain":
        return check(name, "not_applicable", "no tool in this scenario")
    if not view["known"]:
        return check(name, "unverified", "no run or response record")
    if view["scenario"] == "function":
        calls = [event for event in view["events"]
                 if event.get("type") == "function_call" and event.get("name") == common.DEMO_FUNCTION["name"]]
        if calls:
            return check(name, "confirmed", f"function_call {calls[0]['name']} "
                                            f"{json.dumps(calls[0].get('arguments'), ensure_ascii=False)}")
        return check(name, "failed", "no function_call item for the demo function")
    calls = [event for event in view["events"] if event.get("type") == "mcp_call"]
    if not calls:
        return check(name, "failed", "no mcp_call item in the output")
    bad = [event for event in calls if event.get("error") or event.get("status") not in (None, "completed")]
    if bad:
        return check(name, "failed", f"mcp_call {bad[0].get('name')} status={bad[0].get('status')} "
                                     f"error={common.clip(bad[0].get('error'), 160)}")
    return check(name, "confirmed", f"mcp_call {calls[0].get('name')} completed "
                                    f"({calls[0].get('output_chars')} chars of output)")


def order_check(view):
    name = "tool_then_answer_order"
    if view["scenario"] == "plain":
        return check(name, "not_applicable", "no tool in this scenario")
    sequence = view["sequence"]
    if not sequence:
        return check(name, "unverified", "no item sequence recorded")
    tools = [index for index, kind in enumerate(sequence) if kind in TOOL_ITEMS]
    messages = [index for index, kind in enumerate(sequence) if kind == "message"]
    if not tools or not messages:
        return check(name, "failed", f"sequence lacks a tool call or a final message: {' > '.join(map(str, sequence))}")
    status = "confirmed" if messages[-1] > tools[-1] else "failed"
    return check(name, status, f"sequence: {' > '.join(map(str, sequence))}")


def stock_values(view):
    values = []
    for result in view["function_results"]:
        try:
            stock = json.loads(result.get("output") or "")["stock"]
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(stock, int):
            values.append(stock)
    return values


def normalize_url(url):
    return url.split("#")[0].split("?")[0].rstrip("/").lower()


def result_used_check(view):
    name = "tool_result_used"
    if view["scenario"] == "plain":
        return check(name, "not_applicable", "no tool in this scenario")
    if not view["answer"]:
        return check(name, "unverified", "no final answer recorded")
    if view["scenario"] == "function":
        values = stock_values(view)
        if not values:
            return check(name, "unverified", "no function result recorded")
        if any(token in view["answer"] for value in values for token in (str(value), f"{value:,}")):
            return check(name, "confirmed", "final answer contains the value returned by the function")
        return check(name, "failed", "final answer does not contain the function result value; compare manually")
    sources = {normalize_url(url) for event in view["events"] if event.get("type") == "mcp_call"
               for url in event.get("output_urls") or []}
    cited = {normalize_url(url) for url in common.urls_in(view["answer"])}
    if not sources:
        return check(name, "unverified", "tool output carried no URL to compare")
    if sources & cited:
        return check(name, "confirmed", f"final answer cites {len(sources & cited)} URL(s) found in the tool output")
    return check(name, "unverified", "answer URLs do not match tool output URLs; compare the texts manually")


def thinking_check(view):
    name = "no_thinking_trace"
    usage = view["usage"] or {}
    if not (view["answer"] or usage):
        return check(name, "unverified", "no output or usage recorded")
    reasoning_items = view["sequence"].count("reasoning")
    problems = []
    if reasoning_items:
        problems.append(f"{reasoning_items} reasoning item(s)")
    if usage.get("reasoning_tokens"):
        problems.append(f"reasoning_tokens={usage['reasoning_tokens']}")
    if view["think_tag"]:
        problems.append("<think tag in the output")
    if problems:
        return check(name, "failed", "thinking output observed: " + ", ".join(problems))
    return check(name, "confirmed", f"no reasoning item, reasoning token or <think tag; output_tokens="
                                    f"{usage.get('output_tokens')}, answer_chars={len(view['answer'])}; compare "
                                    "them to spot hidden reasoning (server-side default needs the API owner's confirmation)")


def usage_check(view):
    name = "usage_recorded"
    usage = view["usage"] or {}
    if usage.get("input_tokens") is not None and usage.get("output_tokens") is not None:
        return check(name, "confirmed", f"input_tokens={usage['input_tokens']}, output_tokens={usage['output_tokens']}")
    return check(name, "unverified", "no usage recorded")


def nonce_check(view):
    name = "nonce_echoed"
    if view["scenario"] != "plain":
        return check(name, "not_applicable", "nonce echo is checked in the plain scenario")
    contains = (view["run"] or {}).get("final_answer_contains_nonce")
    if contains is None or not view["answer"]:
        return check(name, "unverified", "no final answer with a nonce recorded")
    return check(name, "confirmed" if contains else "failed",
                 "final answer echoes the nonce" if contains else "final answer lacks the nonce")


def negative_check(view):
    name = "negative_control_failed_as_expected"
    run = view["run"] or {}
    if not (run.get("request") or {}).get("expect_failure"):
        return check(name, "not_applicable", "not a negative control")
    error = run.get("error") or {}
    detail = f"stopped={run.get('stopped')}; error={error.get('status_code') or error.get('code') or error.get('type')}"
    return check(name, "confirmed" if run.get("ok") else "failed", detail)


def history_check(view):
    name = "conversation_tool_history"
    if view["scenario"] == "plain":
        return check(name, "not_applicable", "no tool in this scenario")
    if view["items"] is None:
        return check(name, "unverified", "conversation items were not retrieved")
    kinds = [item.get("type") for item in view["items"] if item.get("type") in TOOL_ITEMS]
    if kinds:
        return check(name, "confirmed", f"{len(kinds)} tool item(s) in the conversation: {', '.join(kinds)}")
    return check(name, "failed", "conversation holds no tool item")


def secret_check(view):
    name = "request_secret_not_in_server_records"
    scan = view["secret_scan"]
    echoed = (view["run"] or {}).get("request_header_echoed")
    if scan is None and echoed is None:
        return check(name, "not_applicable", "no request-time secret was used or given with --scan-env")
    results = {SCAN_SURFACES[key]: (scan or {}).get(key) for key in SCAN_SURFACES
               if (scan or {}).get(key) is not None}
    if echoed is not None:
        results["run responses"] = echoed
    hits = [surface for surface, found in results.items() if found]
    if hits:
        return check(name, "failed", f"a request-time secret value appears in: {', '.join(hits)}; "
                                     "treat it as stored by Foundry and rotate it")
    if not results:
        return check(name, "unverified", "no server record could be scanned")
    return check(name, "confirmed", f"value not found in: {', '.join(results)}; Application Insights traces "
                                    "and portal screens are not covered")


def evaluate(view):
    return [function(view) for function in (
        model_reference_check, response_agent_check, completed_check, tool_listed_check, tool_called_check,
        order_check, result_used_check, thinking_check, usage_check, nonce_check, negative_check, history_check,
        secret_check)]


def make_evidence(args, existing, run, records, errors, marks):
    view = build_view(args, run, records)
    observed = ((records["agent_version"] or {}).get("definition") or {}).get("model")
    return {
        "schema": SCHEMA,
        "label": args.label,
        "generated_at": common.now_iso(),
        "scope": common.SCOPE_NOTE,
        "expected": {"model_reference": view["expected_model"], "scenario": view["scenario"], "agent": view["agent"]},
        "run": run,
        "server_records": {**records, "errors": errors},
        "observations": {"agent_definition_model": observed, "response_model": view["response"].get("model"),
                         "final_answer": common.clip(view["answer"], 400) if view["answer"] else None},
        "checks": evaluate(view),
        "manual_checks": manual_checks(existing, marks),
    }


def report(path, evidence):
    counts = {status: 0 for status in STATUSES}
    for row in evidence["checks"] + evidence["manual_checks"]:
        counts[row["status"]] += 1
    evidence["summary"] = counts
    common.write_json(path, evidence)
    lines = [f"evidence: {common.display_path(path)}"]
    lines += [f"{row['id']}: {row['status']}" for row in evidence["checks"] + evidence["manual_checks"]]
    lines.append(", ".join(f"{status}={count}" for status, count in counts.items()))
    sys.stdout.write(common.redact_text("\n".join(lines)) + "\n")


def scan_literals(args, live):
    if not all(common.ENV_NAME_RE.fullmatch(name) for name in args.scan_env):
        raise common.ConfigError("--scan-env expects an environment variable name")
    return [common.secret_from_env(name) for name in args.scan_env] if live else []


def collect(args, clients):
    for value in (args.response_id, args.conversation_id):
        if value is not None and not ID_RE.fullmatch(value):
            raise common.ConfigError("response/conversation id contains unsupported characters")
    if args.agent_name:
        common.check_agent_name(args.agent_name)
    path = common.evidence_path(args.label)
    run_path = common.evidence_path(args.label, "run")
    marks = parse_marks(args.mark)
    existing, run = load(path), load(run_path)
    literals = scan_literals(args, live=not (args.dry_run or args.offline))
    if args.dry_run:
        common.emit({"action": "dry-run", "evidence_file": common.display_path(path),
                     "run_file": common.display_path(run_path), "run_file_present": run is not None,
                     "existing_evidence": existing is not None, "marks": sorted(marks),
                     "scan_env": list(args.scan_env),
                     "fetch": [] if args.offline else ["agents.get_version", "responses.retrieve",
                                                       "conversations.items.list"]})
        return 0
    if args.offline and existing:
        evidence = dict(existing, manual_checks=manual_checks(existing, marks))
    else:
        errors = []
        records = {"agent_version": None, "response": None, "conversation_items": None, "secret_scan": None}
        if not args.offline:
            common.require_live(args)
            records = fetch_records(args, common.connect(args, clients), run, errors, literals)
        evidence = make_evidence(args, existing, run, records, errors, marks)
    report(path, evidence)
    return 0


def main(argv=None, clients=None):
    common.configure_output()
    args = build_parser().parse_args(argv)
    try:
        return collect(args, clients)
    except common.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - summarized for the operator
        common.emit({"action": "error", "error": common.describe_error(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
