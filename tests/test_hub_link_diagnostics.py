"""Tests for the link close diagnostics (demo/frontend/hub.py and app.py).

Every link that ends leaves one line saying why, on stderr (logger axk2.link) and in the admin events, and
nothing about how the link behaves changes. Most tests drive Hub.serve with a fake socket; the last class
uses the real uvicorn, websocket client and backend_link.Link of test_demo_frontend to see what a real
disconnect message carries.
"""
import asyncio
import contextlib
import datetime
import io
import json
import logging
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "demo" / "frontend", ROOT / "aml" / "src", ROOT / "tests"):
    sys.path.insert(0, str(path))
try:
    import app as frontend
    import hub as hub_module
    import test_demo_frontend as loopback
    from store import Store, hash_password, token_digest
finally:
    del sys.path[:3]

TOKEN = loopback.TOKEN
DIGEST = token_digest(TOKEN)
HEAD = f"digest={DIGEST[:8]}"
LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ axk2\.link (?P<text>.+)$")


class Clock:
    """Stands in for the time module inside hub.py only, so that the ages in the lines are exact."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


def disconnect(code=None, reason=None):
    message = {"type": "websocket.disconnect"}
    if code is not None:
        message["code"] = code
    if reason is not None:
        message["reason"] = reason
    return message


def incoming(**message):
    return {"type": "websocket.receive", "text": json.dumps(message)}


class FakeSocket:
    """The part of a Starlette WebSocket that Hub.serve and Hub.writer use.

    receive() returns what a test put in `inbox` (a message, or an exception to raise). close() records the
    code and, like a socket whose peer answers the close frame, ends receive() with a disconnect message that
    carries `reply_code` (the code sent, if none is given).
    """

    def __init__(self, send_error=None, close_error=None, reply_code=None):
        self.inbox, self.closed_with = asyncio.Queue(), []
        self.send_error, self.close_error, self.reply_code = send_error, close_error, reply_code

    async def receive(self):
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_text(self, text):
        if self.send_error is not None:
            raise self.send_error

    async def close(self, code=1000, reason=None):
        self.closed_with.append(code)
        if self.close_error is not None:
            raise self.close_error
        self.inbox.put_nowait(disconnect(self.reply_code or code))


class NoAzure:
    """create_app wants an Azure client; these tests never reach it."""


def facts_of_log(text):
    return text.split(f"{HEAD} ", 1)[1]


def facts_of_event(text):
    return text.split(": link closed ", 1)[1]


class LinkCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "results").mkdir()
        config = frontend.Config(
            data=root / "data", results_dir=root / "results", aml_dir=ROOT / "aml", session_secret="s" * 32,
            demo_hash=hash_password("demo-password", rounds=1000),
            admin_hash=hash_password("admin-password", rounds=1000),
            workspaces={"uks": "w1"}, max_active=2, max_queue=2)
        patch = mock.patch.object(hub_module.LOG, "handlers", [])  # a fresh process: create_app has to install it
        patch.start()
        self.addCleanup(patch.stop)
        self.app = frontend.create_app(config, azure=NoAzure(), start_supervisor=False, profiles=False)
        self.store, self.hub = self.app.state.store, self.app.state.hub
        self.store.data["jobs"][DIGEST] = {"region": "uks", "name": "job-1", "status": "Running"}
        self.store.data["active"] = DIGEST
        self.clock = Clock()
        patch = mock.patch.object(hub_module, "time", self.clock)
        patch.start()
        self.addCleanup(patch.stop)
        self.stderr = io.StringIO()
        redirect = contextlib.redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    async def until(self, predicate, timeout=5):
        async def poll():
            while not predicate():
                await asyncio.sleep(0)
        await asyncio.wait_for(poll(), timeout)

    async def stop(self, task):
        """Ends a link a test left running, while stderr and the clock are still patched."""
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def link(self, socket=None):
        """Serve a fake socket as the job's link; returns it and the serve task once the hub registered it."""
        socket = socket or FakeSocket()
        task = asyncio.create_task(self.hub.serve(socket, DIGEST))
        self.addCleanup(self.stop, task)
        await self.until(lambda: getattr(self.hub.links.get(DIGEST), "ws", None) is socket)
        return socket, task

    async def status(self, socket, **extra):
        before = self.hub.links[DIGEST].status_at
        socket.inbox.put_nowait(incoming(type="status", healthy=True, **extra))
        await self.until(lambda: self.hub.links[DIGEST].status_at != before)

    def logged(self):
        """What the link logger wrote to stderr, without timestamp and logger name.

        Other output (asyncio's debug-mode slow-callback warnings, for one) can share the stream, so only lines
        that name the logger are taken, and each of those must have the exact format.
        """
        texts = []
        for line in self.stderr.getvalue().splitlines():
            if " axk2.link " in line:
                match = LINE.match(line)
                self.assertIsNotNone(match, line)
                texts.append(match["text"])
        return texts

    def events(self):
        return [event["message"] for event in self.store.data["events"] if event["kind"] == "link"]

    def assertSameFacts(self):
        """The last close line and the last close event say the same thing."""
        closed = [text for text in self.logged() if text.startswith("link closed ")][-1]
        event = [text for text in self.events() if ": link closed " in text][-1]
        self.assertEqual(facts_of_event(event), facts_of_log(closed))


