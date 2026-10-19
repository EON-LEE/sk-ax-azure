"""Read-only inspection of the Foundry project connections that BYOM agents use.

Lists the project connections without credential values and compares the expected BYOM connections with
the contract given on the command line: category, credential type, deploymentInPath and model list.
Nothing is written to the cloud, credential values are never requested (no include_credentials), and the
OpenAI client is never built, so this script cannot call a model. Connection ids, target hosts and
metadata values outside a short allowlist never appear in the output; for the header hints only the
header names from authConfig and customHeaders are listed, never their values. --dry-run prints the
plan without SDK import, credentials or network.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

import byom_common as common

SCHEMA = "foundry-byom-connections/1"
SAFE_METADATA = {"deploymentinpath": "deploymentInPath", "inferenceapiversion": "inferenceAPIVersion",
                 "deploymentapiversion": "deploymentAPIVersion", "authtype": "authType", "apitype": "apiType"}
MODEL_LIST_KEYS = ("staticmodels", "models")
DISCOVERY_KEYS = ("listmodelsendpoint", "getmodelendpoint", "deploymentprovider")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common.add_connection_args(parser)
    parser.add_argument("--expect", action="append", default=[], metavar="CONNECTION=MODEL[,MODEL...]",
                        help="expected BYOM connection and the models behind it (repeatable); "
                             "only expected connections are named in the output")
    parser.add_argument("--category", default="ApiManagement", help="expected connection category")
    parser.add_argument("--credential-type", default="ApiKey", help="expected credential type")
    parser.add_argument("--deployment-in-path", choices=("true", "false"), default="false",
                        help="expected deploymentInPath: true puts /deployments/<model> into the route")
    parser.add_argument("--evidence-label", metavar="LABEL",
                        help="also save the result to evidence/foundry-byom/<label>.connections.json "
                             "(ignored with --dry-run)")
    return parser


def parse_expect(values):
    expected = {}
    for value in values:
        name, separator, models = value.partition("=")
        names = common.comma_list(models)
        if not separator or not common.CONNECTION_RE.fullmatch(name) or not names \
                or not all(common.MODEL_RE.fullmatch(model) for model in names):
            raise common.ConfigError("--expect needs CONNECTION=MODEL[,MODEL...] with valid names")
        expected.setdefault(name, []).extend(model for model in names if model not in expected.get(name, []))
    return expected


def tolerant_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def model_entries(metadata):
    """Names, formats and versions from a static model list; None when the metadata has no such list."""
    for key in MODEL_LIST_KEYS:
        if key in metadata:
            parsed = tolerant_json(metadata[key])
            break
    else:
        return None
    items = parsed if isinstance(parsed, list) else list(parsed.values()) if isinstance(parsed, dict) else []
    entries = []
    for item in items:
        if isinstance(item, str):
            entries.append({"name": item})
        elif isinstance(item, dict):
            properties = item.get("properties")
            model = properties.get("model") if isinstance(properties, dict) else None
            model = model if isinstance(model, dict) else {}
            entry = {"name": item.get("name") or model.get("name")}
            if model.get("name") and model.get("name") != entry["name"]:
                entry["model"] = model["name"]
            for field in ("format", "version"):
                if model.get(field) or item.get(field):
                    entry[field] = model.get(field) or item.get(field)
            entries.append(entry)
    return entries


def model_discovery(metadata):
    parsed = tolerant_json(metadata.get("modeldiscovery")) if "modeldiscovery" in metadata else None
    if not isinstance(parsed, dict):
        return None
    lowered = {str(key).lower(): value for key, value in parsed.items()}
    return {key: common.clip(lowered[key], 100) for key in DISCOVERY_KEYS if key in lowered} or None


def target_path(target):
    """Scheme and path of the connection target; the host is deployment-specific and is dropped."""
    if not isinstance(target, str):
        return None, None
    parts = urllib.parse.urlsplit(target)
    return parts.scheme or None, parts.path or "/"


def auth_headers(metadata):
    """Header names the gateway connection is configured with; header values and key templates are never copied."""
    hints = {}
    config = tolerant_json(metadata["authconfig"]) if "authconfig" in metadata else None
    if isinstance(config, dict):
        hints["auth_config"] = {key: common.clip(config[key], 60) for key in ("type", "name") if key in config}
    headers = tolerant_json(metadata["customheaders"]) if "customheaders" in metadata else None
    if isinstance(headers, dict):
        hints["custom_header_names"] = sorted(str(key) for key in headers)[:20]
    return hints or None


def describe_connection(connection):
    """Whitelisted view of a connection; ids, hosts, credential values and unlisted metadata values are dropped."""
    plain = common.to_plain(connection) or {}
    raw_metadata = plain.get("metadata") if isinstance(plain.get("metadata"), dict) else {}
    metadata = {str(key).lower(): value for key, value in raw_metadata.items()}
    credentials = plain.get("credentials") if isinstance(plain.get("credentials"), dict) else {}
    returned = [value for key, value in credentials.items() if key != "type" and isinstance(value, str) and value]
    for value in returned:
        common.register_secret(value)
    scheme, path = target_path(plain.get("target"))
    default = plain.get("isDefault", plain.get("is_default"))
    return {
        "name": plain.get("name"),
        "category": plain.get("type"),
        "credential_type": credentials.get("type"),
        "credential_value_returned": bool(returned),
        "is_default": default if isinstance(default, bool) else None,
        "target_scheme": scheme,
        "target_path": path,
        "metadata_keys": sorted(str(key) for key in raw_metadata),
        "metadata": {SAFE_METADATA[key]: common.clip(metadata[key], 80) for key in SAFE_METADATA if key in metadata},
        "deployment_in_path": as_bool(metadata.get("deploymentinpath")),
        "auth_headers": auth_headers(metadata),
        "model_discovery": model_discovery(metadata),
        "models": model_entries(metadata),
    }


def check(identifier, status, detail):
    return {"id": identifier, "status": status, "detail": detail}


def same(actual, wanted):
    return isinstance(actual, str) and actual.lower() == wanted.lower()


def compare(summary, contract, models):
    """Contract checks for one expected connection; a value the service did not expose stays unverified."""
    checks = [check("category", "confirmed" if same(summary["category"], contract["category"]) else "failed",
                    f"category is {summary['category']!r}, contract {contract['category']!r}"),
              check("credential_type", "confirmed" if same(summary["credential_type"], contract["credential_type"])
                    else "failed", f"credential type is {summary['credential_type']!r}, "
                                   f"contract {contract['credential_type']!r}")]
    in_path = summary["deployment_in_path"]
    if in_path is None:
        checks.append(check("deployment_in_path", "unverified", "metadata does not expose deploymentInPath"))
    else:
        checks.append(check("deployment_in_path", "confirmed" if in_path == contract["deployment_in_path"] else "failed",
                            f"deploymentInPath is {in_path}, contract {contract['deployment_in_path']}"))
    listed = summary["models"]
    if listed is None:
        checks.append(check("models", "unverified", "metadata has no static model list"))
    else:
        names = {entry.get("name") for entry in listed} | {entry.get("model") for entry in listed}
        missing = [model for model in models if model not in names]
        checks.append(check("models", "failed" if missing else "confirmed",
                            f"missing from the connection model list: {missing}" if missing
                            else f"model list contains {models}"))
    checks.append(check("credential_value_not_returned", "failed" if summary["credential_value_returned"] else "confirmed",
                        "the service returned a credential value without being asked"
                        if summary["credential_value_returned"] else "no credential value in the response"))
    return checks


def collect(args, clients, expected, contract):
    """List and fetch the connections; a failed list does not stop the per-connection checks."""
    clients = common.run_stage("connect", common.connect, args, clients, with_openai=False)
    listed, list_error = [], None
    try:
        listed = common.run_stage("connections.list", lambda: list(clients.project.connections.list()))
    except common.StageFailure as exc:
        list_error = common.describe_error(exc.original)
    listed_names = None if list_error else {(common.to_plain(item) or {}).get("name") for item in listed}
    records, get_errors = [], {}
    for name in expected:
        try:
            records.append((name, describe_connection(clients.project.connections.get(name))))
        except common.ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded per connection so the other connections are still checked
            get_errors[name] = common.describe_error(exc)
    connections, checks = [], []
    for name, summary in records:
        item_checks = compare(summary, contract, expected[name])
        connections.append({"connection": name, "found": True,
                            "in_list": None if listed_names is None else name in listed_names,
                            "summary": summary, "checks": item_checks})
        checks.extend({**item, "id": f"{name}:{item['id']}"} for item in item_checks)
    for name, error in get_errors.items():
        connections.append({"connection": name, "found": False,
                            "in_list": None if listed_names is None else name in listed_names, "get_error": error})
        checks.append(check(f"{name}:found", "failed", f"connections.get failed with status {error['status_code']}"))
    if list_error:
        checks.append(check("connections.list", "failed",
                            f"connections.list failed with status {list_error['status_code']}"))
    others = [] if listed_names is None else [
        describe_connection(item) for item in listed if (common.to_plain(item) or {}).get("name") not in expected]
    return {
        "action": "inspected",
        "auth": common.credential_info(args),
        "contract": contract,
        "connection_count": None if list_error else len(listed),
        "connections": connections,
        "other_connections": [{"category": item["category"], "credential_type": item["credential_type"]}
                              for item in others],
        **({"list_error": list_error} if list_error else {}),
        "checks": checks,
        "ok": not any(item["status"] == "failed" for item in checks),
        "complete": bool(checks) and all(item["status"] == "confirmed" for item in checks),
    }


def inspect_connections(args, clients):
    expected = parse_expect(args.expect)
    contract = {"category": args.category, "credential_type": args.credential_type,
                "deployment_in_path": args.deployment_in_path == "true",
                "expected": [{"connection": name, "models": models} for name, models in expected.items()]}
    if args.evidence_label:
        common.evidence_path(args.evidence_label, "connections")
    if args.dry_run:
        common.emit({"action": "dry-run", "plan": {
            "calls": ["connections.list()"] + [f"connections.get({name!r})" for name in expected],
            "include_credentials": False, "openai_client": False, "contract": contract}})
        return 0
    common.require_live(args)
    try:
        result = collect(args, clients, expected, contract)
    except common.StageFailure as exc:
        result = {"action": "error", "stage": exc.stage, "error": common.describe_error(exc.original),
                  "auth": common.credential_info(args), "contract": contract}
    common.emit_result(result, args.evidence_label, "connections", SCHEMA)
    if result["action"] == "error" or not result["ok"]:
        return 1
    return 0 if result["complete"] else 3


def main(argv=None, clients=None):
    common.configure_output()
    args = build_parser().parse_args(argv)
    try:
        return inspect_connections(args, clients)
    except common.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - summarized for the operator, never a raw traceback with headers
        common.emit({"action": "error", "error": common.describe_error(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
