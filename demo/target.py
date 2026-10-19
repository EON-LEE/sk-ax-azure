"""Isolated subscription deployment and bounded GPU operations (run under WSL/Linux).

Configuration, plans, state and credentials stay in a private per-target directory.
No account switching, source-resource discovery, resource deletion or secret output.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "demo" / "infra"
TERMINAL = {"Canceled", "Completed", "Failed"}


def write_private(path, value):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
    temporary.replace(path)


def validate_config(config):
    if not re.fullmatch(r"[a-z][a-z0-9]{5,15}", config["name"]):
        raise ValueError("Invalid target name")
    if not re.fullmatch(r"[0-9a-f-]{36}", config["subscription_id"]):
        raise ValueError("An explicit target subscription is required")
    if config.get("resource_group", "rg-" + config["name"]) != "rg-" + config["name"]:
        raise ValueError("Resource group must belong to this deployment")


def safe_plan(plan, subscription, resource_group):
    """Reject deletes, foreign scope and an accidental source-state import."""
    prefix = f"/subscriptions/{subscription}/resourcegroups/{resource_group}".lower()
    changes = plan.get("resource_changes", [])
    if not changes:
        return
    for item in changes:
        change = item["change"]
        if "delete" in change["actions"]:
            raise ValueError("Deletion/replacement is not allowed by target deployment")
        for side in ("before", "after"):
            value = change.get(side) or {}
            resource_id = value.get("id")
            if resource_id and resource_id.startswith("/subscriptions/"):
                normalized = resource_id.lower()
                if normalized != prefix and not normalized.startswith(prefix + "/"):
                    raise ValueError("Terraform plan touches a resource outside the target RG")
            if value.get("resource_group_name", resource_group).lower() != resource_group.lower():
                raise ValueError("Terraform plan references a foreign resource group")


def bundle(root, destination):
    front = root / "demo/frontend"
    files = {p.relative_to(front).as_posix(): p for p in front.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts
             and not any(part.startswith(".env") for part in p.relative_to(front).parts)}
    files["aml/render_job.py"] = root / "aml/render_job.py"
    for name in ("demo-fp8-nd96.yml", "demo-nvfp4-nd96.yml"):
        files["aml/jobs/" + name] = root / "aml/jobs" / name
    for path in (root / "aml/src").iterdir():
        if path.is_file() and path.suffix in {".py", ".sh", ".json"}:
            files["aml/src/" + path.name] = path
    report = root / "docs/report"
    for path in (report / "skt_published.json", report / "figures/throughput.json",
                 *sorted((report / "figures").glob("*.png")), root / "evidence/a100-benchmarks.json"):
        if path.is_file():
            files["static/results/" + path.name] = path
    expected = {}
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            data = path.read_bytes()
            if path.suffix != ".png":
                data = data.replace(b"\r\n", b"\n")
            archive.writestr(name, data)
            expected[name] = hashlib.sha256(data).hexdigest()
    return expected


class Target:
    def __init__(self, directory):
        self.directory = directory
        self.config = json.loads((directory / "target.json").read_text())
        validate_config(self.config)
        self.sub = self.config["subscription_id"]
        self.name = self.config["name"]
        self.rg = "rg-" + self.name
        self.workspace = self.name + "-mlw"
        self.url = "https://" + self.name + ".azurewebsites.net"
        self.env = dict(os.environ, ARM_SUBSCRIPTION_ID=self.sub,
                        ARM_TENANT_ID=self.config["tenant_id"],
                        TF_DATA_DIR=str(directory / "terraform"))
        self.arm = (f"https://management.azure.com/subscriptions/{self.sub}/resourceGroups/{self.rg}"
                    f"/providers/Microsoft.MachineLearningServices/workspaces/{self.workspace}")
        self.token = None
        self.admin = None

    def az(self, *args):
        run = subprocess.run(["az", *args, "--subscription", self.sub, "-o", "json"],
                             env=self.env, capture_output=True, text=True)
        if run.returncode:
            # Error output can contain request bodies/settings. Keep full diagnostics private.
            (self.directory / "azure-error.log").write_text(run.stderr)
            raise RuntimeError(f"Azure CLI {args[0]} {args[1]} failed; see private azure-error.log")
        return json.loads(run.stdout) if run.stdout.strip() else {}

    def terraform(self, *args, capture=False):
        run = subprocess.run(["terraform", *args], cwd=INFRA, env=self.env, text=True,
                             capture_output=True)
        if not capture:
            (self.directory / ("terraform-" + args[0] + ".log")).write_text(run.stdout + run.stderr)
        if run.returncode:
            raise RuntimeError("Terraform failed; inspect private terraform-" + args[0] + ".log")
        return run.stdout

    def request(self, url, body=None, cloud=False, raw=False):
        if cloud:
            if self.token is None:
                self.token = self.az("account", "get-access-token",
                                     "--resource", "https://management.azure.com/")["accessToken"]
            credential = self.token
        else:
            if self.admin is None:
                self.admin = json.loads((self.directory / "secrets.json").read_text())["admin"]
            credential = self.admin
        headers = {"Authorization": "Bearer " + credential}
        if body is not None:
            headers["Content-Type"] = "application/zip" if raw else "application/json"
        request = urllib.request.Request(url, data=body if raw else
                                         json.dumps(body).encode() if body is not None else None,
                                         headers=headers)
        with urllib.request.urlopen(request, timeout=90) as response:
            data = response.read()
            return json.loads(data) if data else {}

    def kudu(self, code):
        command = 'python3 -c "exec(__import__(\'base64\').b64decode(\'' + base64.b64encode(code.encode()).decode() + '\'))"'
        result = self.request(f"https://{self.name}.scm.azurewebsites.net/api/command",
                              {"command": command, "dir": "/home/site/wwwroot"}, cloud=True)
        if result["ExitCode"]:
            raise RuntimeError("Kudu read-only verification failed")
        return json.loads(result["Output"])

    def provision(self, apply_plan=True):
        account = self.az("account", "show")
        if account["id"] != self.sub or account["tenantId"] != self.config["tenant_id"]:
            raise ValueError("Cached target identity does not match the isolated configuration")
        for provider in ("Microsoft.Web", "Microsoft.Storage", "Microsoft.KeyVault",
                         "Microsoft.Insights", "Microsoft.OperationalInsights", "Microsoft.MachineLearningServices"):
            state = self.az("provider", "show", "--namespace", provider)["registrationState"]
            if state != "Registered":
                if not apply_plan:
                    raise ValueError("Required target provider is unregistered; provision first")
                self.az("provider", "register", "--namespace", provider, "--wait")
        self.terraform("init", "-input=false", "-reconfigure",
                       "-backend-config=path=" + str(self.directory / "terraform.tfstate"))
        self.terraform("fmt", "-check")
        self.terraform("validate")
        variables = self.directory / "input.tfvars.json"
        write_private(variables, {k: self.config[k] for k in
                      ("subscription_id", "tenant_id", "name", "web_location", "gpu_location")})
        plan = self.directory / "target.tfplan"
        self.terraform("plan", "-input=false", "-lock-timeout=60s",
                       "-var-file=" + str(variables), "-out=" + str(plan))
        parsed = json.loads(self.terraform("show", "-json", str(plan), capture=True))
        safe_plan(parsed, self.sub, self.rg)
        proof = {"target_only": True, "source_changes": 0, "resource_changes": [
            {"address": item["address"], "actions": item["change"]["actions"]}
            for item in parsed.get("resource_changes", [])], "web_sku": "B1", "gpu_min": 0, "gpu_max": 3}
        write_private(self.directory / "safe-plan.json", proof)
        print(json.dumps(proof, indent=2), flush=True)
        if apply_plan:
            self.terraform("apply", "-input=false", "-lock-timeout=60s", str(plan))
            print("Applied target-only plan. B1 capacity 1; GPU min=0/max=3. No GPU jobs submitted.")
        else:
            print("Target-only plan saved; no apply or cloud mutation performed.")

    def deploy(self):
        if (self.directory / "frontend-expected.json").exists():
            if not self.status()["effective_zero"]:
                raise ValueError("Target redeployment requires verified GPU OFF; run off first")
        import importlib.util
        spec = importlib.util.spec_from_file_location("ax_store", ROOT / "demo/frontend/store.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        path = self.directory / "secrets.json"
        if path.exists():
            secure = json.loads(path.read_text())
        else:
            secure = {"admin": secrets.token_urlsafe(32), "demo": secrets.token_urlsafe(32),
                      "session": secrets.token_urlsafe(48)}
            write_private(path, secure)
        settings = [
            "AXK2_SESSION_SECRET=" + secure["session"],
            "AXK2_ADMIN_PASSWORD_HASH=" + module.hash_password(secure["admin"]),
            "AXK2_DEMO_PASSWORD_HASH=" + module.hash_password(secure["demo"]),
        ]
        # Secrets go through a protected file, never argv, stdout, Terraform input or git.
        with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, suffix=".json") as stream:
            json.dump(dict(item.split("=", 1) for item in settings), stream)
            stream.flush()
            self.az("webapp", "config", "appsettings", "set", "-g", self.rg, "-n", self.name,
                    "--settings", "@" + stream.name)
        archive = self.directory / "frontend.zip"
        expected = bundle(ROOT, archive)
        write_private(self.directory / "frontend-expected.json", expected)
        deployment_url = f"https://{self.name}.scm.azurewebsites.net/api/deployments/latest"
        try:
            previous_id = self.request(deployment_url, cloud=True).get("id")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            previous_id = None
        # One upload only. If transport fails, do NOT automatically submit another deployment.
        self.request(f"https://{self.name}.scm.azurewebsites.net/api/zipdeploy?isAsync=true",
                     archive.read_bytes(), cloud=True, raw=True)
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            time.sleep(20)
            value = self.request(deployment_url, cloud=True)
            if value.get("id") != previous_id and value.get("complete"):
                if value.get("status") != 4:
                    raise RuntimeError("OneDeploy failed; inspect existing deployment, do not repeat blindly")
                write_private(self.directory / "deployment-proof.json", {
                    k: value.get(k) for k in ("id", "status", "complete", "active", "end_time")})
                break
        else:
            raise TimeoutError("Deployment not verified; inspect the in-flight deployment before retrying")
        self.verify_archive(expected)
        print(self.url)

    def verify_archive(self, expected):
        code = """