class CloseReasonTests(LinkCase):
    async def test_peer_close_logs_code_reason_and_ages_in_both_places(self):
        socket, task = await self.link()
        self.clock.now += 3000.0
        await self.status(socket, note="payload-marker")
        self.clock.now += 20.0
        socket.inbox.put_nowait(disconnect(1006, "network reset"))
        await task
        facts = 'code=1006 reason="network reset" up=3020.0s since_status=20.0s lost_streams=0'
        self.assertEqual(self.logged(), [f"link opened {HEAD} previous=none", f"link closed {HEAD} {facts}"])
        self.assertEqual(self.events(), ["uks job-1: link opened previous=none", f"uks job-1: link closed {facts}"])
        self.assertNotIn("payload-marker", self.stderr.getvalue() + "".join(self.events()))

    async def test_link_without_a_status_has_no_status_age(self):
        socket, task = await self.link()
        self.clock.now += 5.0
        socket.inbox.put_nowait(disconnect(1000))
        await task
        self.assertEqual(self.logged()[-1], f"link closed {HEAD} code=1000 up=5.0s since_status=- lost_streams=0")
        self.assertSameFacts()

    async def test_logger_name_level_and_message_are_what_the_operator_greps_for(self):
        with self.assertLogs("axk2.link", "INFO") as captured:
            socket, task = await self.link()
            socket.inbox.put_nowait(disconnect(1001, "going away"))
            await task
        self.assertEqual([record.levelname for record in captured.records], ["INFO", "INFO"])
        self.assertEqual(captured.records[0].getMessage(), f"link opened {HEAD} previous=none")
        self.assertEqual(captured.records[1].getMessage(),
                         f'link closed {HEAD} code=1001 reason="going away" up=0.0s since_status=- lost_streams=0')
        self.assertEqual(self.logged(), [])  # assertLogs swaps the handler out: nothing reached stderr

    async def test_a_close_code_that_is_not_an_int_is_not_trusted(self):
        for code in (True, "1006", 1.5, None):
            with self.subTest(code=code):
                socket, task = await self.link()
                socket.inbox.put_nowait({"type": "websocket.disconnect", "code": code})
                await task
                self.assertEqual(self.logged()[-1], f"link closed {HEAD} code=- up=0.0s since_status=- lost_streams=0")

    async def test_cancelled_streams_are_counted_and_still_told_they_are_lost(self):
        socket, task = await self.link()
        queues = [self.hub.links[DIGEST].streams.setdefault(rid, asyncio.Queue()) for rid in ("r1", "r2")]
        socket.inbox.put_nowait(disconnect(1001))
        await task
        self.assertEqual([queue.get_nowait() for queue in queues], [{"type": "lost"}] * 2)
        self.assertIn(" lost_streams=2", self.logged()[-1])
        self.assertSameFacts()


class ExceptionTests(LinkCase):
    async def test_an_exception_that_ends_serve_is_named_without_its_message(self):
        socket, task = await self.link()
        socket.inbox.put_nowait(RuntimeError("secret-detail"))
        with self.assertRaises(RuntimeError):
            await task
        self.assertEqual(self.logged()[-1], f"link closed {HEAD} code=- up=0.0s since_status=- lost_streams=0"
                                            " error=RuntimeError")
        self.assertNotIn("secret-detail", self.stderr.getvalue() + "".join(self.events()))
        self.assertSameFacts()

    async def test_cancellation_is_named_too(self):
        socket, task = await self.link()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.logged()[-1].endswith(" error=CancelledError"))
        self.assertSameFacts()

    async def test_a_failing_status_handler_shows_as_the_error_that_ended_the_link(self):
        self.hub.on_status = mock.Mock(side_effect=KeyError("secret-key"))
        socket, task = await self.link()
        socket.inbox.put_nowait(incoming(type="status", healthy=True))
        with self.assertRaises(KeyError):
            await task
        self.assertTrue(self.logged()[-1].endswith(" error=KeyError"))
        self.assertNotIn("secret-key", self.stderr.getvalue())


