"""Unregistered, local-only MCP delegation contract; not a Copilot model provider."""
import asyncio
import json
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import FastMCP


def create_mcp(endpoint: str, *, transport: httpx.AsyncBaseTransport | None = None) -> FastMCP:
    url = urlsplit(endpoint)
    if (url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1")
            or url.path != "/responses" or url.username or url.password or url.query or url.fragment):
        raise ValueError("Only a loopback Responses fixture is permitted; cloud authentication is not implemented")
    server = FastMCP("AXHostedDelegationContract")
    gate = asyncio.Lock()

    @server.tool()
    async def ask_ax(prompt: str, delegation_confirmed: bool = False) -> str:
        """Explicitly delegate supplied synthetic text to A.X; not automatic context forwarding.

        The caller must first obtain the user's permission to delegate this exact text.
        This local argument is a contract gate, not proof of human approval.
        """
        if delegation_confirmed is not True:
            raise ValueError("Explicit delegation confirmation is required")
        if not prompt.strip() or len(prompt.encode("utf-8")) > 4096:
            raise ValueError("Provide non-empty text of at most 4096 UTF-8 bytes")
        if gate.locked():
            raise RuntimeError("One delegated request is already active")
        async with gate:
            async with httpx.AsyncClient(transport=transport, timeout=30, trust_env=False,
                                         follow_redirects=False) as client:
                async with client.stream("POST", endpoint, json={
                    "input": prompt, "stream": False, "store": False, "background": False,
                    "max_output_tokens": 64,
                }) as response:
                    response.raise_for_status()
                    if not response.headers.get("content-type", "").startswith("application/json"):
                        raise ValueError("Responses endpoint did not return JSON")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise ValueError("Delegated response exceeds 64 KiB")
            result = json.loads(raw)
            if result.get("status") != "completed":
                raise RuntimeError("Delegated response did not complete successfully")
            text = []
            for item in result.get("output", []):
                if item.get("type") != "message" or item.get("role") != "assistant":
                    raise ValueError("Only assistant text output is supported")
                for part in item.get("content", []):
                    if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                        raise ValueError("Only text output is supported")
                    text.append(part["text"])
            if not text:
                raise ValueError("Delegated response contains no assistant text")
            return "".join(text)

    return server
