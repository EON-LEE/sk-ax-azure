"""Real standard MCP discovery/call on an in-process fixture, NOT enterprise Web IQ access."""
import json
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from unittest.mock import patch

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo" / "frontend"))
from agent import agent_response
from hub import Gate
from web_iq import MAX_RESPONSE, PublicSearchPermission, VERTICALS, WebIQ, WebIQProblem, citations
from workspace import Workspace
sys.path.pop(0)
from tests.test_demo_agent import FixtureHub, FixtureLink

SETTINGS = {
    "AXK2_WEBIQ_ENDPOINT": "https://enterprise.example.org/mcp",
    "AXK2_WEBIQ_BEARER_TOKEN": "fixture-secret-only",
    "AXK2_WEBIQ_ACCESS_APPROVED": "1",
    "AXK2_WEBIQ_TOOL": "public_lookup",
    "AXK2_WEBIQ_QUERY_FIELD": "q",
    "AXK2_WEBIQ_ARGUMENTS_JSON": '{"count": 2}',
}


@asynccontextmanager
async def provider_fixture():
    calls = []
    server = FastMCP("standard-MCP-fixture", stateless_http=True, json_response=True,
                    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @server.tool()
    async def public_lookup(q: str, count: int = 2) -> dict:
        calls.append({"q": q, "count": count})
        return {"results": [{"title": "Actual fixture provenance",
                             "url": "https://learn.microsoft.com/agent-framework/overview/",
                             "snippet": "Returned by the protocol fixture, not live Web IQ."}]}

    @server.tool()
    async def bounded_public_lookup(query: str, maxResults: int = 10, maxLength: int = 10000,
                                    contentFormat: Literal["passage", "html"] = "html",
                                    safeSearch: Literal["off", "strict"] = "strict") -> dict:
        arguments = {"query": query, "maxResults": maxResults, "maxLength": maxLength,
                     "contentFormat": contentFormat, "safeSearch": safeSearch}
        calls.append(arguments)
        return {"results": [{"url": "https://learn.microsoft.com/agent-framework/",
                             "title": "Bounded fixture", "passage": "Actual fixture text" * maxLength}]}

    app = server.streamable_http_app()
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return original_client(*args, transport=httpx.ASGITransport(app=app), **kwargs)

    async with server.session_manager.run():
        with patch("web_iq.httpx.AsyncClient", side_effect=client_factory):
            yield WebIQ(SETTINGS), calls


class WebIQTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_endpoint_means_no_operational_tool_or_network(self):
        provider = WebIQ({})
        await provider.prepare()
        self.assertEqual(provider.tools(), [])
        self.assertEqual(provider.status()["reason"], "enterprise_endpoint_required")
        self.assertFalse(provider.status()["verified_search"])
        self.assertNotIn("web_iq_search", [t.name for t in Workspace().tools()])
        with self.assertRaises(WebIQProblem):
            await provider.search("python")

    async def test_configuration_is_explicit_https_secure_and_approved(self):
        for change, reason in [
            ({"AXK2_WEBIQ_ENDPOINT": "http://127.0.0.1/mcp"}, "approved_https_endpoint_required"),
            ({"AXK2_WEBIQ_ENDPOINT": "https://user:secret@example.com/mcp"}, "approved_https_endpoint_required"),
            ({"AXK2_WEBIQ_ENDPOINT": "https://example.com/mcp?key=secret"}, "endpoint_must_not_contain_credentials_or_query"),
            ({"AXK2_WEBIQ_ACCESS_APPROVED": "0"}, "enterprise_access_and_query_approval_required"),
            ({"AXK2_WEBIQ_BEARER_TOKEN": ""}, "secure_provider_credential_required"),
            ({"AXK2_WEBIQ_TOOL": ""}, "provider_tool_and_query_field_required"),
            ({"AXK2_WEBIQ_ARGUMENTS_JSON": "[]"}, "invalid_provider_arguments"),
        ]:
            provider = WebIQ(dict(SETTINGS, **change))
            await provider.prepare()
            self.assertEqual(provider.status()["reason"], reason)
            self.assertNotIn("secret", json.dumps(provider.status()))

    async def test_verified_prior_art_api_key_contract_not_inference(self):
        provider = WebIQ({"AXK2_WEBIQ_API_KEY": "fixture-secret-only", "AXK2_WEBIQ_ACCESS_APPROVED": "1"})
        self.assertEqual(provider.endpoint, "https://api.microsoft.ai/v3/mcp")
        self.assertEqual((provider.name, provider.query_field), ("web", "query"))
        self.assertIsNone(provider.reason)
        self.assertNotIn("fixture-secret-only", str(provider.status()))

    async def test_real_sdk_initialize_discovery_query_and_provenance(self):
        async with provider_fixture() as (provider, calls):
            await provider.prepare()
            self.assertTrue(provider.ready, provider.reason)
            self.assertFalse(provider.verified_search)
            self.assertEqual([t.name for t in provider.tools()], ["web_iq_search"])
            result = json.loads(await provider.search("agent-framework"))
            self.assertEqual(calls, [{"q": "agent-framework", "count": 2}])
            self.assertEqual(result["citations"][0]["title"], "Actual fixture provenance")
            self.assertEqual(result["citations"][0]["provenance"]["snippet"],
                             "Returned by the protocol fixture, not live Web IQ.")
            self.assertTrue(provider.verified_search)
            await provider.search("Samsung stock latest news")
            self.assertEqual(len(calls), 2)

    async def test_discovered_contract_not_guessed_and_arguments_validated(self):
        async with provider_fixture() as (provider, calls):
            for change, reason in [
                ({"AXK2_WEBIQ_TOOL": "imaginary_search"}, "configured_tool_not_advertised"),
                ({"AXK2_WEBIQ_QUERY_FIELD": "query"}, "query_field_does_not_match_advertised_schema"),
                ({"AXK2_WEBIQ_ARGUMENTS_JSON": '{"count":"wrong type"}'}, "provider_discovery_failed"),
            ]:
                invalid = WebIQ(dict(SETTINGS, **change))
                await invalid.prepare()
                self.assertFalse(invalid.ready)
                self.assertEqual(invalid.reason, reason)
                self.assertEqual(invalid.tools(), [])
            self.assertEqual(calls, [])

    async def test_advertised_passage_bounds_avoid_oversized_html_defaults(self):
        async with provider_fixture() as (_, calls):
            provider = WebIQ(dict(SETTINGS, AXK2_WEBIQ_TOOL="bounded_public_lookup",
                                  AXK2_WEBIQ_QUERY_FIELD="query", AXK2_WEBIQ_ARGUMENTS_JSON="{}"))
            await provider.prepare()
            self.assertTrue(provider.ready, provider.reason)
            result = json.loads(await provider.search("agent-framework"))
            self.assertEqual(calls, [{"query": "agent-framework", "maxResults": 5,
                                     "contentFormat": "passage", "maxLength": 1500, "safeSearch": "strict"}])
            self.assertTrue(provider.verified_search)
            self.assertEqual(result["citations"][0]["title"], "Bounded fixture")
            self.assertLess(len(json.dumps(result).encode()), MAX_RESPONSE)
            self.assertNotIn("maxResults", (WebIQ(SETTINGS)).arguments)

    async def test_explicit_valid_provider_bounds_are_not_overwritten(self):
        async with provider_fixture() as (_, calls):
            provider = WebIQ(dict(SETTINGS, AXK2_WEBIQ_TOOL="bounded_public_lookup",
                                  AXK2_WEBIQ_QUERY_FIELD="query",
                                  AXK2_WEBIQ_ARGUMENTS_JSON='{"maxResults":2,"maxLength":800}'))
            await provider.prepare()
            self.assertTrue(provider.ready, provider.reason)
            await provider.search("python")
            self.assertEqual((calls[0]["maxResults"], calls[0]["maxLength"]), (2, 800))
            self.assertEqual((calls[0]["contentFormat"], calls[0]["safeSearch"]), ("passage", "strict"))

    async def test_actual_maf_function_action_replay_and_no_private_query(self):
        async with provider_fixture() as (provider, calls):
            space = Workspace()
            space.put("confidential.txt", b"never send this secret")
            link = FixtureLink("web_iq_search", {"q": "Python docs"})
            response = agent_response(FixtureHub(link), Gate(1, 2), {
                "messages": [{"role": "user", "content": "search public Python docs"}], "model": "axk2"},
                space, True, web_iq=provider)
            output = b"".join([chunk async for chunk in response.body_iterator]).decode()
            self.assertIn('"state": "success"', output)
            self.assertIn('"citations"', output)
            self.assertEqual(calls, [{"q": "Python docs", "count": 2}])
            self.assertNotIn("confidential", str(calls))
            self.assertNotIn("fixture-secret-only", output + str(link.requests))
            self.assertEqual(len(link.requests), 2)

    async def test_upload_derived_query_is_visible_error_without_provider_query(self):
        async with provider_fixture() as (provider, calls):
            link = FixtureLink("web_iq_search", {"q": "uploaded confidential contents"})
            space = Workspace()
            space.put("private.txt", b"uploaded confidential contents")
            response = agent_response(FixtureHub(link), Gate(1, 2), {
                "messages": [{"role": "user", "content": "invalid topic fixture"}], "model": "axk2"},
                space, True, web_iq=provider)
            output = b"".join([chunk async for chunk in response.body_iterator]).decode()
            self.assertIn('"state": "error"', output)
            self.assertNotIn('"state": "success"', output)
            self.assertEqual(calls, [])
            self.assertNotIn("fixture-secret-only", output)
            self.assertEqual(len(link.requests), 2)

    async def test_failure_redaction_and_size_bound(self):
        class Session:
            async def call_tool(self, *args):
                raise RuntimeError("secret-bearer-value")

        @asynccontextmanager
        async def factory():
            yield Session()

        provider = WebIQ(SETTINGS, session_factory=factory)
        provider.ready = True
        from jsonschema import Draft202012Validator
        provider.validator = Draft202012Validator({"type": "object"})
        provider.catalog[provider.name] = ({"type": "object", "properties": {"q": {"type": "string"}, "count": {"type": "integer"}}},
                                           provider.validator, "q", "fixture")
        with self.assertRaises(WebIQProblem) as caught:
            await provider.search("python")
        self.assertNotIn("secret-bearer-value", str(caught.exception))

        from mcp.types import CallToolResult, TextContent
        class LargeSession:
            async def call_tool(self, *args):
                return CallToolResult(content=[TextContent(type="text", text="x" * (MAX_RESPONSE + 1))])
        @asynccontextmanager
        async def large_factory():
            yield LargeSession()
        provider.session_factory = large_factory
        with self.assertRaisesRegex(WebIQProblem, "provider_response_limit"):
            await provider.search("python")
        self.assertFalse(provider.verified_search)

    def test_citations_are_returned_not_invented_or_active(self):
        result = citations({"results": [{"url": "https://learn.microsoft.com/x", "title": "real"},
                                        {"url": "javascript:alert(1)"}, {"url": "http://127.0.0.1/admin"}]})
        self.assertEqual([r["url"] for r in result], ["https://learn.microsoft.com/x"])


if __name__ == "__main__":
    unittest.main()
