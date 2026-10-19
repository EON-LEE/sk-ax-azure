"""Foundry + API Management gateway for the A.X K2 BYOM demo (run under WSL/Linux).

State, parameter files and credentials stay in a private per-deployment directory outside the checkout.
This tool never switches the Azure CLI account and never prints subscription or tenant identifiers, keys or
tokens. The only deletion it can perform is `destroy`, limited to the dedicated BYOM resource group.
"""
import argparse
import base64
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
EVIDENCE = ROOT / "evidence" / "foundry-byom"
ARM = "https://management.azure.com"
APIM_API = "2024-05-01"
FOUNDRY_API = "2025-04-01-preview"
APIM_USD_PER_HOUR = 0.96
NAMED_VALUE = "axk2-backend-key"
APIM_SUBSCRIPTION = "foundry-byom"
BACKEND_KEY_FILE = "model-api-backend.key"
SUBSCRIPTION_KEY_FILE = "apim-subscription.key"
STAGES = ("foundation", "apim", "gateway", "connections")
TERMINAL = {"Succeeded", "Failed", "Canceled"}
API_PATHS = {"nvfp4": "nvfp4/v1", "fp8": "fp8/v1"}
# Committed evidence must not name the deployment: host names by suffix, then the deployment prefix.
PUBLIC_HOSTS = ((r"[A-Za-z0-9.-]+\.azure-api\.net", "<apim-gateway>"),
                (r"[A-Za-z0-9.-]+\.azurewebsites\.net", "<web-app>"),
                (r"[A-Za-z0-9.-]+\.services\.ai\.azure\.com", "<foundry-endpoint>"))


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_private(path, value, text=False):
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value if text else json.dumps(value, indent=2))
    temporary.replace(path)


def read_secret_file(path):
    """Read a one-line secret that must be private to its owner."""
    if path.stat().st_mode & 0o077:
        raise PermissionError("Secret file must be mode 0600")
    value = path.read_text().strip()
    if len(value) < 32 or re.search(r"\s", value):
        raise ValueError("Secret file does not hold a single-line key")
    return value


def error_code(raw):
    try:
        return json.loads(raw)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return ""


def body_keys(raw):
    try:
        value = json.loads(raw)
    except ValueError:
        return f"non-json:{len(raw)}B"
    return sorted(value) if isinstance(value, dict) else type(value).__name__


def scrub(value, deployment):
    """Return a copy of a JSON value without deployment-specific host names or the deployment prefix."""
    if isinstance(value, dict):
        return {scrub(key, deployment): scrub(item, deployment) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item, deployment) for item in value]
    if isinstance(value, str):
        for pattern, placeholder in PUBLIC_HOSTS:
            value = re.sub(pattern, placeholder, value, flags=re.IGNORECASE)
        return re.sub(re.escape(deployment), "<deployment>", value, flags=re.IGNORECASE)
    return value


