"""Foundry BYOM agent scripts: config, redaction, run flow and evidence checks with a mocked SDK; no Azure calls."""
import contextlib
import enum
import http.server
import importlib.util
import inspect
import io
import itertools
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "demo" / "foundry-byom" / "agent"


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"foundry_byom_{name}", AGENT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(AGENT_DIR))
try:
    import byom_common as common
    create_agent = load_script("create_agent")
    inspect_connections = load_script("inspect_connections")
    run_prompt = load_script("run_prompt")
    run_series = load_script("run_series")
    collect_evidence = load_script("collect_evidence")
finally:
    sys.path.remove(str(AGENT_DIR))

try:
    from azure.ai.projects import AIProjectClient, models as sdk_models
    from azure.ai.projects.operations import AgentsOperations, ConnectionsOperations
    from azure.core.credentials import AccessToken
    from openai.resources.conversations.items import Items as ConversationItems
    from openai.resources.responses import Responses as OpenAIResponses
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

ENDPOINT = "https://example-resource.services.ai.azure.com/api/projects/demo-project"
CONNECTION = "demo-conn"
MODEL_REF = "demo-conn/ax-k2-nvfp4"
CONVERSATION = "conv_test"
GUID = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
DOC_URL = "https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/bring-your-own-model"
MCP_TOOL_RECORD = {"type": "mcp", "server_label": "microsoft_learn", "server_url": "https://learn.microsoft.com/api/mcp",
                   "allowed_tools": ["microsoft_docs_search"], "require_approval": "never"}
LONG_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f6071"


class ApiError(Exception):
    def __init__(self, status_code, message="request failed", code=None, api_type=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.type = api_type


class Model:
    """Non-mapping object that serializes like an SDK model."""

    def __init__(self, **data):
        self.data = data

    def model_dump(self, **_kwargs):
        return dict(self.data)


class FakeTool:
    kind = ""

    def __init__(self, **fields):
        self.fields = fields

    def model_dump(self, **_kwargs):
        return {"type": self.kind, **self.fields}


class FakeMCPTool(FakeTool):
    kind = "mcp"


class FakeFunctionTool(FakeTool):
    kind = "function"


class FakePromptAgentDefinition:
    def __init__(self, **fields):
        self.fields = fields

    def model_dump(self, **_kwargs):
        fields = dict(self.fields)
        fields["tools"] = [tool.model_dump() for tool in fields.get("tools", [])]
        return {"kind": "prompt", **fields}


FAKE_MODELS = types.SimpleNamespace(MCPTool=FakeMCPTool, FunctionTool=FakeFunctionTool,
                                    PromptAgentDefinition=FakePromptAgentDefinition)


class FakeAgents:
    def __init__(self, latest=None, get_error=None):
        self.latest = latest
        self.get_error = get_error
        self.versions = {latest["version"]: latest["definition"]} if latest else {}
        self.created = []
        self.deleted = []

    def get(self, agent_name):
        if self.get_error:
            raise self.get_error
        if self.latest is None:
            raise ApiError(404, "agent not found")
        return Model(name=agent_name, versions={"latest": self.latest})

    def get_version(self, agent_name, agent_version):
        if agent_version not in self.versions:
            raise ApiError(404, "version not found")
        return Model(name=agent_name, version=agent_version, definition=self.versions[agent_version])

    def create_version(self, agent_name, definition):
        self.created.append((agent_name, definition))
        version, stored = str(len(self.created)), definition.model_dump()
        self.latest = {"version": version, "definition": stored}
        self.versions[version] = stored
        return Model(name=agent_name, version=version, definition=stored)

    def delete(self, agent_name):
        self.deleted.append(agent_name)


class FakeConnections:
    """Connection operations with the real signatures: get(name, *, include_credentials=False)."""

    def __init__(self, items=(), get_errors=None, list_error=None):
        self.items = list(items)
        self.get_errors = dict(get_errors or {})
        self.list_error = list_error
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        if self.list_error:
            raise self.list_error
        return iter(self.items)

    def get(self, name, **kwargs):
        self.calls.append(("get", name, kwargs))
        if name in self.get_errors:
            raise self.get_errors[name]
        for item in self.items:
            if common.to_plain(item).get("name") == name:
                return item
        raise ApiError(404, "connection not found")


class FakeResponses:
    def __init__(self, replies=(), stored=None):
        self.replies = list(replies)
        self.stored = dict(stored or {})
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def retrieve(self, response_id):
        reply = self.stored[response_id]
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeItems:
    def __init__(self, items=()):
        self.items = list(items)
        self.calls = []

    def list(self, conversation_id, order=None, limit=None):
        self.calls.append({"conversation_id": conversation_id, "order": order, "limit": limit})
        return iter(self.items)


class FakeConversations:
    def __init__(self, items=()):
        self.items = FakeItems(items)
        self.created = 0
        self.deleted = []

    def create(self):
        self.created += 1
        return Model(id=CONVERSATION)

    def delete(self, conversation_id):
        self.deleted.append(conversation_id)


def fake_clients(replies=(), agents=None, stored=None, items=(), connections=None):
    openai = types.SimpleNamespace(responses=FakeResponses(replies, stored), conversations=FakeConversations(items))
    project = types.SimpleNamespace(agents=agents or FakeAgents(), connections=connections or FakeConnections())
    return common.Clients(project=project, openai=openai, models=FAKE_MODELS)


def message(text, role="assistant"):
    return {"type": "message", "role": role, "content": [{"type": "output_text", "text": text}]}


def list_tools(names=("microsoft_docs_search",), error=None):
    return {"type": "mcp_list_tools", "server_label": "microsoft_learn", "tools": [{"name": name} for name in names],
            "error": error}


def mcp_call(output, name="microsoft_docs_search", status="completed", error=None):
    return {"type": "mcp_call", "server_label": "microsoft_learn", "name": name, "arguments": '{"query": "BYOM"}',
            "output": output, "status": status, "error": error}


def function_call(call_id="call_1", name="lookup_demo_stock", arguments='{"sku": "AX-100"}'):
    return {"type": "function_call", "name": name, "arguments": arguments, "call_id": call_id, "status": "completed"}


def approval_request(request_id="mcpr_1", name="microsoft_docs_search"):
    return {"type": "mcp_approval_request", "id": request_id, "server_label": "microsoft_learn", "name": name,
            "arguments": '{"query": "BYOM"}'}


def response(output, response_id="resp_1", status="completed", version="3", **extra):
    data = {"id": response_id, "status": status, "model": "fake-backend-model",
            "agent_reference": {"type": "agent_reference", "name": "byom-demo-agent", "version": version},
            "conversation": {"id": CONVERSATION}, "output": output,
            "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
                      "output_tokens_details": {"reasoning_tokens": 0}}}
    data.update(extra)
    return Model(**data)


def stored_definition(model=MODEL_REF, tools=()):
    return {"kind": "prompt", "model": model, "instructions": common.DEFAULT_INSTRUCTIONS, "temperature": 1.0,
            "tools": list(tools)}


def connection_data(name, category="ApiManagement", credential="ApiKey", in_path="false", models=("ax-k2-nvfp4",),
                    default=False, **metadata):
    """Connection as the service returns it: wire keys, ARM id and gateway host included on purpose."""
    listed = [{"name": model, "properties": {"model": {"name": model, "version": "1", "format": "OpenAI"}}}
              for model in models]
    return {"name": name, "id": f"/subscriptions/{GUID}/resourceGroups/rg-demo/providers/x/connections/{name}",
            "type": category, "target": "https://gateway.example.invalid/demo/v1", "isDefault": default,
            "credentials": {"type": credential},
            "metadata": {"deploymentInPath": in_path, "inferenceAPIVersion": "2024-10-21",
                         "staticModels": json.dumps(listed), **metadata}}


def connection_item(name, **options):
    return Model(**connection_data(name, **options))


