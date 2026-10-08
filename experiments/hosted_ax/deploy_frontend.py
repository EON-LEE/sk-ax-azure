"""Narrow approved frontend rollout with preserved known-good bundle and GPU state checks."""
import argparse
import hashlib
import io
import json
import shlex
import subprocess
import tarfile
import time
import zipfile
from pathlib import Path

import httpx

from deploy_cloud import ACCOUNT, AGENT, PROJECT, RG, api_app, az, foundry

BASELINE = "f146262aa1a7d75608af4a490b7fac1ca3b9a4ac"
APP = "axk2-a100-demo"
APP_RG = "rg-axk2-demo-208d24c1"
SITE = f"https://{APP}.azurewebsites.net"
ROOT = Path(__file__).resolve().parents[2]
CHANGES = {"demo/frontend/app.py", "demo/frontend/requirements.txt", "demo/frontend/hosted_model.py"}


def bundle(path, *, rollout):
    archive = subprocess.run(["git", "archive", BASELINE], cwd=ROOT, capture_output=True, check=True).stdout
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for item in source.getmembers():
            if not item.isfile():
                continue
            name = item.name
            dest = None
            if name.startswith("demo/frontend/"):
                dest = name.removeprefix("demo/frontend/")
            elif (name == "aml/render_job.py" or name in ("aml/jobs/demo-fp8-nd96.yml", "aml/jobs/demo-nvfp4-nd96.yml")
                  or name.startswith("aml/src/") and Path(name).suffix in (".py", ".sh", ".json")):
                dest = name
            elif (name in ("docs/report/skt_published.json", "docs/report/figures/throughput.json",
                           "evidence/a100-benchmarks.json")
                  or name.startswith("docs/report/figures/") and name.endswith(".png")):
                dest = "static/results/" + Path(name).name
            if dest:
                files[dest] = source.extractfile(item).read().replace(b"\r\n", b"\n") if not dest.endswith(".png") else source.extractfile(item).read()
    if rollout:
        changed = subprocess.run(["git", "diff", "--name-only", BASELINE, "--", "demo/frontend"],
                                 cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
        if set(changed) != CHANGES:
            raise RuntimeError("Frontend changes differ from the three explicitly allowed route files")
        for name in CHANGES:
            files[name.removeprefix("demo/frontend/")] = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as output:
        for name, data in sorted(files.items()):
            output.writestr(name, data)
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def kudu(command):
    credentials = json.loads(az("webapp", "deployment", "list-publishing-credentials",
                               "--name", APP, "--resource-group", APP_RG, "-o", "json"))
    response = httpx.post(f"https://{APP}.scm.azurewebsites.net/api/command",
                          auth=(credentials["publishingUserName"], credentials["publishingPassword"]),
                          json={"command": command, "dir": "/home"}, timeout=60, trust_env=False)
    response.raise_for_status()
    result = response.json()
    if result.get("ExitCode", 0):
        raise RuntimeError("Read-only Kudu verification command failed")
    return result["Output"].strip()


def snapshot():
    code = """import json,hashlib,pathlib
p=pathlib.Path('/home/data')
rows=[]
for path in (p/'state.json',p/'models/nvfp4/state.json'):
 d=json.loads(path.read_text())
 jobs=d.get('jobs',{})
 rows.append({'active':d.get('active'),'active_since':d.get('active_since'),
 'jobs':sorted((key,row.get('name'),row.get('source_sha')) for key,row in jobs.items())})
sources=list((p/'sources').glob('*.tgz'))
valid=bool(sources) and all(hashlib.sha256(path.read_bytes()).hexdigest()==path.stem for path in sources)
print(json.dumps({'job_binding_digest':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),
'source_archives_valid':valid,'source_archive_count':len(sources)}))"""
    return json.loads(kudu("python3 -c " + shlex.quote(code)))


def deploy(path):
    command = "wslpath -u " + shlex.quote(str(path.resolve()))
    result = subprocess.run(["wsl", "-d", "Ubuntu", "--", "bash", "-c", "tr -d '\\r' | bash"],
                            input=command + "\n", capture_output=True, text=True, check=True)
    az("webapp", "deploy", "--name", APP, "--resource-group", APP_RG, "--type", "zip",
       "--src-path", result.stdout.strip(), "--async", "false", "--timeout", "300000", "-o", "none")
    for _ in range(24):
        response = httpx.get(SITE + "/healthz", timeout=10, trust_env=False)
        if response.status_code == 200:
            return
        time.sleep(5)
    raise RuntimeError("Frontend did not become healthy; known-good bundle is preserved")


def rollout(destination):
    rollback = destination / "frontend-f146262-rollback.zip"
    candidate = destination / "frontend-hosted-trial.zip"
    bundle(rollback, rollout=False)
    expected = bundle(candidate, rollout=True)
    before = snapshot()
    if not before["source_archives_valid"]:
        raise RuntimeError("GPU source archive integrity is not verified; stop before deployment")
    account = json.loads(az("cognitiveservices", "account", "show", "--name", ACCOUNT,
                           "--resource-group", RG, "-o", "json"))
    arm = az("account", "get-access-token", "--resource", "https://management.azure.com", "-o", "json")
    tenant = json.loads(arm)["tenant"]
    agent = foundry("GET", f"/agents/{AGENT}?api-version=v1")
    from datetime import datetime
    created = datetime.fromisoformat(account["systemData"]["createdAt"].replace("Z", "+00:00")).timestamp()
    deadline = created + 3600
    if time.time() >= deadline:
        raise RuntimeError("Original 60-minute trial window has expired; no rollout made")
    application = api_app()
    az("webapp", "config", "appsettings", "set", "--name", APP, "--resource-group", APP_RG,
       "--settings", "AX_HOSTED_TENANT=" + tenant, "AX_HOSTED_AUDIENCE=" + application["appId"],
       "AX_HOSTED_CALLER=" + agent["instance_identity"]["principal_id"],
       "AX_HOSTED_DEADLINE=" + str(deadline), "-o", "none")
    print("Trial-only authentication settings configured; public demo policy unchanged.", flush=True)
    deploy(candidate)
    after = snapshot()
    if after != before:
        raise RuntimeError("GPU job/archive invariants changed; stop and coordinate before rollback")
    probe = httpx.post(SITE + "/models/nvfp4/api/hosted/chat", json={}, timeout=15, trust_env=False)
    if probe.status_code != 401:
        raise RuntimeError(f"Unauthenticated gateway probe returned {probe.status_code}, expected 401")
    report = {"baseline": BASELINE, "source_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
               capture_output=True, text=True, check=True).stdout.strip(),
              "gpu_invariants_preserved": True, "unauthenticated_status": probe.status_code,
              "deadline": deadline, "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
              "expected_changed_file_sha256": {key: expected[key] for key in ("app.py", "requirements.txt", "hosted_model.py")},
              "actual_model_calls": 0}
    (destination / "frontend-hosted-rollout.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Frontend healthy; GPU job bindings/archive checks preserved; gateway rejects unauthenticated calls.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    rollout(args.destination)