class WriterTests(LinkCase):
    async def test_send_failure_names_the_exception_and_the_1011_close(self):
        socket, task = await self.link(FakeSocket(send_error=ConnectionResetError("secret-peer-detail")))
        self.hub.links[DIGEST].send({"type": "req", "id": "r1"})
        await task
        self.assertEqual(socket.closed_with, [1011])
        self.assertEqual(self.logged()[-1], f"link closed {HEAD} code=1011 up=0.0s since_status=- lost_streams=0"
                                            " writer_error=ConnectionResetError close_1011=yes")
        self.assertNotIn("secret-peer-detail", self.stderr.getvalue() + "".join(self.events()))
        self.assertSameFacts()

    async def test_a_1011_close_that_fails_is_reported_as_failed(self):
        socket, task = await self.link(FakeSocket(send_error=OSError("x"), close_error=RuntimeError("y")))
        conn = self.hub.links[DIGEST]
        conn.send({"type": "req", "id": "r1"})
        await self.until(lambda: conn.close_1011 == "failed")
        socket.inbox.put_nowait(disconnect(1006))
        await task
        self.assertEqual(socket.closed_with, [1011])
        self.assertTrue(self.logged()[-1].endswith(" code=1006 up=0.0s since_status=- lost_streams=0"
                                                   " writer_error=OSError close_1011=failed"))

    async def test_a_healthy_writer_adds_nothing(self):
        socket, task = await self.link()
        self.hub.links[DIGEST].send({"type": "req", "id": "r1"})
        socket.inbox.put_nowait(disconnect(1000))
        await task
        self.assertNotIn("writer_error", self.logged()[-1])
        self.assertNotIn("close_1011", self.logged()[-1])


class ReplaceTests(LinkCase):
    async def test_reconnect_with_the_same_digest_is_told_from_a_new_link(self):
        first, first_task = await self.link(FakeSocket(reply_code=1000))  # the peer answers our 4000 with a plain close
        self.clock.now += 10.0
        await self.status(first)
        self.clock.now += 30.0
        second, second_task = await self.link()
        await first_task
        self.assertEqual(first.closed_with, [4000])
        self.assertIs(self.hub.links[DIGEST].ws, second)  # the replacement stays registered
        second.inbox.put_nowait(disconnect(1000))
        await second_task
        self.assertEqual(self.logged(), [
            f"link opened {HEAD} previous=none",
            f"link opened {HEAD} previous=replaced(4000) prev_up=40.0s prev_since_status=30.0s",
            f"link closed {HEAD} code=1000 up=40.0s since_status=30.0s lost_streams=0 closed_by=replaced",
            f"link closed {HEAD} code=1000 up=0.0s since_status=- lost_streams=0"])
        self.assertEqual(self.events(), [
            "uks job-1: link opened previous=none",
            "uks job-1: link opened previous=replaced(4000) prev_up=40.0s prev_since_status=30.0s",
            "uks job-1: link closed code=1000 up=40.0s since_status=30.0s lost_streams=0 closed_by=replaced",
            "uks job-1: link closed code=1000 up=0.0s since_status=- lost_streams=0"])

    async def test_the_replaced_link_still_loses_its_streams(self):
        first, first_task = await self.link()
        queue = self.hub.links[DIGEST].streams.setdefault("r1", asyncio.Queue())
        await self.link()
        await first_task
        self.assertEqual(queue.get_nowait(), {"type": "lost"})
        self.assertIn(" lost_streams=1 closed_by=replaced", self.logged()[-1])

    async def test_supervisor_drop_is_labelled(self):
        socket, task = await self.link(FakeSocket(reply_code=1005))
        self.hub.drop(DIGEST)
        await task
        self.assertEqual(socket.closed_with, [4000])
        self.assertEqual(self.logged()[-1], f"link closed {HEAD} code=1005 up=0.0s since_status=- lost_streams=0"
                                            " closed_by=dropped")
        self.assertSameFacts()

    async def test_the_first_reason_wins(self):
        socket, task = await self.link()
        conn = self.hub.links[DIGEST]
        conn.close("dropped")
        conn.close("shutdown")
        await task
        self.assertTrue(self.logged()[-1].endswith(" closed_by=dropped"))

    async def test_app_shutdown_is_labelled(self):
        async with self.app.router.lifespan_context(self.app):
            socket, task = await self.link()
        await task
        self.assertEqual(socket.closed_with, [4000])
        self.assertTrue(self.logged()[-1].endswith(" closed_by=shutdown"))
        self.assertSameFacts()


