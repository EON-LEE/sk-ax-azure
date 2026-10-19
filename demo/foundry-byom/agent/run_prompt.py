"""Run one prompt against the Foundry agent through the Responses API and print a secret-free summary.

The agent is addressed with extra_body agent_reference. Function calls are answered locally by the one
whitelisted demo function; MCP approvals are granted only for tools named with --approve-tool.
--dry-run prints the request plan without SDK import, credentials or network use.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
import re
import secrets
import sys

import byom_common as common

SCENARIO_PROMPTS = {
    "plain": "한 문장으로 인사하고, 아래 확인 코드를 그대로 포함하세요. 확인 코드: {nonce}",
    "function": "재고 조회 도구로 SKU AX-100의 재고 수량을 확인한 뒤, 수량과 아래 확인 코드를 한 문장으로 알려 주세요. 확인 코드: {nonce}",
    "mcp": ("Microsoft Learn에서 Foundry Agent Service의 BYOM(Bring your own model)을 검색해 두 문장으로 요약하고 "
            "근거 URL 한 개를 붙이세요. 확인 코드는 검색어에 넣지 말고 답변 끝에 그대로 적으세요. 확인 코드: {nonce}"),
}
NONCE_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")
VERSION_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")
HEADER_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
PENDING_STOPS = ("max_rounds", "oauth_consent_required", "incomplete")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common.add_connection_args(parser)
    parser.add_argument("--agent-name", default=common.env("FOUNDRY_BYOM_AGENT", common.DEFAULT_AGENT))
    parser.add_argument("--agent-version", help="pin an agent version (default: the latest version)")
    parser.add_argument("--scenario", choices=tuple(SCENARIO_PROMPTS), default="plain")
    prompt = parser.add_mutually_exclusive_group()
    prompt.add_argument("--prompt", help="custom prompt (default: the scenario prompt)")
    prompt.add_argument("--prompt-file", help="UTF-8 file with the prompt")
    parser.add_argument("--nonce", help="trace code placed in the prompt for log correlation (default: random)")
    parser.add_argument("--max-output-tokens", type=int, default=512, help="output cap per response; 0 omits it")
    parser.add_argument("--max-tool-calls", type=int, default=4, help="tool-call cap per response; 0 omits it")
    parser.add_argument("--max-rounds", type=int, default=4, help="request rounds including follow-ups")
    parser.add_argument("--timeout", type=float, default=180, help="seconds per request; 0 omits it")
    parser.add_argument("--approve-tool", action="append", default=[], metavar="NAME",
                        help="approve MCP approval requests for this tool (repeatable)")
    parser.add_argument("--no-conversation", action="store_true",
                        help="chain every follow-up with previous_response_id instead of a conversation "
                             "(MCP approval follow-ups are always chained this way, as in the Learn samples)")
    parser.add_argument("--delete-conversation", action="store_true",
                        help="delete the conversation afterwards (default: keep it for console inspection)")
    parser.add_argument("--expect-failure", action="store_true",
                        help="negative control: the run succeeds only if the call fails")
    parser.add_argument("--evidence-label", help="write the run summary to evidence/foundry-byom/<label>.run.json")
    parser.add_argument("--mcp-request-header", action="append", default=[], metavar="NAME=ENVVAR",
                        help="experimental: send this header to the MCP server at request time; the value is read "
                             "from the environment variable and is never printed or saved")
    common.add_mcp_args(parser)
    return parser


def build_prompt(args):
    if args.prompt_file:
        try:
            text = Path(args.prompt_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise common.ConfigError(f"cannot read prompt file: {type(exc).__name__}") from exc
    elif args.prompt:
        text = args.prompt
    else:
        return SCENARIO_PROMPTS[args.scenario].format(nonce=args.nonce)
    return text if args.nonce in text else f"{text} [추적 ID: {args.nonce}]"


def header_pairs(items):
    pairs = []
    for item in items:
        name, separator, variable = item.partition("=")
        if not separator or not HEADER_RE.fullmatch(name) or not common.ENV_NAME_RE.fullmatch(variable):
            raise common.ConfigError("--mcp-request-header expects NAME=ENVVAR (header name, environment variable name)")
        pairs.append((name, variable))
    return pairs


def prepare(args):
    common.check_agent_name(args.agent_name)
    if args.agent_version is not None and not VERSION_RE.fullmatch(args.agent_version):
        raise common.ConfigError("invalid agent version")
    if args.max_rounds < 1:
        raise common.ConfigError("--max-rounds must be at least 1")
    args.nonce = args.nonce or f"r-{secrets.token_hex(4)}"
    if not NONCE_RE.fullmatch(args.nonce):
        raise common.ConfigError("--nonce must match [A-Za-z0-9._-]{1,40}")
    if not all(common.TOOL_NAME_RE.fullmatch(name) for name in args.approve_tool):
        raise common.ConfigError("invalid --approve-tool name")
    if args.evidence_label:
        common.evidence_path(args.evidence_label, "run")
    pairs = header_pairs(args.mcp_request_header)
    if pairs and args.scenario != "mcp":
        raise common.ConfigError("--mcp-request-header applies only to --scenario mcp")
    return build_prompt(args), pairs


def request_tools(args, pairs, live):
    """MCP tool override carrying request-time headers; values exist only in memory and in the request."""
    if not pairs:
        return None
    tool = common.mcp_tool_spec(args)
    if live:
        tool["headers"] = {name: common.secret_from_env(variable) for name, variable in pairs}
    return [tool]


def request_secrets(tools):
    return [value for tool in tools or [] for value in (tool.get("headers") or {}).values()]


def merge_scan(previous, found):
    """Combine per-round scan results: a hit wins, then a clean scan, otherwise not scanned (None)."""
    if previous is True or found is True:
        return True
    return previous if found is None else found


def agent_reference(args):
    reference = {"type": "agent_reference", "name": args.agent_name}
    if args.agent_version:
        reference["version"] = args.agent_version
    return reference


def request_kwargs(args, payload, conversation_id, previous_id, tools):
    kwargs = {"input": payload, "extra_body": {"agent_reference": agent_reference(args)}}
    for key, value in (("max_output_tokens", args.max_output_tokens), ("max_tool_calls", args.max_tool_calls),
                       ("timeout", args.timeout)):
        if value and value > 0:
            kwargs[key] = value
    if conversation_id:
        kwargs["conversation"] = conversation_id
    elif previous_id:
        kwargs["previous_response_id"] = previous_id
    if tools:
        kwargs["tools"] = tools
    return kwargs


def chain_label(conversation_id, previous_id):
    """How a request is chained to earlier turns; mirrors request_kwargs (a conversation wins over previous_id)."""
    if conversation_id:
        return "conversation"
    return "previous_response_id" if previous_id else "none"


def plan(args, prompt, pairs):
    options = request_kwargs(args, prompt, None if args.no_conversation else "<new conversation>", None, None)
    options.pop("input")
    return {"action": "dry-run", "plan": {
        "agent_reference": agent_reference(args),
        "input": prompt,
        "nonce": args.nonce,
        "use_conversation": not args.no_conversation,
        "request_options": options,
        "tools_override": request_tools(args, pairs, live=False),
        "request_header_names": [name for name, _ in pairs],
        "approve_tools": sorted(set(args.approve_tool)),
        "max_rounds": args.max_rounds,
        "evidence_file": (common.display_path(common.evidence_path(args.evidence_label, "run"))
                          if args.evidence_label else None),
    }}


def follow_ups(args, items):
    """Local answers for pending tool items: (inputs, function results, stop reason)."""
    inputs, results = [], []
    for item in items:
        kind = item.get("type")
        if kind == "function_call":
            output = common.run_demo_function(item.get("name"), item.get("arguments"), args.nonce)
            inputs.append({"type": "function_call_output", "call_id": item.get("call_id"), "output": output})
            results.append({"name": item.get("name"), "call_id": item.get("call_id"), "output": output})
        elif kind == "mcp_approval_request":
            if item.get("name") not in args.approve_tool:
                return [], [], f"approval_required:{item.get('name')}"
            inputs.append({"type": "mcp_approval_response", "approve": True, "approval_request_id": item.get("id")})
        elif kind == "oauth_consent_request":
            return [], [], "oauth_consent_required"
    return inputs, results, None


def delete_conversation(openai, conversation_id):
    try:
        openai.conversations.delete(conversation_id)
        return True
    except Exception:  # noqa: BLE001 - cleanup is best effort
        return False


def execute(args, clients, prompt, tools, pairs):
    openai = clients.openai
    result = {
        "schema": "foundry-byom-run/1",
        "started_at": common.now_iso(),
        "request": {
            "scenario": args.scenario, "agent_name": args.agent_name, "agent_version": args.agent_version,
            "prompt": common.clip(prompt), "nonce": args.nonce,
            "limits": {"max_output_tokens": args.max_output_tokens, "max_tool_calls": args.max_tool_calls,
                       "max_rounds": args.max_rounds, "timeout_s": args.timeout},
            "use_conversation": not args.no_conversation,
            "approve_tools": sorted(set(args.approve_tool)),
            "request_header_names": [name for name, _ in pairs],
            "expect_failure": args.expect_failure,
        },
        "request_header_echoed": None,
        "conversation_id": None, "conversation_deleted": None, "rounds": [], "function_results": [],
        "stopped": None, "error": None,
    }
    literals = request_secrets(tools)
    conversation_id = previous_id = chain_conversation = None
    payload, answer = prompt, ""
    try:
        if not args.no_conversation:
            conversation_id = chain_conversation = common.to_plain(openai.conversations.create()).get("id")
            result["conversation_id"] = conversation_id
        for round_number in range(1, args.max_rounds + 1):
            response = openai.responses.create(
                **request_kwargs(args, payload, chain_conversation, previous_id, tools))
            data = common.to_plain(response)
            if literals:
                result["request_header_echoed"] = merge_scan(result["request_header_echoed"],
                                                             common.scan_for_secrets(data, literals))
            summary = common.summarize_response(data)
            summary["chained_by"] = chain_label(chain_conversation, previous_id)
            result["rounds"].append(summary)
            previous_id = data.get("id")
            items = [item for item in data.get("output") or [] if isinstance(item, Mapping)]
            answer = common.answer_text(items)
            if data.get("status") not in (None, "completed"):
                result["stopped"] = data["status"]
                break
            inputs, results, stop = follow_ups(args, items)
            if stop or not inputs:
                result["stopped"] = stop or "completed"
                break
            if round_number == args.max_rounds:
                result["stopped"] = "max_rounds"
                break
            result["function_results"].extend(results)
            if any(entry.get("type") == "mcp_approval_response" for entry in inputs):
                chain_conversation = None
            payload = inputs
    except Exception as exc:  # noqa: BLE001 - summarized; partial rounds are kept
        result["stopped"] = "error"
        result["error"] = common.describe_error(exc)
    finally:
        if conversation_id and args.delete_conversation:
            result["conversation_deleted"] = delete_conversation(openai, conversation_id)
    return finish(args, result, answer)


def finish(args, result, answer):
    rounds = result["rounds"]
    if result["error"] is None and rounds and rounds[-1]["error"]:
        result["error"] = rounds[-1]["error"]
    result["final_answer"] = common.clip(answer, 1200) if answer else None
    result["final_answer_contains_nonce"] = args.nonce in answer
    result["item_sequence"] = [kind for item in rounds for kind in item["output_item_types"]]
    result["tool_events"] = [event for item in rounds for event in item["tool_events"]]
    failed = result["error"] is not None or result["stopped"] in ("error", "failed", "cancelled")
    result["ok"] = failed if args.expect_failure else result["stopped"] == "completed"
    return result


def exit_code(args, result):
    if result["ok"]:
        return 0
    stopped = result["stopped"] or ""
    pending = stopped in PENDING_STOPS or stopped.startswith("approval_required")
    return 3 if pending and not args.expect_failure else 1


def main(argv=None, clients=None):
    common.configure_output()
    args = build_parser().parse_args(argv)
    try:
        prompt, pairs = prepare(args)
        if args.dry_run:
            common.emit(plan(args, prompt, pairs))
            return 0
        common.require_live(args)
        tools = request_tools(args, pairs, live=True)
        clients = common.connect(args, clients)
    except common.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - client construction failed before any request
        common.emit({"action": "error", "error": common.describe_error(exc)})
        return 1
    result = execute(args, clients, prompt, tools, pairs)
    if args.evidence_label:
        common.write_json(common.evidence_path(args.evidence_label, "run"), result)
    common.emit(result)
    if result["request_header_echoed"]:
        print("warning: a request header value appears in the Foundry response; treat it as stored and rotate it",
              file=sys.stderr)
    return exit_code(args, result)


if __name__ == "__main__":
    sys.exit(main())
