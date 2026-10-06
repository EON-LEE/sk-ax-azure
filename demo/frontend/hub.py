"""Links from the GPU job (backend_link.py on node 0) and the chat streams relayed over them.

A link authenticates with its job's token and then multiplexes request streams on one WebSocket.
LinkConn.send() only queues (a writer task drains the queue), so that cleanup code can send a `cancel`
from a `finally` block while the request is being cancelled. Chat requests pass a FIFO gate:
at most max_active are relayed at once and at most max_queue wait, and waiting clients see their position.
"""
import asyncio
import collections
import json
import secrets
import time

from starlette.responses import StreamingResponse

FRESH = 30  # seconds a link status stays valid for routing


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def upstream_error(status, text):
    """A non-200 answer from vLLM as a short Korean message plus vLLM's own text."""
    detail = text.strip()
    try:
        payload = json.loads(detail)
        error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        detail = str(error.get("message") or detail)
    except (ValueError, AttributeError):
        pass
    detail = detail[:400]
    lowered = detail.lower()
    if status == 400 and ("context length" in lowered or "maximum context" in lowered or "too long" in lowered
                          or "max_model_len" in lowered or "prompt length" in lowered):
        return {"code": "too_long", "message": "입력이 모델의 최대 길이(262,144 토큰)를 넘습니다. 문서나 대화를 줄여 주세요.",
                "detail": detail}
    return {"code": "upstream", "status": status, "message": f"모델 서버가 요청을 처리하지 못했습니다 (HTTP {status}).",
            "detail": detail}


class Full(Exception):
    pass


class Ticket:
    __slots__ = ("granted", "future", "left", "joined")

    def __init__(self):
        self.granted, self.future, self.left, self.joined = False, None, False, time.time()


class Gate:
    def __init__(self, max_active, max_queue):
        self.max_active, self.max_queue = max_active, max_queue
        self.active, self.waiting = 0, collections.OrderedDict()

    def join(self):
        ticket = Ticket()
        if self.active < self.max_active and not self.waiting:
            ticket.granted = True
            self.active += 1
        elif len(self.waiting) >= self.max_queue:
            raise Full()
        else:
            ticket.future = asyncio.get_running_loop().create_future()
            self.waiting[ticket] = None
        return ticket

    def position(self, ticket):
        for index, waiting in enumerate(self.waiting, 1):
            if waiting is ticket:
                return index
        return 0

    def leave(self, ticket):
        if ticket.left:
            return
        ticket.left = True
        if ticket.granted:
            self.active -= 1
        else:
            self.waiting.pop(ticket, None)
        self.promote()

    def promote(self):
        while self.active < self.max_active and self.waiting:
            ticket, _ = self.waiting.popitem(last=False)
            ticket.granted = True
            self.active += 1
            if not ticket.future.done():
                ticket.future.set_result(True)


class LinkConn:
    def __init__(self, ws, digest):
        self.ws, self.digest = ws, digest
        self.outbox, self.streams = asyncio.Queue(), {}
        self.hello = self.status = self.status_at = None
        self.connected, self.closed = time.time(), False

    def send(self, message):
        if not self.closed:
            self.outbox.put_nowait(json.dumps(message, ensure_ascii=False))

    def close(self):
        if not self.closed:
            self.outbox.put_nowait(None)

    @property
    def ready(self):
        return (not self.closed and bool((self.status or {}).get("healthy"))
                and time.time() - (self.status_at or 0) < FRESH)


