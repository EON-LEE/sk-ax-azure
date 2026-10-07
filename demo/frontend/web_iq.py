"""Optional enterprise Web IQ via discovered standard MCP; never a substitute search provider."""
import asyncio
import copy
import ipaddress
import json
import logging
import os
import re
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from agent_framework import FunctionTool
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

LOG = logging.getLogger(__name__)
MAX_RESPONSE = 192 * 1024
VERTICALS = {"autosuggest", "browse", "finance", "images", "news", "places",
             "sonic", "sports", "videos", "web"}


class PublicSearchPermission:
    """Per-turn permission: attachments are never an implicit external-search grant."""
    def __init__(self, user_text, files):
        self.user_text = user_text
        self.files = files
        self.explicit = bool(re.search(r"검색|찾아|search|look up|browse|news|뉴스|주가|날씨|식당", user_text, re.I))

    def check(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 1000 or any(ord(c) < 32 for c in text):
            raise WebIQProblem("invalid_public_query")
        if re.search(r"(?i)(bearer\s+|api[_-]?key\s*[=:]|password\s*[=:]|-----BEGIN|sk-[a-z0-9]{16})", text):
            raise WebIQProblem("credential_query_forbidden")
        if not self.files:
            return
        if not self.explicit:
            raise WebIQProblem("public_search_requires_explicit_user_request_with_attachments")
        # With private files present, every substantive search term must come from the user's request.
        user_words = set(re.findall(r"[\w]+", self.user_text.casefold()))
        if any(word not in user_words for word in re.findall(r"[\w]+", text.casefold()) if len(word) > 1):
            raise WebIQProblem("upload_derived_search_requires_separate_public_query")
        for name, data in self.files.items():
            if name.casefold() in text.casefold():
                raise WebIQProblem("uploaded_filename_search_forbidden")
            decoded = data[:200000].decode("utf-8", errors="ignore").casefold()
            compact = text.strip().casefold()
            if len(compact) >= 12 and compact in decoded:
                raise WebIQProblem("uploaded_content_search_forbidden")


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
                or parsed.port not in {None, 80, 443}
                or "." not in host or host.endswith((".localhost", ".local", ".internal", ".test"))
                or parsed.fragment):
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
            url = next((public_url(node[k]) for k in ("hostPageUrl", "businessUrl", "url", "uri", "sourceUrl")
                        if k in node and public_url(node[k])), None)
            if url and url not in seen:
                seen.add(url)
                found.append({"url": url, "title": str(node.get("title", node.get("name", url)))[:240],
                              "provenance": node})
            for child in node.values():
                visit(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1)

    visit(value)
    return found