class Byom:
    def __init__(self, core, directory):
        config = json.loads((core / "target.json").read_text())
        self.sub, self.tenant, self.name = config["subscription_id"], config["tenant_id"], config["name"]
        if not re.fullmatch(r"[a-z][a-z0-9]{5,15}", self.name):
            raise ValueError("Invalid deployment name")
        if not all(re.fullmatch(r"[0-9a-f-]{36}", item) for item in (self.sub, self.tenant)):
            raise ValueError("Core target configuration is incomplete")
        self.core, self.directory = core, directory
        self.location = config["web_location"]
        self.rg = f"rg-{self.name}-byom"
        self.apim = f"{self.name}-apim"
        self.account = f"{self.name}-foundry"
        self.project = "axk2-demo"
        self.logs = f"{self.name}-byom-logs"
        self.insights = f"{self.name}-byom-ai"
        self.web = f"https://{self.name}.azurewebsites.net"
        self.gateway = f"https://{self.apim}.azure-api.net"
        self.scope = f"{ARM}/subscriptions/{self.sub}/resourceGroups/{self.rg}/providers"
        self.env = dict(os.environ, PYTHONWARNINGS="ignore", AZURE_CORE_COLLECT_TELEMETRY="false")
        self.state_path = directory / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self._token = None
        self._workspace = None

    def save(self):
        write_private(self.state_path, self.state)

    def redact(self, text):
        for value, label in ((self.sub, "<SUB>"), (self.tenant, "<TENANT>")):
            text = text.replace(value, label)
        text = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "<GUID>", text)
        text = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "<EMAIL>", text)
        text = re.sub(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]*", "<JWT>", text)
        return re.sub(r"\b[0-9a-fA-F]{32,}\b", "<HEX>", text)

    def az(self, *args, output="json", timeout=900):
        run = subprocess.run(["az", *args, "--subscription", self.sub, "--only-show-errors", "-o", output],
                             env=self.env, capture_output=True, text=True, timeout=timeout)
        if run.returncode:
            # Error output can be long; keep the full text private and show a redacted excerpt.
            (self.directory / "azure-error.log").write_text(run.stderr)
            raise RuntimeError(f"az {' '.join(args[:2])} failed: " + self.redact(run.stderr.strip())[:1500])
        if output == "json":
            return json.loads(run.stdout) if run.stdout.strip() else {}
        return run.stdout.strip()

    def verify_identity(self):
        account = self.az("account", "show")
        if account["id"] != self.sub or account["tenantId"] != self.tenant or account["state"] != "Enabled":
            raise ValueError("The Azure CLI profile does not match the target configuration")

    def token(self):
        if self._token is None:
            self._token = self.az("account", "get-access-token", "--resource", ARM + "/")["accessToken"]
        return self._token

    def operator(self):
        payload = self.token().split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims["oid"], "ServicePrincipal" if claims.get("idtyp") == "app" else "User"

    def arm(self, method, path, body=None, api=APIM_API, timeout=120):
        """Call ARM and return (status, json). Error bodies are reduced to their error code."""
        data = json.dumps(body).encode() if body is not None else (b"" if method in ("POST", "PUT") else None)
        request = urllib.request.Request(
            f"{self.scope}/{path}?api-version={api}", method=method, data=data,
            headers={"Authorization": "Bearer " + self.token(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            return exc.code, {"error": error_code(exc.read())}

    def group_exists(self):
        return self.az("group", "exists", "-n", self.rg, output="tsv") == "true"

    def ensure_group(self):
        created = self.state.setdefault("createdAt", now())
        if not self.group_exists():
            self.az("group", "create", "-n", self.rg, "-l", self.location, "--tags", "purpose=axk2-demo",
                    f"deployment={self.name}", f"createdAt={created}", output="none")
            print("Resource group created.", flush=True)
        self.save()

    def tags(self):
        return {"purpose": "axk2-demo", "deployment": self.name, "createdAt": self.state["createdAt"]}

    def params(self, stage):
        if stage == "foundation":
            oid, kind = self.operator()
            return {"accountName": self.account, "projectName": self.project, "logName": self.logs,
                    "aiName": self.insights, "operatorObjectId": oid, "operatorPrincipalType": kind,
                    "tags": self.tags()}
        if stage == "apim":
            return {"apimName": self.apim, "aiName": self.insights, "tags": self.tags()}
        if stage == "gateway":
            return {"apimName": self.apim, "backendBaseUrl": self.web}
        return {"accountName": self.account, "projectName": self.project, "apimName": self.apim}

    def init(self):
        self.verify_identity()
        self.state.setdefault("createdAt", now())
        self.save()
        print("Identity matches the target configuration; private state ready. No cloud changes.")

    def deploy(self, stage, plan_only):
        self.verify_identity()
        if stage == "foundation" and not plan_only:
            self.ensure_group()
        if not self.group_exists():
            if plan_only:
                print("Resource group does not exist yet; nothing to plan.")
                return
            raise RuntimeError("Resource group is missing; run the foundation stage first")
        self.state.setdefault("createdAt", now())
        if stage == "gateway" and self.arm("GET", f"Microsoft.ApiManagement/service/{self.apim}/namedValues/{NAMED_VALUE}")[0] != 200:
            raise RuntimeError("Backend key named value is missing; run the key action before the gateway stage")
        parameters = self.directory / f"params-{stage}.json"
        write_private(parameters, {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {key: {"value": value} for key, value in self.params(stage).items()}})
        common = ["-g", self.rg, "-f", str(HERE / f"{stage}.bicep"), "-p", "@" + str(parameters)]
        try:
            changes = self.az("deployment", "group", "what-if", *common, "--result-format", "ResourceIdOnly",
                              "--no-pretty-print")
        except RuntimeError as exc:
            print("What-if unavailable, continuing with an incremental deployment:", exc, flush=True)
            changes = {"changes": []}
        summary = {}
        for change in changes.get("changes", []):
            summary.setdefault(change["changeType"], []).append(change["resourceId"].split("/providers/", 1)[-1])
        if summary.get("Delete"):
            raise ValueError("What-if reports a deletion; refusing to continue")
        for kind, items in summary.items():
            print(f"what-if {kind}: {len(items)}")
            for item in items[:30]:
                print("  " + self.redact(item))
        if plan_only:
            return
        name = f"byom-{stage}-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}"
        if stage == "apim":
            record = self.state.setdefault("apim", {})
            record.setdefault("requestedAt", now())
            record["deployment"] = name
        self.save()
        self.az("deployment", "group", "create", *common, "-n", name, "--no-wait", output="none")
        self.wait(stage, name, 3600 if stage == "apim" else 1200)

    def failures(self, name, depth=0):
        rows = self.az("deployment", "operation", "group", "list", "-g", self.rg, "-n", name, "--query",
                       "[?properties.provisioningState=='Failed'].{type:properties.targetResource.resourceType,"
                       "name:properties.targetResource.resourceName,code:properties.statusMessage.error.code,"
                       "message:properties.statusMessage.error.message}")
        for row in rows:
            print(self.redact(json.dumps(row))[:1200], flush=True)
            if row["type"] == "Microsoft.Resources/deployments" and depth < 2:
                self.failures(row["name"], depth + 1)

    def wait(self, stage, name, limit):
        started, last, misses = time.monotonic(), "", 0
        while time.monotonic() - started < limit:
            try:
                state = self.az("deployment", "group", "show", "-g", self.rg, "-n", name,
                                "--query", "properties.provisioningState", output="tsv")
            except RuntimeError:
                # A just-submitted deployment can be briefly invisible.
                misses += 1
                if misses > 5:
                    raise
                time.sleep(10)
                continue
            elapsed = int(time.monotonic() - started)
            if state != last or elapsed % 120 < 20:
                print(f"[{stage}] {state} after {elapsed}s", flush=True)
                last = state
            if state in TERMINAL:
                break
            time.sleep(20 if stage != "apim" else 30)
        else:
            raise TimeoutError(f"{stage} deployment still running; inspect it before retrying")
        if state != "Succeeded":
            self.failures(name)
            raise RuntimeError(f"{stage} deployment {state}")
        self.state.setdefault("deployments", {})[stage] = {"name": name, "completedAt": now()}
        if stage == "apim":
            self.state["apim"]["readyAt"] = now()
        self.save()
        outputs = self.az("deployment", "group", "show", "-g", self.rg, "-n", name,
                          "--query", "properties.outputs")
        print(f"[{stage}] Succeeded.", self.redact(json.dumps({k: v.get("value") for k, v in (outputs or {}).items()})))

    def backend_key(self):
        return read_secret_file(self.core / BACKEND_KEY_FILE)

    def put_backend_key(self):
        self.verify_identity()
        key = self.backend_key()
        path = f"Microsoft.ApiManagement/service/{self.apim}/namedValues/{NAMED_VALUE}"
        status, _ = self.arm("PUT", path, {"properties": {"displayName": NAMED_VALUE, "value": key,
                                                           "secret": True, "tags": ["axk2"]}})
        if status not in (200, 201, 202):
            raise RuntimeError(f"Named value PUT failed with HTTP {status}")
        for _ in range(12):
            status, found = self.arm("POST", path + "/listValue")
            if status == 200 and hmac.compare_digest(found.get("value", ""), key):
                break
            time.sleep(5)
        else:
            raise RuntimeError("Named value could not be verified")
        status, meta = self.arm("GET", path)
        print(f"Backend key stored as secret named value (HTTP {status}, secret={meta['properties']['secret']}); "
              "value verified in memory, not printed.")

    def subscription_key(self):
        status, found = self.arm("POST", f"Microsoft.ApiManagement/service/{self.apim}/subscriptions/{APIM_SUBSCRIPTION}/listSecrets")
        if status != 200:
            raise RuntimeError(f"APIM subscription key could not be read (HTTP {status})")
        return found["primaryKey"]

    def write_subscription_key(self):
        self.verify_identity()
        write_private(self.directory / SUBSCRIPTION_KEY_FILE, self.subscription_key() + "\n", text=True)
        print("APIM subscription key written to the private directory (mode 0600); not printed.")

    def http(self, method, url, headers=None, body=None):
        request = urllib.request.Request(url, method=method, data=body, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def probe(self, label):
        """Authentication checks that never reach the model: a missing or wrong key, then the model list."""
        self.verify_identity()
        key_path = self.directory / SUBSCRIPTION_KEY_FILE
        good = read_secret_file(key_path) if key_path.exists() else self.subscription_key()
        wrong = secrets.token_hex(16)
        results = []
        for api, path in API_PATHS.items():
            for check, headers, endpoint, method, body in (
                    ("no-key", {}, "chat/completions", "POST", b'{"model":"x","messages":[]}'),
                    ("wrong-key", {"api-key": wrong}, "chat/completions", "POST", b'{"model":"x","messages":[]}'),
                    ("valid-key-models", {"api-key": good}, "models", "GET", None)):
                if body is not None:
                    headers = {**headers, "Content-Type": "application/json"}
                status, raw, response = self.http(method, f"{self.gateway}/{path}/{endpoint}", headers, body)
                results.append({"api": api, "check": check, "method": method, "endpoint": f"/{path}/{endpoint}",
                                "status": status, "body_keys": body_keys(raw),
                                "apim_request_id": response.get("x-apim-request-id"),
                                "x_request_id": response.get("x-request-id")})
        report = {"observedAt": now(), "gateway": self.gateway, "checks": results}
        self.save_evidence(f"probe-{label}", report, (good, wrong, self.sub, self.tenant))
        print(json.dumps(report, indent=2))

    def save_evidence(self, name, value, forbidden):
        text = json.dumps(scrub(value, self.name), indent=2)
        if any(item in text for item in forbidden) or re.search(r"eyJ[\w-]{10,}\.", text):
            raise ValueError("Evidence contains a secret or identifier; not written")
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / f"{name}.json").write_text(text + "\n")

    def log_query(self, text):
        """Run a read-only Log Analytics query against the BYOM workspace; rows come back as dictionaries."""
        if self._workspace is None:
            status, found = self.arm("GET", f"Microsoft.OperationalInsights/workspaces/{self.logs}", api="2023-09-01")
            if status != 200:
                raise RuntimeError(f"Log Analytics workspace could not be read (HTTP {status})")
            token = self.az("account", "get-access-token", "--resource", "https://api.loganalytics.io")["accessToken"]
            self._workspace = (found["properties"]["customerId"], token)
        workspace, token = self._workspace
        request = urllib.request.Request(
            f"https://api.loganalytics.io/v1/workspaces/{workspace}/query", method="POST",
            data=json.dumps({"query": text}).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                table = json.loads(response.read())["tables"][0]
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Log query failed with HTTP {exc.code} {error_code(exc.read())}")
        names = [column["name"] for column in table["columns"]]
        return [dict(zip(names, row)) for row in table["rows"]]

    def telemetry(self, minutes, label, forbid):
        """What the gateway wrote to Application Insights: request metadata and request-shape traces only."""
        self.verify_identity()
        query = self.log_query
        window = f"TimeGenerated > ago({minutes}m)"
        requests = query(
            f"AppRequests | where {window} | extend p = parse_json(Properties) | project TimeGenerated, Name, "
            "ResultCode, DurationMs, Api = tostring(p['API Name']), Operation = tostring(p['Operation Name']), "
            "Subscription = tostring(p['Subscription Name']), RequestId = tostring(p['Request Id']) "
            "| order by TimeGenerated asc | take 500")
        traces = query(f"AppTraces | where {window} | project TimeGenerated, Message "
                       "| order by TimeGenerated asc | take 500")
        hits = query(f"union AppRequests, AppTraces, AppDependencies, AppExceptions | where {window} "
                     f"| where * has '{forbid}' | count")[0]["Count"]
        for row in traces:
            try:
                row["Message"] = json.loads(row["Message"])
            except ValueError:
                pass
        report = {"observedAt": now(), "windowMinutes": minutes, "requests": requests, "traces": traces,
                  "forbiddenText": forbid, "forbiddenTextHits": hits}
        self.save_evidence(f"telemetry-{label}", report, (self.sub, self.tenant))
        for row in requests:
            print(self.redact(f"{row['TimeGenerated'][:19]} {row['Name']} -> {row['ResultCode']} "
                              f"{row['DurationMs']:.0f}ms {row['Api']}/{row['Operation']} "
                              f"subscription={row['Subscription'] or '-'}"))
        print(f"{len(requests)} request(s), {len(traces)} trace(s); '{forbid}' found {hits} time(s). "
              f"Saved evidence/foundry-byom/telemetry-{label}.json")

    def connections(self, label):
        """Connection records as ARM and the project data plane report them; secrets are never requested."""
        self.verify_identity()
        status, found = self.arm("GET", f"Microsoft.CognitiveServices/accounts/{self.account}/projects/"
                                 f"{self.project}/connections", api=FOUNDRY_API)
        if status != 200:
            raise RuntimeError(f"Connections could not be listed (HTTP {status})")
        token = self.az("account", "get-access-token", "--resource", "https://ai.azure.com")["accessToken"]
        endpoint = f"https://{self.account}.services.ai.azure.com/api/projects/{self.project}"

        def plane(path):
            request = urllib.request.Request(f"{endpoint}/{path}?api-version=v1",
                                             headers={"Authorization": "Bearer " + token})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.status, json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as exc:
                return exc.code, {"error": error_code(exc.read())}

        def clean(value):
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()
                        if key.lower() not in ("id", "credentials", "key", "apikey", "systemdata")}
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value

        def readable(item):
            metadata = item.get("metadata")
            if isinstance(metadata, dict) and isinstance(metadata.get("models"), str):
                try:
                    metadata["models"] = json.loads(metadata["models"])
                except ValueError:
                    pass
            return item

        plane_status, plane_found = plane("connections")
        deployments_status, deployments = plane("deployments")
        report = {
            "observedAt": now(),
            "arm": [readable({"name": item["name"], **clean(item["properties"])}) for item in found.get("value", [])],
            "dataPlane": {
                "httpStatus": plane_status,
                "connections": ([readable(clean(item)) for item in plane_found.get("value", [])]
                                if plane_status == 200 else plane_found),
                "deploymentsHttpStatus": deployments_status,
                "deploymentCount": len(deployments.get("value", [])) if deployments_status == 200 else None}}
        self.save_evidence(f"connections-{label}", report, (self.sub, self.tenant))
        for item in report["arm"]:
            metadata = item.get("metadata") or {}
            print(f"{item['name']}: category={item.get('category')} auth={item.get('authType')} "
                  f"deploymentInPath={metadata.get('deploymentInPath')} target={item.get('target')}")
        print(f"data plane HTTP {plane_status}, deployments {report['dataPlane']['deploymentCount']}. "
              f"Saved evidence/foundry-byom/connections-{label}.json")

    def status(self):
        self.verify_identity()
        report = {"resourceGroup": self.rg, "createdAt": self.state.get("createdAt"), "observedAt": now()}
        if not self.group_exists():
            report["exists"] = False
            print(json.dumps(report, indent=2))
            return
        report["resources"] = sorted(
            f"{item['type'].split('/')[-1]}:{item['name']}" for item in self.az(
                "resource", "list", "-g", self.rg, "--query", "[].{name:name,type:type}"))
        status, apim = self.arm("GET", f"Microsoft.ApiManagement/service/{self.apim}")
        if status == 200:
            record = self.state.get("apim", {})
            started = record.get("requestedAt")
            hours = ((datetime.now(timezone.utc) - datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ")
                      .replace(tzinfo=timezone.utc)).total_seconds() / 3600) if started else None
            report["apim"] = {"sku": apim["sku"]["name"], "capacity": apim["sku"]["capacity"],
                              "state": apim["properties"]["provisioningState"], "requestedAt": started,
                              "readyAt": record.get("readyAt"), "hourlyUsd": APIM_USD_PER_HOUR,
                              "elapsedHours": round(hours, 2) if hours is not None else None,
                              "estimatedUsd": round(hours * APIM_USD_PER_HOUR, 2) if hours is not None else None}
        status, found = self.arm("GET", f"Microsoft.CognitiveServices/accounts/{self.account}/projects/{self.project}/connections",
                                 api=FOUNDRY_API)
        if status == 200:
            report["connections"] = [{"name": item["name"], "category": item["properties"].get("category"),
                                      "target": item["properties"].get("target"),
                                      "authType": item["properties"].get("authType")}
                                     for item in found.get("value", [])]
        print(self.redact(json.dumps(report, indent=2)))

    def destroy(self, confirm):
        self.verify_identity()
        if confirm != self.rg:
            raise ValueError("Pass --confirm with the exact BYOM resource group name")
        tags = self.az("group", "show", "-n", self.rg, "--query", "tags") or {}
        if tags.get("purpose") != "axk2-demo" or tags.get("deployment") != self.name:
            raise ValueError("Resource group is not tagged as this BYOM deployment; refusing to delete")
        self.az("group", "delete", "-n", self.rg, "--yes", "--no-wait", output="none")
        print("Deletion requested. If the names are needed again, purge the soft-deleted API Management "
              "and Foundry resources afterwards.")


def label(value):
    if not re.fullmatch(r"[a-z0-9-]+", value):
        raise argparse.ArgumentTypeError("label must match [a-z0-9-]+")
    return value


def label_text(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", value):
        raise argparse.ArgumentTypeError("text must match [A-Za-z0-9_-]{3,40}")
    return value


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-directory", type=Path, default=os.environ.get("AXK2_CORE_DIRECTORY"),
                        help="private directory of the core deployment (holds target.json and the backend key); "
                             "defaults to the AXK2_CORE_DIRECTORY environment variable")
    parser.add_argument("--directory", type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("init")
    deploy = actions.add_parser("deploy")
    deploy.add_argument("stage", choices=STAGES)
    deploy.add_argument("--plan-only", action="store_true")
    actions.add_parser("key")
    actions.add_parser("subkey")
    probe = actions.add_parser("probe")
    probe.add_argument("--label", default="gateway", type=label)
    telemetry = actions.add_parser("telemetry")
    telemetry.add_argument("--minutes", type=int, default=30)
    telemetry.add_argument("--label", default="gateway", type=label)
    telemetry.add_argument("--forbid", default="SENTINEL", type=label_text,
                           help="text that must not appear in any log (a marker placed in test message bodies)")
    connections = actions.add_parser("connections")
    connections.add_argument("--label", default="gateway", type=label)
    actions.add_parser("status")
    destroy = actions.add_parser("destroy")
    destroy.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.core_directory is None:
        parser.error("--core-directory or the AXK2_CORE_DIRECTORY environment variable is required")
    core = args.core_directory.expanduser().resolve()
    directory = (args.directory or core.with_name(core.name + "-byom")).expanduser().resolve()
    if directory == ROOT or ROOT in directory.parents:
        raise ValueError("State and secrets must live outside the checkout")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise PermissionError("Private directory must be mode 0700 on a Linux filesystem, not a Windows mount")
    byom = Byom(core, directory)
    read_only = args.action in ("status", "probe", "telemetry", "connections")
    with (directory / "operation.lock").open("a") as lock:
        if not read_only:
            import fcntl  # Linux only; imported here so the helpers stay importable elsewhere
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "deploy":
            byom.deploy(args.stage, args.plan_only)
        elif args.action == "key":
            byom.put_backend_key()
        elif args.action == "subkey":
            byom.write_subscription_key()
        elif args.action == "probe":
            byom.probe(args.label)
        elif args.action == "telemetry":
            byom.telemetry(args.minutes, args.label, args.forbid)
        elif args.action == "connections":
            byom.connections(args.label)
        elif args.action == "destroy":
            byom.destroy(args.confirm)
        else:
            getattr(byom, args.action)()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, PermissionError, TimeoutError, BlockingIOError, urllib.error.URLError) as exc:
        print("ERROR:", type(exc).__name__, exc, file=sys.stderr)
        sys.exit(1)