class OutputTests(LinkCase):
    async def test_two_lines_per_link_however_busy(self):
        socket, task = await self.link()
        for _ in range(50):
            socket.inbox.put_nowait(incoming(type="status", healthy=True))
            socket.inbox.put_nowait({"type": "websocket.receive", "text": "not json"})
        socket.inbox.put_nowait(disconnect(1000))
        await task
        self.assertEqual(len(self.logged()), 2)
        self.assertEqual(len(self.events()), 2)

    async def test_only_a_short_digest_prefix_and_clean_one_line_text_are_written(self):
        hostile = 'bye"\nlink closed digest=ffffffff code=1000 ' + "x" * 200
        socket, task = await self.link()
        socket.inbox.put_nowait(disconnect(1008, hostile))
        await task
        everything = self.stderr.getvalue() + "\n".join(self.events())
        self.assertIn(DIGEST[:8], self.stderr.getvalue())
        self.assertNotIn(DIGEST[:9], everything)
        self.assertEqual(len(self.logged()), 2)  # the reason did not start a line of its own
        reason = re.search(r'reason="([^"]*)"', self.logged()[-1])
        self.assertTrue(reason[1].startswith("bye??link closed digest?ffffffff code?1000 x"), reason[1])
        self.assertEqual(len(reason[1]), 64)

    async def test_the_longest_close_event_keeps_its_last_fields(self):
        Long = type("L" * 80, (Exception,), {})
        socket, task = await self.link(FakeSocket(send_error=Long("x"), close_error=Long("y")))
        conn = self.hub.links[DIGEST]
        conn.send({"type": "req", "id": "r1"})
        await self.until(lambda: conn.close_1011 == "failed")
        self.clock.now += 10 ** 7
        socket.inbox.put_nowait(disconnect(1011, "r" * 500))
        await task
        event = self.events()[-1]
        self.assertLess(len(event), 400)  # Store.event cuts at 500 characters
        self.assertTrue(event.endswith(f" writer_error={'L' * 40} close_1011=failed"))

    async def test_close_event_keeps_its_active_gate_and_open_event_does_not_need_it(self):
        self.store.data["active"] = None
        socket, task = await self.link()
        socket.inbox.put_nowait(disconnect(1000))
        await task
        self.assertEqual(self.events(), ["uks job-1: link opened previous=none"])
        self.assertEqual(len(self.logged()), 2)  # the log has both lines either way

    async def test_a_bare_hub_logs_without_the_app(self):
        bare = hub_module.Hub()
        socket = FakeSocket()
        task = asyncio.create_task(bare.serve(socket, DIGEST))
        self.addCleanup(self.stop, task)
        await self.until(lambda: DIGEST in bare.links)
        socket.inbox.put_nowait(disconnect(1000))
        await task
        self.assertEqual(len(self.logged()), 2)

    async def test_failing_diagnostics_do_not_stop_the_link_and_keep_the_old_event_text(self):
        self.hub.on_open = mock.Mock(side_effect=ValueError("boom"))
        with mock.patch.object(hub_module, "elapsed", side_effect=TypeError("boom")):
            socket, task = await self.link()
            await self.status(socket)  # the link still does its job
            socket.inbox.put_nowait(disconnect(1000))
            await task
        self.assertEqual(self.logged(), [f"link opened {HEAD} previous=none",
                                         "link diagnostics failed: ValueError",
                                         "link diagnostics failed: TypeError"])
        self.assertEqual(self.events(), ["uks job-1: link closed"])  # on_close still ran, with the old text


