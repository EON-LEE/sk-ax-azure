"""Local-only Foundry Responses -> MAF -> A.X /api/chat contract experiment."""
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx
from agent_framework import BaseChatClient, ChatResponse, ChatResponseUpdate, Content, Message, Agent, ResponseStream
from agent_framework_foundry_hosting import ResponsesHostServer


class AXRelayClient(BaseChatClient):
    STORES_BY_DEFAULT = False

    def __init__(self, endpoint: str, *, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__()
        url = urlsplit(endpoint)
        if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost")
                or url.username or url.password or url.query or url.fragment
                or url.path not in ("/api/chat", "/models/nvfp4/api/chat")):
            raise ValueError("This experiment only permits a loopback A.X chat fixture; cloud auth is not implemented")
        self.endpoint, self.transport = endpoint, transport

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        response = ResponseStream(self.updates(messages, options), finalizer=ChatResponse.from_updates)
        return response if stream else response.get_final_response()

    async def updates(
        self, messages: Sequence[Message], options: Mapping[str, Any],
    ) -> AsyncIterator[ChatResponseUpdate]:
        options = await self._validate_options(options)
        if options.get("tools"):
            raise ValueError("Arbitrary tools require an approved model gateway, not the current /api/chat contract")
        rows = []
        for message in messages:
            if str(message.role) not in ("system", "user", "assistant"):
                raise ValueError("Only text conversation messages are supported in this experiment")
            if any(content.type != "text" for content in message.contents):
                raise ValueError("Files, tool calls and non-text content are not supported in this experiment")
            rows.append({"role": str(message.role), "content": message.text})
        if options.get("instructions"):
            rows.insert(0, {"role": "system", "content": options["instructions"]})
        body = {"messages": rows, "thinking": False, "max_tokens": 64}
        if not rows or len(rows) > 400 or len(json.dumps(body).encode()) > 4 * 1024 * 1024:
            raise ValueError("A.X input exceeds the existing chat contract")
        async with httpx.AsyncClient(transport=self.transport, timeout=30, follow_redirects=False, trust_env=False) as client:
            async with client.stream("POST", self.endpoint, json=body) as response:
                response.raise_for_status()
                if not response.headers.get("content-type", "").startswith("text/event-stream"):
                    raise ValueError("A.X relay did not return an SSE stream")
                event, data, finished = "message", [], False
                async for line in response.aiter_lines():
                    if len(line) > 250_000:
                        raise ValueError("A.X SSE line exceeds the experiment limit")
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line:
                        payload = "\n".join(data)
                        if event == "error":
                            raise RuntimeError("A.X relay reported an upstream error")
                        if event == "end":
                            if not finished:
                                raise RuntimeError("A.X relay ended without a model finish reason")
                            return
                        if event == "message" and payload and payload != "[DONE]":
                            chunk = json.loads(payload)
                            if chunk.get("error"):
                                raise RuntimeError("A.X model reported an upstream error")
                            for choice in chunk.get("choices", []):
                                delta = choice.get("delta") or {}
                                if delta.get("tool_calls"):
                                    raise ValueError("Unexpected tool calls from a text-only model request")
                                contents = [Content("text", text=delta["content"])] if delta.get("content") else []
                                reason = choice.get("finish_reason")
                                if reason:
                                    if reason not in ("stop", "length"):
                                        raise ValueError("Unsupported A.X model finish reason")
                                    finished = True
                                if contents or reason:
                                    yield ChatResponseUpdate(role="assistant", contents=contents, finish_reason=reason)
                        event, data = "message", []
                raise RuntimeError("A.X relay disconnected without its terminal end event")


def create_server(client: AXRelayClient) -> ResponsesHostServer:
    agent = Agent(client=client, name="AXHostedContract",
                  instructions="Answer using only A.X inference. Tools and file processing are unavailable.")
    return ResponsesHostServer(agent)


if __name__ == "__main__":
    create_server(AXRelayClient(os.environ["AX_CHAT_ENDPOINT"])).run(host="127.0.0.1")