def result_items(sources):
    """Display actual provider fields, never upgrade retrieval time into data freshness."""
    items = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("webResults", "newsResults", "financeResults", "placeResults",
                    "imageResults", "videoResults", "sportsResults", "suggestions", "results"):
            rows = source.get(key, [])
            if not isinstance(rows, list):
                continue
            for row in rows[:5]:
                if not isinstance(row, dict):
                    if isinstance(row, str):
                        items.append({"kind": key, "title": row[:240]})
                    continue
                item = {"kind": key, "title": str(row.get("title", row.get("name", row.get("sport", ""))))[:240]}
                url = next((public_url(row.get(k)) for k in ("hostPageUrl", "businessUrl", "url") if public_url(row.get(k))), None)
                if url:
                    item["url"] = url
                item["snippet"] = str(row.get("content", row.get("description", row.get("caption", ""))))[:1500]
                for field in ("publishedAt", "datePublished", "lastUpdatedAt", "crawledAt", "location", "games"):
                    if field in row:
                        item[field] = row[field]
                if key == "financeResults":
                    instrument = row.get("data", {}).get("instrument", {})
                    item["quote"] = {field: instrument.get(field) for field in (
                        "symbol", "displayName", "price", "currency", "exchange", "exchangeTimeZone",
                        "lastTradedAt", "primaryDataProvider", "dataProviders", "delay", "isDelayed")}
                    item["quote"]["delay_status"] = "unknown unless explicitly returned by provider"
                if key in {"imageResults", "videoResults"}:
                    item["media_notice"] = "Source link only; search results do not grant redistribution or embedding rights."
                items.append(item)
        if source.get("url") and source.get("content") and public_url(source["url"]):
            items.append({"kind": "page", "url": source["url"], "title": str(source.get("title", ""))[:240],
                          "snippet": str(source["content"])[:1500]})
    return items[:20]


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
        self.catalog = {}
        self.verified_tools = set()
        self.unsupported = {}
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
                "tools": sorted(self.catalog), "verified_tools": sorted(self.verified_tools),
                "unsupported": self.unsupported,
                "scope": "public queries; attachments require explicit user search terms, never automatic upload-derived queries"}

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
                    advertised = {}
                    for _ in range(5):
                        page = await session.list_tools(cursor=cursor)
                        advertised.update({t.name: t for t in page.tools})
                        if not page.nextCursor:
                            break
                        cursor = page.nextCursor
                    else:
                        raise WebIQProblem("provider_catalog_limit")
                    if self.name not in advertised:
                        raise WebIQProblem("configured_tool_not_advertised")
                    for name, selected in advertised.items():
                        if name not in VERTICALS and name != self.name:
                            continue
                        schema = selected.inputSchema
                        field = "url" if name == "browse" else self.query_field if name == self.name else "query"
                        if len(json.dumps(schema)) > 16000 or any(
                            isinstance(node, dict) and "$ref" in node and not str(node["$ref"]).startswith("#/")
                            for node in self.schema_nodes(schema)
                        ):
                            self.unsupported[name] = "unsupported_provider_schema"
                            continue
                        if schema.get("type") != "object" or schema.get("properties", {}).get(field, {}).get("type") != "string":
                            self.unsupported[name] = "query_field_does_not_match_advertised_schema"
                            continue
                        Draft202012Validator.check_schema(schema)
                        self.catalog[name] = (schema, Draft202012Validator(schema), field, selected.description or name)
                    if self.name not in self.catalog:
                        raise WebIQProblem(self.unsupported.get(self.name, "unsupported_provider_schema"))
                    self.validator = self.catalog[self.name][1]
                    self.validator.validate(self.options(self.name, {self.query_field: "public search"}))
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

    def options(self, name, supplied):
        schema, _, _, _ = self.catalog[name]
        fields = schema["properties"]
        result = {k: v for k, v in self.arguments.items() if k in fields} if name == self.name else {
            k: v for k, v in self.arguments.items() if k in {"language", "region"} and k in fields}
        defaults = {"maxResults": 5, "maxResultsWeb": 5, "maxLength": 1500,
                    "contentFormat": "passage", "safeSearch": "strict",
                    "liveCrawl": "none", "renderDynamicPages": False}
        for key, value in defaults.items():
            if key in fields and key not in result and Draft202012Validator(fields[key]).is_valid(value):
                result[key] = value
        if "contentFormat" in fields and "contentFormat" not in result:
            result["contentFormat"] = "text"
        result.update(supplied)
        if set(result) - set(fields):
            raise WebIQProblem("unknown_provider_argument")
        for key in ("language", "region"):
            if key in result and (not isinstance(result[key], str) or not re.fullmatch(
                r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?" if name == "sonic" and key == "language" else r"[A-Za-z]{2}",
                result[key]
            )):
                raise WebIQProblem("invalid_public_locale")
        for key, cap in {"maxResults": 5, "maxResultsWeb": 5, "maxLength": 1500}.items():
            if key in result and (not isinstance(result[key], int) or not 1 <= result[key] <= cap):
                raise WebIQProblem("provider_argument_limit")
        if result.get("liveCrawl", "none") != "none" or result.get("renderDynamicPages", False):
            raise WebIQProblem("active_browse_not_enabled")
        if "responseFilter" in result and (
            not isinstance(result["responseFilter"], list)
            or any(value not in {"webResults", "newsResults", "financeResults"} for value in result["responseFilter"])
        ):
            raise WebIQProblem("invalid_sonic_response_filter")
        return result

    async def validate_browse(self, url):
        if not public_url(url) or urlsplit(url).query or re.search(r"(?i)/(login|signin|oauth|auth|logout)(/|$)", urlsplit(url).path):
            raise WebIQProblem("unsafe_browse_url")
        host = urlsplit(url).hostname
        try:
            addresses = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, None), 5)
        except (OSError, TimeoutError):
            raise WebIQProblem("browse_dns_failed") from None
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise WebIQProblem("private_browse_origin")
        # Only indexed retrieval: no live crawl or dynamic execution; reject returned unsafe links.

    async def search(self, query, name=None, supplied=None, permission=None):
        name = name or self.name
        if not self.ready:
            raise WebIQProblem(self.reason or "provider_not_ready")
        if name not in self.catalog:
            raise WebIQProblem("provider_tool_not_advertised")
        schema, validator, field, _ = self.catalog[name]
        permission = permission or PublicSearchPermission("", {})
        permission.check(query)
        arguments = self.options(name, dict(supplied or {}, **{field: query}))
        for key, value in (supplied or {}).items():
            if isinstance(value, str) and key not in {"language", "region"} and "enum" not in schema["properties"].get(key, {}):
                permission.check(value)
        try:
            validator.validate(arguments)
            if name == "browse":
                await self.validate_browse(query)
            async with asyncio.timeout(30), self.session_factory() as session:
                result = await session.call_tool(name, arguments)
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
                items = result_items(sources)
                output = json.dumps({"provider": "Microsoft Web IQ", "vertical": name, "public_query": query,
                                     "retrieved_at": datetime.now(timezone.utc).isoformat(),
                                     "items": items,
                                     "result_status": "results" if items else "no_displayable_results",
                                     "provider_content": payload, "citations": citations(sources),
                                     "limitations": "Provider-returned provenance only. Retrieval time is NOT quote/publication time; market coverage, exchange, currency and delay must be verified from actual data."},
                                    ensure_ascii=False)
                if len(output.encode()) > MAX_RESPONSE:
                    raise WebIQProblem("provider_response_limit")
                self.verified_search = True
                self.verified_tools.add(name)
                return output
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = problem_code(exc, "provider_query_failed")
            LOG.warning("Web IQ query failed (%s)", code)
            raise WebIQProblem(code) from None

    def tools(self, permission=None):
        if not self.ready:
            return []

        def bind(name, field):
            async def invoke(**arguments):
                query = arguments.pop(field)
                try:
                    return await self.search(query, name, arguments, permission)
                except WebIQProblem as exc:
                    # Real typed failure is visible to both the action card and AX's next tool round.
                    return json.dumps({"status": "error", "provider": "Microsoft Web IQ",
                                       "vertical": name, "error": exc.code,
                                       "limits": {"maxResults": 5, "maxResultsWeb": 5, "maxLength": 1500},
                                       "instruction": "No successful result. Correct invalid arguments or explain the actual provider limitation."})
            return invoke

        result = []
        for name, (schema, _, field, description) in self.catalog.items():
            # Each native MAF tool advertises its own actual discovered schema, not a guessed union.
            exposed = copy.deepcopy(schema)
            exposed["additionalProperties"] = False
            bounded = self.options(name, {field: "https://www.python.org/" if name == "browse" else "public query"})
            for key, cap in {"maxResults": 5, "maxResultsWeb": 5, "maxLength": 1500}.items():
                if key in exposed["properties"]:
                    exposed["properties"][key]["maximum"] = cap
                    exposed["properties"][key]["description"] = f"Application hard limit {cap}; omit to use bounded default. Never exceed {cap}."
            for key, value in bounded.items():
                if key != field:
                    exposed["properties"][key]["default"] = value
                    if key in {"language", "region", "contentFormat", "safeSearch"}:
                        exposed["properties"][key]["description"] = f"Application default: {value}. " + (
                            "One language/country code only, never comma-separated." if key in {"language", "region"} else
                            "Omit unless needed; use an advertised enum value.")
            if "liveCrawl" in exposed["properties"]:
                exposed["properties"]["liveCrawl"]["enum"] = ["none"]
            if "renderDynamicPages" in exposed["properties"]:
                exposed["properties"]["renderDynamicPages"]["enum"] = [False]
            if "responseFilter" in exposed["properties"]:
                exposed["properties"]["responseFilter"]["items"] = {
                    "type": "string", "enum": ["webResults", "newsResults", "financeResults"]}
            result.append(FunctionTool(name="web_iq_" + (name if name in VERTICALS else "search"),
                                       description=description + " Public queries only. Never transmit uploaded content or credentials.",
                                       func=bind(name, field), input_model=exposed))
        return result