import hashlib,json,subprocess,tarfile
from pathlib import Path
expected=EXPECTED
found={}
packages=set()
p=subprocess.Popen(['zstd','-dc','/home/site/wwwroot/output.tar.zst'],stdout=subprocess.PIPE)
with tarfile.open(fileobj=p.stdout,mode='r|') as archive:
 for item in archive:
  name=item.name.removeprefix('./')
  if name in expected and item.isfile():
   found[name]=hashlib.sha256(archive.extractfile(item).read()).hexdigest()
  for package in ('uvicorn','fastapi','agent_framework'):
   if name.startswith('antenv/') and name.endswith('/site-packages/'+package+'/__init__.py'):
    packages.add(package)
assert p.wait()==0
print(json.dumps({'expected':len(expected),'matched':sum(found.get(n)==h for n,h in expected.items()),
 'oryx_manifest':Path('/home/site/wwwroot/oryx-manifest.toml').is_file(),'runtime_packages':sorted(packages)}))
""".replace("EXPECTED", repr(expected))
        result = self.kudu(code)
        write_private(self.directory / "persisted-proof.json", result)
        if (result["expected"] != result["matched"] or not result["oryx_manifest"]
                or set(result["runtime_packages"]) != {"uvicorn", "fastapi", "agent_framework"}):
            raise ValueError("Persisted frontend source or Oryx runtime is incomplete")

    def power(self, wanted):
        errors = []
        for prefix in ("", "/models/nvfp4"):
            try:
                value = self.request(self.url + prefix + "/api/admin/power", {"state": wanted})
                if value["status"]["desired"] != wanted:
                    raise ValueError("Power request was not durably accepted")
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                errors.append(type(exc).__name__)
        if errors:
            raise RuntimeError("Profile power operation failed: " + ", ".join(errors))

    def status(self):
        profiles = []
        for prefix in ("", "/models/nvfp4"):
            value = self.request(self.url + prefix + "/api/admin/state")
            profiles.append({"model": "fp8" if not prefix else "nvfp4",
                             "desired": value["status"]["desired"],
                             "power": value["status"]["power"], "jobs": len(value["jobs"])})
        compute = self.request(self.arm + "/computes/a100-nd96-lp?api-version=2024-04-01", cloud=True)
        properties = compute["properties"]["properties"]
        nodes = self.request(self.arm + "/computes/a100-nd96-lp/listNodes?api-version=2024-04-01",
                             {}, cloud=True)
        records = nodes.get("nodes", nodes.get("value"))
        if not isinstance(records, list):
            raise ValueError("Actual node enumeration unavailable")
        jobs, url = [], self.arm + "/jobs?api-version=2024-04-01"
        while url:
            page = self.request(url, cloud=True)
            for item in page.get("value", []):
                p = item["properties"]
                if str(p.get("computeId", "")).split("/")[-1] == "a100-nd96-lp" and p["status"] not in TERMINAL:
                    jobs.append({"name": item["name"], "status": p["status"]})
            url = page.get("nextLink")
        result = {"profiles": profiles, "current": properties["currentNodeCount"],
                  "target": properties["targetNodeCount"], "actual_nodes": len(records), "jobs": jobs,
                  "observed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        result["effective_zero"] = (result["current"] == result["target"] == result["actual_nodes"] == 0
                                    and not jobs and all(p["desired"] == "off" and p["jobs"] == 0 for p in profiles))
        if result["effective_zero"]:
            persisted = self.kudu("""
