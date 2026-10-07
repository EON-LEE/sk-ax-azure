"""Public vertical contracts and per-turn privacy; fixtures are not live provider evidence."""
import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from mcp.types import CallToolResult, TextContent

from tests.test_web_iq import WebIQ, WebIQProblem, PublicSearchPermission, VERTICALS
from tests.test_demo_agent import FixtureHub, FixtureLink
from agent import agent_response
from hub import Gate
from workspace import Workspace


def contracts():
    result = []
    for name in sorted(VERTICALS):
        field = "url" if name == "browse" else "query"
        props = {field: {"type": "string"}, "language": {"type": "string"}, "region": {"type": "string"}}
        if name in {"web", "news", "images", "videos", "places", "autosuggest"}:
            props["maxResults"] = {"type": "integer", "minimum": 1, "maximum": 50}
        if name in {"web", "news", "browse", "sonic"}:
            props.update({"maxLength": {"type": "integer", "minimum": 1, "maximum": 500000},
                          "contentFormat": {"type": "string", "enum": ["text", "html", "markdown"] +
                                            ([] if name == "browse" else ["passage"])}})
        if name in {"web", "images", "videos", "autosuggest"}:
            props["safeSearch"] = {"type": "string", "enum": ["off", "strict"]}
        if name == "browse":
            props.update({"liveCrawl": {"type": "string", "enum": ["none", "fallback", "force"]},
                          "renderDynamicPages": {"type": "boolean"}})
        if name == "sonic":
            props["maxResultsWeb"] = {"type": "integer", "minimum": 1, "maximum": 50}
            props["responseFilter"] = {"type": "array", "items": {"type": "string"}}
        result.append(SimpleNamespace(name=name, description="Actual-contract-shaped " + name,
                                      inputSchema={"type": "object", "required": [field], "properties": props,
                                                   "additionalProperties": False}))
    return result


class VerticalTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, mode="ok"):
        calls = []
        class Session:
            async def list_tools(self, cursor=None):
                return SimpleNamespace(tools=contracts(), nextCursor=None)

            async def call_tool(self, name, arguments):
                calls.append((name, arguments))
                if mode == "cancel":
                    raise asyncio.CancelledError()
                if mode == "timeout":
                    raise TimeoutError()
                if mode == "error":
                    return CallToolResult(isError=True, content=[TextContent(type="text", text="error")])
                if mode == "large":
                    return CallToolResult(content=[TextContent(type="text", text="x" * 200000)])
                return CallToolResult(content=[TextContent(type="text", text=json.dumps({
                    "results": [{"title": name, "url": "https://example.org/result", "content": "Actual fixture"}]}))])
        @asynccontextmanager
        async def session():
            yield Session()
        return WebIQ({"AXK2_WEBIQ_API_KEY": "fixture", "AXK2_WEBIQ_ACCESS_APPROVED": "1"},
                     session_factory=session), calls

    async def test_every_discovered_vertical_schema_and_actual_maf_invocation(self):
        provider, calls = self.provider()
        await provider.prepare()
        self.assertEqual(set(provider.catalog), VERTICALS)
        tools = provider.tools()
        self.assertEqual({t.name for t in tools}, {"web_iq_" + name for name in VERTICALS})
        for tool in tools:
            props = tool.to_json_schema_spec()["function"]["parameters"]["properties"]
            if "maxResults" in props:
                self.assertEqual(props["maxResults"]["maximum"], 5)
                self.assertIn("hard limit 5", props["maxResults"]["description"])
            if "maxLength" in props:
                self.assertEqual(props["maxLength"]["default"], 1500)
                self.assertIn("1500", props["maxLength"]["description"])
        for name in VERTICALS:
            with self.subTest(name=name), patch.object(provider, "validate_browse"):
                query = "https://example.org/" if name == "browse" else "Samsung public news"
                field = "url" if name == "browse" else "query"
                link = FixtureLink("web_iq_" + name, {field: query})
                response = agent_response(FixtureHub(link), Gate(1, 2), {
                    "messages": [{"role": "user", "content": query}], "model": "axk2"},
                    Workspace(), True, web_iq=provider)
                output = b"".join([chunk async for chunk in response.body_iterator]).decode()
                self.assertIn('"state": "success"', output)
                self.assertEqual(calls[-1][0], name)
                self.assertEqual(calls[-1][1][field], query)
                if name == "finance":
                    self.assertEqual(set(calls[-1][1]), {"query", "language", "region"})
                if name == "browse":
                    self.assertEqual(calls[-1][1]["contentFormat"], "text")
                    self.assertEqual(calls[-1][1]["liveCrawl"], "none")
                self.assertNotIn('"topic"', json.dumps(link.requests))
                self.assertEqual(len(link.requests), 2)
        self.assertEqual(provider.verified_tools, VERTICALS)

    async def test_all_verticals_failure_size_timeout_cancel(self):
        for name in VERTICALS:
            for mode, code in [("error", "provider_tool_error"), ("large", "provider_response_limit"),
                               ("timeout", "provider_timeout")]:
                with self.subTest(name=name, mode=mode), patch("web_iq.WebIQ.validate_browse"):
                    provider, _ = self.provider(mode)
                    await provider.prepare()
                    with self.assertRaisesRegex(WebIQProblem, code):
                        await provider.search("https://example.org/" if name == "browse" else "public query", name)
                    self.assertFalse(provider.verified_search)
            provider, _ = self.provider("cancel")
            await provider.prepare()
            with patch.object(provider, "validate_browse"), self.assertRaises(asyncio.CancelledError):
                await provider.search("https://example.org/" if name == "browse" else "public query", name)

    async def test_real_error_code_and_limits_replayed_to_ax_without_category_override(self):
        provider, calls = self.provider()
        await provider.prepare()
        for args in ({"query": "public query", "maxResults": 10},
                     {"query": "public query", "_name": "finance"}):
            link = FixtureLink("web_iq_web", args)
            response = agent_response(FixtureHub(link), Gate(1, 2), {
                "messages": [{"role": "user", "content": "public query"}], "model": "axk2"},
                Workspace(), True, web_iq=provider)
            output = b"".join([chunk async for chunk in response.body_iterator]).decode()
            self.assertIn('"state": "error"', output)
            self.assertNotIn('"state": "success"', output)
            self.assertIn("provider_argument_limit" if "maxResults" in args else "Unexpected argument", output)
            self.assertEqual(len(link.requests), 2)
        self.assertEqual(calls, [])

    async def test_schema_options_and_unsafe_urls(self):
        provider, calls = self.provider()
        await provider.prepare()
        for name in VERTICALS:
            with self.assertRaises(WebIQProblem):
                await provider.search("public query", name, {"unknown": True})
        for supplied in ({"maxResults": 100}, {"maxLength": 2000}, {"liveCrawl": "force"},
                         {"renderDynamicPages": True}, {"language": "private uploaded contents"}):
            with self.assertRaises(WebIQProblem):
                await provider.search("https://example.org/", "browse" if "liveCrawl" in supplied or "renderDynamicPages" in supplied else "web", supplied)
        for url in ("http://127.0.0.1/", "http://169.254.169.254/", "https://localhost/",
                    "https://example.org/?token=secret", "https://example.org/login",
                    "https://user:password@example.org/"):
            with self.assertRaisesRegex(WebIQProblem, "unsafe_browse_url"):
                await provider.search(url, "browse")
        with patch("web_iq.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 0))]):
            with self.assertRaisesRegex(WebIQProblem, "private_browse_origin"):
                await provider.search("https://example.org/", "browse")
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(WebIQProblem, "invalid_sonic_response_filter"):
            await provider.search("public query", "sonic", {"responseFilter": ["uploaded content"]})

    def test_per_turn_public_query_permission(self):
        files = {"private.txt": b"secret confidential launch plan"}
        for user, query in (("summarize upload", "Samsung"),
                            ("search Samsung news", "secret confidential launch plan"),
                            ("search private.txt", "private.txt"),
                            ("search Samsung news", "launch plan Samsung")):
            with self.assertRaises(WebIQProblem):
                PublicSearchPermission(user, files).check(query)
        PublicSearchPermission("search Samsung news", files).check("Samsung news")
        PublicSearchPermission("", {}).check("Seoul restaurants")
        with self.assertRaises(WebIQProblem):
            PublicSearchPermission("", {}).check("api_key=secret")

    async def test_discovery_pagination_and_unsupported_tools(self):
        provider, _ = self.provider()
        advertised = contracts()
        bad = SimpleNamespace(name="unknown_execution", description="not allowed",
                              inputSchema={"type": "object", "properties": {"query": {"type": "string"}}})
        class Session:
            async def list_tools(self, cursor=None):
                return SimpleNamespace(tools=advertised[:5] if cursor is None else advertised[5:] + [bad],
                                       nextCursor="next" if cursor is None else None)
        @asynccontextmanager
        async def factory():
            yield Session()
        provider.session_factory = factory
        await provider.prepare()
        self.assertEqual(set(provider.catalog), VERTICALS)
        self.assertNotIn("unknown_execution", provider.catalog)

    def test_finance_and_media_provenance_not_invented(self):
        from web_iq import result_items
        rows = result_items([{"financeResults": [{"title": "삼성전자", "url": "https://example.org/quote",
                    "data": {"instrument": {"symbol": "005930", "currency": "KRW", "price": 100,
                                          "lastTradedAt": "2026-10-07T14:12:01+09:00",
                                          "primaryDataProvider": "LSEG"}}}],
                              "imageResults": [{"title": "licensed elsewhere", "hostPageUrl": "https://example.org/image"}]}])
        self.assertIsNone(rows[0]["quote"]["exchange"])
        self.assertIsNone(rows[0]["quote"]["isDelayed"])
        self.assertEqual(rows[0]["quote"]["lastTradedAt"], "2026-10-07T14:12:01+09:00")
        self.assertIn("rights", rows[1]["media_notice"])
