"""Optional enterprise Web IQ via discovered standard MCP; never a substitute search provider."""
import asyncio
import ipaddress
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlsplit

import httpx
from agent_framework import tool
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

LOG = logging.getLogger(__name__)
MAX_RESPONSE = 192 * 1024
TOPICS = {
    "agent-framework": "Microsoft Agent Framework Python official documentation streaming tools",
    "document-intelligence": "Microsoft Azure Document Intelligence official documentation prebuilt read",
    "python": "Python official documentation standard library",
    "web-platform": "MDN official documentation HTML CSS JavaScript",
    "web-iq": "Microsoft Web IQ official documentation enterprise MCP",
}
Topic = Literal["agent-framework", "document-intelligence", "python", "web-platform", "web-iq"]


class WebIQProblem(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Web IQ unavailable ({code}); no successful search result is available.")


def problem_code(exc, default):
    if isinstance(exc, WebIQProblem):
        return exc.code
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            code = problem_code(child, default)
            if code != default:
                return code
    if isinstance(exc, httpx.HTTPStatusError):
        return {401: "provider_authentication_failed", 403: "provider_access_denied",
                404: "provider_endpoint_not_found", 429: "provider_rate_limited"}.get(
                    exc.response.status_code, default)
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "provider_timeout"
    return default


def public_url(value):
    if not isinstance(value, str) or len(value) > 2000:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or "." not in host or host.endswith((".localhost", ".local", ".internal"))):
            return None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return value
    except ValueError:
        pass
    return None


def citations(value):
    """Preserve only URLs actually returned by the provider, with their original provenance."""
    found, seen, visits = [], set(), 0

    def visit(node, depth=0):
        nonlocal visits
        visits += 1
        if visits > 1000 or depth > 16 or len(found) >= 12:
            return
        if isinstance(node, dict):
            url = next((public_url(node[k]) for k in ("url", "uri", "sourceUrl")
                        if k in node and public_url(node[k])), None)
            if url and url not in seen:
                seen.add(url)
                found.append({"url": url, "title": str(node.get("title", url))[:240],
                              "provenance": node})
            for child in node.values():
                visit(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1)

    visit(value)
    return found


class LimitedStream(httpx.AsyncByteStream):
    def __init__(self, inner):
        self.inner = inner

    async def __aiter__(self):
        size = 0
        async for chunk in self.inner:
            size += len(chunk)
            if size > MAX_RESPONSE:
                raise WebIQProblem("provider_response_limit")
            yield chunk

    async def aclose(self):
        await self.inner.aclose()


async def limit_response(response):
    response.stream = LimitedStream(response.stream)