class RestartTests(LinkCase):
    def link_events_on_disk(self):
        """The link events a web app that starts now would restore from state.json."""
        return [event["message"] for event in Store(self.store.root).data["events"] if event["kind"] == "link"]

    async def test_link_events_reach_state_json_with_the_next_save_and_come_back_after_a_restart(self):
        socket, task = await self.link()
        socket.inbox.put_nowait(disconnect(1006))
        await task
        written = self.events()
        self.assertEqual(len(written), 2)
        self.assertIn(" link closed code=1006 ", written[1])
        self.assertEqual(self.link_events_on_disk(), [])  # nothing has saved them yet: a kill now would lose them
        self.store.save()  # the supervisor does this after every pass
        self.assertEqual(self.link_events_on_disk(), written)


class InstallTests(unittest.TestCase):
    def fresh_logger(self):
        """The link logger as in a process that has not configured it yet."""
        for name, value in (("handlers", []), ("level", logging.NOTSET), ("propagate", True)):
            patch = mock.patch.object(hub_module.LOG, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        return hub_module.LOG

    def test_install_adds_one_stderr_handler_to_the_link_logger_only(self):
        log, root = self.fresh_logger(), logging.getLogger()
        before = (list(root.handlers), root.level)
        hub_module.install_logging()
        hub_module.install_logging()
        self.assertEqual([type(handler) for handler in log.handlers], [hub_module.Stderr])
        self.assertEqual((log.level, log.propagate), (logging.INFO, False))
        self.assertEqual((list(root.handlers), root.level), before)

    def test_the_handler_writes_a_utc_stamped_line_to_the_stderr_of_the_moment(self):
        log = self.fresh_logger()
        hub_module.install_logging()
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):  # the handler looks stderr up when it writes
            log.info("probe %s", "line")
        self.assertRegex(captured.getvalue(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ axk2\.link probe line\n$")
        stamp = datetime.datetime.strptime(captured.getvalue()[:20], "%Y-%m-%dT%H:%M:%SZ")
        age = datetime.datetime.now(datetime.timezone.utc) - stamp.replace(tzinfo=datetime.timezone.utc)
        self.assertLess(abs(age.total_seconds()), 10)

    def test_clip_removes_quotes_line_breaks_and_length(self):
        self.assertEqual(hub_module.clip('a"b\nc\td'), "a?b?c?d")
        self.assertEqual(hub_module.clip("가나다 ok-1"), "가나다 ok-1")
        self.assertEqual(len(hub_module.clip("x" * 500)), 64)
        self.assertEqual(hub_module.clip("x" * 500, 40), "x" * 40)


class LoopbackTests(loopback.ServerCase):
    """The real uvicorn, websocket client and backend_link.Link: what a disconnect message really carries."""

    async def asyncSetUp(self):
        self.stderr = io.StringIO()
        redirect = contextlib.redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        await super().asyncSetUp()

    def closed_lines(self):
        return [line for line in self.stderr.getvalue().splitlines() if " link closed " in line]

    async def test_drop_and_client_close_reach_stderr_and_events(self):
        hub = self.app.state.hub
        await self.start_link()
        self.assertEqual(len(hub.links), 1)
        hub.drop(DIGEST)  # the server closes the socket
        await self.until(lambda: len(self.closed_lines()) == 1)
        await self.until(lambda: hub.ready_link() is not None)  # the link dialled back in
        await self.stop_link()  # the client closes the socket
        await self.until(lambda: len(self.closed_lines()) == 2)
        dropped, client = (LINE.match(line)["text"] for line in self.closed_lines())
        self.assertRegex(dropped, rf"^link closed {HEAD} code=\d+ up=\d+\.\ds since_status=\S+ lost_streams=0"
                                  r" closed_by=dropped$")
        self.assertRegex(client, rf"^link closed {HEAD} code=1000 up=\d+\.\ds since_status=\S+ lost_streams=0$")
        events = [e["message"] for e in self.store.data["events"] if e["kind"] == "link"]
        self.assertEqual(sum(": link opened " in event for event in events), 2)
        self.assertEqual([facts_of_event(event) for event in events if ": link closed " in event],
                         [facts_of_log(dropped), facts_of_log(client)])

    async def test_web_app_shutdown_shows_as_code_1012(self):
        await self.start_link()
        self.server.should_exit = True
        await asyncio.wait_for(self.serving, 10)
        await self.until(lambda: len(self.closed_lines()) == 1)
        self.assertRegex(LINE.match(self.closed_lines()[0])["text"], rf"^link closed {HEAD} code=1012 up=")


if __name__ == "__main__":
    unittest.main()