class ScriptCase(unittest.TestCase):
    """Isolates environment variables, runtime secrets and the evidence directory for every test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        keep = {name: value for name, value in os.environ.items()
                if not name.startswith(("FOUNDRY_", "BYOM_")) and not common.SECRET_ENV.search(name)}
        for patcher in (patch.dict(os.environ, keep, clear=True),
                        patch.object(common, "_runtime_secrets", set()),
                        patch.object(common, "EVIDENCE_DIR", Path(self.tmp.name))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_script(self, module, argv, clients=None, live=True, **options):
        arguments = (["--project-endpoint", ENDPOINT] if live else []) + list(argv)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.main(arguments, clients=clients, **options)
        return code, out.getvalue(), err.getvalue()

    def read_saved(self, name):
        return json.loads((Path(self.tmp.name) / name).read_text(encoding="utf-8"))

    def spec_for(self, *argv, dry_run=True):
        args = create_agent.build_parser().parse_args((["--dry-run"] if dry_run else []) + list(argv))
        return common.definition_spec(args)


class ConfigTests(ScriptCase):
    def test_model_reference_joins_connection_and_model(self):
        self.assertEqual(self.spec_for("--connection", CONNECTION)["model"], MODEL_REF)
        self.assertEqual(self.spec_for("--connection", CONNECTION, "--model", "ax-k2-fp8")["model"],
                         "demo-conn/ax-k2-fp8")

    def test_connection_is_required_unless_dry_run(self):
        self.assertEqual(self.spec_for()["model"], "<connection>/ax-k2-nvfp4")
        with self.assertRaises(common.ConfigError):
            self.spec_for(dry_run=False)

    def test_invalid_connection_and_model_names_are_rejected(self):
        for argv in (["--connection", "a/b"], ["--connection", "demo-conn", "--model", "x y"],
                     ["--connection", "demo-conn", "--model", "../x"]):
            with self.subTest(argv=argv), self.assertRaises(common.ConfigError):
                self.spec_for(*argv)

    def test_agent_name_rules(self):
        for name in ("byom-demo-agent", "A", "a1", "x" * 63):
            self.assertEqual(common.check_agent_name(name), name)
        for name in ("", "-lead", "trail-", "under_score", "has space", "x" * 64, None):
            with self.subTest(name=name), self.assertRaises(common.ConfigError):
                common.check_agent_name(name)

    def test_project_endpoint_must_be_a_foundry_project_url(self):
        self.assertEqual(common.check_endpoint(ENDPOINT + "/"), ENDPOINT)
        for value in (None, "", "http://example-resource.services.ai.azure.com/api/projects/p",
                      "https://example.invalid/api/projects/p",
                      "https://example-resource.services.ai.azure.com.example.invalid/api/projects/p",
                      "https://example-resource.services.ai.azure.com/",
                      "https://example-resource.services.ai.azure.com/api/projects/p?key=1",
                      "https://user:pw@example-resource.services.ai.azure.com/api/projects/p"):
            with self.subTest(value=value), self.assertRaises(common.ConfigError):
                common.check_endpoint(value)

    def test_project_endpoint_rejects_whitespace_and_control_characters(self):
        for tail in ("\r", "\n", "\r\n", "\t", " ", "\x00", "\x7f", "\u00a0", "\u200b", "\ud55c", "/\r"):
            with self.subTest(tail=repr(tail)), self.assertRaises(common.ConfigError) as caught:
                common.check_endpoint(ENDPOINT + tail)
            self.assertNotIn(ENDPOINT, str(caught.exception))
        with self.assertRaises(common.ConfigError):
            common.check_endpoint(ENDPOINT.replace("/api/", "/api/\r"))
        self.assertEqual(common.check_endpoint(ENDPOINT + "/"), ENDPOINT)

    def test_tool_specs(self):
        self.assertEqual(self.spec_for("--connection", CONNECTION)["tools"], [])
        function_spec = self.spec_for("--connection", CONNECTION, "--tool", "function")
        self.assertEqual(function_spec["tools"][0]["name"], "lookup_demo_stock")
        function_spec["tools"][0]["name"] = "changed"
        self.assertEqual(common.DEMO_FUNCTION["name"], "lookup_demo_stock")
        self.assertEqual(self.spec_for("--connection", CONNECTION, "--tool", "mcp")["tools"], [MCP_TOOL_RECORD])

    def test_specs_never_carry_headers(self):
        spec = self.spec_for("--connection", CONNECTION, "--tool", "mcp", "--mcp-connection", "demo-store")
        self.assertNotIn("headers", spec["tools"][0])
        self.assertNotIn("authorization", spec["tools"][0])
        self.assertEqual(spec["tools"][0]["project_connection_id"], "demo-store")

    def test_web_iq_preset_needs_an_explicit_url(self):
        base = ("--connection", CONNECTION, "--tool", "mcp", "--mcp-preset", "webiq")
        with self.assertRaises(common.ConfigError):
            self.spec_for(*base)
        tool = self.spec_for(*base, "--mcp-url", "https://mcp.example.invalid/v1")["tools"][0]
        self.assertEqual((tool["server_label"], tool["allowed_tools"], tool["server_url"]),
                         ("web_iq", ["web"], "https://mcp.example.invalid/v1"))

    def test_mcp_url_must_be_plain_https(self):
        for url in ("http://mcp.example.invalid/x", "https://mcp.example.invalid/x?key=abc",
                    "https://user:pw@mcp.example.invalid/x", "https://mcp.example.invalid/x#frag"):
            with self.subTest(url=url), self.assertRaises(common.ConfigError):
                self.spec_for("--connection", CONNECTION, "--tool", "mcp", "--mcp-url", url)

    def test_mcp_url_rejects_whitespace_and_control_characters(self):
        for tail in ("\r", "\n", "\t", " ", "\x00"):
            with self.subTest(tail=repr(tail)), self.assertRaises(common.ConfigError):
                self.spec_for("--connection", CONNECTION, "--tool", "mcp", "--mcp-url",
                              MCP_TOOL_RECORD["server_url"] + tail)

    def test_invalid_mcp_names_are_rejected(self):
        for argv in (["--mcp-label", "bad label"], ["--mcp-allowed-tools", "ok,bad tool"],
                     ["--mcp-connection", "bad/connection"]):
            with self.subTest(argv=argv), self.assertRaises(common.ConfigError):
                self.spec_for("--connection", CONNECTION, "--tool", "mcp", *argv)

    def test_instructions_file_and_temperature(self):
        path = Path(self.tmp.name) / "instructions.txt"
        path.write_text("  한국어로 답한다.  \n", encoding="utf-8")
        spec = self.spec_for("--connection", CONNECTION, "--instructions-file", str(path), "--temperature", "0.2")
        self.assertEqual((spec["instructions"], spec["temperature"]), ("한국어로 답한다.", 0.2))
        self.assertNotIn("temperature", self.spec_for("--connection", CONNECTION))
        with self.assertRaises(common.ConfigError) as caught:
            self.spec_for("--instructions-file", str(Path(self.tmp.name) / "missing.txt"))
        self.assertNotIn(self.tmp.name, str(caught.exception))

    def test_sdk_definition_uses_the_given_models(self):
        spec = self.spec_for("--connection", CONNECTION, "--tool", "mcp")
        definition = common.to_sdk_definition(spec, FAKE_MODELS)
        self.assertEqual(definition.fields["model"], MODEL_REF)
        self.assertEqual([type(tool) for tool in definition.fields["tools"]], [FakeMCPTool])
        with self.assertRaises(common.ConfigError):
            common.to_sdk_definition({"model": MODEL_REF, "tools": [{"type": "bing_grounding"}]}, FAKE_MODELS)

    def test_demo_function_is_deterministic_and_whitelisted(self):
        stock = common.demo_stock("r-1", "AX-100")
        self.assertTrue(1000 <= stock <= 9999)
        self.assertEqual(stock, common.demo_stock("r-1", "AX-100"))
        self.assertNotEqual(stock, common.demo_stock("r-2", "AX-100"))
        self.assertEqual(json.loads(common.run_demo_function("lookup_demo_stock", '{"sku": "AX-100"}', "r-1")),
                         {"sku": "AX-100", "stock": stock})
        for name, arguments in (("other_function", "{}"), ("lookup_demo_stock", "not json"),
                                ("lookup_demo_stock", "[]"), ("lookup_demo_stock", None)):
            with self.subTest(name=name, arguments=arguments):
                self.assertIn("error", json.loads(common.run_demo_function(name, arguments, "r-1")))
        long_sku = json.loads(common.run_demo_function("lookup_demo_stock", json.dumps({"sku": "S" * 99}), "r-1"))
        self.assertEqual(len(long_sku["sku"]), 32)


class CanonicalTests(ScriptCase):
    def test_service_defaults_and_filter_objects_are_ignored(self):
        wanted = common.canonical_definition(self.spec_for("--connection", CONNECTION, "--tool", "mcp"))
        stored = dict(stored_definition(tools=[dict(MCP_TOOL_RECORD, headers=None,
                                                    allowed_tools={"tool_names": ["microsoft_docs_search"]})]),
                      top_p=1.0, metadata={"owner": "demo"})
        self.assertTrue(common.covers(common.canonical_definition(stored), wanted))

    def test_real_differences_are_detected(self):
        wanted = common.canonical_definition(self.spec_for("--connection", CONNECTION, "--tool", "mcp"))
        variants = {
            "model": stored_definition(model="other/ax-k2-nvfp4", tools=[MCP_TOOL_RECORD]),
            "url": stored_definition(tools=[dict(MCP_TOOL_RECORD, server_url="https://example.invalid/mcp")]),
            "extra tool": stored_definition(tools=[MCP_TOOL_RECORD, MCP_TOOL_RECORD]),
            "no tool": stored_definition(),
            "instructions": dict(stored_definition(tools=[MCP_TOOL_RECORD]), instructions="다른 지침"),
        }
        for label, stored in variants.items():
            with self.subTest(label=label):
                self.assertFalse(common.covers(common.canonical_definition(stored), wanted))

    def test_covers_semantics(self):
        self.assertTrue(common.covers({"a": 1, "b": 2}, {"a": 1}))
        self.assertFalse(common.covers({"a": 1}, {"a": 1, "b": 2}))
        self.assertFalse(common.covers([1, 2], [1]))
        self.assertFalse(common.covers(None, {"a": 1}))


class RedactionTests(ScriptCase):
    def test_identifiers_are_masked(self):
        arm_id = f"/subscriptions/{GUID}/resourceGroups/rg-demo/providers/Microsoft.CognitiveServices/accounts/demo"
        text = f"id {arm_id} tenant {GUID} mail user@example.com ip 10.20.30.40 host demo-res.services.ai.azure.com"
        masked = common.redact_text(text)
        for leaked in (GUID, "user@example.com", "10.20.30.40", "demo-res", "resourceGroups"):
            self.assertNotIn(leaked, masked)
        for marker in ("arm_id", "email", "ipv4", "azure_host"):
            self.assertIn(f"<redacted:{marker}>", masked)

    def test_tokens_and_key_values_are_masked(self):
        fake_jwt = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJlMDEyMw"
        masked = common.redact_text(f"jwt {fake_jwt} Authorization: Bearer abcdefgh12345678 "
                                    '{"api_key": "abc123xyz"} x-apikey=k3y-value-1')
        for leaked in (fake_jwt, "abcdefgh12345678", "abc123xyz", "k3y-value-1"):
            self.assertNotIn(leaked, masked)
        self.assertIn('"api_key": "<redacted>"', masked)

    def test_short_service_ids_and_public_urls_survive(self):
        text = "resp_1 conv_9f8e7d6c input_tokens=120 https://learn.microsoft.com/api/mcp"
        self.assertEqual(common.redact_text(text), text)

    def test_azure_hosts_with_several_labels_are_masked(self):
        hosts = ("demo.apim.azure-api.net", "demo.eastus.services.ai.azure.com", "demo-app.azurewebsites.net",
                 "store.blob.core.windows.net", "kv-demo.vault.azure.net", "contoso.onmicrosoft.com")
        masked = common.redact_text("; ".join(f"https://{host}/x" for host in hosts))
        self.assertNotIn("demo", masked)
        self.assertNotIn("contoso", masked)
        self.assertEqual(masked.count("<redacted:azure_host>"), len(hosts))

    def test_project_path_segment_is_generalized_and_stable(self):
        masked = common.redact_text(f"POST {ENDPOINT}/openai/v1/responses and /api/projects/other-project?api-version=1")
        self.assertNotIn("demo-project", masked)
        self.assertNotIn("other-project", masked)
        self.assertEqual(masked.count("/api/projects/<project>"), 2)
        self.assertEqual(common.redact_text(masked), masked)

    def test_runtime_literals_ignore_case_and_may_carry_an_alias(self):
        env = {"BYOM_REDACT_LITERALS": "Deploy-Prefix, real-conn=<conn-a>, ab=<x>, bad=alias, xyz"}
        with patch.dict(os.environ, env):
            masked = common.redact_text("deploy-prefix-1 REAL-CONN/ax-k2-nvfp4 ab bad=alias xyz")
            dumped = common.dumps({"model_reference": "real-conn/ax-k2-nvfp4", "note": "Real-Conn"})
        self.assertEqual(masked, "<redacted>-1 <conn-a>/ax-k2-nvfp4 ab <redacted> xyz")
        self.assertIn("<conn-a>/ax-k2-nvfp4", dumped)
        self.assertNotIn("real-conn", dumped.lower())

    def test_sensitive_keys_are_replaced_wholesale(self):
        payload = {"headers": {"x-apikey": "k"}, "Authorization": "v", "x-api-key": "w", "input_tokens": 5,
                   "note": "ok", "token": None}
        self.assertEqual(common.redact(payload),
                         {"headers": "<redacted>", "Authorization": "<redacted>", "x-api-key": "<redacted>",
                          "input_tokens": 5, "note": "ok", "token": None})

    def test_environment_and_registered_secrets_are_masked(self):
        env = {"DEMO_API_KEY": "env-secret-value-1", "BYOM_REDACT_LITERALS": "custom-literal-9, abc",
               "SHORT_SECRET": "tiny"}
        with patch.dict(os.environ, env):
            masked = common.redact_text("a env-secret-value-1 b custom-literal-9 c abc d tiny")
        self.assertEqual(masked, "a <redacted> b <redacted> c abc d tiny")
        common.register_secret("hdr-secret-value")
        common.register_secret("abc")
        common.register_secret(None)
        self.assertEqual(common.redact_text("x hdr-secret-value y abc"), "x <redacted> y abc")

    def test_dumps_keeps_korean_text_and_stays_valid_json(self):
        text = common.dumps({"answer": "안녕하세요", "mail": "user@example.com"})
        self.assertIn("안녕하세요", text)
        self.assertNotIn("user@example.com", text)
        self.assertEqual(json.loads(text)["answer"], "안녕하세요")

    def test_evidence_labels_are_restricted(self):
        for label in ("../x", "A", "", "a/b", "x.run", "x.create", "x.connections", "x.series", "-lead", None):
            with self.subTest(label=label), self.assertRaises(common.ConfigError):
                common.evidence_path(label)
        self.assertEqual(common.evidence_path("demo-1").name, "demo-1.json")
        self.assertEqual(common.evidence_path("demo-1", "run").name, "demo-1.run.json")
        self.assertEqual(common.evidence_path("demo-1", "create").name, "demo-1.create.json")
        self.assertEqual(common.evidence_path("demo-1", "connections").name, "demo-1.connections.json")
        self.assertEqual(common.evidence_path("demo-1", "series").name, "demo-1.series.json")

    def test_now_iso_defaults_to_seconds_and_can_report_milliseconds(self):
        seconds = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00")
        milliseconds = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00")
        self.assertRegex(common.now_iso(), seconds)
        self.assertRegex(common.now_iso("milliseconds"), milliseconds)

    def test_display_path_never_exposes_local_directories(self):
        self.assertEqual(common.display_path(Path(self.tmp.name) / "x.json"), "x.json")
        self.assertEqual(common.display_path(common.ROOT / "demo" / "a.md"), "demo/a.md")

    def test_to_plain_handles_sdk_like_objects(self):
        class Color(enum.Enum):
            RED = "red"

        class Flaky:
            def model_dump(self, **_kwargs):
                raise TypeError("not serializable")

            def as_dict(self):
                return {"ok": 1}

        self.assertEqual(common.to_plain({"a": Model(b=[Color.RED, (1, 2)]), "c": None, "d": Flaky()}),
                         {"a": {"b": ["red", [1, 2]]}, "c": None, "d": {"ok": 1}})
        self.assertTrue(common.to_plain(object()).startswith("<object object"))

    def test_clip_and_urls(self):
        self.assertEqual(common.clip("abcdef", 4), "abcd...(+2 chars)")
        self.assertIsNone(common.clip(None))
        text = "see https://learn.microsoft.com/a/b. and (https://learn.microsoft.com/a/b), https://example.com/c?x=1;"
        self.assertEqual(common.urls_in(text), ["https://learn.microsoft.com/a/b", "https://example.com/c?x=1"])


class ServiceIdTests(ScriptCase):
    def test_long_ids_keep_only_the_first_eight_characters(self):
        for prefix in ("resp", "caresp", "conv", "msg", "fc", "rs", "mcpr", "mcpl", "call", "ws"):
            with self.subTest(prefix=prefix):
                self.assertEqual(common.redact_text(f"{prefix}_{LONG_ID}"), f"{prefix}_0a1b2c3d…")

    def test_ids_in_sentences_json_and_keys_are_shortened(self):
        payload = {"response_id": f"resp_{LONG_ID}", f"conv_{LONG_ID}": ["x", f"see (msg_{LONG_ID}), ok"]}
        self.assertEqual(common.redact(payload),
                         {"response_id": "resp_0a1b2c3d…", "conv_0a1b2c3d…": ["x", "see (msg_0a1b2c3d…), ok"]})
        self.assertEqual(common.redact_text(f"응답resp_{LONG_ID}입니다"), "응답resp_0a1b2c3d…입니다")

    def test_shortening_is_idempotent(self):
        once = common.redact_text(f"resp_{LONG_ID} conv_{LONG_ID}")
        self.assertEqual(once, "resp_0a1b2c3d… conv_0a1b2c3d…")
        self.assertEqual(common.redact_text(once), once)

    def test_request_ids_keep_only_the_first_eight_characters(self):
        text = ("{'request_id': 'fe49cff6b2f854e3d8ed52e2dfb9e48f'} x-request-id: fe49cff6b2f854e3d8ed52e2dfb9e48f "
                "Request ID: fe49cff6b2f854e3d8ed52e2dfb9e48f")
        expected = "{'request_id': 'fe49cff6…'} x-request-id: fe49cff6… Request ID: fe49cff6…"
        self.assertEqual(common.redact_text(text), expected)
        self.assertEqual(common.redact_text(expected), expected)
        self.assertEqual(common.redact_text(text, keep_ids=True), text)

    def test_short_or_unrelated_hex_values_are_not_request_ids(self):
        text = "{'request_id': 'req-1'} request_id_length: 12345678901234567890 etag: fe49cff6b2f854e3d8ed52e2dfb9e48f"
        self.assertEqual(common.redact_text(text), text)

    def test_other_names_codes_and_short_ids_are_left_alone(self):
        text = ("r-0a1b2c3d byom-marker-0a1b2c3d4e5f6071 lookup_demo_stock max_output_tokens previous_response_id "
                "mcp_approval_request microsoft_docs_search call_1 conv_9f8e7d6c FOUNDRY_PROJECT_ENDPOINT "
                f"{common.PRIVATE_DIR_ENV}")
        self.assertEqual(common.redact_text(text), text)

    def test_words_without_digits_are_not_treated_as_ids(self):
        text = "tool_microsoftdocsearchresult resp_abcdefghijklmnop"
        self.assertEqual(common.redact_text(text), text)

    def test_keep_ids_leaves_ids_whole_but_still_masks_everything_else(self):
        payload = {"id": f"resp_{LONG_ID}", "note": f"{GUID} user@example.com conv_{LONG_ID}", "headers": {"k": "v"}}
        full = common.redact(payload, keep_ids=True)
        self.assertEqual(full["id"], f"resp_{LONG_ID}")
        self.assertEqual(full["note"], f"<redacted:guid> <redacted:email> conv_{LONG_ID}")
        self.assertEqual(full["headers"], "<redacted>")
        self.assertIn(f"resp_{LONG_ID}", common.dumps(payload, keep_ids=True))
        self.assertNotIn(LONG_ID, common.dumps(payload))


class PrivateCopyTests(ScriptCase):
    def setUp(self):
        super().setUp()
        self.private_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.private_tmp.cleanup)
        self.private = Path(self.private_tmp.name) / "raw"

    def use_private(self):
        patcher = patch.dict(os.environ, {common.PRIVATE_DIR_ENV: str(self.private)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def quiet_write(self, name, payload):
        target = Path(self.tmp.name) / name
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            common.write_json(target, payload)
        return target, err.getvalue()

    def test_write_json_keeps_whole_ids_only_in_the_private_copy(self):
        self.use_private()
        target, err = self.quiet_write("unit.run.json", {"response_id": f"resp_{LONG_ID}", "tenant": GUID})
        public = json.loads(target.read_text(encoding="utf-8"))
        private = json.loads((self.private / "unit.run.json").read_text(encoding="utf-8"))
        self.assertEqual(public, {"response_id": "resp_0a1b2c3d…", "tenant": "<redacted:guid>"})
        self.assertEqual(private, {"response_id": f"resp_{LONG_ID}", "tenant": "<redacted:guid>"})
        self.assertEqual(err, "saved private copy unit.run.json\n")

    def test_without_the_variable_only_the_public_file_is_written(self):
        target, err = self.quiet_write("unit.run.json", {"response_id": f"resp_{LONG_ID}"})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"response_id": "resp_0a1b2c3d…"})
        self.assertEqual(err, "")
        self.assertFalse(self.private.exists())
        self.assertIsNone(common.private_directory())
        self.assertIsNone(common.private_copy(target))

    def test_the_private_directory_must_be_absolute_and_outside_the_repository(self):
        for value in ("relative/dir", str(common.ROOT), str(common.ROOT / "evidence" / "private")):
            with self.subTest(value=value), patch.dict(os.environ, {common.PRIVATE_DIR_ENV: value}):
                with self.assertRaises(common.ConfigError):
                    common.private_directory()

    def test_a_file_is_not_accepted_as_the_private_directory(self):
        file = Path(self.private_tmp.name) / "file"
        file.write_text("x", encoding="utf-8")
        with patch.dict(os.environ, {common.PRIVATE_DIR_ENV: str(file)}), self.assertRaises(common.ConfigError):
            common.private_directory()

    def test_live_runs_reject_a_bad_private_directory_before_any_call(self):
        guard = AssertionError("cloud access before the private directory was checked")
        with patch.dict(os.environ, {common.PRIVATE_DIR_ENV: "relative/dir"}), \
                patch.object(common, "open_clients", side_effect=guard):
            code, out, err = self.run_script(run_prompt, [])
        self.assertEqual((code, out), (2, ""))
        self.assertIn(common.PRIVATE_DIR_ENV, err)

    def test_the_private_copy_is_found_and_preferred_when_reading(self):
        self.use_private()
        target, _ = self.quiet_write("unit.run.json", {"conversation_id": f"conv_{LONG_ID}"})
        self.assertEqual(common.private_copy(target), self.private.resolve() / "unit.run.json")
        self.assertEqual(collect_evidence.load(target), {"conversation_id": f"conv_{LONG_ID}"})
        (self.private / "unit.run.json").unlink()
        self.assertEqual(collect_evidence.load(target), {"conversation_id": "conv_0a1b2c3d…"})

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_private_files_are_owner_only(self):
        self.use_private()
        self.quiet_write("unit.run.json", {"a": 1})
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.private / "unit.run.json").stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_a_group_readable_private_directory_is_refused(self):
        self.private.mkdir()
        os.chmod(self.private, 0o755)
        with patch.dict(os.environ, {common.PRIVATE_DIR_ENV: str(self.private)}), self.assertRaises(common.ConfigError):
            common.private_directory()

    def test_a_run_saves_shortened_ids_publicly_and_whole_ids_privately(self):
        self.use_private()
        reply = response([message("hi r-fixed07")], f"resp_{LONG_ID}", conversation={"id": f"conv_{LONG_ID}"})
        clients = fake_clients([reply])
        clients.openai.conversations.create = lambda: Model(id=f"conv_{LONG_ID}")
        code, out, err = self.run_script(run_prompt, ["--nonce", "r-fixed07", "--evidence-label", "unit-ids"], clients)
        public = (Path(self.tmp.name) / "unit-ids.run.json").read_text(encoding="utf-8")
        private = (self.private / "unit-ids.run.json").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        for text in (out, public):
            self.assertNotIn(LONG_ID, text)
            self.assertIn("resp_0a1b2c3d…", text)
            self.assertIn("conv_0a1b2c3d…", text)
        self.assertIn(f"resp_{LONG_ID}", private)
        self.assertIn(f"conv_{LONG_ID}", private)
        self.assertNotIn(LONG_ID, err)

    def test_collect_evidence_fetches_with_the_whole_ids_from_the_private_run_file(self):
        self.use_private()
        output = [list_tools(), mcp_call(f"Title: BYOM\nLink: {DOC_URL}"), message(f"BYOM {DOC_URL} r-fixed08")]
        final = response(output, f"resp_{LONG_ID}", conversation={"id": f"conv_{LONG_ID}"})
        agents = FakeAgents()
        agents.versions["3"] = stored_definition(tools=[MCP_TOOL_RECORD])
        clients = fake_clients([final], agents=agents, stored={f"resp_{LONG_ID}": final},
                               items=[message("질문", role="user"), *output])
        clients.openai.conversations.create = lambda: Model(id=f"conv_{LONG_ID}")
        self.run_script(run_prompt, ["--scenario", "mcp", "--nonce", "r-fixed08", "--evidence-label", "unit-priv"], clients)
        code, _, _ = self.run_script(collect_evidence, ["--label", "unit-priv", "--connection", CONNECTION], clients)
        evidence = self.read_saved("unit-priv.json")
        self.assertEqual(code, 0)
        self.assertEqual(clients.openai.conversations.items.calls[0]["conversation_id"], f"conv_{LONG_ID}")
        self.assertEqual(evidence["server_records"]["errors"], [])
        self.assertNotIn(LONG_ID, json.dumps(evidence))
        self.assertIn(f"resp_{LONG_ID}", (self.private / "unit-priv.json").read_text(encoding="utf-8"))

    def test_shortened_ids_in_a_run_file_are_reported_not_fetched(self):
        final = response([message("hi r-fixed09")], f"resp_{LONG_ID}", conversation={"id": f"conv_{LONG_ID}"})
        agents = FakeAgents()
        agents.versions["3"] = stored_definition()
        clients = fake_clients([final], agents=agents, stored={f"resp_{LONG_ID}": final})
        clients.openai.conversations.create = lambda: Model(id=f"conv_{LONG_ID}")
        self.run_script(run_prompt, ["--nonce", "r-fixed09", "--evidence-label", "unit-short"], clients)
        code, _, _ = self.run_script(collect_evidence, ["--label", "unit-short", "--connection", CONNECTION], clients)
        errors = self.read_saved("unit-short.json")["server_records"]["errors"]
        self.assertEqual(code, 0)
        self.assertEqual([(row["call"], row["type"]) for row in errors],
                         [("responses.retrieve", "ShortenedId"), ("conversations.items.list", "ShortenedId")])
        self.assertEqual(clients.openai.conversations.items.calls, [])


class SecretScanTests(ScriptCase):
    SECRET = "marker-value-123456"

    def test_scan_finds_values_in_nested_and_sdk_like_records(self):
        self.assertTrue(common.scan_for_secrets({"a": [{"b": f"x {self.SECRET} y"}]}, [self.SECRET]))
        self.assertTrue(common.scan_for_secrets(Model(output=[Model(text=self.SECRET)]), [self.SECRET]))
        self.assertTrue(common.scan_for_secrets({self.SECRET: 1}, [self.SECRET]))
        self.assertFalse(common.scan_for_secrets({"a": "nothing to see"}, [self.SECRET]))

    def test_scan_matches_json_escaped_values(self):
        literal = 'abc"def\\ghi-123'
        self.assertTrue(common.scan_for_secrets({"a": f"x {literal} y"}, [literal]))

    def test_scan_ignores_values_that_are_too_short_to_track(self):
        self.assertIsNone(common.scan_for_secrets({"a": "s3cr3t"}, ["s3cr3t"]))
        self.assertIsNone(common.scan_for_secrets({"a": 1}, []))
        self.assertFalse(common.scan_for_secrets({"a": "s3cr3t"}, ["s3cr3t", self.SECRET]))

    def test_secret_from_env_registers_the_value_for_redaction(self):
        with patch.dict(os.environ, {"DEMO_MARKER": self.SECRET}):
            self.assertEqual(common.secret_from_env("DEMO_MARKER"), self.SECRET)
        self.assertEqual(common.redact_text(f"a {self.SECRET} b"), "a <redacted> b")

    def test_secret_from_env_rejects_bad_names_and_values(self):
        for name, value in (("bad name", "x" * 20), ("DEMO_EMPTY", ""), ("DEMO_UNSET", None), ("DEMO_SHORT", "s3cr3t")):
            with self.subTest(name=name), patch.dict(os.environ, {} if value is None else {name: value}):
                with self.assertRaises(common.ConfigError) as caught:
                    common.secret_from_env(name)
                self.assertNotIn("s3cr3t", str(caught.exception))

    def test_merge_scan_prefers_a_hit_then_a_clean_scan(self):
        merge = run_prompt.merge_scan
        self.assertEqual([merge(None, None), merge(None, False), merge(False, None), merge(False, False),
                          merge(False, True), merge(True, False), merge(True, None)],
                         [None, False, False, False, True, True, True])


class SummaryTests(ScriptCase):
    def test_mcp_response_is_summarized_without_the_full_tool_output(self):
        output = f"result {DOC_URL} " + "x" * 900
        summary = common.summarize_response(
            response([list_tools(), mcp_call(output), message(f"요약입니다. {DOC_URL}")]))
        self.assertEqual(summary["output_item_types"], ["mcp_list_tools", "mcp_call", "message"])
        listing, call = summary["tool_events"]
        self.assertEqual(listing["tools"], ["microsoft_docs_search"])
        self.assertEqual((call["name"], call["arguments"], call["output_chars"]),
                         ("microsoft_docs_search", {"query": "BYOM"}, len(output)))
        self.assertLess(len(call["output_preview"]), 260)
        self.assertEqual(call["output_urls"], [DOC_URL])
        self.assertEqual(summary["answer_urls"], [DOC_URL])
        self.assertEqual(summary["agent"], {"name": "byom-demo-agent", "version": "3"})
        self.assertEqual(summary["conversation_id"], CONVERSATION)
        self.assertEqual(summary["usage"],
                         {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150, "reasoning_tokens": 0})
        self.assertEqual((summary["think_tag"], summary["reasoning_item_count"]), (False, 0))

    def test_reasoning_items_and_think_tags_are_flagged(self):
        summary = common.summarize_response(
            response([{"type": "reasoning", "id": "rs_1", "summary": []}, message("<think>메모</think>답")]))
        self.assertEqual(summary["reasoning_item_count"], 1)
        self.assertTrue(summary["think_tag"])
        self.assertEqual(summary["tool_events"], [])

    def test_failed_response_keeps_the_error_code(self):
        summary = common.summarize_response(
            response([], status="failed", error={"code": "server_error", "message": "backend rejected the request"}))
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["error"], {"code": "server_error", "message": "backend rejected the request"})

    def test_describe_error_adds_a_hint_and_skips_bodies(self):
        summary = common.describe_error(ApiError(403, "Forbidden", code="PermissionDenied"))
        self.assertEqual((summary["status_code"], summary["code"]), (403, "PermissionDenied"))
        self.assertIn("Foundry User", summary["hint"])
        self.assertNotIn("hint", common.describe_error(ValueError("plain failure")))

    def test_describe_error_keeps_the_service_message_target_and_details(self):
        exc = ApiError(400, "m" * 2000)
        exc.error = types.SimpleNamespace(code="InvalidModel", target="definition.model", details=[
            types.SimpleNamespace(code="NotFound", message="x" * 500)])
        summary = common.describe_error(exc)
        self.assertEqual((summary["code"], summary["target"]), ("InvalidModel", "definition.model"))
        self.assertTrue(summary["message"].startswith("m" * common.ERROR_MESSAGE_LIMIT))
        self.assertEqual(len(summary["details"]), 1)
        self.assertEqual(summary["details"][0]["code"], "NotFound")
        self.assertLess(len(summary["details"][0]["message"]), 440)
        self.assertNotIn("target", common.describe_error(ApiError(400, "plain")))

    def test_describe_error_keeps_the_rejected_parameter_and_a_short_request_id(self):
        exc = ApiError(400, "invalid payload", code="invalid_payload")
        exc.param = "tools"
        exc.request_id = "0a1b2c3d-1111-2222-3333-444444444444"
        summary = common.describe_error(exc)
        self.assertEqual((summary["param"], summary["request_id"]), ("tools", "0a1b2c3d"))
        plain = common.describe_error(ApiError(400, "plain"))
        self.assertNotIn("param", plain)
        self.assertNotIn("request_id", plain)
        exc.param, exc.request_id = 5, ""
        self.assertNotIn("param", common.describe_error(exc))
        self.assertNotIn("request_id", common.describe_error(exc))

    def test_describe_error_keeps_the_service_error_type_apart_from_the_exception_class(self):
        summary = common.describe_error(ApiError(500, "invalid content", api_type="model_error"))
        self.assertEqual((summary["type"], summary["status_code"], summary["api_type"]), ("ApiError", 500, "model_error"))
        self.assertIsNone(summary["code"])
        for value in (None, 5, ["model_error"]):
            with self.subTest(value=value):
                self.assertNotIn("api_type", common.describe_error(ApiError(500, "plain", api_type=value)))

    def test_short_request_id_keeps_only_the_first_characters(self):
        self.assertEqual(common.short_request_id("0a1b2c3d-1111-2222-3333-444444444444"), "0a1b2c3d")
        self.assertEqual(common.short_request_id("abc"), "abc")
        for value in (None, "", 5, ["0a1b2c3d"]):
            with self.subTest(value=value):
                self.assertIsNone(common.short_request_id(value))


class ClientSetupTests(ScriptCase):
    def build(self, kind="cli", pin=None):
        created = []

        class Credential:
            def __init__(self, **kwargs):
                created.append(kwargs)

        identity = types.SimpleNamespace(AzureCliCredential=Credential, DefaultAzureCredential=Credential,
                                         InteractiveBrowserCredential=Credential)
        env = {common.AZ_SUBSCRIPTION_ENV: pin} if pin else {}
        with patch.dict(os.environ, env), patch.dict(
                sys.modules, {"azure": types.SimpleNamespace(identity=identity), "azure.identity": identity}):
            common.make_credential(kind)
        return created

    def test_cli_credential_is_pinned_to_the_subscription_from_the_environment(self):
        self.assertEqual(self.build("cli", pin="demo subscription"), [{"subscription": "demo subscription"}])
        self.assertEqual(self.build("cli"), [{}])
        self.assertEqual(self.build("default", pin="demo subscription"), [{}])

    def test_credential_info_reports_the_pin_without_its_value(self):
        args = types.SimpleNamespace(auth="cli")
        with patch.dict(os.environ, {common.AZ_SUBSCRIPTION_ENV: "demo subscription name"}):
            self.assertEqual(common.credential_info(args), {"mode": "cli", "az_subscription_pinned": True})
        self.assertNotIn("demo subscription name", common.dumps({"error": "failed for demo subscription name"}))
        self.assertEqual(common.credential_info(args), {"mode": "cli", "az_subscription_pinned": False})
        self.assertEqual(common.credential_info(types.SimpleNamespace(auth="browser")),
                         {"mode": "browser", "az_subscription_pinned": False})

    def test_stage_failures_name_the_failed_call_and_keep_config_errors(self):
        with self.assertRaises(common.StageFailure) as caught:
            common.run_stage("agents.get", lambda: 1 / 0)
        self.assertEqual(caught.exception.stage, "agents.get")
        self.assertIsInstance(caught.exception.original, ZeroDivisionError)
        with self.assertRaises(common.ConfigError):
            common.run_stage("connect", lambda: (_ for _ in ()).throw(common.ConfigError("bad")))
        self.assertEqual(common.run_stage("x", lambda a, b=0: a + b, 1, b=2), 3)

    def test_http_request_counter_counts_every_request_and_only_response_posts(self):
        counter = common.HttpRequestCounter()
        self.assertEqual(counter.summary(), {"requests": 0, "responses_posts": 0})
        for method, path in (("POST", "/p/openai/v1/conversations"), ("POST", "/p/openai/v1/responses"),
                             ("GET", "/p/openai/v1/responses/resp_1"), ("POST", "/p/openai/v1/responses/")):
            counter(types.SimpleNamespace(method=method, url=types.SimpleNamespace(path=path)))
        self.assertEqual(counter.summary(), {"requests": 4, "responses_posts": 2})
        counter.reset()
        self.assertEqual(counter.summary(), {"requests": 0, "responses_posts": 0})
        counter(types.SimpleNamespace(method="POST", url=types.SimpleNamespace(path="/p/openai/v1/responses")))
        self.assertEqual(counter.summary(), {"requests": 1, "responses_posts": 1})


class CreateAgentTests(ScriptCase):
    def live(self, *argv, clients):
        return self.run_script(create_agent, ["--connection", CONNECTION, *argv], clients)

    def test_dry_run_prints_the_plan_without_cloud_access(self):
        guard = AssertionError("cloud access during dry-run")
        with patch.object(common, "open_clients", side_effect=guard), patch.object(common, "connect", side_effect=guard):
            code, out, err = self.run_script(create_agent, ["--dry-run", "--tool", "mcp"], live=False)
        payload = json.loads(out)
        self.assertEqual((code, err, payload["action"]), (0, "", "dry-run"))
        self.assertEqual(payload["model_reference"], "<connection>/ax-k2-nvfp4")
        self.assertEqual(payload["definition"]["tools"], [MCP_TOOL_RECORD])

    def test_creates_a_version_when_the_agent_is_missing(self):
        clients = fake_clients(agents=FakeAgents(latest=None))
        code, out, _ = self.live("--tool", "mcp", clients=clients)
        payload = json.loads(out)
        self.assertEqual((code, payload["action"], payload["version"], payload["model_reference"]),
                         (0, "created", "1", MODEL_REF))
        name, definition = clients.project.agents.created[0]
        self.assertEqual(name, "byom-demo-agent")
        self.assertEqual(definition.fields["model"], MODEL_REF)
        self.assertEqual([tool.kind for tool in definition.fields["tools"]], ["mcp"])
        self.assertNotIn("headers", definition.fields["tools"][0].fields)

    def test_reuses_the_matching_latest_version(self):
        clients = fake_clients(agents=FakeAgents(latest={"version": "3", "definition": stored_definition()}))
        code, out, _ = self.live(clients=clients)
        payload = json.loads(out)
        self.assertEqual((code, payload["action"], payload["version"]), (0, "reused", "3"))
        self.assertEqual(clients.project.agents.created, [])

    def test_creates_a_new_version_when_the_definition_differs(self):
        stored = {"version": "3", "definition": stored_definition(model="other/ax-k2-nvfp4")}
        clients = fake_clients(agents=FakeAgents(latest=stored))
        code, out, _ = self.live(clients=clients)
        self.assertEqual((code, json.loads(out)["action"]), (0, "created"))
        self.assertEqual(len(clients.project.agents.created), 1)

    def test_force_creates_even_when_unchanged(self):
        clients = fake_clients(agents=FakeAgents(latest={"version": "3", "definition": stored_definition()}))
        code, out, _ = self.live("--force", clients=clients)
        self.assertEqual((code, json.loads(out)["action"]), (0, "created"))

    def test_created_version_is_read_back_and_the_stored_model_is_compared(self):
        code, out, _ = self.live(clients=fake_clients(agents=FakeAgents(latest=None)))
        verification = json.loads(out)["verification"]
        self.assertEqual(code, 0)
        self.assertTrue(verification["ok"])
        self.assertEqual(verification["requested_model"], MODEL_REF)
        self.assertEqual(verification["agents_get"], {"version": "1", "model": MODEL_REF, "matches": True})
        self.assertEqual(verification["get_version"], {"version": "1", "model": MODEL_REF, "matches": True})

    def test_a_stored_model_that_differs_from_the_request_is_a_failure(self):
        class Rewriting(FakeAgents):
            def create_version(self, agent_name, definition):
                created = super().create_version(agent_name, definition)
                self.versions["1"]["model"] = "other/ax-k2-nvfp4"
                return created

        code, out, _ = self.live(clients=fake_clients(agents=Rewriting()))
        verification = json.loads(out)["verification"]
        self.assertEqual((code, verification["ok"]), (1, False))
        self.assertEqual(verification["agents_get"], {"version": "1", "model": "other/ax-k2-nvfp4", "matches": False})

    def test_read_back_failures_are_recorded_with_their_stage(self):
        class NoReadBack(FakeAgents):
            def get_version(self, agent_name, agent_version):
                raise ApiError(500, "backend unavailable")

        code, out, _ = self.live(clients=fake_clients(agents=NoReadBack()))
        payload = json.loads(out)
        self.assertEqual((code, payload["action"], payload["verification"]["ok"]), (1, "created", False))
        self.assertEqual(payload["verification"]["stage"], "verify.agents.get_version")
        self.assertEqual(payload["verification"]["error"]["status_code"], 500)

    def test_creation_errors_name_the_stage_and_are_saved_as_evidence(self):
        class Rejecting(FakeAgents):
            def create_version(self, agent_name, definition):
                raise ApiError(400, f"model {definition.model_dump()['model']} was not found", code="invalid_model")

        clients = fake_clients(agents=Rejecting())
        code, out, err = self.live("--evidence-label", "unit-create-error", clients=clients)
        payload = json.loads(out)
        self.assertEqual((code, payload["action"], payload["stage"]), (1, "error", "agents.create_version"))
        self.assertEqual((payload["error"]["status_code"], payload["error"]["code"]), (400, "invalid_model"))
        self.assertIn(f"model {MODEL_REF} was not found", payload["error"]["message"])
        saved = self.read_saved("unit-create-error.create.json")
        self.assertEqual((saved["schema"], saved["action"], saved["stage"]),
                         ("foundry-byom-create/1", "error", "agents.create_version"))
        self.assertIn("saved unit-create-error.create.json", err)

    def test_evidence_file_holds_the_result_and_the_auth_summary_without_the_pinned_name(self):
        pinned = "demo subscription name"
        with patch.dict(os.environ, {common.AZ_SUBSCRIPTION_ENV: pinned}):
            code, out, err = self.live("--evidence-label", "unit-create",
                                       clients=fake_clients(agents=FakeAgents(latest=None)))
        saved = self.read_saved("unit-create.create.json")
        self.assertEqual(code, 0)
        self.assertEqual((saved["schema"], saved["scope"]), ("foundry-byom-create/1", common.SCOPE_NOTE))
        self.assertEqual(saved["auth"], {"mode": "cli", "az_subscription_pinned": True})
        self.assertTrue(saved["verification"]["ok"])
        self.assertNotIn(pinned, json.dumps(saved) + out + err)

    def test_the_openai_client_is_never_built_or_needed(self):
        seen = []

        def open_without_openai(args, with_openai=True):
            seen.append(with_openai)
            return fake_clients(agents=FakeAgents(latest=None))

        with patch.object(common, "open_clients", side_effect=open_without_openai):
            code, out, _ = self.live(clients=None)
        self.assertEqual((code, json.loads(out)["action"], seen), (0, "created", [False]))
        clients = fake_clients(agents=FakeAgents(latest=None))
        clients.openai = None
        self.assertEqual(self.live(clients=clients)[0], 0)

    def test_a_reserved_evidence_label_is_rejected_before_any_call(self):
        with patch.object(common, "connect", side_effect=AssertionError("cloud access")):
            code, out, err = self.live("--evidence-label", "x.create", clients=fake_clients())
        self.assertEqual((code, out), (2, ""))
        self.assertIn("label", err)

    def test_api_failure_is_summarized_and_nothing_is_created(self):
        agents = FakeAgents(get_error=ApiError(403, "Forbidden", code="PermissionDenied"))
        clients = fake_clients(agents=agents)
        code, out, _ = self.live(clients=clients)
        payload = json.loads(out)
        self.assertEqual((code, payload["action"], payload["error"]["status_code"]), (1, "error", 403))
        self.assertIn("hint", payload["error"])
        self.assertEqual(agents.created, [])

    def test_configuration_errors_exit_with_code_2(self):
        cases = (([], "--connection"),
                 (["--connection", CONNECTION], "FOUNDRY_PROJECT_ENDPOINT"),
                 (["--project-endpoint", "https://example.invalid/api/projects/p", "--connection", CONNECTION],
                  "project endpoint"),
                 (["--project-endpoint", ENDPOINT + "\r", "--connection", CONNECTION], "project endpoint"),
                 (["--project-endpoint", ENDPOINT, "--connection", CONNECTION, "--agent-name", "bad_name"],
                  "agent name"))
        for argv, expected in cases:
            with self.subTest(argv=argv):
                code, out, err = self.run_script(create_agent, argv, fake_clients(), live=False)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("config error", err)
                self.assertIn(expected, err)

    def test_missing_sdk_is_reported_as_a_config_error(self):
        with patch.object(common, "open_clients", side_effect=ImportError("no azure")):
            code, out, err = self.live(clients=None)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("pip install -r demo/foundry-byom/agent/requirements.txt", err)

    def test_credential_failure_is_reported_without_a_traceback(self):
        with patch.object(common, "open_clients", side_effect=RuntimeError("az login required")):
            code, out, err = self.live(clients=None)
        payload = json.loads(out)
        self.assertEqual((code, payload["action"], payload["error"]["type"], err), (1, "error", "RuntimeError", ""))

    def test_delete_removes_only_the_named_agent(self):
        clients = fake_clients()
        code, out, _ = self.run_script(create_agent, ["--delete", "tmp-control-agent"], clients)
        self.assertEqual((code, json.loads(out)["action"], clients.project.agents.deleted),
                         (0, "deleted", ["tmp-control-agent"]))
        guard = AssertionError("cloud access during dry-run")
        with patch.object(common, "connect", side_effect=guard):
            code, out, _ = self.run_script(create_agent, ["--delete", "tmp-control-agent", "--dry-run"], live=False)
        self.assertEqual((code, json.loads(out)["action"]), (0, "dry-run"))
        code, _, err = self.run_script(create_agent, ["--delete", "bad_name"], clients)
        self.assertEqual(code, 2)
        self.assertEqual(clients.project.agents.deleted, ["tmp-control-agent"])


class InspectConnectionsTests(ScriptCase):
    def inspect(self, items, *argv, expect=(("demo-conn", "ax-k2-nvfp4"),), connections=None):
        connections = connections or FakeConnections(items)
        tokens = [token for name, models in expect for token in ("--expect", f"{name}={models}")]
        code, out, err = self.run_script(inspect_connections, tokens + list(argv), fake_clients(connections=connections))
        return types.SimpleNamespace(code=code, out=out, err=err, connections=connections,
                                     result=json.loads(out) if out else None)

    def statuses(self, run):
        return {entry["id"]: entry["status"] for entry in run.result["checks"]}

    def test_matching_connection_confirms_every_check_and_hides_ids_and_hosts(self):
        run = self.inspect([connection_item("demo-conn")])
        self.assertEqual((run.code, run.result["action"], run.result["ok"], run.result["complete"]),
                         (0, "inspected", True, True))
        self.assertEqual(set(self.statuses(run).values()), {"confirmed"})
        summary = run.result["connections"][0]["summary"]
        self.assertEqual((summary["category"], summary["credential_type"], summary["deployment_in_path"]),
                         ("ApiManagement", "ApiKey", False))
        self.assertEqual((summary["target_scheme"], summary["target_path"]), ("https", "/demo/v1"))
        self.assertEqual(summary["metadata"], {"deploymentInPath": "false", "inferenceAPIVersion": "2024-10-21"})
        self.assertEqual(summary["models"], [{"name": "ax-k2-nvfp4", "format": "OpenAI", "version": "1"}])
        self.assertEqual(run.result["connection_count"], 1)
        for leaked in ("gateway.example.invalid", "/subscriptions", GUID, "resourceGroups"):
            self.assertNotIn(leaked, run.out)

    def test_contract_mismatches_are_reported_as_failed(self):
        item = connection_item("demo-conn", category="ModelGateway", credential="CustomKeys", in_path="true",
                               models=("other-model",))
        run = self.inspect([item])
        self.assertEqual((run.code, run.result["ok"]), (1, False))
        self.assertEqual(self.statuses(run), {
            "demo-conn:category": "failed", "demo-conn:credential_type": "failed",
            "demo-conn:deployment_in_path": "failed", "demo-conn:models": "failed",
            "demo-conn:credential_value_not_returned": "confirmed"})

    def test_values_the_service_does_not_expose_stay_unverified(self):
        item = Model(name="demo-conn", type="ApiManagement", target="https://gateway.example.invalid/demo",
                     credentials={"type": "ApiKey"}, metadata={})
        run = self.inspect([item])
        statuses = self.statuses(run)
        self.assertEqual((run.code, run.result["ok"], run.result["complete"]), (3, True, False))
        self.assertEqual((statuses["demo-conn:deployment_in_path"], statuses["demo-conn:models"]),
                         ("unverified", "unverified"))
        self.assertEqual(statuses["demo-conn:category"], "confirmed")

    def test_a_missing_connection_fails_its_check_without_stopping_the_others(self):
        run = self.inspect([connection_item("demo-conn")],
                           expect=(("demo-conn", "ax-k2-nvfp4"), ("missing-conn", "ax-k2-fp8")))
        found = {entry["connection"]: entry for entry in run.result["connections"]}
        self.assertEqual(run.code, 1)
        self.assertTrue(found["demo-conn"]["found"])
        self.assertEqual((found["missing-conn"]["found"], found["missing-conn"]["get_error"]["status_code"]),
                         (False, 404))
        self.assertEqual(self.statuses(run)["missing-conn:found"], "failed")
        self.assertEqual(self.statuses(run)["demo-conn:category"], "confirmed")

    def test_credential_values_are_never_requested(self):
        run = self.inspect([connection_item("demo-conn")])
        self.assertEqual([call[0] for call in run.connections.calls], ["list", "get"])
        for call in run.connections.calls:
            self.assertNotIn("include_credentials", call[-1])

    def test_a_returned_credential_value_is_flagged_and_scrubbed(self):
        item = connection_item("demo-conn")
        item.data["credentials"] = {"type": "ApiKey", "key": "leaked-key-value-1"}
        run = self.inspect([item], "--evidence-label", "unit-leak")
        saved = (Path(self.tmp.name) / "unit-leak.connections.json").read_text(encoding="utf-8")
        self.assertEqual(run.code, 1)
        self.assertTrue(run.result["connections"][0]["summary"]["credential_value_returned"])
        self.assertEqual(self.statuses(run)["demo-conn:credential_value_not_returned"], "failed")
        self.assertNotIn("leaked-key-value-1", run.out + run.err + saved)

    def test_header_names_are_listed_without_values(self):
        item = connection_item("demo-conn", authConfig=json.dumps({"type": "api_key", "name": "x-api-key",
                                                                   "format": "{api_key}"}),
                               customHeaders=json.dumps({"x-tenant": "sensitive-header-value"}),
                               unlisted="secret-metadata-value")
        run = self.inspect([item])
        summary = run.result["connections"][0]["summary"]
        self.assertEqual(summary["auth_headers"], {"auth_config": {"type": "api_key", "name": "x-api-key"},
                                                   "custom_header_names": ["x-tenant"]})
        self.assertIn("unlisted", summary["metadata_keys"])
        for leaked in ("sensitive-header-value", "secret-metadata-value", "{api_key}"):
            self.assertNotIn(leaked, run.out)

    def test_other_connections_are_counted_but_not_named(self):
        other = connection_item("private-conn", category="AzureOpenAI")
        run = self.inspect([connection_item("demo-conn"), other])
        self.assertEqual(run.result["connection_count"], 2)
        self.assertEqual(run.result["other_connections"], [{"category": "AzureOpenAI", "credential_type": "ApiKey"}])
        self.assertNotIn("private-conn", run.out)

    def test_a_failed_list_does_not_stop_the_per_connection_checks(self):
        connections = FakeConnections([connection_item("demo-conn")],
                                      list_error=ApiError(403, "Forbidden", code="PermissionDenied"))
        run = self.inspect([], connections=connections)
        statuses = self.statuses(run)
        self.assertEqual((run.code, run.result["list_error"]["status_code"], run.result["connection_count"]),
                         (1, 403, None))
        self.assertIsNone(run.result["connections"][0]["in_list"])
        self.assertEqual((statuses["connections.list"], statuses["demo-conn:category"]), ("failed", "confirmed"))
        self.assertEqual(run.result["other_connections"], [])

    def test_a_credential_failure_is_saved_as_an_error_record(self):
        with patch.object(common, "open_clients", side_effect=RuntimeError("az login required")):
            code, out, _ = self.run_script(inspect_connections, ["--expect", "demo-conn=ax-k2-nvfp4",
                                                                 "--evidence-label", "unit-error"], clients=None)
        payload = json.loads(out)
        self.assertEqual((code, payload["action"], payload["stage"], payload["error"]["type"]),
                         (1, "error", "connect", "RuntimeError"))
        self.assertEqual(self.read_saved("unit-error.connections.json")["action"], "error")

    def test_result_is_saved_as_evidence_when_a_label_is_given(self):
        run = self.inspect([connection_item("demo-conn")], "--evidence-label", "unit-conn")
        saved = self.read_saved("unit-conn.connections.json")
        self.assertEqual((saved["schema"], saved["scope"], saved["ok"], saved["complete"]),
                         ("foundry-byom-connections/1", common.SCOPE_NOTE, True, True))
        self.assertIn("saved unit-conn.connections.json", run.err)

    def test_the_openai_client_is_never_built(self):
        seen = []

        def open_without_openai(args, with_openai=True):
            seen.append(with_openai)
            return fake_clients(connections=FakeConnections([connection_item("demo-conn")]))

        with patch.object(common, "open_clients", side_effect=open_without_openai):
            code, _, _ = self.run_script(inspect_connections, ["--expect", "demo-conn=ax-k2-nvfp4"], clients=None)
        self.assertEqual((code, seen), (0, [False]))

    def test_dry_run_makes_no_calls_and_writes_nothing(self):
        guard = AssertionError("cloud access during dry-run")
        with patch.object(common, "open_clients", side_effect=guard), patch.object(common, "connect", side_effect=guard):
            code, out, err = self.run_script(inspect_connections, ["--dry-run", "--expect", "demo-conn=ax-k2-nvfp4",
                                                                   "--evidence-label", "unit-dry"], live=False)
        plan = json.loads(out)["plan"]
        self.assertEqual((code, err, plan["include_credentials"], plan["openai_client"]), (0, "", False, False))
        self.assertEqual(plan["calls"], ["connections.list()", "connections.get('demo-conn')"])
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_configuration_errors_exit_with_code_2(self):
        cases = (([], "FOUNDRY_PROJECT_ENDPOINT"), (["--project-endpoint", ENDPOINT, "--expect", "bad"], "--expect"),
                 (["--project-endpoint", ENDPOINT, "--evidence-label", "x.connections"], "label"))
        for argv, expected in cases:
            with self.subTest(argv=argv):
                code, out, err = self.run_script(inspect_connections, argv, fake_clients(), live=False)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(expected, err)

    def test_expect_values_are_validated_and_merged(self):
        parse = inspect_connections.parse_expect
        self.assertEqual(parse(["a-conn=m-1,m-2", "a-conn=m-2,m-3"]), {"a-conn": ["m-1", "m-2", "m-3"]})
        for bad in ("a-conn", "a-conn=", "bad name=m", "a-conn=bad model", "=m"):
            with self.subTest(value=bad), self.assertRaises(common.ConfigError):
                parse([bad])

    def test_model_lists_are_read_from_several_shapes(self):
        entries = inspect_connections.model_entries
        self.assertEqual(entries({"staticmodels": '["a-model", "b-model"]'}), [{"name": "a-model"}, {"name": "b-model"}])
        self.assertEqual(entries({"models": [{"name": "m2", "properties": {"model": {"name": "base", "version": "2"}}}]}),
                         [{"name": "m2", "model": "base", "version": "2"}])
        self.assertEqual(entries({"staticmodels": "not json"}), [])
        self.assertIsNone(entries({}))

    def test_default_flag_accepts_wire_and_python_keys(self):
        describe = inspect_connections.describe_connection
        flags = (describe(connection_item("a", default=True))["is_default"],
                 describe(Model(name="b", type="ApiManagement", is_default=True))["is_default"],
                 describe({"name": "c"})["is_default"])
        self.assertEqual(flags, (True, True, None))


class RunPromptTests(ScriptCase):
    def test_dry_run_plan_has_no_cloud_access(self):
        guard = AssertionError("cloud access during dry-run")
        argv = ["--dry-run", "--scenario", "function", "--nonce", "r-dry01", "--agent-version", "3",
                "--evidence-label", "dry1"]
        with patch.object(common, "open_clients", side_effect=guard), patch.object(common, "connect", side_effect=guard):
            code, out, err = self.run_script(run_prompt, argv, live=False)
        plan = json.loads(out)["plan"]
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(plan["agent_reference"], {"type": "agent_reference", "name": "byom-demo-agent", "version": "3"})
        self.assertIn("r-dry01", plan["input"])
        self.assertEqual(plan["request_options"]["conversation"], "<new conversation>")
        self.assertEqual(plan["request_options"]["max_output_tokens"], 512)
        self.assertEqual(plan["evidence_file"], "dry1.run.json")

    def test_dry_run_shows_header_names_but_no_values(self):
        argv = ["--dry-run", "--scenario", "mcp", "--mcp-request-header", "x-demo-marker=DEMO_MARKER"]
        code, out, _ = self.run_script(run_prompt, argv, live=False)
        plan = json.loads(out)["plan"]
        self.assertEqual((code, plan["request_header_names"]), (0, ["x-demo-marker"]))
        self.assertNotIn("headers", plan["tools_override"][0])

    def test_plain_run_uses_a_conversation_and_the_agent_reference(self):
        clients = fake_clients([response([message("안녕하세요. r-fixed01")])])
        code, out, _ = self.run_script(run_prompt, ["--nonce", "r-fixed01"], clients)
        result = json.loads(out)
        call = clients.openai.responses.calls[0]
        self.assertEqual((code, result["ok"], result["stopped"], result["final_answer_contains_nonce"]),
                         (0, True, "completed", True))
        self.assertEqual(call["conversation"], CONVERSATION)
        self.assertEqual(call["extra_body"], {"agent_reference": {"type": "agent_reference", "name": "byom-demo-agent"}})
        self.assertEqual((call["max_output_tokens"], call["max_tool_calls"], call["timeout"]), (512, 4, 180))
        self.assertIn("r-fixed01", call["input"])
        self.assertNotIn("tools", call)
        self.assertEqual(clients.openai.conversations.deleted, [])

    def test_agent_version_is_pinned_when_requested(self):
        clients = fake_clients([response([message("ok")])])
        self.run_script(run_prompt, ["--agent-version", "3"], clients)
        reference = clients.openai.responses.calls[0]["extra_body"]["agent_reference"]
        self.assertEqual(reference, {"type": "agent_reference", "name": "byom-demo-agent", "version": "3"})

    def test_result_carries_the_client_http_counts_only_when_the_clients_count_them(self):
        _, out, _ = self.run_script(run_prompt, [], fake_clients([response([message("ok")])]))
        self.assertIsNone(json.loads(out)["client_http"])
        counted = fake_clients([response([message("ok")])])
        counted.http = types.SimpleNamespace(summary=lambda: {"requests": 2, "responses_posts": 1})
        _, out, _ = self.run_script(run_prompt, [], counted)
        self.assertEqual(json.loads(out)["client_http"], {"requests": 2, "responses_posts": 1})

    def test_zero_limits_are_omitted(self):
        clients = fake_clients([response([message("ok")])])
        self.run_script(run_prompt, ["--max-output-tokens", "0", "--max-tool-calls", "0", "--timeout", "0"], clients)
        call = clients.openai.responses.calls[0]
        for key in ("max_output_tokens", "max_tool_calls", "timeout"):
            self.assertNotIn(key, call)

    def test_conversation_is_deleted_only_on_request(self):
        clients = fake_clients([response([message("ok")])])
        _, out, _ = self.run_script(run_prompt, ["--delete-conversation"], clients)
        self.assertEqual(clients.openai.conversations.deleted, [CONVERSATION])
        self.assertTrue(json.loads(out)["conversation_deleted"])

    def test_custom_prompt_gets_a_trace_id_when_it_has_none(self):
        clients = fake_clients([response([message("ok")])])
        self.run_script(run_prompt, ["--prompt", "자유 질문", "--nonce", "r-fixed04"], clients)
        self.assertEqual(clients.openai.responses.calls[0]["input"], "자유 질문 [추적 ID: r-fixed04]")

    def test_function_round_trip_answers_with_the_local_stock(self):
        stock = common.demo_stock("r-fixed02", "AX-100")
        clients = fake_clients([response([function_call()], "resp_1"),
                                response([message(f"AX-100 재고는 {stock}개입니다. r-fixed02")], "resp_2")])
        code, out, _ = self.run_script(run_prompt, ["--scenario", "function", "--nonce", "r-fixed02"], clients)
        result = json.loads(out)
        calls = clients.openai.responses.calls
        self.assertEqual((code, result["ok"], result["stopped"], len(calls)), (0, True, "completed", 2))
        sent = calls[1]["input"]
        self.assertEqual((sent[0]["type"], sent[0]["call_id"]), ("function_call_output", "call_1"))
        self.assertEqual(json.loads(sent[0]["output"]), {"sku": "AX-100", "stock": stock})
        self.assertEqual(calls[1]["conversation"], CONVERSATION)
        self.assertEqual(calls[1]["extra_body"]["agent_reference"]["name"], "byom-demo-agent")
        self.assertEqual(result["item_sequence"], ["function_call", "message"])
        self.assertEqual(result["function_results"][0]["call_id"], "call_1")

    def test_unknown_function_calls_are_answered_with_an_error_not_executed(self):
        clients = fake_clients([response([function_call(name="delete_everything", arguments="{}")]),
                                response([message("모릅니다")])])
        self.run_script(run_prompt, ["--scenario", "function"], clients)
        output = clients.openai.responses.calls[1]["input"][0]["output"]
        self.assertEqual(json.loads(output), {"error": "unknown function"})

    def test_previous_response_id_chains_follow_ups_without_a_conversation(self):
        clients = fake_clients([response([function_call()], "resp_1"), response([message("done")], "resp_2")])
        self.run_script(run_prompt, ["--scenario", "function", "--no-conversation"], clients)
        first, second = clients.openai.responses.calls
        self.assertNotIn("conversation", first)
        self.assertNotIn("previous_response_id", first)
        self.assertEqual(second["previous_response_id"], "resp_1")
        self.assertNotIn("conversation", second)
        self.assertEqual(clients.openai.conversations.created, 0)

    def test_mcp_approval_is_withheld_unless_the_tool_is_named(self):
        clients = fake_clients([response([list_tools(), approval_request()])])
        code, out, _ = self.run_script(run_prompt, ["--scenario", "mcp"], clients)
        result = json.loads(out)
        self.assertEqual((code, result["ok"], result["stopped"]), (3, False, "approval_required:microsoft_docs_search"))
        self.assertEqual(len(clients.openai.responses.calls), 1)

    def test_mcp_approval_is_sent_for_a_named_tool(self):
        clients = fake_clients([response([list_tools(), approval_request()], "resp_1"),
                                response([list_tools(), mcp_call("ok"), message("done")], "resp_2")])
        code, out, _ = self.run_script(
            run_prompt, ["--scenario", "mcp", "--approve-tool", "microsoft_docs_search"], clients)
        self.assertEqual((code, json.loads(out)["stopped"]), (0, "completed"))
        self.assertEqual(clients.openai.responses.calls[1]["input"],
                         [{"type": "mcp_approval_response", "approve": True, "approval_request_id": "mcpr_1"}])

    def test_mcp_approval_follow_up_is_chained_by_previous_response_id_not_the_conversation(self):
        clients = fake_clients([response([list_tools(), approval_request()], "resp_1"),
                                response([list_tools(), mcp_call("ok"), message("done")], "resp_2")])
        _, out, _ = self.run_script(
            run_prompt, ["--scenario", "mcp", "--approve-tool", "microsoft_docs_search"], clients)
        first, second = clients.openai.responses.calls
        self.assertEqual(first["conversation"], CONVERSATION)
        self.assertNotIn("previous_response_id", first)
        self.assertEqual(second["previous_response_id"], "resp_1")
        self.assertNotIn("conversation", second)
        self.assertEqual(second["extra_body"]["agent_reference"]["name"], "byom-demo-agent")
        self.assertEqual([item["chained_by"] for item in json.loads(out)["rounds"]],
                         ["conversation", "previous_response_id"])

    def test_rounds_after_an_approval_stay_on_previous_response_id(self):
        clients = fake_clients([response([list_tools(), approval_request()], "resp_1"),
                                response([function_call()], "resp_2"),
                                response([message("done")], "resp_3")])
        self.run_script(
            run_prompt, ["--scenario", "mcp", "--approve-tool", "microsoft_docs_search"], clients)
        _, second, third = clients.openai.responses.calls
        self.assertEqual((second["previous_response_id"], third["previous_response_id"]), ("resp_1", "resp_2"))
        self.assertNotIn("conversation", third)

    def test_function_follow_ups_keep_the_conversation(self):
        clients = fake_clients([response([function_call()], "resp_1"), response([message("done")], "resp_2")])
        _, out, _ = self.run_script(run_prompt, ["--scenario", "function"], clients)
        self.assertNotIn("previous_response_id", clients.openai.responses.calls[1])
        self.assertEqual([item["chained_by"] for item in json.loads(out)["rounds"]], ["conversation", "conversation"])

    def test_chain_labels_without_a_conversation(self):
        clients = fake_clients([response([function_call()], "resp_1"), response([message("done")], "resp_2")])
        _, out, _ = self.run_script(run_prompt, ["--scenario", "function", "--no-conversation"], clients)
        self.assertEqual([item["chained_by"] for item in json.loads(out)["rounds"]], ["none", "previous_response_id"])

    def test_each_round_records_when_it_was_requested_and_how_long_it_took(self):
        clients = fake_clients([response([function_call()], "resp_1"), response([message("done")], "resp_2")])
        _, out, _ = self.run_script(run_prompt, ["--scenario", "function"], clients)
        result = json.loads(out)
        stamp = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00")
        for item in result["rounds"]:
            self.assertRegex(item["requested_at"], stamp)
            self.assertRegex(item["responded_at"], stamp)
            self.assertLessEqual(item["requested_at"], item["responded_at"])
            self.assertGreaterEqual(item["elapsed_s"], 0)
        self.assertEqual(result["last_request_at"], result["rounds"][-1]["requested_at"])
        self.assertRegex(result["started_at"], stamp)
        self.assertRegex(result["finished_at"], stamp)
        self.assertLessEqual(result["started_at"], result["rounds"][0]["requested_at"])
        self.assertLessEqual(result["rounds"][-1]["responded_at"], result["finished_at"])

    def test_each_round_records_the_short_request_id_when_the_service_sends_one(self):
        class Tagged(Model):
            _request_id = "0a1b2c3d-1111-2222-3333-444444444444"

        reply = response([message("hi")], "resp_1")
        tagged = Tagged(**reply.data)
        clients = fake_clients([tagged])
        _, out, _ = self.run_script(run_prompt, ["--scenario", "plain"], clients)
        self.assertEqual(json.loads(out)["rounds"][0]["request_id"], "0a1b2c3d")
        clients = fake_clients([response([message("hi")], "resp_1")])
        _, out, _ = self.run_script(run_prompt, ["--scenario", "plain"], clients)
        self.assertIsNone(json.loads(out)["rounds"][0]["request_id"])

    def test_a_failed_request_still_records_when_it_was_requested(self):
        clients = fake_clients([RuntimeError("boom")])
        _, out, _ = self.run_script(run_prompt, ["--scenario", "plain"], clients)
        result = json.loads(out)
        self.assertEqual(result["rounds"], [])
        self.assertIsNotNone(result["last_request_at"])
        self.assertIsNotNone(result["finished_at"])

    def test_round_cap_stops_a_pending_tool_loop(self):
        clients = fake_clients([response([function_call(f"call_{index}")], f"resp_{index}") for index in range(4)])
        code, out, _ = self.run_script(run_prompt, ["--scenario", "function", "--max-rounds", "2"], clients)
        result = json.loads(out)
        self.assertEqual((code, result["stopped"], len(clients.openai.responses.calls)), (3, "max_rounds", 2))
        self.assertEqual(len(result["function_results"]), 1)

    def test_oauth_consent_stops_without_printing_the_link(self):
        consent = {"type": "oauth_consent_request", "id": "oauth_1", "server_label": "microsoft_learn",
                   "consent_link": "https://login.example.invalid/authorize?state=SECRETSTATE"}
        clients = fake_clients([response([consent])])
        code, out, _ = self.run_script(run_prompt, ["--scenario", "mcp"], clients)
        result = json.loads(out)
        self.assertEqual((code, result["stopped"]), (3, "oauth_consent_required"))
        self.assertNotIn("SECRETSTATE", out)
        self.assertEqual(result["tool_events"], [{"type": "oauth_consent_request", "needs_user_action": True}])

    def test_api_error_is_summarized_with_redaction(self):
        error = ApiError(403, f"Forbidden for Bearer abcdefgh12345678 tenant {GUID}", code="PermissionDenied")
        code, out, _ = self.run_script(run_prompt, [], fake_clients([error]))
        result = json.loads(out)
        self.assertEqual((code, result["stopped"], result["ok"], result["error"]["status_code"]), (1, "error", False, 403))
        self.assertIn("hint", result["error"])
        self.assertEqual(result["conversation_id"], CONVERSATION)
        for leaked in ("abcdefgh12345678", GUID):
            self.assertNotIn(leaked, out)

    def test_failed_status_is_reported_with_the_service_error(self):
        failed = response([], status="failed", error={"code": "server_error", "message": "bad gateway"})
        code, out, _ = self.run_script(run_prompt, [], fake_clients([failed]))
        result = json.loads(out)
        self.assertEqual((code, result["stopped"], result["error"]), (1, "failed", {"code": "server_error",
                                                                                      "message": "bad gateway"}))

    def test_incomplete_status_exits_with_code_3(self):
        partial = response([message("부분")], status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        code, out, _ = self.run_script(run_prompt, [], fake_clients([partial]))
        result = json.loads(out)
        self.assertEqual((code, result["stopped"], result["rounds"][0]["incomplete_reason"]),
                         (3, "incomplete", "max_output_tokens"))

    def test_expect_failure_inverts_the_verdict(self):
        code, out, _ = self.run_script(run_prompt, ["--expect-failure"], fake_clients([ApiError(404, "model not found")]))
        self.assertEqual((code, json.loads(out)["ok"]), (0, True))
        code, out, _ = self.run_script(run_prompt, ["--expect-failure"], fake_clients([response([message("ok")])]))
        self.assertEqual((code, json.loads(out)["ok"]), (1, False))

    def test_run_file_is_written_for_the_label(self):
        clients = fake_clients([response([message("hi r-fixed03")])])
        self.run_script(run_prompt, ["--nonce", "r-fixed03", "--evidence-label", "unit-run"], clients)
        saved = self.read_saved("unit-run.run.json")
        self.assertEqual(saved["schema"], "foundry-byom-run/1")
        self.assertTrue(saved["final_answer_contains_nonce"])

    def test_request_header_value_never_leaves_memory(self):
        secret = "marker-value-123456"
        clients = fake_clients([response([list_tools(), mcp_call(f"ok {DOC_URL}"), message("done")])])
        argv = ["--scenario", "mcp", "--mcp-request-header", "x-demo-marker=DEMO_MARKER", "--evidence-label", "hdr"]
        with patch.dict(os.environ, {"DEMO_MARKER": secret}):
            code, out, _ = self.run_script(run_prompt, argv, clients)
        saved = (Path(self.tmp.name) / "hdr.run.json").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(clients.openai.responses.calls[0]["tools"][0]["headers"], {"x-demo-marker": secret})
        for text in (out, saved):
            self.assertNotIn(secret, text)
            self.assertNotIn("<redacted", text)
        self.assertEqual(json.loads(out)["request"]["request_header_names"], ["x-demo-marker"])
        self.assertIs(json.loads(out)["request_header_echoed"], False)

    def test_echoed_request_header_is_reported_but_never_printed(self):
        secret = "marker-value-123456"
        echo = [dict(MCP_TOOL_RECORD, headers={"x-demo-marker": secret})]
        clients = fake_clients([response([list_tools(), mcp_call(f"ok {DOC_URL}"), message("done")], tools=echo)])
        argv = ["--scenario", "mcp", "--mcp-request-header", "x-demo-marker=DEMO_MARKER", "--evidence-label", "echo"]
        with patch.dict(os.environ, {"DEMO_MARKER": secret}):
            code, out, err = self.run_script(run_prompt, argv, clients)
        saved = (Path(self.tmp.name) / "echo.run.json").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIs(json.loads(out)["request_header_echoed"], True)
        self.assertIn("warning", err)
        for text in (out, err, saved):
            self.assertNotIn(secret, text)

    def test_runs_without_request_headers_have_no_echo_verdict(self):
        code, out, _ = self.run_script(run_prompt, [], fake_clients([response([message("ok")])]))
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out)["request_header_echoed"])

    def test_missing_header_variable_is_a_config_error(self):
        argv = ["--scenario", "mcp", "--mcp-request-header", "x-demo-marker=DEMO_MARKER_MISSING"]
        code, out, err = self.run_script(run_prompt, argv, fake_clients())
        self.assertEqual((code, out), (2, ""))
        self.assertIn("DEMO_MARKER_MISSING", err)

    def test_short_header_value_is_rejected_because_it_cannot_be_tracked(self):
        argv = ["--scenario", "mcp", "--mcp-request-header", "x-demo-marker=DEMO_MARKER"]
        with patch.dict(os.environ, {"DEMO_MARKER": "s3cr3t"}):
            code, out, err = self.run_script(run_prompt, argv, fake_clients())
        self.assertEqual((code, out), (2, ""))
        self.assertIn("fewer than 8 characters", err)
        self.assertNotIn("s3cr3t", err)

    def test_invalid_arguments_exit_with_config_error(self):
        cases = (["--nonce", "bad nonce"], ["--agent-name", "bad_name"], ["--max-rounds", "0"],
                 ["--agent-version", "v 1"], ["--approve-tool", "bad tool"], ["--evidence-label", "../x"],
                 ["--scenario", "plain", "--mcp-request-header", "x-demo=DEMO_X"],
                 ["--scenario", "mcp", "--mcp-request-header", "no-equals"])
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, err = self.run_script(run_prompt, argv, fake_clients())
                self.assertEqual((code, out), (2, ""))
                self.assertIn("config error", err)

    def test_client_construction_failure_is_summarized(self):
        with patch.object(common, "open_clients", side_effect=RuntimeError("az login required")):
            code, out, err = self.run_script(run_prompt, [])
        self.assertEqual((code, json.loads(out)["error"]["type"], err), (1, "RuntimeError", ""))


class CollectEvidenceTests(ScriptCase):
    def mcp_world(self, answer=None, model=MODEL_REF, extra_output=(), reply_extra=None):
        answer = answer or f"BYOM은 외부 모델을 연결합니다. {DOC_URL} r-fixed05"
        output = [list_tools(), mcp_call(f"Title: BYOM\nLink: {DOC_URL}\nTenant: {GUID}"), *extra_output, message(answer)]
        final = response(output, "resp_1", **(reply_extra or {}))
        agents = FakeAgents()
        agents.versions["3"] = stored_definition(model=model, tools=[MCP_TOOL_RECORD])
        items = [message("질문", role="user"), *output]
        return fake_clients([final], agents=agents, stored={"resp_1": final}, items=items)

    def function_world(self, answer):
        stock = common.demo_stock("r-fixed06", "AX-100")
        output = [message(answer.format(stock=stock, comma=f"{stock:,}"))]
        replies = [response([function_call()], "resp_1"), response(output, "resp_2")]
        agents = FakeAgents()
        agents.versions["3"] = stored_definition(tools=[dict(common.DEMO_FUNCTION)])
        items = [message("질문", role="user"), function_call(),
                 {"type": "function_call_output", "call_id": "call_1", "output": json.dumps({"sku": "AX-100", "stock": stock})},
                 *output]
        return fake_clients(replies, agents=agents, stored={"resp_2": replies[1]}, items=items)

    def capture(self, label, clients, run_argv, collect_argv=()):
        code, _, _ = self.run_script(run_prompt, [*run_argv, "--evidence-label", label], clients)
        self.assertIn(code, (0, 1, 3))
        code, out, err = self.run_script(collect_evidence, ["--label", label, "--connection", CONNECTION, *collect_argv],
                                         clients)
        self.assertEqual((code, err), (0, ""))
        evidence = self.read_saved(f"{label}.json")
        return evidence, {row["id"]: row for row in evidence["checks"]}, out

    def test_mcp_flow_is_confirmed_end_to_end(self):
        evidence, checks, out = self.capture("unit-mcp", self.mcp_world(), ["--scenario", "mcp", "--nonce", "r-fixed05"])
        confirmed = ("agent_definition_model_reference", "response_agent_reference", "response_completed", "tool_listed",
                     "tool_called", "tool_then_answer_order", "tool_result_used", "no_thinking_trace", "usage_recorded",
                     "conversation_tool_history")
        for identifier in confirmed:
            self.assertEqual(checks[identifier]["status"], "confirmed", identifier)
        self.assertEqual((checks["nonce_echoed"]["status"], checks["negative_control_failed_as_expected"]["status"],
                          checks["request_secret_not_in_server_records"]["status"]),
                         ("not_applicable", "not_applicable", "not_applicable"))
        self.assertEqual(evidence["summary"], {"confirmed": 10, "failed": 0, "unverified": 6, "not_applicable": 3})
        self.assertEqual({row["status"] for row in evidence["manual_checks"]}, {"unverified"})
        self.assertEqual(evidence["observations"]["agent_definition_model"], MODEL_REF)
        self.assertIn("not a Model Catalog registration", evidence["scope"])
        self.assertIn("evidence: unit-mcp.json", out)
        self.assertIn("agent_definition_model_reference: confirmed", out)

    def test_written_files_and_output_are_redacted(self):
        clients = self.mcp_world()
        _, _, out = self.capture("unit-redact", clients, ["--scenario", "mcp", "--nonce", "r-fixed05"])
        for name in ("unit-redact.json", "unit-redact.run.json"):
            text = (Path(self.tmp.name) / name).read_text(encoding="utf-8")
            self.assertNotIn(GUID, text)
            self.assertIn("<redacted:guid>", text)
        self.assertNotIn(self.tmp.name, out)

    def test_model_reference_mismatch_is_a_failure(self):
        _, checks, _ = self.capture("unit-mismatch", self.mcp_world(model="other-conn/ax-k2-nvfp4"),
                                    ["--scenario", "mcp", "--nonce", "r-fixed05"])
        self.assertEqual(checks["agent_definition_model_reference"]["status"], "failed")
        self.assertIn("other-conn/ax-k2-nvfp4", checks["agent_definition_model_reference"]["detail"])

    def test_without_a_connection_the_model_reference_stays_unverified(self):
        clients = self.mcp_world()
        self.run_script(run_prompt, ["--scenario", "mcp", "--evidence-label", "unit-noconn"], clients)
        code, _, _ = self.run_script(collect_evidence, ["--label", "unit-noconn"], clients)
        checks = {row["id"]: row for row in self.read_saved("unit-noconn.json")["checks"]}
        self.assertEqual(code, 0)
        self.assertEqual(checks["agent_definition_model_reference"]["status"], "unverified")
        self.assertIn("--connection", checks["agent_definition_model_reference"]["detail"])

    def test_reasoning_traces_are_a_failure(self):
        extra = [{"type": "reasoning", "id": "rs_1", "summary": []}]
        _, checks, _ = self.capture("unit-reason", self.mcp_world(extra_output=extra),
                                    ["--scenario", "mcp", "--nonce", "r-fixed05"])
        self.assertEqual(checks["no_thinking_trace"]["status"], "failed")
        _, checks, _ = self.capture("unit-think", self.mcp_world(answer=f"<think>생각</think>{DOC_URL} r-fixed05"),
                                    ["--scenario", "mcp", "--nonce", "r-fixed05"])
        self.assertEqual(checks["no_thinking_trace"]["status"], "failed")
        _, checks, _ = self.capture("unit-tokens", self.mcp_world(reply_extra={"usage": {
            "input_tokens": 100, "output_tokens": 300, "output_tokens_details": {"reasoning_tokens": 250}}}),
            ["--scenario", "mcp", "--nonce", "r-fixed05"])
        self.assertEqual(checks["no_thinking_trace"]["status"], "failed")
        self.assertIn("reasoning_tokens=250", checks["no_thinking_trace"]["detail"])

    def test_function_result_must_appear_in_the_answer(self):
        run = ["--scenario", "function", "--nonce", "r-fixed06"]
        for label, answer, expected in (("unit-fn-ok", "재고는 {stock}개입니다.", "confirmed"),
                                        ("unit-fn-comma", "재고는 {comma}개입니다.", "confirmed"),
                                        ("unit-fn-bad", "재고는 알 수 없습니다.", "failed")):
            with self.subTest(label=label):
                _, checks, _ = self.capture(label, self.function_world(answer), run)
                self.assertEqual(checks["tool_result_used"]["status"], expected)
                self.assertEqual(checks["tool_called"]["status"], "confirmed")
                self.assertEqual(checks["tool_then_answer_order"]["status"], "confirmed")
                self.assertEqual(checks["conversation_tool_history"]["status"], "confirmed")

    def test_conversation_without_a_tool_call_item_names_what_it_holds(self):
        clients = self.mcp_world()
        items = clients.openai.conversations.items.items
        items[:] = [item for item in items if item.get("type") != "mcp_call"]
        _, checks, _ = self.capture("unit-history-gap", clients, ["--scenario", "mcp", "--nonce", "r-fixed05"])
        row = checks["conversation_tool_history"]
        self.assertEqual(row["status"], "failed")
        self.assertIn("function_call/mcp_call", row["detail"])
        self.assertIn("mcp_list_tools", row["detail"])
        self.assertNotIn("message", row["detail"].split("it holds:")[1])
        self.assertEqual(checks["tool_called"]["status"], "confirmed")

    def test_plain_nonce_echo(self):
        clients = fake_clients([response([message("안녕하세요 r-fixed07")])])
        _, checks, _ = self.capture("unit-plain", clients, ["--nonce", "r-fixed07"])
        self.assertEqual(checks["nonce_echoed"]["status"], "confirmed")
        self.assertEqual(checks["tool_called"]["status"], "not_applicable")

    SECRET = "marker-value-123456"
    HEADER_RUN = ["--scenario", "mcp", "--nonce", "r-fixed05", "--mcp-request-header", "x-demo-marker=DEMO_MARKER"]

    def secret_files(self, label):
        return [(Path(self.tmp.name) / name).read_text(encoding="utf-8") for name in (f"{label}.json", f"{label}.run.json")]

    def test_request_secret_absent_from_server_records_is_confirmed(self):
        with patch.dict(os.environ, {"DEMO_MARKER": self.SECRET}):
            evidence, checks, out = self.capture("unit-secret-ok", self.mcp_world(), self.HEADER_RUN,
                                                 ["--scan-env", "DEMO_MARKER"])
        row = checks["request_secret_not_in_server_records"]
        self.assertEqual(row["status"], "confirmed")
        self.assertIn("run responses", row["detail"])
        self.assertIn("conversation items", row["detail"])
        self.assertEqual(evidence["server_records"]["secret_scan"], {
            "values_checked": 1, "agent_version": False, "response": False, "conversation_items": False})
        for text in (out, *self.secret_files("unit-secret-ok")):
            self.assertNotIn(self.SECRET, text)

    def test_request_secret_echoed_in_a_server_record_is_a_failure_and_stays_masked(self):
        echo = {"type": "mcp_list_tools", "server_label": "microsoft_learn", "tools": [{"name": "microsoft_docs_search"}],
                "error": None, "request_headers": {"x-demo-marker": self.SECRET}}
        clients = self.mcp_world()
        clients.openai.conversations.items.items.append(echo)
        with patch.dict(os.environ, {"DEMO_MARKER": self.SECRET}):
            evidence, checks, out = self.capture("unit-secret-leak", clients, self.HEADER_RUN,
                                                 ["--scan-env", "DEMO_MARKER"])
        row = checks["request_secret_not_in_server_records"]
        self.assertEqual(row["status"], "failed")
        self.assertIn("conversation items", row["detail"])
        self.assertNotIn("retrieved response", row["detail"])
        self.assertIs(evidence["server_records"]["secret_scan"]["conversation_items"], True)
        self.assertIn("request_secret_not_in_server_records: failed", out)
        for text in (out, *self.secret_files("unit-secret-leak")):
            self.assertNotIn(self.SECRET, text)

    def test_request_secret_echoed_in_the_run_response_is_a_failure(self):
        echo = [dict(MCP_TOOL_RECORD, headers={"x-demo-marker": self.SECRET})]
        clients = self.mcp_world(reply_extra={"tools": echo})
        with patch.dict(os.environ, {"DEMO_MARKER": self.SECRET}):
            evidence, checks, out = self.capture("unit-secret-run", clients, self.HEADER_RUN)
        row = checks["request_secret_not_in_server_records"]
        self.assertEqual((row["status"], evidence["run"]["request_header_echoed"]), ("failed", True))
        self.assertIn("run responses", row["detail"])
        for text in (out, *self.secret_files("unit-secret-run")):
            self.assertNotIn(self.SECRET, text)

    def test_scan_without_any_retrievable_record_is_unverified(self):
        clients = fake_clients([response([message("ok r-fixed08")])])
        with patch.dict(os.environ, {"DEMO_MARKER": self.SECRET}):
            _, checks, _ = self.capture("unit-secret-none", clients, ["--nonce", "r-fixed08", "--no-conversation"],
                                        ["--scan-env", "DEMO_MARKER"])
        self.assertEqual(checks["request_secret_not_in_server_records"]["status"], "unverified")

    def test_scan_env_needs_a_set_variable_of_sufficient_length(self):
        argv = ["--label", "unit-scan-config", "--connection", CONNECTION, "--scan-env"]
        for name, value in (("DEMO_MARKER_MISSING", None), ("DEMO_MARKER", "s3cr3t")):
            with self.subTest(name=name), patch.dict(os.environ, {} if value is None else {name: value}):
                code, out, err = self.run_script(collect_evidence, [*argv, name], self.mcp_world())
                self.assertEqual((code, out), (2, ""))
                self.assertIn(name, err)
                self.assertNotIn("s3cr3t", err)
        code, _, err = self.run_script(collect_evidence, [*argv, "bad name"], self.mcp_world())
        self.assertEqual((code, "config error" in err), (2, True))

    def test_dry_run_lists_scan_variable_names_only(self):
        with patch.dict(os.environ, {"DEMO_MARKER": self.SECRET}):
            code, out, _ = self.run_script(collect_evidence, ["--dry-run", "--label", "unit-scan-dry",
                                                              "--scan-env", "DEMO_MARKER"], live=False)
        self.assertEqual((code, json.loads(out)["scan_env"]), (0, ["DEMO_MARKER"]))
        self.assertNotIn(self.SECRET, out)

    def test_negative_control_failure_is_the_expected_outcome(self):
        clients = fake_clients([ApiError(404, "model not found")])
        evidence, checks, _ = self.capture("unit-negative", clients, ["--expect-failure"])
        self.assertEqual(checks["negative_control_failed_as_expected"]["status"], "confirmed")
        self.assertIn("404", checks["negative_control_failed_as_expected"]["detail"])
        self.assertEqual(checks["response_completed"]["status"], "not_applicable")
        self.assertTrue(evidence["server_records"]["errors"])

    def test_a_request_that_failed_before_any_output_is_not_judged_on_tools(self):
        clients = fake_clients([ApiError(500, "The model produced invalid content.")])
        evidence, checks, _ = self.capture("unit-failed-request", clients, ["--scenario", "mcp"])
        self.assertEqual(checks["response_completed"]["status"], "failed")
        self.assertIn("500", checks["response_completed"]["detail"])
        for identifier in ("tool_listed", "tool_called", "conversation_tool_history"):
            with self.subTest(check=identifier):
                self.assertEqual(checks[identifier]["status"], "unverified")
                self.assertIn("failed before any output (500)", checks[identifier]["detail"])
        self.assertEqual(evidence["summary"]["failed"], 1)

    def test_a_failure_after_a_recorded_response_still_judges_the_tools_seen_so_far(self):
        clients = fake_clients([response([list_tools(), approval_request()], "resp_1"), ApiError(500, "boom")])
        _, checks, _ = self.capture("unit-late-failure", clients,
                                    ["--scenario", "mcp", "--approve-tool", "microsoft_docs_search"])
        self.assertEqual(checks["tool_listed"]["status"], "confirmed")
        self.assertEqual(checks["tool_called"]["status"], "failed")

    def test_negative_control_that_succeeds_is_a_failure(self):
        clients = fake_clients([response([message("ok")])])
        _, checks, _ = self.capture("unit-negative-ok", clients, ["--expect-failure"])
        self.assertEqual(checks["negative_control_failed_as_expected"]["status"], "failed")

    def test_server_fetch_errors_are_recorded_not_fatal(self):
        clients = self.mcp_world()
        clients.openai.responses.stored["resp_1"] = ApiError(404, "response not found")
        evidence, checks, _ = self.capture("unit-fetch", clients, ["--scenario", "mcp", "--nonce", "r-fixed05"])
        errors = evidence["server_records"]["errors"]
        self.assertEqual([(row["call"], row["status_code"]) for row in errors], [("responses.retrieve", 404)])
        self.assertEqual(checks["tool_called"]["status"], "confirmed")

    def test_conversation_items_alone_are_enough_without_a_run_file(self):
        clients = self.mcp_world()
        argv = ["--label", "unit-pseudo", "--connection", CONNECTION, "--agent-version", "3",
                "--response-id", "resp_1", "--conversation-id", CONVERSATION]
        code, _, _ = self.run_script(collect_evidence, argv, clients)
        evidence = self.read_saved("unit-pseudo.json")
        checks = {row["id"]: row for row in evidence["checks"]}
        self.assertEqual(code, 0)
        self.assertIsNone(evidence["run"])
        self.assertEqual(evidence["expected"]["scenario"], "mcp")
        for identifier in ("tool_listed", "tool_called", "tool_then_answer_order", "tool_result_used",
                           "agent_definition_model_reference", "response_agent_reference"):
            self.assertEqual(checks[identifier]["status"], "confirmed", identifier)

    def test_manual_marks_are_recorded_and_kept_across_runs(self):
        marks = ["--mark", "console_model_settings=confirmed:seen on the agent details tab",
                 "--mark", "apim_gateway_log=failed"]
        evidence, _, _ = self.capture("unit-marks", self.mcp_world(), ["--scenario", "mcp"], marks)
        rows = {row["id"]: row for row in evidence["manual_checks"]}
        self.assertEqual((rows["console_model_settings"]["status"], rows["apim_gateway_log"]["status"]),
                         ("confirmed", "failed"))
        self.assertEqual(rows["console_model_settings"]["note"], "seen on the agent details tab")
        self.assertTrue(rows["console_model_settings"]["marked_at"])
        self.assertEqual(rows["console_tool_history"]["status"], "unverified")
        code, _, _ = self.run_script(collect_evidence, ["--label", "unit-marks", "--connection", CONNECTION],
                                     self.mcp_world())
        again = {row["id"]: row for row in self.read_saved("unit-marks.json")["manual_checks"]}
        self.assertEqual((code, again["console_model_settings"]["status"]), (0, "confirmed"))

    def test_offline_mode_only_applies_marks_to_existing_evidence(self):
        self.capture("unit-offline", self.mcp_world(), ["--scenario", "mcp"])
        before = self.read_saved("unit-offline.json")
        guard = AssertionError("cloud access in offline mode")
        with patch.object(common, "connect", side_effect=guard):
            code, _, _ = self.run_script(collect_evidence, ["--label", "unit-offline", "--offline",
                                                            "--mark", "tracing_visible=confirmed"], live=False)
        after = self.read_saved("unit-offline.json")
        self.assertEqual(code, 0)
        self.assertEqual(after["checks"], before["checks"])
        self.assertEqual({row["id"]: row["status"] for row in after["manual_checks"]}["tracing_visible"], "confirmed")

    def test_no_inputs_confirm_nothing(self):
        code, _, _ = self.run_script(collect_evidence, ["--label", "unit-empty", "--offline"], live=False)
        evidence = self.read_saved("unit-empty.json")
        statuses = {row["status"] for row in evidence["checks"] + evidence["manual_checks"]}
        self.assertEqual(code, 0)
        self.assertNotIn("confirmed", statuses)
        self.assertNotIn("failed", statuses)

    def test_invalid_input_exits_with_config_error(self):
        cases = (["--label", "../x"], ["--label", "ok", "--mark", "nonexistent=confirmed"],
                 ["--label", "ok", "--mark", "tracing_visible=maybe"], ["--label", "ok", "--response-id", "bad id"],
                 ["--label", "ok", "--agent-name", "bad_name"])
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, err = self.run_script(collect_evidence, argv, fake_clients(), live=False)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("config error", err)

    def test_missing_run_file_content_is_a_config_error(self):
        (Path(self.tmp.name) / "unit-broken.run.json").write_text("[1, 2]", encoding="utf-8")
        code, out, err = self.run_script(collect_evidence, ["--label", "unit-broken", "--offline"], live=False)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("not a JSON object", err)


class FakeClock:
    """Monotonic clock of the series runner: sleeping advances it, and every sleep is recorded."""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def fake_post(path):
    return types.SimpleNamespace(method="POST", url=types.SimpleNamespace(path=path))


class TimedResponses(FakeResponses):
    """Every create() takes fake time and puts fake POSTs on the shared counter, a failing call included."""

    def __init__(self, clock, counter, replies=(), duration=10.5, posts=(), on_create=None):
        super().__init__(replies)
        self.clock, self.counter, self.duration = clock, counter, duration
        self.posts, self.on_create = list(posts), on_create

    def create(self, **kwargs):
        if self.on_create:
            self.on_create(len(self.calls))
        for _ in range(self.posts.pop(0) if self.posts else 1):
            self.counter(fake_post("/openai/v1/responses"))
        self.clock.now += self.duration
        return super().create(**kwargs)


class CountingConversations(FakeConversations):
    def __init__(self, counter):
        super().__init__()
        self.counter = counter

    def create(self):
        self.counter(fake_post("/openai/v1/conversations"))
        return super().create()


def series_clients(clock, replies, counted=True, **options):
    counter = common.HttpRequestCounter()
    agents = FakeAgents(latest={"version": "1", "definition": stored_definition(tools=[MCP_TOOL_RECORD])})
    openai = types.SimpleNamespace(responses=TimedResponses(clock, counter, replies, **options),
                                   conversations=CountingConversations(counter) if counted else FakeConversations())
    project = types.SimpleNamespace(agents=agents, connections=FakeConnections())
    return common.Clients(project=project, openai=openai, models=FAKE_MODELS, http=counter if counted else None)


class RunSeriesTests(ScriptCase):
    ABLATION = "byom-demo-mcp-ablation-fp8"
    WEAK = "byom-demo-mcp-agent-fp8"
    STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00")
    SEARCH_OUTPUT = f"Title: BYOM\nLink: {DOC_URL}"

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        numbers = itertools.count(1)
        patcher = patch.object(run_series, "secrets",
                               types.SimpleNamespace(token_hex=lambda _size: f"{next(numbers):08x}"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def argv(self, label, *extra, calls=3):
        return ["--call", f"{self.ABLATION}:1", "--repeat", str(calls), "--expect-calls", str(calls),
                "--label", label, *extra]

    def answer(self, number):
        return f"요약입니다. {DOC_URL} r-{number:08x}"

    def mcp_reply(self, number, **options):
        output = [list_tools(), mcp_call(self.SEARCH_OUTPUT), message(self.answer(number))]
        return response(output, f"resp_{number}", **options)

    def run_calls(self, argv, replies, **options):
        clients = series_clients(self.clock, replies, **options)
        code, out, err = self.run_script(run_series, argv, clients, clock=self.clock, sleep=self.clock.sleep)
        return clients, code, out, err

    def saved_names(self):
        return sorted(path.name for path in Path(self.tmp.name).iterdir())

    def test_dry_run_lists_the_interleaved_schedule_without_files(self):
        argv = ["--call", f"{self.WEAK}:1", "--call", f"{self.ABLATION}:2", "--repeat", "2", "--label", "unit-plan",
                "--dry-run"]
        code, out, err = self.run_script(run_series, argv, live=False)
        plan = json.loads(out)["plan"]
        calls = plan["calls"]
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((plan["label"], plan["series_file"], plan["planned_calls"], plan["min_gap_s"]),
                         ("unit-plan", "unit-plan.series.json", 4, 60.0))
        self.assertIn("no retry", plan["stop_rule"])
        self.assertEqual([call["index"] for call in calls], [1, 2, 3, 4])
        self.assertEqual([(call["agent_reference"]["name"], call["agent_reference"]["version"]) for call in calls],
                         [(self.WEAK, "1"), (self.ABLATION, "2")] * 2)
        self.assertEqual([call["run_file"] for call in calls], [f"unit-plan-0{number}.run.json" for number in range(1, 5)])
        self.assertEqual([call["nonce"] for call in calls], [f"r-{number:08x}" for number in range(1, 5)])
        for call in calls:
            self.assertIn(call["nonce"], call["input"])
            self.assertEqual(call["request_options"],
                             {"extra_body": {"agent_reference": call["agent_reference"]}, "max_output_tokens": 384,
                              "timeout": 180, "conversation": "<new conversation>"})
            self.assertEqual((call["use_conversation"], call["max_rounds"]), (True, 4))
        self.assertEqual(self.saved_names(), [])

    def test_call_values_need_a_valid_name_and_an_explicit_version(self):
        for value in ("byom-demo-agent", "byom-demo-agent:", "byom-demo-agent:bad version", "Bad_Name:1", ":1"):
            with self.subTest(call=value):
                argv = ["--call", value, "--label", "unit-bad", "--dry-run"]
                code, out, err = self.run_script(run_series, argv, live=False)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("config error", err)

    def test_labels_must_be_valid_and_leave_room_for_the_call_number(self):
        for label in ("", "Upper", "unit-x.series", "unit-x.run", "a" * 63):
            with self.subTest(label=label):
                argv = ["--call", f"{self.ABLATION}:1", "--label", label, "--dry-run"]
                code, out, err = self.run_script(run_series, argv, live=False)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("config error", err)

    def test_limits_are_refused_before_any_client_call(self):
        cases = (
            (["--repeat", "0"], "--repeat must be between"),
            (["--repeat", "13"], "--repeat must be between"),
            (["--call", f"{self.WEAK}:1", "--repeat", "7"], "at most 12 are allowed"),
            (["--expect-calls", "2"], "--expect-calls 2 does not match the 1 scheduled calls"),
            (["--min-gap-s", "14.9"], "--min-gap-s must be between"),
            (["--min-gap-s", "nan"], "--min-gap-s must be between"),
            (["--min-gap-s", "3601"], "--min-gap-s must be between"),
            (["--max-output-tokens", "0"], "--max-output-tokens must be at least 1"),
            (["--timeout", "0"], "--timeout must be above 0"),
            (["--timeout", "601"], "--timeout must be above 0"),
            (["--timeout", "nan"], "--timeout must be above 0"),
            (["--max-rounds", "0"], "--max-rounds must be at least 1"),
        )
        for extra, fragment in cases:
            with self.subTest(extra=extra):
                clients = series_clients(self.clock, [self.mcp_reply(1)])
                argv = ["--call", f"{self.ABLATION}:1", "--expect-calls", "1", "--label", "unit-limits", *extra]
                code, out, err = self.run_script(run_series, argv, clients, clock=self.clock, sleep=self.clock.sleep)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(fragment, err)
                self.assertEqual((clients.openai.responses.calls, clients.openai.conversations.created), ([], 0))
                self.assertEqual(self.saved_names(), [])

    def test_a_live_run_needs_expect_calls(self):
        clients = series_clients(self.clock, [self.mcp_reply(1)])
        argv = ["--call", f"{self.ABLATION}:1", "--label", "unit-noexpect"]
        code, out, err = self.run_script(run_series, argv, clients, clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--expect-calls is required", err)
        self.assertEqual((clients.openai.responses.calls, clients.openai.conversations.created), ([], 0))

    def test_calls_run_in_order_with_a_gap_and_a_record_per_call(self):
        clients, code, out, err = self.run_calls(self.argv("unit-a"), [self.mcp_reply(number) for number in (1, 2, 3)])
        series = json.loads(out)
        calls = series["calls"]
        self.assertEqual(code, 0)
        self.assertEqual(self.read_saved("unit-a.series.json"), series)
        self.assertEqual(self.saved_names(), ["unit-a-01.run.json", "unit-a-02.run.json", "unit-a-03.run.json",
                                              "unit-a.series.json"])
        sent = clients.openai.responses.calls
        self.assertEqual(len(sent), 3)
        for number, kwargs in enumerate(sent, start=1):
            self.assertEqual(kwargs["extra_body"]["agent_reference"],
                             {"type": "agent_reference", "name": self.ABLATION, "version": "1"})
            self.assertEqual((kwargs["conversation"], kwargs["max_output_tokens"], kwargs["timeout"]),
                             (CONVERSATION, 384, 180))
            self.assertIn(f"r-{number:08x}", kwargs["input"])
            self.assertFalse({"tools", "max_tool_calls", "previous_response_id"} & set(kwargs))
        self.assertEqual([call["gap_before_s"] for call in calls], [None, 60.0, 60.0])
        self.assertEqual(self.clock.sleeps, [60.0, 60.0])
        for number, call in enumerate(calls, start=1):
            with self.subTest(call=number):
                self.assertEqual((call["index"], call["agent"], call["version"], call["nonce"]),
                                 (number, self.ABLATION, "1", f"r-{number:08x}"))
                self.assertEqual((call["ok"], call["stopped"], call["error"], call["model_error"]),
                                 (True, "completed", None, False))
                self.assertEqual(call["duration_ms"], 10500)
                self.assertEqual(call["client_http"], {"requests": 2, "responses_posts": 1})
                self.assertEqual(call["item_sequence"], ["mcp_list_tools", "mcp_call", "message"])
                self.assertTrue(call["mcp_called"])
                self.assertEqual(call["mcp_calls"], [{"name": "microsoft_docs_search", "status": "completed",
                                                      "arguments_object": True, "query_chars": 4,
                                                      "output_chars": len(self.SEARCH_OUTPUT)}])
                self.assertEqual((call["answer_chars"], call["final_answer_contains_nonce"], call["think_tag"]),
                                 (len(self.answer(number)), True, False))
                self.assertEqual((call["response_model"], call["response_id"], call["conversation_id"]),
                                 ("fake-backend-model", f"resp_{number}", CONVERSATION))
                self.assertEqual((call["usage"]["input_tokens"], call["usage"]["output_tokens"]), (120, 30))
                self.assertEqual((call["contaminated"], call["contamination_note"]), (None, None))
                for key in ("started_at", "first_request_at", "finished_at"):
                    self.assertTrue(self.STAMP.fullmatch(call[key]), call[key])
                self.assertLessEqual(call["started_at"], call["first_request_at"])
                self.assertLessEqual(call["first_request_at"], call["finished_at"])
                self.assertEqual(call["first_request_at"], call["rounds"][0]["requested_at"])
                self.assertEqual(set(call["rounds"][0]), {"requested_at", "responded_at", "elapsed_s", "response_id",
                                                          "status", "usage", "output_item_types"})
        for before, after in zip(calls, calls[1:]):
            self.assertLessEqual(before["finished_at"], after["started_at"])
        self.assertEqual((series["schema"], series["planned_calls"], series["stopped_early"], series["stop_reason"],
                          series["error"]), ("foundry-byom-series/1", 3, False, None, None))
        self.assertTrue(self.STAMP.fullmatch(series["generated_at"]))
        self.assertEqual(series["plan"], {
            "calls": [f"{self.ABLATION}:1"] * 3, "scenario": "mcp", "custom_prompt": False, "min_gap_s": 60.0,
            "expect_calls": 3, "continue_on_model_error": False, "stop_rule": run_series.STOP_RULE,
            "limits": {"max_output_tokens": 384, "max_tool_calls": 0, "max_rounds": 4, "timeout_s": 180}})
        self.assertEqual(list(series["agents"]), [f"{self.ABLATION}:1"])
        self.assertEqual(series["summary"], {
            "calls": 3, "ok": 3, "mcp_called": 3, "ok_without_mcp": 0, "model_error": 0,
            "by_agent": {f"{self.ABLATION}:1": {"calls": 3, "ok": 3, "mcp_called": 3, "ok_without_mcp": 0,
                                                "model_error": 0}}})
        for fragment in (f"precheck ok: {self.ABLATION}:1", "call 2/3: waiting for the 60 s gap",
                         "call 3/3: waiting for the 60 s gap", f"call 3/3 {self.ABLATION}:1 ok=True",
                         "duration_ms=10500", "items=mcp_list_tools>mcp_call>message", "usage=120/30",
                         "client_http={'requests': 2, 'responses_posts': 1}", "error=-", "saved unit-a.series.json"):
            self.assertIn(fragment, err)

    def test_custom_limits_reach_every_request_and_the_series_file(self):
        argv = self.argv("unit-custom", "--max-output-tokens", "200", "--max-tool-calls", "2", "--timeout", "90",
                         "--max-rounds", "2", "--min-gap-s", "20", calls=2)
        clients, code, out, _ = self.run_calls(argv, [self.mcp_reply(1), self.mcp_reply(2)])
        series = json.loads(out)
        self.assertEqual(code, 0)
        for kwargs in clients.openai.responses.calls:
            self.assertEqual((kwargs["max_output_tokens"], kwargs["max_tool_calls"], kwargs["timeout"]), (200, 2, 90))
        self.assertEqual(self.clock.sleeps, [20.0])
        self.assertEqual(series["plan"]["limits"],
                         {"max_output_tokens": 200, "max_tool_calls": 2, "max_rounds": 2, "timeout_s": 90})
        self.assertEqual(series["plan"]["min_gap_s"], 20.0)

    def test_wait_for_gap_sleeps_only_for_the_missing_time(self):
        clock = FakeClock(100.0)
        self.assertIsNone(run_series.wait_for_gap(None, 60.0, clock, clock.sleep))
        self.assertEqual(run_series.wait_for_gap(40.0, 60.0, clock, clock.sleep), 60.0)
        self.assertEqual(run_series.wait_for_gap(10.0, 60.0, clock, clock.sleep), 90.0)
        self.assertEqual(clock.sleeps, [])
        falls_short = iter([10.0])

        def short_sleep(seconds):
            clock.sleeps.append(round(seconds, 3))
            clock.now += next(falls_short, seconds)

        self.assertEqual(run_series.wait_for_gap(84.0, 32.0, clock, short_sleep), 32.0)
        self.assertEqual(clock.sleeps, [16.0, 6.0])

    def test_the_series_stops_at_the_first_call_that_does_not_complete(self):
        failed = response([], status="failed", error={"code": "model_error", "message": "model failed"})
        incomplete = response([message("부분")], status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        cases = (
            (ApiError(500, "model failed", code="model_error"), 1, "error", True,
             "call 2 did not complete: error (500 model_error)"),
            (ApiError(500, "The model produced invalid content", api_type="model_error"), 1, "error", True,
             "call 2 did not complete: error (500 model_error)"),
            (ApiError(503, "backend not ready", code="backend_unavailable"), 1, "error", False,
             "call 2 did not complete: error (503 backend_unavailable)"),
            (ApiError(503, "backend not ready", code="model_error", api_type="server_error"), 1, "error", False,
             "call 2 did not complete: error (503 model_error server_error)"),
            (TimeoutError("request timed out"), 1, "error", False, "call 2 did not complete: error"),
            (failed, 1, "failed", True, "call 2 did not complete: failed (model_error)"),
            (response([], status="failed", error="boom"), 1, "failed", False, "call 2 did not complete: failed"),
            (incomplete, 3, "incomplete", False, "call 2 did not complete: incomplete"),
            (response([list_tools(), approval_request()]), 3, "approval_required:microsoft_docs_search", False,
             "call 2 did not complete: approval_required:microsoft_docs_search"),
        )
        for number, (failure, expected_code, stopped, is_model_error, reason) in enumerate(cases, start=1):
            with self.subTest(reason=reason):
                self.clock = FakeClock()
                label = f"unit-stop-{number}"
                replies = [self.mcp_reply(1), failure, self.mcp_reply(3)]
                clients, code, out, err = self.run_calls(self.argv(label), replies)
                series = json.loads(out)
                calls = series["calls"]
                self.assertEqual(code, expected_code)
                self.assertEqual((series["stopped_early"], series["stop_reason"], series["planned_calls"]),
                                 (True, reason, 3))
                self.assertEqual(len(clients.openai.responses.calls), 2)
                self.assertEqual(len(clients.openai.responses.replies), 1)
                self.assertEqual([call["ok"] for call in calls], [True, False])
                self.assertEqual((calls[1]["stopped"], calls[1]["model_error"]), (stopped, is_model_error))
                self.assertEqual(calls[1]["client_http"], {"requests": 2, "responses_posts": 1})
                self.assertEqual((series["summary"]["calls"], series["summary"]["ok"], series["summary"]["model_error"]),
                                 (2, 1, int(is_model_error)))
                self.assertEqual(self.clock.sleeps, [60.0])
                self.assertTrue((Path(self.tmp.name) / f"{label}-02.run.json").exists())
                self.assertFalse((Path(self.tmp.name) / f"{label}-03.run.json").exists())
                self.assertIn("call 2/3 ", err)
                self.assertNotIn("call 3/3", err)

    def test_a_failure_of_the_first_call_stops_without_any_wait(self):
        failure = ApiError(503, "backend not ready", code="backend_unavailable")
        clients, code, out, _ = self.run_calls(self.argv("unit-first"), [failure, self.mcp_reply(2)])
        series = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(series["stop_reason"], "call 1 did not complete: error (503 backend_unavailable)")
        self.assertEqual(len(series["calls"]), 1)
        self.assertIsNone(series["calls"][0]["gap_before_s"])
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(len(clients.openai.responses.calls), 1)

    def test_model_error_means_a_500_or_a_failed_response_with_that_service_error(self):
        cases = (
            ({"status_code": 500, "code": None, "api_type": "model_error"}, True),
            ({"status_code": 500, "code": "model_error", "api_type": "server_error"}, True),
            ({"status_code": None, "code": "model_error"}, True),
            ({"status_code": 503, "code": "model_error", "api_type": "server_error"}, False),
            ({"status_code": 429, "code": "model_error"}, False),
            ({"status_code": 500, "code": "backend_unavailable", "api_type": "server_error"}, False),
            ({"status_code": 500, "code": None}, False),
            ({"status_code": None, "code": "timeout"}, False),
            ({}, False),
            (None, False),
            ("model_error", False),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertIs(run_series.is_model_error(error), expected)

    def test_error_summaries_keep_the_service_error_type_and_survive_a_plain_string(self):
        error = {"type": "InternalServerError", "status_code": 500, "code": None, "api_type": "model_error",
                 "request_id": "1eba5b6a", "message": "m", "details": [{"code": "x"}]}
        self.assertEqual(run_series.brief_error(error), {"type": "InternalServerError", "status_code": 500,
                                                         "api_type": "model_error", "request_id": "1eba5b6a",
                                                         "message": "m"})
        self.assertIsNone(run_series.brief_error(None))
        self.assertEqual(run_series.brief_error("boom"), {"message": "boom"})
        self.assertEqual(run_series.failure_labels(error), ["500", "model_error"])
        self.assertEqual(run_series.failure_labels({"status_code": 503, "code": "model_error", "api_type": "server_error"}),
                         ["503", "model_error", "server_error"])
        self.assertEqual((run_series.failure_labels("boom"), run_series.failure_labels(None)), ([], []))

    def test_a_model_error_is_recorded_and_the_series_goes_on_with_the_option(self):
        failure = ApiError(500, "The model produced invalid content", api_type="model_error")
        replies = [self.mcp_reply(1), failure, self.mcp_reply(3)]
        clients, code, out, err = self.run_calls(self.argv("unit-go", "--continue-on-model-error"), replies)
        series = json.loads(out)
        calls = series["calls"]
        self.assertEqual(code, 0)
        self.assertEqual((series["stopped_early"], series["stop_reason"], series["error"]), (False, None, None))
        self.assertEqual((len(clients.openai.responses.calls), clients.openai.conversations.created), (3, 3))
        self.assertEqual([call["ok"] for call in calls], [True, False, True])
        self.assertEqual([call["model_error"] for call in calls], [False, True, False])
        self.assertEqual((calls[1]["stopped"], calls[1]["error"]["status_code"], calls[1]["error"]["api_type"]),
                         ("error", 500, "model_error"))
        self.assertEqual([call["gap_before_s"] for call in calls], [None, 60.0, 60.0])
        self.assertEqual(self.clock.sleeps, [60.0, 60.0])
        self.assertEqual(self.saved_names(), ["unit-go-01.run.json", "unit-go-02.run.json", "unit-go-03.run.json",
                                              "unit-go.series.json"])
        self.assertEqual(self.read_saved("unit-go.series.json"), series)
        self.assertEqual((series["summary"]["calls"], series["summary"]["ok"], series["summary"]["model_error"]),
                         (3, 2, 1))
        self.assertEqual((series["plan"]["continue_on_model_error"], series["plan"]["stop_rule"]),
                         (True, run_series.CONTINUE_RULE))
        self.assertIn("call 2/3 byom-demo-mcp-ablation-fp8:1 ok=False", err)
        self.assertIn("error=ApiError/500/model_error", err)
        self.assertIn("call 3/3 byom-demo-mcp-ablation-fp8:1 ok=True", err)

    def test_the_option_still_stops_at_every_failure_that_is_not_a_model_error(self):
        incomplete = response([message("부분")], status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        cases = (
            ApiError(503, "backend not ready", code="model_error", api_type="server_error"),
            ApiError(429, "slow down", code="rate_limit_exceeded"),
            ApiError(401, "unauthorized"),
            TimeoutError("request timed out"),
            incomplete,
            response([list_tools(), approval_request()]),
        )
        for number, failure in enumerate(cases, start=1):
            with self.subTest(number=number):
                self.clock = FakeClock()
                label = f"unit-opt-stop-{number}"
                clients, code, out, err = self.run_calls(self.argv(label, "--continue-on-model-error"),
                                                         [self.mcp_reply(1), failure, self.mcp_reply(3)])
                series = json.loads(out)
                self.assertNotEqual(code, 0)
                self.assertTrue(series["stopped_early"])
                self.assertTrue(series["stop_reason"].startswith("call 2 did not complete: "))
                self.assertEqual([call["ok"] for call in series["calls"]], [True, False])
                self.assertFalse(series["calls"][1]["model_error"])
                self.assertEqual(len(clients.openai.responses.calls), 2)
                self.assertEqual(self.clock.sleeps, [60.0])
                self.assertNotIn("call 3/3", err)

    def test_a_model_error_followed_by_another_failure_stops_at_that_failure(self):
        replies = [ApiError(500, "invalid content", api_type="model_error"),
                   ApiError(503, "backend not ready", code="backend_unavailable"), self.mcp_reply(3)]
        clients, code, out, _ = self.run_calls(self.argv("unit-two-fail", "--continue-on-model-error"), replies)
        series = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(series["stop_reason"], "call 2 did not complete: error (503 backend_unavailable)")
        self.assertEqual([call["model_error"] for call in series["calls"]], [True, False])
        self.assertEqual(series["summary"]["model_error"], 1)
        self.assertEqual(len(clients.openai.responses.calls), 2)
        self.assertEqual(self.clock.sleeps, [60.0])

    def test_a_model_error_stops_the_series_without_the_option(self):
        replies = [self.mcp_reply(1), ApiError(500, "invalid content", api_type="model_error"), self.mcp_reply(3)]
        clients, code, out, _ = self.run_calls(self.argv("unit-no-opt"), replies)
        series = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(series["stop_reason"], "call 2 did not complete: error (500 model_error)")
        self.assertEqual((len(series["calls"]), series["calls"][1]["model_error"]), (2, True))
        self.assertEqual(len(clients.openai.responses.calls), 2)

    def test_dry_run_shows_whether_a_model_error_lets_the_series_go_on(self):
        for extra, rule in (([], run_series.STOP_RULE), (["--continue-on-model-error"], run_series.CONTINUE_RULE)):
            with self.subTest(extra=extra):
                argv = ["--call", f"{self.ABLATION}:1", "--label", "unit-rule", "--dry-run", *extra]
                code, out, _ = self.run_script(run_series, argv, live=False)
                plan = json.loads(out)["plan"]
                self.assertEqual(code, 0)
                self.assertEqual((plan["continue_on_model_error"], plan["stop_rule"]), (bool(extra), rule))
        self.assertIn("no retry", run_series.CONTINUE_RULE)

    def test_precheck_only_reads_the_agent_versions_and_sends_no_model_request(self):
        clients = series_clients(self.clock, [self.mcp_reply(1)])
        clients.project.agents.get_version = Mock(wraps=clients.project.agents.get_version)
        argv = ["--call", f"{self.ABLATION}:1", "--call", f"{self.WEAK}:1", "--repeat", "2", "--label", "unit-pre",
                "--precheck-only"]
        code, out, err = self.run_script(run_series, argv, clients, clock=self.clock, sleep=self.clock.sleep)
        result = json.loads(out)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((result["action"], result["ok"]), ("precheck", True))
        self.assertEqual(list(result["agents"]), [f"{self.ABLATION}:1", f"{self.WEAK}:1"])
        self.assertEqual(result["agents"][f"{self.ABLATION}:1"]["model"], MODEL_REF)
        self.assertEqual(clients.project.agents.get_version.call_count, 2)
        self.assertEqual((clients.openai.responses.calls, clients.openai.conversations.created), ([], 0))
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.saved_names(), [])

    def test_precheck_only_reports_a_missing_agent_version_and_exits_1(self):
        clients = series_clients(self.clock, [self.mcp_reply(1)])
        argv = ["--call", f"{self.ABLATION}:9", "--label", "unit-pre-missing", "--precheck-only"]
        code, out, _ = self.run_script(run_series, argv, clients, clock=self.clock, sleep=self.clock.sleep)
        result = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual((result["action"], result["ok"], result["stage"]), ("precheck", False, "agents.get_version"))
        self.assertEqual(result["error"]["status_code"], 404)
        self.assertEqual((clients.openai.responses.calls, clients.openai.conversations.created), ([], 0))
        self.assertEqual(self.saved_names(), [])

    def test_precheck_only_cannot_be_combined_with_a_dry_run(self):
        argv = ["--call", f"{self.ABLATION}:1", "--label", "unit-pre-dry", "--precheck-only", "--dry-run"]
        code, out, err = self.run_script(run_series, argv, live=False)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--precheck-only", err)

    def test_precheck_only_builds_no_openai_client(self):
        for extra, with_openai in ((["--precheck-only"], False), (["--expect-calls", "1"], True)):
            with self.subTest(extra=extra):
                clients = series_clients(self.clock, [self.mcp_reply(1)])
                argv = ["--call", f"{self.ABLATION}:1", "--label", "unit-pre-client", *extra]
                with patch.object(common, "open_clients", Mock(return_value=clients)) as opened:
                    code, _, _ = self.run_script(run_series, argv, None, clock=self.clock, sleep=self.clock.sleep)
                self.assertEqual(code, 0)
                self.assertIs(opened.call_args.kwargs["with_openai"], with_openai)

    def test_a_completed_call_without_the_mcp_tool_is_a_result_and_not_a_stop(self):
        without_tool = response([message(self.answer(2))], "resp_2")
        _, code, out, _ = self.run_calls(self.argv("unit-nomcp"), [self.mcp_reply(1), without_tool, self.mcp_reply(3)])
        series = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual([call["mcp_called"] for call in series["calls"]], [True, False, True])
        self.assertEqual((series["calls"][1]["item_sequence"], series["calls"][1]["mcp_calls"]), (["message"], []))
        self.assertEqual({key: series["summary"][key] for key in ("calls", "ok", "mcp_called", "ok_without_mcp",
                                                                   "model_error")},
                         {"calls": 3, "ok": 3, "mcp_called": 2, "ok_without_mcp": 1, "model_error": 0})
        self.assertEqual((series["stopped_early"], series["stop_reason"]), (False, None))

    def test_the_summary_counts_each_agent_version_on_its_own(self):
        replies = [self.mcp_reply(1), self.mcp_reply(2), response([message(self.answer(3))], "resp_3"),
                   self.mcp_reply(4)]
        argv = ["--call", f"{self.WEAK}:1", "--call", f"{self.ABLATION}:1", "--repeat", "2", "--expect-calls", "4",
                "--label", "unit-two"]
        clients, code, out, _ = self.run_calls(argv, replies)
        series = json.loads(out)
        order = [self.WEAK, self.ABLATION, self.WEAK, self.ABLATION]
        self.assertEqual(code, 0)
        self.assertEqual([call["agent"] for call in series["calls"]], order)
        self.assertEqual([kwargs["extra_body"]["agent_reference"]["name"] for kwargs in clients.openai.responses.calls],
                         order)
        self.assertEqual(list(series["agents"]), [f"{self.WEAK}:1", f"{self.ABLATION}:1"])
        self.assertEqual(series["summary"]["by_agent"], {
            f"{self.WEAK}:1": {"calls": 2, "ok": 2, "mcp_called": 1, "ok_without_mcp": 1, "model_error": 0},
            f"{self.ABLATION}:1": {"calls": 2, "ok": 2, "mcp_called": 2, "ok_without_mcp": 0, "model_error": 0}})

    def test_mcp_evidence_keeps_lengths_and_never_the_query_text(self):
        searched = dict(mcp_call("o" * 40), arguments=json.dumps({"query": "QUERY-" + "x" * 594}))
        broken = dict(mcp_call(None, status="failed", error={"code": "tool_error", "message": "boom"}),
                      arguments="not json {")
        reply = response([list_tools(), searched, broken, message("답변 r-00000001")], "resp_1")
        _, code, out, _ = self.run_calls(self.argv("unit-query", calls=1), [reply])
        call = json.loads(out)["calls"][0]
        self.assertEqual(code, 0)
        self.assertEqual(call["item_sequence"], ["mcp_list_tools", "mcp_call", "mcp_call", "message"])
        self.assertEqual(call["mcp_calls"], [
            {"name": "microsoft_docs_search", "status": "completed", "arguments_object": True, "query_chars": 600,
             "output_chars": 40},
            {"name": "microsoft_docs_search", "status": "failed", "arguments_object": False,
             "error": {"code": "tool_error", "message": "boom"}}])
        saved = (Path(self.tmp.name) / "unit-query.series.json").read_text(encoding="utf-8")
        for text in (out, saved):
            self.assertNotIn("QUERY-", text)

    def test_client_http_is_counted_per_call_and_absent_without_a_counter(self):
        _, code, out, _ = self.run_calls(self.argv("unit-http", calls=2), [self.mcp_reply(1), self.mcp_reply(2)],
                                         posts=[2, 1])
        self.assertEqual(code, 0)
        self.assertEqual([call["client_http"] for call in json.loads(out)["calls"]],
                         [{"requests": 3, "responses_posts": 2}, {"requests": 2, "responses_posts": 1}])
        _, code, out, _ = self.run_calls(self.argv("unit-nohttp", calls=1), [self.mcp_reply(1)], counted=False)
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out)["calls"][0]["client_http"])

    def test_a_failed_precheck_makes_no_model_call(self):
        argv = ["--call", f"{self.ABLATION}:9", "--expect-calls", "1", "--label", "unit-missing"]
        clients, code, out, err = self.run_calls(argv, [self.mcp_reply(1)])
        series = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual((clients.openai.responses.calls, clients.openai.conversations.created), ([], 0))
        self.assertEqual((series["stopped_early"], series["stop_reason"], series["calls"]),
                         (True, "precheck failed at agents.get_version", []))
        self.assertEqual(series["error"]["status_code"], 404)
        self.assertNotIn("precheck ok", err)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.saved_names(), ["unit-missing.series.json"])

    def test_the_precheck_reads_every_distinct_agent_version_once(self):
        clients = series_clients(self.clock, [self.mcp_reply(number) for number in (1, 2, 3, 4)])
        clients.project.agents.get_version = Mock(wraps=clients.project.agents.get_version)
        argv = ["--call", f"{self.ABLATION}:1", "--call", f"{self.WEAK}:1", "--repeat", "2", "--expect-calls", "4",
                "--label", "unit-precheck"]
        code, out, _ = self.run_script(run_series, argv, clients, clock=self.clock, sleep=self.clock.sleep)
        agents = json.loads(out)["agents"]
        self.assertEqual(code, 0)
        self.assertEqual([call.kwargs for call in clients.project.agents.get_version.call_args_list],
                         [{"agent_name": self.ABLATION, "agent_version": "1"},
                          {"agent_name": self.WEAK, "agent_version": "1"}])
        self.assertEqual(list(agents), [f"{self.ABLATION}:1", f"{self.WEAK}:1"])
        for view in agents.values():
            self.assertEqual((view["model"], view["temperature"]), (MODEL_REF, 1.0))
            self.assertEqual(view["instructions_chars"], len(common.DEFAULT_INSTRUCTIONS))
            self.assertRegex(view["instructions_sha256_12"], r"^[0-9a-f]{12}$")
            self.assertEqual([tool["type"] for tool in view["tools"]], ["mcp"])

    def test_chars_of_inverts_the_clip_suffix(self):
        self.assertEqual(run_series.chars_of("짧은 질의"), 5)
        self.assertEqual(run_series.chars_of(common.clip("가" * 500, 200)), 500)

    def test_the_short_request_id_is_recorded_and_the_whole_id_never_is(self):
        class Tagged(Model):
            _request_id = "fe49cff6b2f854e3d8ed52e2dfb9e48f"

        tagged = Tagged(**self.mcp_reply(1).data)
        _, code, out, _ = self.run_calls(self.argv("unit-rid", calls=1), [tagged])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["calls"][0]["rounds"][0]["request_id"], "fe49cff6")
        for text in (out, *(path.read_text(encoding="utf-8") for path in Path(self.tmp.name).iterdir())):
            self.assertNotIn("fe49cff6b2f8", text)

    def test_public_files_shorten_service_ids_and_private_copies_keep_them(self):
        private_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(private_tmp.cleanup)
        private = Path(private_tmp.name) / "raw"
        reply = response([list_tools(), mcp_call(self.SEARCH_OUTPUT), message(self.answer(1))], f"resp_{LONG_ID}",
                         conversation={"id": f"conv_{LONG_ID}"})
        clients = series_clients(self.clock, [reply])
        clients.openai.conversations.create = lambda: Model(id=f"conv_{LONG_ID}")
        with patch.dict(os.environ, {common.PRIVATE_DIR_ENV: str(private)}):
            code, out, err = self.run_script(run_series, self.argv("unit-ids", calls=1), clients,
                                             clock=self.clock, sleep=self.clock.sleep)
        names = ("unit-ids.series.json", "unit-ids-01.run.json")
        self.assertEqual(code, 0)
        for text in (out, *(self.tmp_file(name) for name in names)):
            self.assertNotIn(LONG_ID, text)
            self.assertIn("resp_0a1b2c3d…", text)
            self.assertIn("conv_0a1b2c3d…", text)
        for name in names:
            whole = (private / name).read_text(encoding="utf-8")
            self.assertIn(f"resp_{LONG_ID}", whole)
            self.assertIn(f"conv_{LONG_ID}", whole)
        self.assertNotIn(LONG_ID, err)

    def tmp_file(self, name):
        return (Path(self.tmp.name) / name).read_text(encoding="utf-8")

    def test_the_series_file_is_saved_before_each_call_starts(self):
        path = Path(self.tmp.name) / "unit-save.series.json"
        seen = []
        self.run_calls(self.argv("unit-save"), [self.mcp_reply(number) for number in (1, 2, 3)],
                       on_create=lambda _index: seen.append(len(json.loads(path.read_text(encoding="utf-8"))["calls"])))
        self.assertEqual(seen, [0, 1, 2])

    def test_a_custom_prompt_gets_the_trace_code_and_stays_out_of_the_series_file(self):
        argv = ["--call", f"{self.ABLATION}:1", "--repeat", "2", "--expect-calls", "2", "--label", "unit-prompt",
                "--prompt", "자유 질문입니다"]
        clients, code, out, _ = self.run_calls(argv, [self.mcp_reply(1), self.mcp_reply(2)])
        self.assertEqual(code, 0)
        self.assertEqual([kwargs["input"] for kwargs in clients.openai.responses.calls],
                         ["자유 질문입니다 [추적 ID: r-00000001]", "자유 질문입니다 [추적 ID: r-00000002]"])
        self.assertTrue(json.loads(out)["plan"]["custom_prompt"])
        self.assertNotIn("자유 질문입니다", out)
        self.assertNotIn("자유 질문입니다", self.tmp_file("unit-prompt.series.json"))

    def test_a_shared_nonce_repeats_the_same_prompt_in_every_call(self):
        nonce = "r-7acb8605"
        replies = [response([list_tools(), mcp_call(self.SEARCH_OUTPUT), message(f"요약입니다. {DOC_URL} {nonce}")],
                            f"resp_{number}") for number in (1, 2, 3)]
        clients, code, out, _ = self.run_calls([*self.argv("unit-shared"), "--nonce", nonce], replies)
        calls = json.loads(out)["calls"]
        expected = run_prompt.SCENARIO_PROMPTS["mcp"].format(nonce=nonce)
        self.assertEqual(code, 0)
        self.assertEqual([kwargs["input"] for kwargs in clients.openai.responses.calls], [expected] * 3)
        self.assertEqual([call["nonce"] for call in calls], [nonce] * 3)
        self.assertEqual([call["final_answer_contains_nonce"] for call in calls], [True] * 3)

    def test_a_shared_nonce_is_checked_offline_and_shown_in_the_dry_run(self):
        base = ["--call", f"{self.ABLATION}:1", "--repeat", "2", "--label", "unit-nonce", "--dry-run"]
        code, out, err = self.run_script(run_series, [*base, "--nonce", "r-7acb8605"], live=False)
        calls = json.loads(out)["plan"]["calls"]
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([call["nonce"] for call in calls], ["r-7acb8605"] * 2)
        self.assertEqual(calls[0]["input"], calls[1]["input"])
        for bad in ("bad nonce", "", "x" * 41):
            with self.subTest(nonce=bad):
                code, out, err = self.run_script(run_series, [*base, "--nonce", bad], live=False)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--nonce must match", err)
        self.assertEqual(self.saved_names(), [])

    def test_a_client_construction_error_is_reported_and_nothing_is_saved(self):
        with patch.object(common, "connect", side_effect=RuntimeError("boom")):
            code, out, _ = self.run_script(run_series, self.argv("unit-conn", calls=1),
                                           clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["action"], "error")
        self.assertEqual(self.saved_names(), [])


class OfflineTests(ScriptCase):
    def test_dry_runs_never_touch_the_network_or_the_sdk(self):
        guard = AssertionError("network or SDK access during --dry-run")
        scripts = ((create_agent, ["--dry-run", "--tool", "mcp"]), (run_prompt, ["--dry-run", "--scenario", "mcp"]),
                   (run_series, ["--dry-run", "--call", "byom-demo-mcp-ablation-fp8:1", "--label", "dry"]),
                   (inspect_connections, ["--dry-run", "--expect", "demo-conn=ax-k2-nvfp4"]),
                   (collect_evidence, ["--dry-run", "--label", "dry", "--mark", "tracing_visible=confirmed"]))
        with patch("socket.socket.connect", side_effect=guard), patch("socket.getaddrinfo", side_effect=guard), \
                patch.object(common, "open_clients", side_effect=guard), \
                patch.object(common, "make_credential", side_effect=guard):
            for module, argv in scripts:
                with self.subTest(script=module.__name__):
                    code, out, err = self.run_script(module, argv, live=False)
                    self.assertEqual((code, err), (0, ""))
                    self.assertEqual(json.loads(out)["action"], "dry-run")
                    self.assertNotIn(self.tmp.name, out)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])


@unittest.skipUnless(SDK_AVAILABLE, "azure-ai-projects is not installed")
class SdkCompatibilityTests(ScriptCase):
    def test_definitions_round_trip_through_the_real_models(self):
        function_tool = self.spec_for("--connection", CONNECTION, "--tool", "function")["tools"][0]
        mcp_spec = self.spec_for("--connection", CONNECTION, "--tool", "mcp")
        for label, spec in (("none", self.spec_for("--connection", CONNECTION)), ("function", self.spec_for(
                "--connection", CONNECTION, "--tool", "function")), ("mcp", mcp_spec),
                ("both", dict(mcp_spec, tools=[function_tool, *mcp_spec["tools"]]))):
            with self.subTest(label=label):
                definition = common.to_sdk_definition(spec, sdk_models)
                self.assertIsInstance(definition, sdk_models.PromptAgentDefinition)
                stored = common.to_plain(definition)
                self.assertEqual((stored["kind"], stored["model"]), ("prompt", MODEL_REF))
                self.assertTrue(common.covers(common.canonical_definition(stored), common.canonical_definition(spec)))

    def test_sdk_surface_used_by_the_scripts_exists(self):
        for name, required in (("get", ("agent_name",)), ("get_version", ("agent_name", "agent_version")),
                               ("create_version", ("agent_name", "definition")), ("delete", ("agent_name",))):
            parameters = inspect.signature(getattr(AgentsOperations, name)).parameters
            for parameter in required:
                self.assertIn(parameter, parameters, f"agents.{name}")
        create = inspect.signature(OpenAIResponses.create).parameters
        for parameter in ("input", "conversation", "previous_response_id", "max_output_tokens", "max_tool_calls",
                          "tools", "timeout", "extra_body"):
            self.assertIn(parameter, create, f"responses.create({parameter})")
        for parameter in ("order", "limit"):
            self.assertIn(parameter, inspect.signature(ConversationItems.list).parameters)
        kinds = [parameter.kind for parameter in inspect.signature(AIProjectClient.get_openai_client).parameters.values()]
        self.assertIn(inspect.Parameter.VAR_KEYWORD, kinds)
        self.assertIn("name", inspect.signature(ConnectionsOperations.get).parameters)
        self.assertIn("include_credentials", inspect.signature(ConnectionsOperations.get).parameters)
        self.assertTrue(callable(ConnectionsOperations.list))

    def test_real_connection_models_are_summarized_without_ids_or_hosts(self):
        summary = inspect_connections.describe_connection(
            sdk_models.Connection(connection_data("demo-conn", default=True)))
        self.assertEqual((summary["category"], summary["credential_type"], summary["is_default"],
                          summary["deployment_in_path"]), ("ApiManagement", "ApiKey", True, False))
        self.assertEqual((summary["target_scheme"], summary["target_path"]), ("https", "/demo/v1"))
        self.assertEqual(summary["models"], [{"name": "ax-k2-nvfp4", "format": "OpenAI", "version": "1"}])
        text = json.dumps(summary)
        for leaked in ("gateway.example.invalid", "/subscriptions", GUID):
            self.assertNotIn(leaked, text)
        leaking = connection_data("demo-conn")
        leaking["credentials"] = {"type": "ApiKey", "key": "leaked-key-value-1"}
        flagged = inspect_connections.describe_connection(sdk_models.Connection(leaking))
        self.assertTrue(flagged["credential_value_returned"])
        self.assertNotIn("leaked-key-value-1", common.dumps(flagged))

    def test_open_clients_targets_the_project_openai_route_without_network(self):
        class OfflineCredential:
            def get_token(self, *scopes, **kwargs):
                raise AssertionError("no token should be requested while building the clients")

        args = run_prompt.build_parser().parse_args(["--project-endpoint", ENDPOINT])
        guard = AssertionError("network access while building the clients")
        with patch("socket.socket.connect", side_effect=guard), patch("socket.getaddrinfo", side_effect=guard), \
                patch.object(common, "make_credential", return_value=OfflineCredential()):
            clients = common.open_clients(args)
        self.assertIsInstance(clients.project, AIProjectClient)
        self.assertEqual(str(clients.openai.base_url).rstrip("/"), ENDPOINT + "/openai/v1")
        self.assertEqual(clients.openai.max_retries, 0)
        self.assertIs(clients.models, sdk_models)
        self.assertIsInstance(clients.http, common.HttpRequestCounter)
        self.assertEqual(clients.http.summary(), {"requests": 0, "responses_posts": 0})
        with patch("socket.socket.connect", side_effect=guard), patch("socket.getaddrinfo", side_effect=guard), \
                patch.object(common, "make_credential", return_value=OfflineCredential()):
            project_only = common.open_clients(args, with_openai=False)
        self.assertIsInstance(project_only.project, AIProjectClient)
        self.assertIsNone(project_only.openai)

    def serve_loopback(self):
        """Loopback stand-in for the project endpoint: conversations succeed, every responses call gets a model-side 503."""
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                path = urlsplit(self.path).path
                seen.append((path, json.loads(self.rfile.read(size) or b"{}")))
                if path.endswith("/conversations"):
                    status, payload = 200, {"id": CONVERSATION, "object": "conversation"}
                else:
                    status, payload = 503, {"error": {
                        "code": "model_error", "type": "server_error", "param": None,
                        "message": "Model could not be accessed - Received 503 status code from model."}}
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if status == 503:
                    self.send_header("Retry-After", "1")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        for cleanup in (lambda: thread.join(5), server.server_close, server.shutdown):
            self.addCleanup(cleanup)
        return f"http://127.0.0.1:{server.server_address[1]}/api/projects/demo", seen

    def test_a_failing_call_reaches_the_service_once_and_the_counter_proves_it(self):
        class StaticCredential:
            def get_token(self, *scopes, **kwargs):
                return AccessToken("fake-token", int(time.time()) + 3600)

        endpoint, seen = self.serve_loopback()
        args = run_prompt.build_parser().parse_args(
            ["--project-endpoint", endpoint, "--max-output-tokens", "256", "--max-tool-calls", "0"])
        loopback = {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        with patch.dict(os.environ, loopback), patch.object(common, "make_credential", return_value=StaticCredential()):
            clients = common.open_clients(args)
            prompt, pairs = run_prompt.prepare(args)
            result = run_prompt.execute(args, clients, prompt, None, pairs)
        sent = [body for path, body in seen if path.endswith("/responses")]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["agent_reference"], {"type": "agent_reference", "name": "byom-demo-agent"})
        self.assertEqual((sent[0]["max_output_tokens"], sent[0]["conversation"]), (256, CONVERSATION))
        self.assertNotIn("max_tool_calls", sent[0])
        self.assertNotIn("timeout", sent[0])
        self.assertEqual(result["client_http"], {"requests": 2, "responses_posts": 1})
        self.assertEqual((result["stopped"], result["rounds"]), ("error", []))
        self.assertEqual((result["error"]["status_code"], result["error"]["code"]), (503, "model_error"))


class HygieneTests(unittest.TestCase):
    LEAKS = {
        "guid": common.PATTERNS["guid"],
        "email": common.PATTERNS["email"],
        "jwt": common.PATTERNS["jwt"],
        "resource host": re.compile(
            r"(?<![<\w-])[a-z0-9][a-z0-9-]{2,}\.(?:services\.ai\.azure\.com|openai\.azure\.com"
            r"|cognitiveservices\.azure\.com|azure-api\.net|azurewebsites\.net)\b", re.I),
    }
    RESOURCE_ID = re.compile(r"/subscriptions/(?!<)[^\s\"'<>,;)\[\\]+")

    def test_agent_files_and_evidence_hold_no_identifiers_or_secrets(self):
        files = [path for path in AGENT_DIR.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
        evidence = ROOT / "evidence" / "foundry-byom"
        if evidence.is_dir():
            files.extend(path for path in evidence.rglob("*") if path.is_file())
        self.assertTrue(files)
        found = []
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            patterns = dict(self.LEAKS)
            if path.suffix != ".py":
                patterns["resource id"] = self.RESOURCE_ID
            found += [f"{path.relative_to(ROOT).as_posix()}: {name}" for name, pattern in patterns.items()
                      if pattern.search(text)]
        self.assertEqual(found, [])

    def test_evidence_files_hold_no_whole_service_or_request_ids(self):
        evidence = ROOT / "evidence" / "foundry-byom"
        files = sorted(evidence.rglob("*.json")) if evidence.is_dir() else []
        found = [path.name for path in files
                 if any(pattern.search(path.read_text(encoding="utf-8", errors="replace"))
                        for pattern in (common.SERVICE_ID_RE, common.REQUEST_ID_RE))]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
