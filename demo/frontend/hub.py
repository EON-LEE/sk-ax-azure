"""Links from the GPU job (backend_link.py on node 0) and the chat streams relayed over them.

A link authenticates with its job's token and then multiplexes request streams on one WebSocket.
LinkConn.send() only queues (a writer task drains the queue), so that cleanup code can send a `cancel`
from a `finally` block while the request is being cancelled. Chat requests pass a FIFO gate:
at most max_active are relayed at once and at most max_queue wait, and waiting clients see their position.
"""
import asyncio
import collections
import json
import logging
import re
import secrets
import sys
import time

from starlette.responses import StreamingResponse

FRESH = 30  # seconds a link status stays valid for routing
LOG = logging.getLogger("axk2.link")


class Stderr(logging.StreamHandler):
    """Logs to whatever sys.stderr is at the moment: App Service shows the container's stdout and stderr."""

    def __init__(self):
        super().__init__(sys.stderr)

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value):
        pass


def install_logging():
    """Send the link's INFO lines to stderr. Only this logger is touched, never the root logger, and a second
    call adds nothing."""
    if not LOG.handlers:
        handler = Stderr()
        formatter = logging.Formatter("%(asctime)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        LOG.addHandler(handler)
        LOG.setLevel(logging.INFO)
        LOG.propagate = False


def clip(value, limit=64):
    """Text from the peer or an exception as one short run without quotes, line breaks or control characters."""
    return re.sub(r"[^\w .,:;/()+-]", "?", str(value))[:limit]


def elapsed(now, then):
    return "-" if then is None else "%.1fs" % max(now - then, 0.0)


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
        return {"code": "too_long", "message": "입력이 선택한 모델의 실제 컨텍스트 한도를 넘습니다. 문서나 대화를 줄여 주세요.",
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
        self.close_why = self.writer_error = self.close_1011 = None  # why this side ended the link
        self.open_diag = self.close_diag = ""  # the one-line facts that Hub.note_open / note_close logged

    def send(self, message):
        if not self.closed:
            self.outbox.put_nowait(json.dumps(message, ensure_ascii=False))

    def close(self, why=None):
        if not self.closed:
            self.close_why = self.close_why or why
            self.outbox.put_nowait(None)

    @property
    def ready(self):
        return (not self.closed and bool((self.status or {}).get("healthy"))
                and time.time() - (self.status_at or 0) < FRESH)


class Hub:
    """Connected links by token digest. The application decides which one is active and reacts to events."""

    def __init__(self, active=lambda: None, on_hello=None, on_status=None, on_eval=None, on_close=None,
                 on_open=None):
        self.links, self.active = {}, active
        self.on_hello, self.on_status, self.on_eval, self.on_close = on_hello, on_status, on_eval, on_close
        self.on_open = on_open

    def active_link(self):
        digest = self.active()
        return self.links.get(digest) if digest else None

    def ready_link(self):
        conn = self.active_link()
        return conn if conn and conn.ready else None

    def drop(self, digest):
        conn = self.links.get(digest)
        if conn:
            conn.close("dropped")

    async def model_info(self):
        conn = self.ready_link()
        if conn is None:
            return {"state":"not_ready", "max_context_tokens":None}
        cached = getattr(conn, "model_info", None)
        if cached and time.time() - cached["sampled"] < 30:
            return cached
        rid = secrets.token_hex(8)
        queue = conn.streams[rid] = asyncio.Queue()
        conn.send({"type":"req", "id":rid, "path":"/v1/models", "body":None})
        finished, status, text = False, None, ""
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await queue.get()
                    if message["type"] == "head":
                        status = message.get("status")
                    elif message["type"] == "data":
                        text += message.get("chunk", "")
                        if len(text) > 256_000:
                            raise ValueError("Model metadata exceeds limit")
                    elif message["type"] == "end":
                        finished = True
                        if status != 200:
                            raise ValueError("Runtime model metadata request failed")
                        entries = json.loads(text).get("data", [])
                        if not entries or not isinstance(entries[0], dict):
                            raise ValueError("Runtime returned no model metadata")
                        length = entries[0].get("max_model_len")
                        if not isinstance(length, int) or isinstance(length, bool) or not 0 < length <= 10_000_000:
                            raise ValueError("Runtime did not provide a valid context limit")
                        result = {"state":"verified", "max_context_tokens":length,
                                  "served_model":entries[0].get("id"), "root":entries[0].get("root"),
                                  "sampled":round(time.time(), 1)}
                        conn.model_info = result
                        return result
                    else:
                        raise ValueError("Runtime metadata link was lost")
        except (ValueError, TimeoutError) as exc:
            return {"state":"unavailable", "max_context_tokens":None, "reason":str(exc) or "metadata timeout"}
        finally:
            conn.streams.pop(rid, None)
            if not finished:
                conn.send({"type":"cancel", "id":rid})

    async def serve(self, ws, digest):
        conn = LinkConn(ws, digest)
        previous = self.links.get(digest)
        self.links[digest] = conn
        if previous:
            previous.close("replaced")  # the job reconnected; the old socket is stale
        writer = asyncio.create_task(self.writer(conn))
        code = reason = error = None  # how the socket ended: the peer's close code and reason, or an exception
        self.note_open(conn, previous)
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    code, reason = message.get("code"), message.get("reason")
                    break
                if message.get("text") is None:
                    continue
                try:
                    data = json.loads(message["text"])
                except ValueError:
                    continue
                if isinstance(data, dict):
                    self.dispatch(conn, data)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            conn.closed = True
            writer.cancel()
            if self.links.get(digest) is conn:
                del self.links[digest]
            lost = len(conn.streams)
            for queue in conn.streams.values():
                queue.put_nowait({"type": "lost"})
            conn.streams.clear()
            self.note_close(conn, code, reason, error, lost)
            if self.on_close:
                self.on_close(conn)

    def note_open(self, conn, previous):
        """One log line for an accepted link: a new job, or a reconnect that replaced a socket of the same job."""
        try:
            now = time.time()
            if previous is None:
                conn.open_diag = "previous=none"
            else:
                conn.open_diag = "previous=replaced(4000) prev_up=%s prev_since_status=%s" % (
                    elapsed(now, previous.connected), elapsed(now, previous.status_at))
            LOG.info("link opened digest=%s %s", conn.digest[:8], conn.open_diag)
            if self.on_open:
                self.on_open(conn)
        except Exception as exc:  # diagnostics never change what the link does
            LOG.warning("link diagnostics failed: %s", type(exc).__name__)

    def note_close(self, conn, code, reason, error, lost):
        """One log line for a finished link: why it ended, how long it lived and what it cancelled.

        Only numbers, short sanitised text and class names go in: no tokens, bodies, headers, addresses or
        exception messages, and the digest is cut to 8 characters.
        """
        try:
            now = time.time()
            facts = ["code=%s" % (code if isinstance(code, int) and not isinstance(code, bool) else "-")]
            if reason:
                facts.append('reason="%s"' % clip(reason))
            facts += ["up=%s" % elapsed(now, conn.connected), "since_status=%s" % elapsed(now, conn.status_at),
                      "lost_streams=%d" % lost]
            if conn.close_why:
                facts.append("closed_by=%s" % conn.close_why)
            if error:
                facts.append("error=%s" % clip(error, 40))
            if conn.writer_error:
                facts.append("writer_error=%s close_1011=%s" % (clip(conn.writer_error, 40), conn.close_1011))
            conn.close_diag = " ".join(facts)
            LOG.info("link closed digest=%s %s", conn.digest[:8], conn.close_diag)
        except Exception as exc:
            LOG.warning("link diagnostics failed: %s", type(exc).__name__)

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
        except Exception as exc:
            conn.writer_error, conn.close_1011 = type(exc).__name__, "yes"
            try:
                await conn.ws.close(code=1011)
            except Exception:
                conn.close_1011 = "failed"

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