class Hub:
    """Connected links by token digest. The application decides which one is active and reacts to events."""

    def __init__(self, active=lambda: None, on_hello=None, on_status=None, on_eval=None, on_close=None):
        self.links, self.active = {}, active
        self.on_hello, self.on_status, self.on_eval, self.on_close = on_hello, on_status, on_eval, on_close

    def active_link(self):
        digest = self.active()
        return self.links.get(digest) if digest else None

    def ready_link(self):
        conn = self.active_link()
        return conn if conn and conn.ready else None

    def drop(self, digest):
        conn = self.links.get(digest)
        if conn:
            conn.close()

    async def serve(self, ws, digest):
        conn = LinkConn(ws, digest)
        previous = self.links.get(digest)
        self.links[digest] = conn
        if previous:
            previous.close()  # the job reconnected; the old socket is stale
        writer = asyncio.create_task(self.writer(conn))
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("text") is None:
                    continue
                try:
                    data = json.loads(message["text"])
                except ValueError:
                    continue
                if isinstance(data, dict):
                    self.dispatch(conn, data)
        finally:
            conn.closed = True
            writer.cancel()
            if self.links.get(digest) is conn:
                del self.links[digest]
            for queue in conn.streams.values():
                queue.put_nowait({"type": "lost"})
            conn.streams.clear()
            if self.on_close:
                self.on_close(conn)

    async def writer(self, conn):
        try:
            while True:
                text = await conn.outbox.get()
                if text is None:
                    await conn.ws.close(code=4000)
                    return
                await conn.ws.send_text(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await conn.ws.close(code=1011)
            except Exception:
                pass

    def dispatch(self, conn, message):
        kind = message.get("type")
        if kind in ("head", "data", "end", "error"):
            queue = conn.streams.get(str(message.get("id", "")))
            if queue is not None:
                queue.put_nowait(message)
        elif kind == "status":
            conn.status, conn.status_at = message, time.time()
            if self.on_status:
                self.on_status(conn)
        elif kind == "hello":
            conn.hello = message
            if self.on_hello:
                self.on_hello(conn)
        elif kind == "eval" and self.on_eval:
            self.on_eval(conn, message)


class ChatRelay:
    """One chat request: queue positions while it waits, then vLLM's SSE events, then `event: end`.

    Upstream text is cut at blank lines so the browser only ever receives whole events; our own events are
    `queue`, `error` and `end`. close() is synchronous and idempotent: it runs from `finally` blocks under
    cancellation, where awaiting is not possible.
    """
    keepalive = 10.0
    poll = 2.0

    def __init__(self, hub, gate, body):
        self.hub, self.gate, self.body = hub, gate, body
        self.ticket = gate.join()
        self.conn = self.rid = None
        self.finished = False

    def close(self):
        if self.conn is not None and self.rid is not None:
            self.conn.streams.pop(self.rid, None)
            if not self.finished:
                self.conn.send({"type": "cancel", "id": self.rid})  # vLLM aborts the generation
            self.rid = None
        self.gate.leave(self.ticket)

    def response(self):
        relay = self

        class Response(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    relay.close()

        return Response(self.events(), media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def events(self):
        try:
            while not self.ticket.granted:
                yield sse("queue", {"position": self.gate.position(self.ticket), "waiting": len(self.gate.waiting)})
                await asyncio.wait({self.ticket.future}, timeout=self.poll)
            conn = self.hub.ready_link()
            if conn is None:
                yield sse("error", {"code": "not_ready", "message": "GPU 클러스터가 아직 준비되지 않았습니다."})
                return
            self.conn, self.rid = conn, secrets.token_hex(8)
            queue = conn.streams[self.rid] = asyncio.Queue()
            conn.send({"type": "req", "id": self.rid, "path": "/v1/chat/completions", "body": self.body})
            yield sse("start", {})
            async for chunk in self.upstream(queue):
                yield chunk
        finally:
            self.close()

    async def upstream(self, queue):
        status, buffer, failure = None, "", ""
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), self.keepalive)
            except asyncio.TimeoutError:
                yield b": keep-alive\n\n"  # long prefills send nothing for a while
                continue
            kind = message["type"]
            if kind == "head":
                status = message.get("status")
            elif kind == "data" and status == 200:
                buffer += message.get("chunk", "")
                *complete, buffer = buffer.replace("\r\n", "\n").split("\n\n")
                for event in complete:
                    if event.strip():
                        yield (event + "\n\n").encode("utf-8")
            elif kind == "data":
                failure = (failure + message.get("chunk", ""))[:8192]
            elif kind == "end":
                self.finished = True
                if status != 200:
                    yield sse("error", upstream_error(status, failure))
                    return
                if buffer.strip():
                    yield (buffer + "\n\n").encode("utf-8")
                yield sse("end", {})
                return
            else:  # error from the link, or the link went away
                self.finished = True
                detail = message.get("message", "") if kind == "error" else "link lost"
                yield sse("error", {"code": "link", "message": "GPU 클러스터와의 연결이 끊겼습니다. 잠시 후 다시 시도해 주세요.",
                                    "detail": str(detail)[:300]})
                return
