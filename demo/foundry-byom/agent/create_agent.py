"""Create (or reuse) the Foundry prompt agent whose model is the BYOM connection model.

Idempotent: a new agent version is created only when the stored latest definition differs from the
requested one (or with --force). After creating or reusing, the agent is read back (agents.get and
agents.get_version) and the stored model reference is compared with the requested one. --dry-run prints
the plan without SDK import, credentials or network. Only agent versions in the existing Foundry project
are written; this script never builds the OpenAI client, so it cannot call a model. --delete exists for
throwaway negative-control agents and never runs implicitly.
"""
from __future__ import annotations

import argparse
import sys

import byom_common as common

SCHEMA = "foundry-byom-create/1"


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common.add_connection_args(parser)
    common.add_agent_args(parser)
    parser.add_argument("--force", action="store_true", help="create a new version even if the definition is unchanged")
    parser.add_argument("--delete", metavar="AGENT_NAME",
                        help="delete this agent (all versions) and exit; intended for throwaway control agents")
    parser.add_argument("--evidence-label", metavar="LABEL",
                        help="also save the result to evidence/foundry-byom/<label>.create.json (ignored with --dry-run)")
    return parser


def latest_version(clients, name):
    try:
        details = common.to_plain(clients.project.agents.get(agent_name=name))
    except Exception as exc:  # noqa: BLE001 - only a missing agent is expected here
        if common.status_code(exc) == 404:
            return None
        raise
    return (details.get("versions") or {}).get("latest") or None


def stored_view(details, requested):
    """The stored model reference of one agent version and whether it equals the requested one."""
    plain = common.to_plain(details) or {}
    model = (plain.get("definition") or {}).get("model")
    return {"version": plain.get("version"), "model": model, "matches": model == requested}


def verify(clients, name, version, requested):
    """Read the agent back and compare what the service stored with what was requested."""
    result = {"requested_model": requested}
    latest = common.run_stage("verify.agents.get", latest_version, clients, name)
    result["agents_get"] = stored_view(latest, requested) if latest else {"found": False, "matches": False}
    if version is not None:
        result["get_version"] = stored_view(
            common.run_stage("verify.agents.get_version", clients.project.agents.get_version,
                             agent_name=name, agent_version=str(version)), requested)
    views = [view for key, view in result.items() if key != "requested_model"]
    result["ok"] = bool(views) and all(view["matches"] for view in views)
    return result


def delete_agent(args, clients):
    common.check_agent_name(args.delete)
    if args.dry_run:
        common.emit({"action": "dry-run", "delete": args.delete})
        return 0
    common.require_live(args)
    clients = common.connect(args, clients, with_openai=False)
    clients.project.agents.delete(agent_name=args.delete)
    common.emit({"action": "deleted", "agent": args.delete})
    return 0


def apply_definition(args, clients, spec):
    """Create or reuse the agent version, read it back and return the result record."""
    name = args.agent_name
    clients = common.run_stage("connect", common.connect, args, clients, with_openai=False)
    current = common.run_stage("agents.get", latest_version, clients, name)
    wanted = common.canonical_definition(spec)
    if current and not args.force and common.covers(common.canonical_definition(current.get("definition") or {}), wanted):
        action, details = "reused", current
    else:
        created = common.run_stage("agents.create_version", clients.project.agents.create_version,
                                   agent_name=name, definition=common.to_sdk_definition(spec, clients.models))
        action, details = "created", common.to_plain(created)
    result = {"action": action, "agent": name, "version": details.get("version"), "model_reference": spec["model"],
              "definition": common.canonical_definition(details.get("definition") or spec),
              "auth": common.credential_info(args)}
    try:
        result["verification"] = verify(clients, name, details.get("version"), spec["model"])
    except common.StageFailure as exc:
        result["verification"] = {"requested_model": spec["model"], "ok": False, "stage": exc.stage,
                                  "error": common.describe_error(exc.original)}
    return result


def create_or_reuse(args, clients):
    spec = common.definition_spec(args)
    if args.evidence_label:
        common.evidence_path(args.evidence_label, "create")
    if args.dry_run:
        common.emit({"action": "dry-run", "agent": args.agent_name, "model_reference": spec["model"], "definition": spec})
        return 0
    common.require_live(args)
    try:
        result = apply_definition(args, clients, spec)
    except common.StageFailure as exc:
        result = {"action": "error", "agent": args.agent_name, "model_reference": spec["model"], "stage": exc.stage,
                  "error": common.describe_error(exc.original), "auth": common.credential_info(args)}
    common.emit_result(result, args.evidence_label, "create", SCHEMA)
    return 0 if result["action"] != "error" and result["verification"]["ok"] else 1


def main(argv=None, clients=None):
    common.configure_output()
    args = build_parser().parse_args(argv)
    try:
        return delete_agent(args, clients) if args.delete else create_or_reuse(args, clients)
    except common.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - summarized for the operator, never a raw traceback with headers
        common.emit({"action": "error", "error": common.describe_error(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