class WebIQ:
    def __init__(self, settings=None, session_factory=None):
        env = os.environ if settings is None else settings
        self.api_key = env.get("AXK2_WEBIQ_API_KEY", "")
        self.endpoint = env.get("AXK2_WEBIQ_ENDPOINT", "") or (
            "https://api.microsoft.ai/v3/mcp" if self.api_key else "")
        self.token = env.get("AXK2_WEBIQ_BEARER_TOKEN", "")
        self.name = env.get("AXK2_WEBIQ_TOOL", "") or ("web" if self.api_key else "")
        self.query_field = env.get("AXK2_WEBIQ_QUERY_FIELD", "") or ("query" if self.api_key else "")
        self.arguments_json = env.get("AXK2_WEBIQ_ARGUMENTS_JSON", '{"language":"ko","region":"KR"}'
                                      if self.api_key else "{}")
        self.approved = env.get("AXK2_WEBIQ_ACCESS_APPROVED") == "1"
        self.session_factory = session_factory or self.session
        self.lock = asyncio.Lock()
        self.attempted = self.ready = self.verified_search = False
        self.validator = None
        self.diagnostic = []
        self.reason = self.configuration_problem()

    def configuration_problem(self):
        if not self.endpoint:
            return "enterprise_endpoint_required"
        if not public_url(self.endpoint) or urlsplit(self.endpoint).scheme != "https":
            return "approved_https_endpoint_required"
        parsed = urlsplit(self.endpoint)
        if parsed.query or parsed.fragment:
            return "endpoint_must_not_contain_credentials_or_query"
        if not self.approved:
            return "enterprise_access_and_query_approval_required"
        if self.token and self.api_key:
            return "ambiguous_authentication_configuration"
        if not self.token and not self.api_key:
            return "secure_provider_credential_required"
        if not self.name or not self.query_field:
            return "provider_tool_and_query_field_required"
        try:
            self.arguments = json.loads(self.arguments_json)
            if not isinstance(self.arguments, dict) or len(self.arguments_json) > 4000:
                return "invalid_provider_arguments"
        except ValueError:
            return "invalid_provider_arguments"
        return None

    def status(self):
        return {"provider": "Microsoft Web IQ", "state": "ready" if self.ready else "unavailable",
                "reason": self.reason, "verified_search": self.verified_search,
                "topics": list(TOPICS) if self.ready else [],
                "scope": "fixed public documentation topics only; uploaded content is never sent"}

    @asynccontextmanager
    async def session(self):
        async with httpx.AsyncClient(
            headers={"x-apikey": self.api_key} if self.api_key else {"Authorization": f"Bearer {self.token}"},
            timeout=httpx.Timeout(20), follow_redirects=False, trust_env=False,
            event_hooks={"response": [limit_response]},
        ) as client:
            async with streamable_http_client(self.endpoint, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def prepare(self):
        async with self.lock:
            if self.attempted or self.reason:
                return
            self.attempted = True
            try:
                async with asyncio.timeout(30), self.session_factory() as session:
                    cursor = None
                    selected = None
                    for _ in range(5):
                        page = await session.list_tools(cursor=cursor)
                        selected = next((t for t in page.tools if t.name == self.name), None)
                        if selected or not page.nextCursor:
                            break
                        cursor = page.nextCursor
                    if selected is None:
                        raise WebIQProblem("configured_tool_not_advertised")
                    schema = selected.inputSchema
                    encoded = json.dumps(schema)
                    if len(encoded) > 16000 or any(
                        isinstance(node, dict) and "$ref" in node and not str(node["$ref"]).startswith("#/")
                        for node in self.schema_nodes(schema)
                    ):
                        raise WebIQProblem("unsupported_provider_schema")
                    field = schema.get("properties", {}).get(self.query_field, {})
                    if schema.get("type") != "object" or field.get("type") != "string":
                        raise WebIQProblem("query_field_does_not_match_advertised_schema")
                    Draft202012Validator.check_schema(schema)
                    self.validator = Draft202012Validator(schema)
                    # Opt into bounded passages only when the real provider advertises these fields.
                    for name, value in {"maxResults": 5, "contentFormat": "passage", "maxLength": 1500,
                                        "safeSearch": "strict"}.items():
                        if name in schema.get("properties", {}) and name not in self.arguments:
                            candidate = dict(self.arguments, **{name: value, self.query_field: TOPICS["python"]})
                            if self.validator.is_valid(candidate):
                                self.arguments[name] = value
                    for query in TOPICS.values():
                        self.validator.validate(dict(self.arguments, **{self.query_field: query}))
                    self.ready, self.reason = True, None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.reason = problem_code(exc, "provider_discovery_failed")
                self.diagnostic = self.failure_types(exc)
                LOG.warning("Web IQ discovery failed (%s)", self.reason)

    @staticmethod
    def failure_types(exc):
        if isinstance(exc, BaseExceptionGroup):
            return [item for child in exc.exceptions for item in WebIQ.failure_types(child)]
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        return [{"type": type(exc).__name__, "http_status": status}]

    @staticmethod
    def schema_nodes(node):
        yield node
        if isinstance(node, dict):
            for value in node.values():
                yield from WebIQ.schema_nodes(value)
        elif isinstance(node, list):
            for value in node:
                yield from WebIQ.schema_nodes(value)

    async def search(self, topic):
        if topic not in TOPICS:
            raise WebIQProblem("only_fixed_public_topics_are_allowed")
        if not self.ready:
            raise WebIQProblem(self.reason or "provider_not_ready")
        arguments = dict(self.arguments, **{self.query_field: TOPICS[topic]})
        try:
            self.validator.validate(arguments)
            async with asyncio.timeout(30), self.session_factory() as session:
                result = await session.call_tool(self.name, arguments)
                if result.isError:
                    raise WebIQProblem("provider_tool_error")
                payload = result.model_dump(mode="json", exclude_none=True)
                encoded = json.dumps(payload, ensure_ascii=False)
                if len(encoded.encode()) > MAX_RESPONSE:
                    raise WebIQProblem("provider_response_limit")
                sources = [result.structuredContent] if result.structuredContent is not None else []
                texts = [c.text for c in result.content if c.type == "text"]
                for text in texts:
                    try:
                        sources.append(json.loads(text))
                    except ValueError:
                        pass  # Plain text remains in provider_content; no invented citation structure.
                if not texts and result.structuredContent is None:
                    raise WebIQProblem("provider_returned_no_search_content")
                output = json.dumps({"provider": "Microsoft Web IQ", "public_query": TOPICS[topic],
                                     "provider_content": payload, "citations": citations(sources),
                                     "limitations": "Only provider-returned provenance; no synthesized sources."},
                                    ensure_ascii=False)
                if len(output.encode()) > MAX_RESPONSE:
                    raise WebIQProblem("provider_response_limit")
                self.verified_search = True
                return output
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = problem_code(exc, "provider_query_failed")
            LOG.warning("Web IQ query failed (%s)", code)
            raise WebIQProblem(code) from None

    def tools(self):
        if not self.ready:
            return []

        @tool
        async def web_iq_search(topic: Topic) -> str:
            """Search Web IQ for one fixed PUBLIC official documentation topic. Never send uploaded text or code."""
            return await self.search(topic)

        return [web_iq_search]
