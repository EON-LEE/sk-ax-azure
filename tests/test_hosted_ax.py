"""Real Foundry Responses/MAF runtime, deterministic A.X wire fixture; no cloud calls."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["OTEL_SDK_DISABLED"] = "true"

import httpx
from agent_framework import Agent, Content, Message, tool
from azure.ai.agentserver.responses import InMemoryResponseProvider
from agent_framework_foundry_hosting import ResponsesHostServer
from experiments.hosted_ax.main import AXRelayClient


def model_stream(text="fixture answer", *, end=True):
    chunk = {"choices": [{"delta": {"content": text}, "finish_reason": None}]}
    stop = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    result = (f"event: queue\ndata: {{\"position\": 1}}\n\n"
              f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(stop)}\n\n"
              "data: [DONE]\n\n")
    return result + ("event: end\ndata: {}\n\n" if end else "")


class HostedAXTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AGENTSERVER_STATE_ROOT": self.temp.name})
        self.env.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.env.stop)
        self.requests = []

    def client(self, *, status=200, stream=None, path="/models/nvfp4/api/chat"):
        def handle(request):
            self.requests.append(request)
            return httpx.Response(status, text=stream if stream is not None else model_stream(),
                                  headers={"content-type": "text/event-stream"})
        return AXRelayClient("http://127.0.0.1:12345" + path, transport=httpx.MockTransport(handle))

    def host(self, client):
        return ResponsesHostServer(Agent(client=client, instructions="Use only A.X."),
                                   store=InMemoryResponseProvider())

    async def test_real_responses_host_executes_maf_and_relay(self):
        host = self.host(self.client())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://localhost") as api:
            readiness = await api.get("/readiness")
            self.assertEqual(readiness.status_code, 200)
            response = await api.post("/responses", json={"input": "fixture prompt", "stream": False, "store": False})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"][0]["content"][0]["text"], "fixture answer")
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.url.path, "/models/nvfp4/api/chat")
        body = json.loads(request.content)
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "fixture prompt"})
        self.assertFalse(body["thinking"])
        self.assertEqual(body["max_tokens"], 64)
        self.assertNotIn("store", body)
        self.assertNotIn("tools", body)

    async def test_multiturn_history_and_new_request_isolation(self):
        host = self.host(self.client())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://localhost") as api:
            first = await api.post("/responses", json={"input": "turn one", "store": True})
            self.assertEqual(first.status_code, 200, first.text)
            second = await api.post("/responses", json={"input": "turn two", "previous_response_id": first.json()["id"]})
            self.assertEqual(second.status_code, 200, second.text)
            third = await api.post("/responses", json={"input": "independent conversation", "store": False})
            self.assertEqual(third.status_code, 200, third.text)
        second_rows = json.loads(self.requests[1].content)["messages"]
        self.assertIn({"role": "user", "content": "turn one"}, second_rows)
        self.assertIn({"role": "assistant", "content": "fixture answer"}, second_rows)
        third_rows = json.loads(self.requests[2].content)["messages"]
        self.assertNotIn({"role": "user", "content": "turn one"}, third_rows)

    async def test_real_responses_stream(self):
        host = self.host(self.client())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://localhost") as api:
            response = await api.post("/responses", json={"input": "stream", "stream": True, "store": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("response.output_text.delta", response.text)
        self.assertIn("response.completed", response.text)
        self.assertNotIn("response.failed", response.text)

    async def test_http_error_and_relay_error_are_not_success(self):
        for client, error in (
            (self.client(status=503), httpx.HTTPStatusError),
            (self.client(stream='event: error\ndata: {"code":"link"}\n\n'), RuntimeError),
            (self.client(stream=model_stream(end=False)), RuntimeError),
            (self.client(stream='event: end\ndata: {}\n\n'), RuntimeError),
        ):
            with self.subTest(error=error), self.assertRaises(error):
                await client.get_response([Message(role="user", contents=["test"])])
        self.assertEqual(len(self.requests), 4)

    async def test_host_marks_upstream_error_failed(self):
        host = self.host(self.client(stream='event: error\ndata: {"code":"link"}\n\n'))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://localhost") as api:
            response = await api.post("/responses", json={"input": "failure", "stream": True, "store": False})
        self.assertIn("response.failed", response.text)
        self.assertNotIn("response.completed", response.text)
        self.assertEqual(len(self.requests), 1)

    async def test_tool_file_and_remote_targets_fail_closed(self):
        for url in ("https://axk2-a100-demo.azurewebsites.net/api/chat",
                    "http://localhost/api/agent", "http://localhost/api/chat?token=x",
                    "http://user:password@localhost/api/chat"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                AXRelayClient(url)
        client = self.client()
        with self.assertRaises(ValueError):
            await client.get_response([Message(role="user", contents=[Content("uri", uri="https://example.com/file")])])
        @tool
        def identity(value: int) -> int:
            """Return the supplied integer."""
            return value

        with self.assertRaisesRegex(ValueError, "Arbitrary tools"):
            await client.get_response([Message(role="user", contents=["test"])],
                                      options={"tools": [identity]})
        self.assertEqual(self.requests, [])

    async def test_cancellation_closes_inflight_transport(self):
        started, closed = asyncio.Event(), asyncio.Event()

        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                closed.set()

        client = AXRelayClient("http://localhost/api/chat", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=SlowStream(), headers={"content-type": "text/event-stream"})))
        task = asyncio.create_task(client.get_response([Message(role="user", contents=["cancel"])]))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())

    async def test_responses_cancel_interrupts_model_transport(self):
        started, closed = asyncio.Event(), asyncio.Event()

        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                closed.set()

        client = AXRelayClient("http://localhost/api/chat", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=SlowStream(), headers={"content-type": "text/event-stream"})))
        host = self.host(client)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://localhost") as api:
            response = await api.post("/responses", json={"input": "cancel", "background": True, "store": True})
            self.assertEqual(response.status_code, 200, response.text)
            await asyncio.wait_for(started.wait(), 2)
            response_id = response.json()["id"]
            cancelled = await api.post(f"/responses/{response_id}/cancel")
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            await asyncio.wait_for(closed.wait(), 2)
            self.assertEqual(cancelled.json()["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
