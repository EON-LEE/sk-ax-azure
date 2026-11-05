"""Unregistered real MCP SDK contract over real local Responses/MAF, with model fixture."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["OTEL_SDK_DISABLED"] = "true"

import httpx
from agent_framework import Agent
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.ai.agentserver.responses import InMemoryResponseProvider
from mcp.server.fastmcp.exceptions import ToolError

from experiments.hosted_ax.main import AXRelayClient
from experiments.hosted_ax.mcp_wrapper import create_mcp


class DelegationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AGENTSERVER_STATE_ROOT": self.temp.name})
        self.env.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.env.stop)

    async def test_real_mcp_tool_delegates_to_responses_maf_without_history_reuse(self):
        requests = []

        def model(request):
            requests.append(json.loads(request.content))
            chunk = {"choices": [{"delta": {"content": "AX fixture"}, "finish_reason": "stop"}]}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text=f"data: {json.dumps(chunk)}\n\nevent: end\ndata: {{}}\n\n")

        host = ResponsesHostServer(Agent(client=AXRelayClient(
            "http://localhost/api/chat", transport=httpx.MockTransport(model))),
            store=InMemoryResponseProvider())
        mcp = create_mcp("http://localhost/responses", transport=httpx.ASGITransport(app=host))
        tools = await mcp.list_tools()
        self.assertEqual([tool.name for tool in tools], ["ask_ax"])
        self.assertEqual(set(tools[0].inputSchema["properties"]), {"prompt", "delegation_confirmed"})
        for prompt in ("first synthetic prompt", "separate synthetic prompt"):
            result = await mcp.call_tool("ask_ax", {"prompt": prompt, "delegation_confirmed": True})
            self.assertIn("AX fixture", str(result))
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["messages"], [{"role": "user", "content": "separate synthetic prompt"}])

    async def test_confirmation_and_input_gate_before_network(self):
        calls = []
        mcp = create_mcp("http://localhost/responses", transport=httpx.MockTransport(
            lambda request: calls.append(request)))
        for args in ({"prompt": "text"}, {"prompt": " " , "delegation_confirmed": True},
                     {"prompt": "x" * 4097, "delegation_confirmed": True}):
            with self.assertRaises(ToolError):
                await mcp.call_tool("ask_ax", args)
        self.assertEqual(calls, [])
        with self.assertRaises(ValueError):
            create_mcp("https://example.com/responses")

    async def test_http_failed_and_oversized_results_are_tool_errors(self):
        for response in (
            httpx.Response(403),
            httpx.Response(200, json={"status": "failed"}),
            httpx.Response(200, text="x" * 65537, headers={"content-type": "application/json"}),
        ):
            with self.subTest(response=response), self.assertRaises(ToolError):
                mcp = create_mcp("http://localhost/responses",
                                 transport=httpx.MockTransport(lambda request: response))
                await mcp.call_tool("ask_ax", {"prompt": "synthetic", "delegation_confirmed": True})

    async def test_cancel_closes_transport_and_releases_single_request_gate(self):
        started, closed = asyncio.Event(), asyncio.Event()

        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                closed.set()

        mcp = create_mcp("http://localhost/responses", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=SlowStream(),
                                           headers={"content-type": "application/json"})))
        args = {"prompt": "synthetic", "delegation_confirmed": True}
        task = asyncio.create_task(mcp.call_tool("ask_ax", args))
        await asyncio.wait_for(started.wait(), 2)
        with self.assertRaises(ToolError):
            await mcp.call_tool("ask_ax", args)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        started.clear()
        task = asyncio.create_task(mcp.call_tool("ask_ax", args))
        try:
            await asyncio.wait_for(started.wait(), 2)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