import json
from pathlib import Path
out={}
for model,path in [('fp8','/home/data/state.json'),('nvfp4','/home/data/models/nvfp4/state.json')]:
 value=json.loads(Path(path).read_text())
 out[model]={'desired':value['desired'],'active':value['active'],
  'jobs':len(value['jobs']),'restart':value['restart']}
print(json.dumps(out))
""")
            result["persisted"] = persisted
            result["effective_zero"] = all(p["desired"] == "off" and p["active"] is None
                                            and p["jobs"] == 0 and p["restart"] is False
                                            for p in persisted.values())
        write_private(self.directory / "status-proof.json", result)
        return result

    def off(self):
        self.power("off")
        deadline = time.monotonic() + 1200
        consecutive = 0
        while time.monotonic() < deadline:
            result = self.status()
            consecutive = consecutive + 1 if result["effective_zero"] else 0
            if consecutive == 2:
                print(json.dumps(result, indent=2))
                return
            time.sleep(30)
        raise TimeoutError("OFF persisted but physical node zero not verified; inspect exact target compute")

    def run(self, seconds):
        if not 60 <= seconds <= 7200:
            raise ValueError("A bounded lease of 60-7200 seconds is required")
        # This process owns the lease: keep it attached. SIGKILL/host loss still needs `off`.
        try:
            self.power("on")
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                time.sleep(min(30, max(0, deadline - time.monotonic())))
        finally:
            self.off()

    def on(self, keep_on):
        if not keep_on:
            raise ValueError("Unbounded operation requires explicit --keep-on authorization")
        write_private(self.directory / "gpu-start.json", {
            "requested_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "profiles": ["fp8", "nvfp4"], "max_nodes": 3,
            "keep_on": True, "automatic_shutdown": False,
        })
        self.power("on")
        print("Both target profiles ON; automatic shutdown disabled by explicit operator choice.")

    def webiq(self):
        key = os.environ.get("AXK2_WEBIQ_API_KEY")
        endpoint = os.environ.get("AXK2_WEBIQ_ENDPOINT")
        if not key or not endpoint or not endpoint.startswith("https://"):
            raise ValueError("Supply authorized target-specific AXK2_WEBIQ_API_KEY and HTTPS AXK2_WEBIQ_ENDPOINT")
        with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, suffix=".json") as stream:
            json.dump({"AXK2_WEBIQ_API_KEY": key, "AXK2_WEBIQ_ENDPOINT": endpoint,
                       "AXK2_WEBIQ_ACCESS_APPROVED": "1"}, stream)
            stream.flush()
            self.az("webapp", "config", "appsettings", "set", "-g", self.rg, "-n", self.name,
                    "--settings", "@" + stream.name)
        print("Target-specific Web IQ settings saved; verify capabilities without a search/model call.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("action", choices=("init", "plan", "provision", "deploy", "status", "off", "run", "on", "webiq"))
    parser.add_argument("--subscription")
    parser.add_argument("--name")
    parser.add_argument("--seconds", type=int)
    parser.add_argument("--keep-on", action="store_true")
    args = parser.parse_args()
    directory = args.directory.expanduser().resolve()
    if directory == ROOT or ROOT in directory.parents:
        raise ValueError("Target state/secrets directory must be outside the checkout")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise PermissionError("Target directory must have mode 0700 on a Linux filesystem, not a Windows mount")
    with (directory / "operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "init":
            if (directory / "target.json").exists():
                raise FileExistsError("Target configuration exists; refusing to rebind its state")
            if not args.subscription or not args.name:
                raise ValueError("init requires --subscription and --name")
            config = {"subscription_id": args.subscription, "name": args.name,
                      "web_location": "koreacentral", "gpu_location": "uksouth"}
            validate_config(config)
            result = subprocess.run(["az", "account", "show", "--subscription", args.subscription, "-o", "json"],
                                    capture_output=True, text=True, check=True)
            account = json.loads(result.stdout)
            if account["id"] != args.subscription or account["state"] != "Enabled":
                raise ValueError("Target subscription is not enabled")
            config["tenant_id"] = account["tenantId"]
            write_private(directory / "target.json", config)
            print("Isolated target configuration initialized; no cloud changes.")
            return
        target = Target(directory)
        if args.action == "status":
            print(json.dumps(target.status(), indent=2))
        elif args.action == "run":
            signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
            target.run(args.seconds or 0)
        elif args.action == "on":
            target.on(args.keep_on)
        elif args.action == "plan":
            target.provision(apply_plan=False)
        else:
            getattr(target, args.action)()


if __name__ == "__main__":
    main()
