"""Provision the approved trial agent using the existing explicitly scoped WSL login."""
import argparse
import base64
import hashlib
import io
import json
import re
import shlex
import subprocess
import time
import zipfile
from pathlib import Path
from urllib.parse import urlencode

import httpx

SUB = "ME-M365CPI74210306-eonlee-1"
RG = "rg-axk2-hosted-trial"
ACCOUNT = "axk2-hosted-trial-20261008"
PROJECT = "axk2-hosted-trial"
AGENT = "axk2-hosted"
BASE = f"https://{ACCOUNT}.services.ai.azure.com/api/projects/{PROJECT}"
GRAPH = "https://graph.microsoft.com/v1.0"
ROLE_ID = "0b5dfe5a-fdaa-4e6d-b815-40620e07bf36"


def az(*args):
    command = shlex.join(["az", *args, "--subscription", SUB, "--only-show-errors"])
    result = subprocess.run(
        ["wsl", "-d", "Ubuntu", "--", "bash", "-c", "tr -d '\\r' | bash"],
        input=f'export PATH="$HOME/.local/bin:$PATH"\n{command}\n',
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        diagnostic = re.sub(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}",
                            "[identifier]", result.stderr)
        raise RuntimeError(f"Scoped Azure CLI operation failed ({args[0]}, exit {result.returncode}); "
                           f"no retry: {diagnostic[:1200]}")
    return result.stdout.strip()


def graph(method, path, *, body=None):
    args = ["rest", "--method", method, "--resource", "https://graph.microsoft.com", "--url", GRAPH + path]
    if body is not None:
        args.extend(["--body", json.dumps(body)])
    value = az(*args, "-o", "json")
    return json.loads(value) if value else None


def lookup(path, predicate, select):
    values = graph("GET", path + "?" + urlencode({"$filter": predicate, "$select": select}))["value"]
    if len(values) != 1:
        raise RuntimeError("Expected exactly one approved trial identity")
    return values[0]


def api_app():
    return lookup("/applications", "displayName eq 'axk2-hosted-model-api-trial'", "id,appId")


def role(principal, name, scope, principal_type):
    definitions = json.loads(az("role", "definition", "list", "--name", name, "-o", "json"))
    if len(definitions) != 1:
        raise RuntimeError(f"Expected one built-in role: {name}")
    az("role", "assignment", "create", "--assignee-object-id", principal,
       "--assignee-principal-type", principal_type,
       "--role", definitions[0]["name"], "--scope", scope, "-o", "none")


def token(resource):
    return az("account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv")


def foundry(method, path, *, payload=None, files=None, headers=None):
    auth = {"Authorization": "Bearer " + token("https://ai.azure.com"), **(headers or {})}
    response = httpx.request(method, BASE + path, json=payload, files=files, headers=auth,
                             timeout=120, follow_redirects=False, trust_env=False)
    if response.is_error:
        try:
            code = response.json().get("error", {}).get("code", "unknown")
        except (ValueError, AttributeError):
            code = "non_json_error"
        raise RuntimeError(f"Foundry request failed: HTTP {response.status_code}, code={code}; no retry")
    return response.json() if response.content else None


def prepare():
    project = json.loads(az("cognitiveservices", "account", "project", "show", "--name", ACCOUNT,
                           "--resource-group", RG, "--project-name", PROJECT, "-o", "json"))
    account_scope = az("cognitiveservices", "account", "show", "--name", ACCOUNT,
                       "--resource-group", RG, "--query", "id", "-o", "tsv")
    arm = token("https://management.azure.com")
    encoded_claims = arm.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(encoded_claims + "=" * (-len(encoded_claims) % 4)))
    role(claims["oid"], "Foundry Project Manager", project["id"], "User")
    role(project["identity"]["principalId"], "Foundry User", account_scope, "ServicePrincipal")
    root = Path(__file__).resolve().parent
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("main.py", (root / "main.py").read_bytes().replace(b"\r\n", b"\n"))
        output.writestr("requirements.txt", (root / "requirements-cloud.txt").read_bytes().replace(b"\r\n", b"\n"))
    data = archive.getvalue()
    definition = {
        "kind": "hosted", "cpu": "1", "memory": "2Gi",
        "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}],
        "code_configuration": {"runtime": "python_3_13", "entry_point": ["python", "main.py"],
                               "dependency_resolution": "remote_build"},
        "session_configuration": {"idle_timeout_seconds": 120},
        "environment_variables": {
            "AX_HOSTED_RUNTIME": "1",
            "AX_CHAT_ENDPOINT": "https://axk2-a100-demo.azurewebsites.net/models/nvfp4/api/hosted/chat",
            "AX_GATEWAY_SCOPE": "api://" + api_app()["appId"] + "/.default",
            "OTEL_SDK_DISABLED": "true",
        },
    }
    existing = foundry("GET", "/agents?api-version=v1")
    if any(row["name"] == AGENT for row in existing.get("data", [])):
        raise RuntimeError("Trial agent already exists; review it rather than automatically create a new version")
    created = foundry("POST", "/agents?api-version=v1", files={
        "metadata": (None, json.dumps({"description": "Bounded A.X-only Hosted trial", "definition": definition}),
                     "application/json"),
        "code": ("axk2-hosted.zip", data, "application/zip"),
    }, headers={"x-ms-agent-name": AGENT, "x-ms-code-zip-sha256": hashlib.sha256(data).hexdigest()})
    print("Source ZIP accepted; no model call made.")
    latest = created.get("versions", {}).get("latest") or created
    version = str(latest["version"])
    for _ in range(40):
        status = foundry("GET", f"/agents/{AGENT}/versions/{version}?api-version=v1")
        print("Hosted version state:", status["status"], flush=True)
        if status["status"] == "active":
            print("Hosted version active; inference has not been tested.")
            return
        if status["status"] == "failed":
            error = status.get("error") or {}
            raise RuntimeError(f"Hosted provisioning failed: {error.get('code', 'unknown')}")
        time.sleep(10)
    raise TimeoutError("Hosted provisioning deadline reached; no additional agent created")


def assign():
    agent = foundry("GET", f"/agents/{AGENT}?api-version=v1")
    identity = agent.get("instance_identity", {}).get("principal_id")
    if not isinstance(identity, str):
        raise RuntimeError("Agent identity field requires inspection; no app-role assignment made")
    app = api_app()
    resource = lookup("/servicePrincipals", f"appId eq '{app['appId']}'", "id")
    graph("POST", f"/servicePrincipals/{identity}/appRoleAssignments",
          body={"principalId": identity, "resourceId": resource["id"], "appRoleId": ROLE_ID})
    print("Model.Invoke assigned only to the trial Hosted identity; no client secret created.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "assign", "disable"))
    phase = parser.parse_args().phase
    if phase == "prepare":
        prepare()
    elif phase == "assign":
        assign()
    else:
        foundry("POST", f"/agents/{AGENT}:disable?api-version=v1")
        print("Trial agent endpoint disabled; no resources deleted.")
