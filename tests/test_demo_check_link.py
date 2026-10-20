"""Tests of how the demo-example verifier (demo/tools/check_agentic_examples.py) copes with a bad connection: the
DNS-over-HTTPS pin, which failures count as a lost connection, the one retry per candidate, the streak that stops a
plan, the /api/me retry and the report of all that.

The demo is the loopback fake of test_demo_check_examples. A connection that breaks is the fake closing the socket
without a reply (DROP), a patched resolver or a patched stream. Names are reserved example names; the only addresses
are the public resolver's, a documentation range and loopback. No test goes to the network.
"""
from __future__ import annotations

import contextlib
import gc
import http.client
import io
import json
import re
import socket
import ssl
import tempfile
import threading
import types
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests import test_demo_check_examples as base
from tests.test_demo_check_examples import check

ME, WORKSPACE, AGENT = "/api/me", "/api/workspace", "/api/agent"
UPLOAD, DOWNLOAD, PREVIEW = WORKSPACE + "/upload", WORKSPACE + "/download", WORKSPACE + "/preview"
CANCEL, CLOSE = WORKSPACE + "/cancel", WORKSPACE + "/close"
DROPPED = {"RemoteDisconnected", "ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError"}
PUBLIC, OTHER = check.DOH_ADDRESSES
DOCUMENTED = "203.0.113.9"
HOST = "demo.example.com"
LOST = {"error": "gaierror", "step": "workspace"}
NO_ANSWER = "no DNS-over-HTTPS answer"
FALLBACK = "the DNS-over-HTTPS lookup failed; the system resolver is used"
NO_LOOKUP = "no lookup needed: plain http, loopback or an IP address"


# ------------------------------------------------------------------------------------------------------- fakes
class FakeTls:
    """An ssl.SSLContext that does no TLS: it notes the server name of every handshake and can fail one."""

    check_hostname, verify_mode, post_handshake_auth = True, ssl.CERT_REQUIRED, None

    def __init__(self, error=None):
        self.server_names, self.error = [], error

    def set_alpn_protocols(self, protocols):
        pass

    def wrap_socket(self, sock, server_hostname=None):
        self.server_names.append(server_hostname)
        if self.error:
            raise self.error
        return sock


def fake_ssl(tls):
    return types.SimpleNamespace(create_default_context=lambda: tls)


class FakeSocket:
    def __init__(self, nodelay_error=None):
        self.options, self.timeouts, self.closed, self.nodelay_error = [], [], False, nodelay_error

    def setsockopt(self, level, name, value):
        if self.nodelay_error:
            raise self.nodelay_error
        self.options.append((level, name, value))

    def settimeout(self, seconds):
        self.timeouts.append(seconds)

    def close(self):
        self.closed = True


def fake_socket(opened):
    """The socket module as the verifier sees it, with create_connection replaced."""
    return types.SimpleNamespace(create_connection=opened, IPPROTO_TCP=socket.IPPROTO_TCP,
                                 TCP_NODELAY=socket.TCP_NODELAY)


class ResolverResponse:
    def __init__(self, owner):
        self.owner, self.status = owner, owner.status

    def read(self, limit=-1):
        self.owner.limits.append(limit)
        body = self.owner.body
        return body if limit < 0 else body[:limit]

    def close(self):
        self.owner.responses_closed += 1


class ResolverConnection:
    """What doh_query talks to: one scripted answer of the DNS-over-HTTPS service."""

    def __init__(self, body=b"", status=200, request_error=None, response_error=None):
        self.body, self.status, self.request_error, self.response_error = body, status, request_error, response_error
        self.requests, self.limits, self.closed, self.responses_closed = [], [], 0, 0

    def request(self, method, path, headers=None):
        self.requests.append((method, path, headers))
        if self.request_error:
            raise self.request_error

    def getresponse(self):
        if self.response_error:
            raise self.response_error
        return ResolverResponse(self)

    def close(self):
        self.closed += 1


def dns_answer(*records):
    return json.dumps({"Status": 0, "Answer": [{"name": "x.example.com.", "type": kind, "TTL": 60, "data": data}
                                               for kind, data in records]}).encode("utf-8")


class Connections:
    """The `connections` argument of doh_address: a run of fakes per try, each made only when the try reaches it.
    `tries` holds, for every try, the keyword arguments of its ResolverConnections; the last try repeats."""

    def __init__(self, *tries):
        self.tries, self.calls, self.made = tries, 0, []

    def __call__(self):
        wanted = self.tries[min(self.calls, len(self.tries) - 1)]
        self.calls += 1
        for arguments in wanted:
            connection = ResolverConnection(**arguments)
            self.made.append(connection)
            yield connection


class Echo(BaseHTTPRequestHandler):
    """A loopback server that notes the path, Host and Accept of every GET and answers with server.answer."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Host"), self.headers.get("Accept")))
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(self.server.answer)))
        self.end_headers()
        try:
            self.wfile.write(self.server.answer)
        except OSError:  # the client stops reading once it has what it asks for
            pass


def serve(case, answer=b"{}", status=200):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    server.requests, server.answer, server.status = [], answer, status
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    case.addCleanup(thread.join, 5)
    case.addCleanup(server.server_close)
    case.addCleanup(server.shutdown)
    return server


def record(**more):
    """A run record as run_one makes it, for the report functions."""
    return {"id": "plan-fee", "profile": "fp8", "run": 1, "attempt": 1, "ok": False, "state": "", "http": {},
            "failed": {}, "flags": {}, "tools": [], "files": [], "web_iq_calls": 0, "connection_error": None, **more}


def watched(connection, seen):
    """The connection, noting in seen every response that it hands out."""
    handing = connection.getresponse

    def getresponse():
        response = handing()
        seen.append(response)
        return response

    connection.getresponse = getresponse
    return connection


# ------------------------------------------------------------------------------------------- the name lookup
class PublicAddressTests(unittest.TestCase):
    def test_global_ipv4_addresses_are_accepted(self):
        for text in (PUBLIC, OTHER, "1.1.1.1", "9.9.9.9"):
            with self.subTest(text=text):
                self.assertEqual(check.public_ipv4(text), text)

    def test_everything_else_is_refused(self):
        refused = ("10.1.2.3", "172.16.0.1", "192.168.0.1", "127.0.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
                   "255.255.255.255", DOCUMENTED, "224.0.0.1", "239.255.255.250", "::1", "2001:4860:4860::8888",
                   "8.8.8", "8.8.8.8.8", "8.8.8.8 ", "dns.google", "", None, 8888, ["8.8.8.8"], b"8.8.8.8")
        for value in refused:
            with self.subTest(value=value):
                self.assertIsNone(check.public_ipv4(value))


class ResolverAnswerTests(unittest.TestCase):
    def ask(self, host=HOST, **arguments):
        connection = ResolverConnection(**arguments)
        return check.doh_query(connection, host), connection

    def test_the_question_is_a_plain_get_for_the_a_record(self):
        address, connection = self.ask(body=dns_answer((1, PUBLIC)))
        self.assertEqual(address, PUBLIC)
        ((method, path, headers),) = connection.requests
        self.assertEqual((method, path), ("GET", "/resolve?name=demo.example.com&type=A"))
        self.assertEqual(headers["Accept"], "application/dns-json")
        self.assertEqual((connection.limits, connection.closed, connection.responses_closed),
                         ([check.MAX_DNS_BODY], 1, 1))

    def test_the_name_is_percent_encoded(self):
        _, connection = self.ask(host="a b&c=d", body=dns_answer((1, PUBLIC)))
        self.assertEqual(connection.requests[0][1], "/resolve?name=a%20b%26c%3Dd&type=A")

    def test_the_first_public_a_record_is_taken_past_cname_aaaa_and_private_records(self):
        answer = dns_answer((5, "edge.example.net."), (28, "2001:db8::1"), (1, "10.1.2.3"), (1, "127.0.0.1"),
                            (1, OTHER), (1, PUBLIC))
        self.assertEqual(self.ask(body=answer)[0], OTHER)

    def test_an_answer_without_a_public_a_record_gives_nothing(self):
        bodies = {
            "empty": b"",
            "cname only": dns_answer((5, "edge.example.net.")),
            "private only": dns_answer((1, "10.1.2.3"), (1, "127.0.0.1")),
            "aaaa only": dns_answer((28, "2001:4860:4860::8888")),
            "no answer": json.dumps({"Status": 3}).encode(),
            "answer is not a list": json.dumps({"Answer": {"type": 1, "data": PUBLIC}}).encode(),
            "items are not objects": json.dumps({"Answer": [1, "x", None, [PUBLIC]]}).encode(),
            "no type": json.dumps({"Answer": [{"data": PUBLIC}]}).encode(),
            "type is text": json.dumps({"Answer": [{"type": "1", "data": PUBLIC}]}).encode(),
            "data is not text": json.dumps({"Answer": [{"type": 1, "data": 8888}, {"type": 1, "data": None}]}).encode(),
            "bad json": b"{not json",
            "array": b"[1]",
            "null": b"null",
            "bad utf-8": b"\xff\xfe{}",
        }
        for name, body in bodies.items():
            with self.subTest(name):
                self.assertIsNone(self.ask(body=body)[0])

    def test_a_body_over_the_limit_is_cut_and_gives_nothing(self):
        address, connection = self.ask(body=b" " * check.MAX_DNS_BODY + dns_answer((1, PUBLIC)))
        self.assertIsNone(address)
        self.assertEqual(connection.limits, [check.MAX_DNS_BODY])

    def test_an_http_error_gives_nothing_and_the_body_is_not_read(self):
        for status in (301, 400, 403, 429, 500, 503):
            with self.subTest(status=status):
                address, connection = self.ask(body=dns_answer((1, PUBLIC)), status=status)
                self.assertIsNone(address)
                self.assertEqual((connection.limits, connection.closed, connection.responses_closed), ([], 1, 1))

    def test_a_broken_connection_gives_nothing_and_is_closed(self):
        errors = (OSError("x"), http.client.RemoteDisconnected("gone"), http.client.BadStatusLine("x"),
                  ssl.SSLError("bad"), TimeoutError(), socket.gaierror("no host"), ConnectionRefusedError())
        for error in errors:
            for where in ("request_error", "response_error"):
                with self.subTest(error=type(error).__name__, where=where):
                    address, connection = self.ask(**{where: error})
                    self.assertIsNone(address)
                    self.assertEqual((connection.closed, connection.responses_closed), (1, 0))


class DohAddressTests(unittest.TestCase):
    def lookup(self, connections):
        sleeps = []
        return check.doh_address(HOST, sleeps.append, connections), sleeps

    def test_the_first_answer_wins_and_the_later_connections_are_never_made(self):
        connections = Connections([{"body": dns_answer((1, PUBLIC))}, {"body": dns_answer((1, OTHER))}])
        address, sleeps = self.lookup(connections)
        self.assertEqual((address, sleeps, connections.calls, len(connections.made)), (PUBLIC, [], 1, 1))

    def test_the_next_connection_is_tried_when_one_gives_nothing(self):
        connections = Connections([{"request_error": OSError("x")}, {"status": 503},
                                   {"body": dns_answer((1, OTHER))}])
        address, sleeps = self.lookup(connections)
        self.assertEqual((address, sleeps, connections.calls, len(connections.made)), (OTHER, [], 1, 3))
        self.assertEqual({c.closed for c in connections.made}, {1})

    def test_a_later_try_can_succeed(self):
        connections = Connections([{"status": 503}], [{"body": dns_answer((1, PUBLIC))}])
        address, sleeps = self.lookup(connections)
        self.assertEqual((address, sleeps, connections.calls), (PUBLIC, [check.DOH_PAUSE], 2))

    def test_after_the_last_try_the_lookup_fails_without_naming_the_host(self):
        connections, sleeps = Connections([{"status": 503}, {"request_error": OSError("x")}]), []
        with self.assertRaises(LookupError) as caught:
            check.doh_address(HOST, sleeps.append, connections)
        self.assertEqual(str(caught.exception), NO_ANSWER)
        self.assertEqual(sleeps, [check.DOH_PAUSE] * (check.DOH_TRIES - 1))
        self.assertEqual((connections.calls, len(connections.made)), (check.DOH_TRIES, 2 * check.DOH_TRIES))
        self.assertEqual({c.closed for c in connections.made}, {1})

    def test_the_answer_travels_over_a_real_http_exchange(self):
        server = serve(self, dns_answer((5, "edge.example.net."), (1, PUBLIC)))
        port = server.server_address[1]
        tls = FakeTls()

        def connections():
            yield check.PinnedConnection(check.DOH_NAME, port, "127.0.0.1", 5, tls)

        address, sleeps = self.lookup(connections)
        self.assertEqual((address, sleeps), (PUBLIC, []))
        self.assertEqual(server.requests, [("/resolve?name=demo.example.com&type=A", f"{check.DOH_NAME}:{port}",
                                            "application/dns-json")])
        self.assertEqual(tls.server_names, [check.DOH_NAME])


class ResolverConnectionTests(unittest.TestCase):
    def test_the_service_is_reached_by_address_first_and_by_name_last(self):
        with mock.patch.object(check, "ssl", fake_ssl(FakeTls())):
            made = list(check.doh_connections())
        self.assertEqual([type(c) for c in made],
                         [check.PinnedConnection, check.PinnedConnection, http.client.HTTPSConnection])
        self.assertEqual({(c.host, c.port, c.timeout) for c in made}, {(check.DOH_NAME, 443, check.DOH_TIMEOUT)})
        self.assertEqual([c.address for c in made[:2]], list(check.DOH_ADDRESSES))

    def test_the_connections_are_made_one_at_a_time(self):
        with mock.patch.object(check, "ssl", fake_ssl(FakeTls())):
            lookup = check.doh_connections()
            self.assertIsInstance(next(lookup), check.PinnedConnection)


class PinTests(unittest.TestCase):
    @staticmethod
    def target(scheme="https", host=HOST):
        return check.Target(scheme, host, 443 if scheme == "https" else 80)

    def test_the_system_mode_asks_nothing(self):
        resolve, target = mock.Mock(side_effect=AssertionError("looked up")), self.target()
        self.assertEqual(check.pin(target, "system", resolve), {"mode": "system", "pinned": False})
        self.assertIsNone(target.address)
        resolve.assert_not_called()

    def test_a_target_that_needs_no_lookup_gets_none(self):
        for scheme, host in (("http", "127.0.0.1"), ("http", "localhost"), ("https", "localhost"),
                             ("https", "LOCALHOST"), ("https", "127.0.0.1"), ("https", "::1"),
                             ("https", DOCUMENTED), ("https", "2001:db8::1")):
            with self.subTest(scheme=scheme, host=host):
                resolve, target = mock.Mock(side_effect=AssertionError("looked up")), self.target(scheme, host)
                self.assertEqual(check.pin(target, "doh", resolve),
                                 {"mode": "doh", "pinned": False, "note": NO_LOOKUP})
                self.assertIsNone(target.address)
                resolve.assert_not_called()

    def test_an_https_name_is_looked_up_once_and_the_summary_never_holds_the_address(self):
        resolve, target = mock.Mock(return_value=DOCUMENTED), self.target()
        summary = check.pin(target, "doh", resolve)
        self.assertEqual(summary, {"mode": "doh", "pinned": True})
        self.assertEqual(target.address, DOCUMENTED)
        self.assertNotIn(DOCUMENTED, json.dumps(summary))
        resolve.assert_called_once_with(HOST)

    def test_a_lookup_that_fails_leaves_the_system_resolver(self):
        for error in (LookupError(NO_ANSWER), OSError("the lookup could not start"), ssl.SSLError("no CA file"),
                      socket.gaierror("no host")):
            with self.subTest(error=type(error).__name__):
                target = self.target()
                self.assertEqual(check.pin(target, "doh", mock.Mock(side_effect=error)),
                                 {"mode": "doh", "pinned": False, "note": FALLBACK})
                self.assertIsNone(target.address)


class ServerConnectTests(unittest.TestCase):
    def test_the_connection_class_follows_the_scheme_and_the_pin(self):
        tls = FakeTls()
        with mock.patch.object(check, "ssl", fake_ssl(tls)):
            pinned = check.Server(check.Target("https", HOST, 443, DOCUMENTED), 30).connect()
            by_name = check.Server(check.Target("https", HOST, 8443), 31).connect()
        plain = check.Server(check.Target("http", "127.0.0.1", 8080), 32).connect()
        ignored = check.Server(check.Target("http", "127.0.0.1", 8080, DOCUMENTED), 33).connect()
        self.assertIs(type(pinned), check.PinnedConnection)
        self.assertEqual((pinned.host, pinned.port, pinned.address, pinned.timeout), (HOST, 443, DOCUMENTED, 30))
        self.assertIs(type(by_name), http.client.HTTPSConnection)
        self.assertEqual((by_name.host, by_name.port, by_name.timeout), (HOST, 8443, 31))
        self.assertIs(type(plain), http.client.HTTPConnection)
        self.assertEqual((plain.host, plain.port, plain.timeout), ("127.0.0.1", 8080, 32))
        self.assertIs(type(ignored), http.client.HTTPConnection)
        self.assertEqual((ignored.host, ignored.port, ignored.timeout), ("127.0.0.1", 8080, 33))


class PinnedConnectionTests(unittest.TestCase):
    def test_the_request_goes_to_the_address_while_the_host_name_stays_the_name(self):
        server = serve(self)
        port, tls = server.server_address[1], FakeTls()
        connection = check.PinnedConnection(HOST, port, "127.0.0.1", 10, tls)
        connection.request("GET", "/x", headers={"Accept": "text/plain"})
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"{}"))
        connection.close()
        self.assertEqual(server.requests, [("/x", f"{HOST}:{port}", "text/plain")])
        self.assertEqual(tls.server_names, [HOST])

    def test_the_socket_is_opened_to_the_address_and_given_the_timeout(self):
        for timeout, opened_with in ((5, 5), (120, check.CONNECT_TIMEOUT)):
            with self.subTest(timeout=timeout):
                raw, tls = FakeSocket(), FakeTls()
                opened = mock.Mock(return_value=raw)
                connection = check.PinnedConnection(HOST, 8443, DOCUMENTED, timeout, tls)
                with mock.patch.object(check, "socket", fake_socket(opened)):
                    connection.connect()
                opened.assert_called_once_with((DOCUMENTED, 8443), opened_with)
                self.assertEqual(raw.timeouts, [timeout])
                self.assertEqual(raw.options, [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)])
                self.assertEqual(tls.server_names, [HOST])
                self.assertIs(connection.sock, raw)

    def test_a_connection_that_is_refused_never_reaches_tls(self):
        tls = FakeTls()
        connection = check.PinnedConnection(HOST, 8443, DOCUMENTED, 60, tls)
        opened = mock.Mock(side_effect=ConnectionRefusedError("no"))
        with mock.patch.object(check, "socket", fake_socket(opened)):
            with self.assertRaises(ConnectionRefusedError):
                connection.connect()
        self.assertEqual(tls.server_names, [])
        self.assertIsNone(connection.sock)

    def test_a_failed_handshake_leaves_the_raw_socket_to_be_closed(self):
        raw, tls = FakeSocket(), FakeTls(error=ssl.SSLError("bad certificate"))
        connection = check.PinnedConnection(HOST, 8443, DOCUMENTED, 60, tls)
        with mock.patch.object(check, "socket", fake_socket(mock.Mock(return_value=raw))):
            with self.assertRaises(ssl.SSLError):
                connection.connect()
        self.assertIs(connection.sock, raw)
        connection.close()
        self.assertTrue(raw.closed)
        self.assertIsNone(connection.sock)

    def test_a_socket_that_refuses_nodelay_is_used_all_the_same(self):
        raw = FakeSocket(nodelay_error=OSError("no nodelay"))
        connection = check.PinnedConnection(HOST, 8443, DOCUMENTED, 60, FakeTls())
        with mock.patch.object(check, "socket", fake_socket(mock.Mock(return_value=raw))):
            connection.connect()
        self.assertEqual((raw.timeouts, raw.options), ([60], []))
        self.assertIs(connection.sock, raw)


class DnsOptionTests(unittest.TestCase):
    @staticmethod
    def main(*argv):
        out, err = io.StringIO(), io.StringIO()
        code = check.main(list(argv), environ={}, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_a_bad_value_is_refused_and_not_echoed(self):
        code, out, err = self.main("--dns", "zzz-typed")
        self.assertEqual((code, out, err), (2, "", "check_agentic_examples: error: argument --dns is invalid\n"))

    def test_both_modes_are_accepted_and_nothing_is_sent_without_go(self):
        for mode in ("doh", "system"):
            with self.subTest(mode=mode):
                code, out, err = self.main("--dns", mode, "--only", "plan-fee")
                self.assertEqual((code, err), (0, "nothing was sent: add --go to run this plan\n"))
                self.assertEqual(json.loads(out)["go"], False)


# ------------------------------------------------------------------------------------ the verifier with a demo
class LinkCase(base.DemoCase):
    def https_url(self):
        return "https://%s:%d" % (HOST, self.demo.server.server_address[1])

    def run_cli(self, *argv, url=None, resolve=None, sleep=None, tls=None):
        """Like DemoCase.go, with a resolver to inject and a TLS context that does no TLS."""
        sleeps, out, err = [], io.StringIO(), io.StringIO()
        with mock.patch.object(check, "ssl", fake_ssl(tls or FakeTls())):
            code = check.main(["--go", *argv], environ={"AXK2_DEMO_URL": url or self.demo.url}, stdout=out,
                              stderr=err, sleep=sleep or sleeps.append, resolve=resolve)
        return base.Outcome(code, out.getvalue(), err.getvalue(), sleeps)

    def drop(self, route, hits=None, method="POST", profile="fp8"):
        """The demo closes the connection of a route without a reply: on every hit, or on the numbered hits only."""
        self.demo.overrides[(profile, method, route)] = lambda hit: base.DROP if hits is None or hit in hits else None

    def reply(self, route, status, payload, method="POST", profile="fp8"):
        self.demo.overrides[(profile, method, route)] = lambda hit: (status, payload)

    def hits(self, route, method="POST", profile="fp8"):
        return self.demo.hits.get((profile, method, route), 0)

    def cause(self, rec, step):
        """The exception name of a lost connection; the connection must have broken at the step."""
        link = rec["connection_error"]
        self.assertEqual(link["step"], step)
        self.assertIn(link["error"], DROPPED)
        return link["error"]

    @staticmethod
    def cutting(error):
        """check.events failing once with the error, like a stream that breaks, and working after that."""
        real, state = check.events, {"cut": False}

        def events(response, deadline):
            if not state["cut"]:
                state["cut"] = True
                raise error
            yield from real(response, deadline)

        return mock.patch.object(check, "events", events)

    @contextlib.contextmanager
    def left_open(self, seen=None):
        """Yields a list that holds, once the block has ended, what the block left open: responses that were not
        closed (one can stay open for as long as a traceback that holds its frame lives) and sockets that only the
        garbage collector closed. Server.connect gives out watched connections; a connection made by hand needs
        watched() with the same seen list."""
        seen = [] if seen is None else seen
        left, connect = [], check.Server.connect
        with warnings.catch_warnings(record=True) as caught, mock.patch.object(
                check.Server, "connect", lambda server: watched(connect(server), seen)):
            warnings.simplefilter("always", ResourceWarning)
            yield left
            gc.collect()
            left += [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertTrue(seen, "no response was given out, so nothing was checked")
        left += ["a response with status %s" % r.status for r in seen if not r.isclosed()]

    @staticmethod
    def no_errors():
        return {"attempts": 0, "retried": 0, "final": 0}


class PinnedRunTests(LinkCase):
    def test_a_pinned_run_never_looks_the_name_up_and_keeps_the_name_on_the_wire(self):
        real = socket.getaddrinfo

        def only_by_address(host, port, *rest, **more):
            if host == HOST:
                raise AssertionError("the host name was looked up")
            return real(host, port, *rest, **more)

        tls, resolve = FakeTls(), mock.Mock(return_value="127.0.0.1")
        with mock.patch.object(socket, "getaddrinfo", side_effect=only_by_address):
            outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=2,nvfp4=1", url=self.https_url(),
                                   resolve=resolve, tls=tls)
        self.assertEqual(outcome.code, 0, outcome.err)
        self.assertEqual(len(outcome.runs()), 3)
        resolve.assert_called_once_with(HOST)
        self.assertEqual(set(tls.server_names), {HOST})
        self.assertEqual(len(tls.server_names), len(self.demo.requests))
        self.assertEqual(self.demo.hosts, {f"{HOST}:{self.demo.server.server_address[1]}"})
        self.assertEqual(outcome.summary["scheme"], "https")
        self.assertEqual(outcome.summary["dns"], {"mode": "doh", "pinned": True})
        self.assertEqual(outcome.err.splitlines()[0],
                         "dns: looked up once through DNS-over-HTTPS; the address is not shown")

    def test_the_default_resolver_is_the_doh_lookup_with_the_injected_sleep(self):
        recorded = []
        with mock.patch.object(check, "doh_address", return_value="127.0.0.1") as lookup:
            outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1", url=self.https_url(), sleep=recorded.append)
        self.assertEqual(outcome.code, 0, outcome.err)
        lookup.assert_called_once_with(HOST, recorded.append)
        self.assertEqual(outcome.summary["dns"], {"mode": "doh", "pinned": True})

    def test_neither_the_name_nor_the_address_reaches_the_output(self):
        self.script("plan-fee")["rounds"][-1]["text"] = (
            f"할인 후 총액은 1,242,000원이고 할인 전보다 414,000원 적습니다. (via {HOST} at 127.0.0.1)")
        with tempfile.TemporaryDirectory() as folder:
            result, table = Path(folder, "result.json"), Path(folder, "result.md")
            outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1", "--out", str(result),
                                   "--table", str(table), url=self.https_url(),
                                   resolve=mock.Mock(return_value="127.0.0.1"))
            saved, saved_table = result.read_text("utf-8"), table.read_text("utf-8")
        self.assertEqual(outcome.code, 0, outcome.err)
        self.assertIn("(via <host> at <ip>)", outcome.out)
        self.assertEqual(saved, outcome.out)
        self.assertTrue(outcome.err.endswith(saved_table))
        for text in (outcome.out, outcome.err, saved, saved_table):
            self.assertNotIn(HOST, text)
            self.assertNotIn("127.0.0.1", text)

    def test_a_failed_lookup_falls_back_to_the_system_resolver(self):
        names = []

        def refuse(host, port, *rest, **more):
            names.append(host)
            raise socket.gaierror("no such host")

        resolve = mock.Mock(side_effect=LookupError(NO_ANSWER))
        with mock.patch.object(socket, "getaddrinfo", side_effect=refuse):
            outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1", url=self.https_url(), resolve=resolve)
        self.assertEqual(outcome.summary["dns"], {"mode": "doh", "pinned": False, "note": FALLBACK})
        self.assertEqual(outcome.err.splitlines()[0], "dns: " + FALLBACK)
        self.assertEqual(names, [HOST] * (1 + check.PREFLIGHT_RETRIES))
        (rec,) = outcome.runs()
        self.assertEqual(rec["skipped"], "api/me transport gaierror")
        self.assertEqual((outcome.code, outcome.sleeps, self.demo.requests), (1, [10.0, 10.0], []))

    def test_the_system_mode_leaves_the_name_to_the_system_resolver(self):
        real, names = socket.getaddrinfo, []

        def local(host, port, *rest, **more):
            names.append(host)
            return real("127.0.0.1" if host == HOST else host, port, *rest, **more)

        resolve, tls = mock.Mock(side_effect=AssertionError("looked up")), FakeTls()
        with mock.patch.object(socket, "getaddrinfo", side_effect=local):
            outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1", "--dns", "system", url=self.https_url(),
                                   resolve=resolve, tls=tls)
        self.assertEqual(outcome.code, 0, outcome.err)
        resolve.assert_not_called()
        self.assertEqual(outcome.summary["dns"], {"mode": "system", "pinned": False})
        self.assertNotIn("dns:", outcome.err)
        self.assertEqual(set(names), {HOST})
        self.assertEqual(set(tls.server_names), {HOST})

    def test_a_loopback_demo_over_http_is_never_looked_up(self):
        resolve = mock.Mock(side_effect=AssertionError("looked up"))
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1", resolve=resolve)
        self.assertEqual(outcome.code, 0, outcome.err)
        resolve.assert_not_called()
        self.assertEqual(outcome.summary["dns"], {"mode": "doh", "pinned": False, "note": NO_LOOKUP})
        self.assertEqual(outcome.err.splitlines()[0], "dns: " + NO_LOOKUP)

    def test_a_clean_run_has_one_attempt_and_no_connection_error(self):
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        (rec,) = outcome.runs()
        self.assertEqual((rec["attempt"], rec["connection_error"]), (1, None))
        self.assertNotIn("superseded", rec)
        self.assertEqual(outcome.summary["connection_errors"], self.no_errors())


# ------------------------------------------------------------------------------------ the rules for the plan
class CourseTests(unittest.TestCase):
    @staticmethod
    def attempt(**more):
        return {"id": "plan-fee", "http_5xx": 0, "stop": None, "connection_error": None, "http": {}, **more}

    def test_three_attempts_in_a_row_with_a_lost_connection_stop_the_plan(self):
        course = check.Course()
        for _ in range(2):
            self.assertIsNone(course.judge(self.attempt(connection_error=LOST)))
            self.assertIsNone(course.stop)
        self.assertIsNone(course.judge(self.attempt(connection_error=LOST)))
        self.assertEqual(course.stop, "stopped after 3 attempts in a row with connection errors")

    def test_an_attempt_that_got_through_resets_the_count(self):
        course = check.Course()
        for rec in (self.attempt(connection_error=LOST), self.attempt(connection_error=LOST), self.attempt(),
                    self.attempt(connection_error=LOST), self.attempt(connection_error=LOST)):
            course.judge(rec)
        self.assertIsNone(course.stop)
        course.judge(self.attempt(connection_error=LOST))
        self.assertEqual(course.stop, "stopped after 3 attempts in a row with connection errors")

    def test_a_candidate_is_run_again_once_in_all(self):
        course, rec = check.Course(), self.attempt(connection_error=LOST)
        self.assertTrue(course.again(rec))
        self.assertFalse(course.again(rec))
        self.assertTrue(course.again(self.attempt(id="time-zones", connection_error=LOST)))
        self.assertEqual(course.retried, {"plan-fee", "time-zones"})

    def test_there_is_no_retry_without_a_lost_connection_or_after_a_stop(self):
        course = check.Course()
        self.assertFalse(course.again(self.attempt()))
        course.stop = "stopped after HTTP 429"
        self.assertFalse(course.again(self.attempt(connection_error=LOST)))
        self.assertEqual(course.retried, set())

    def test_a_refusal_status_stops_the_plan_before_any_retry(self):
        course, rec = check.Course(), self.attempt(connection_error=LOST, stop=429)
        self.assertIsNone(course.judge(rec))
        self.assertEqual(course.stop, "stopped after HTTP 429")
        self.assertFalse(course.again(rec))

    def test_a_503_names_the_problem_of_the_profile_without_stopping_the_plan(self):
        course = check.Course()
        for where in ("workspace", "agent"):
            with self.subTest(where=where):
                problem = course.judge(self.attempt(http={where: 503}, http_5xx=1))
                self.assertEqual(problem, "the profile answered HTTP 503")
                self.assertIsNone(course.stop)


class RenderTests(unittest.TestCase):
    @staticmethod
    def rows(*records):
        return [[c.strip() for c in line.split("|")[1:-1]]
                for line in check.render_table(list(records)).splitlines()[2:]]

    @staticmethod
    def progress(position, total, rec):
        out = io.StringIO()
        check.progress_line(position, total, rec, out)
        return out.getvalue()

    def test_a_lost_attempt_is_a_connection_error_row_marked_retried(self):
        failed = {"connection": "gaierror at workspace", "completed": "transport gaierror"}
        (row,) = self.rows(record(connection_error=LOST, superseded=True, failed=failed))
        self.assertEqual(row, ["plan-fee", "FP8", "1", "CONNECTION ERROR", "-", "-", "-/-", "-", "-", "0", "-", "-",
                               "connection: gaierror at workspace; completed: transport gaierror; retried", ""])

    def test_the_last_attempt_is_not_marked_retried_and_a_retry_says_so(self):
        failed = {"connection": "gaierror at workspace", "completed": "transport gaierror"}
        (lost,) = self.rows(record(connection_error=LOST, attempt=2, failed=failed))
        self.assertEqual((lost[2], lost[3], lost[12]),
                         ("1 (retry)", "CONNECTION ERROR", "connection: gaierror at workspace; completed: "
                                                           "transport gaierror"))
        (good,) = self.rows(record(ok=True, attempt=2, total_seconds=7.35, ttft_seconds=3.1, input_tokens=100,
                                   output_tokens=20, model_calls=2, tools=[{"name": "calculator"}]))
        self.assertEqual(good, ["plan-fee", "FP8", "1 (retry)", "PASS", "7.35", "3.1", "100/20", "2", "calculator",
                                "0", "-", "-", "", ""])

    def test_an_ordinary_failure_stays_a_failure(self):
        (row,) = self.rows(record(failed={"time": "46.5 s is over 45 s"}, wall_seconds=46.5))
        self.assertEqual((row[3], row[4], row[12]), ("FAIL", "46.5", "time: 46.5 s is over 45 s"))

    def test_the_progress_line_says_when_the_attempt_will_be_made_again(self):
        lost = record(connection_error=LOST, elapsed_seconds=0.5)
        self.assertEqual(self.progress(1, 4, {**lost, "superseded": True}),
                         "[1/4] FP8 plan-fee #1: CONNECTION ERROR gaierror at workspace, retrying once 0.5s "
                         "calls - tools -\n")
        self.assertEqual(self.progress(2, 4, lost),
                         "[2/4] FP8 plan-fee #1: CONNECTION ERROR gaierror at workspace 0.5s calls - tools -\n")
        good = record(ok=True, attempt=2, total_seconds=7.35, model_calls=2, tools=[{"name": "calculator"}])
        self.assertEqual(self.progress(3, 4, good), "[3/4] FP8 plan-fee #1 (retry): PASS 7.35s calls 2 tools "
                                                    "calculator\n")


class RetryTests(LinkCase):
    def stream_cut(self, error):
        with self.cutting(error):
            outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        first, second = outcome.runs()
        name = type(error).__name__
        self.assertEqual(first["connection_error"], {"error": name, "step": "agent"})
        self.assertEqual((first["state"], first["http"]["agent"]), (f"transport {name}", 200))
        self.assertEqual(first["failed"], {"connection": f"{name} at agent", "completed": f"transport {name}"})
        self.assertTrue(second["ok"], second["failed"])
        self.assertEqual(self.demo.cancelled, ["tok001"])
        self.assertEqual(self.demo.closed, ["tok001", "tok002"])
        self.assertEqual((outcome.code, outcome.sleeps), (0, [10.0]))

    def test_a_lost_run_is_made_once_more_and_the_second_attempt_counts(self):
        self.drop(WORKSPACE, {1})
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        first, second = outcome.runs()
        name = self.cause(first, "workspace")
        self.assertEqual((first["attempt"], first["superseded"], first["ok"]), (1, True, False))
        self.assertEqual((first["state"], first["http"]), (f"transport {name}", {"workspace": None}))
        self.assertEqual(first["failed"], {"connection": f"{name} at workspace", "completed": f"transport {name}"})
        self.assertEqual((second["attempt"], second["ok"], second["connection_error"]), (2, True, None))
        self.assertNotIn("superseded", second)
        self.assertEqual((outcome.code, outcome.sleeps), (0, [10.0]))
        self.assertEqual(outcome.summary["totals"],
                         {"passed": 1, "failed": 0, "skipped": 0, "http_5xx": 0, "web_iq_calls": 0})
        self.assertEqual(outcome.summary["connection_errors"], {"attempts": 1, "retried": 1, "final": 0})
        self.assertTrue(outcome.summary["ok"])
        self.assertEqual((self.demo.created, self.demo.closed), (["tok001"], ["tok001"]))

    def test_the_wait_before_the_retry_is_the_gap(self):
        self.drop(WORKSPACE, {1})
        self.assertEqual(self.run_cli("--only", "plan-fee", "--runs", "fp8=1", "--gap", "25").sleeps, [25.0])

    def test_a_lost_retry_stays_a_connection_error_and_is_not_made_again(self):
        self.drop(WORKSPACE)
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        first, second = outcome.runs()
        self.cause(first, "workspace")
        self.cause(second, "workspace")
        self.assertEqual((first["attempt"], first["superseded"], second["attempt"], second.get("superseded")),
                         (1, True, 2, None))
        self.assertEqual((outcome.code, outcome.summary["ok"], self.hits(WORKSPACE)), (1, False, 2))
        self.assertEqual(outcome.summary["connection_errors"], {"attempts": 2, "retried": 1, "final": 1})
        self.assertEqual(outcome.summary["totals"],
                         {"passed": 0, "failed": 0, "skipped": 0, "http_5xx": 0, "web_iq_calls": 0})

    def test_a_candidate_has_one_retry_in_all(self):
        self.drop(WORKSPACE, {1, 3})
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=2")
        self.assertEqual([(r["run"], r["attempt"]) for r in outcome.runs()], [(1, 1), (1, 2), (2, 1)])
        self.assertEqual([r.get("superseded") for r in outcome.runs()], [True, None, None])
        self.assertEqual([r["ok"] for r in outcome.runs()], [False, True, False])
        self.assertEqual(outcome.sleeps, [10.0, 10.0])
        self.assertEqual(outcome.summary["connection_errors"], {"attempts": 2, "retried": 1, "final": 1})

    def test_every_candidate_gets_its_own_retry(self):
        self.drop(WORKSPACE, {1, 3})
        outcome = self.run_cli("--only", "plan-fee,time-zones", "--runs", "fp8=1")
        self.assertEqual([(r["id"], r["attempt"]) for r in outcome.runs()],
                         [("plan-fee", 1), ("plan-fee", 2), ("time-zones", 1), ("time-zones", 2)])
        self.assertEqual(outcome.summary["totals"]["passed"], 2)
        self.assertEqual(outcome.sleeps, [10.0] * 3)
        self.assertEqual(outcome.summary["connection_errors"], {"attempts": 2, "retried": 2, "final": 0})
        self.assertEqual(outcome.code, 0)

    def test_three_attempts_in_a_row_stop_the_plan_and_skip_the_rest(self):
        self.drop(WORKSPACE)
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=5")
        runs = outcome.runs()
        self.assertEqual([(r["run"], r.get("attempt"), r.get("superseded")) for r in runs],
                         [(1, 1, True), (1, 2, None), (2, 1, None), (3, None, None), (4, None, None), (5, None, None)])
        self.assertEqual({r["skipped"] for r in runs[3:]},
                         {"stopped after 3 attempts in a row with connection errors"})
        self.assertEqual(self.hits(WORKSPACE), 3)
        self.assertEqual(outcome.summary["connection_errors"], {"attempts": 3, "retried": 1, "final": 2})
        self.assertEqual(outcome.summary["totals"],
                         {"passed": 0, "failed": 0, "skipped": 3, "http_5xx": 0, "web_iq_calls": 0})
        self.assertEqual((outcome.code, outcome.sleeps), (1, [10.0, 10.0]))

    def test_a_run_that_got_through_resets_the_streak(self):
        self.drop(WORKSPACE, {1, 2, 4, 5})
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=5")
        runs = outcome.runs()
        self.assertEqual([(r["run"], r["attempt"]) for r in runs], [(1, 1), (1, 2), (2, 1), (3, 1), (4, 1), (5, 1)])
        self.assertEqual([r["ok"] for r in runs], [False, False, True, False, False, True])
        self.assertFalse(any(r.get("skipped") for r in runs))
        self.assertEqual(outcome.summary["connection_errors"], {"attempts": 4, "retried": 1, "final": 3})
        self.assertEqual(outcome.summary["totals"]["passed"], 2)

    def test_a_refusal_status_ends_the_plan_without_a_retry(self):
        self.drop(AGENT)
        self.reply(CLOSE, 429, {"error": "rate_limited"})
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=2")
        first, second = outcome.runs()
        self.cause(first, "agent")
        self.assertEqual((first["stop"], first["http"]["agent"], first["http"]["close"]), (429, None, 429))
        self.assertNotIn("superseded", first)
        self.assertEqual(second["skipped"], "stopped after HTTP 429")
        self.assertEqual((self.hits(AGENT), self.hits(WORKSPACE), outcome.sleeps), (1, 1, []))

    def test_a_reset_stream_is_a_connection_error(self):
        self.stream_cut(ConnectionResetError("cut"))

    def test_a_stream_that_times_out_is_a_connection_error(self):
        self.stream_cut(TimeoutError("timed out"))

    def test_a_stream_that_ends_short_is_a_connection_error(self):
        self.stream_cut(http.client.IncompleteRead(b"x", 9))

    def test_a_lost_agent_request_is_a_connection_error_at_the_agent_step(self):
        self.drop(AGENT, {1})
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        first, second = outcome.runs()
        name = self.cause(first, "agent")
        self.assertEqual(first["http"], {"workspace": 200, "agent": None, "close": 200})
        self.assertEqual(first["failed"], {"connection": f"{name} at agent", "completed": f"transport {name}"})
        self.assertEqual(self.demo.cancelled, ["tok001"])
        self.assertTrue(second["ok"], second["failed"])

    def test_a_name_that_does_not_resolve_once_is_retried(self):
        real, names = socket.getaddrinfo, []

        def lookup(host, port, *rest, **more):
            names.append(host)
            if len(names) == 3:  # the two preflight requests come first
                raise socket.gaierror("no such host")
            return real(host, port, *rest, **more)

        with mock.patch.object(socket, "getaddrinfo", side_effect=lookup):
            outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1", "--dns", "system")
        first, second = outcome.runs()
        self.assertEqual(first["connection_error"], LOST)
        self.assertEqual(first["failed"], {"connection": "gaierror at workspace", "completed": "transport gaierror"})
        self.assertTrue(second["ok"], second["failed"])
        self.assertIn("[1/1] FP8 plan-fee #1: CONNECTION ERROR gaierror at workspace, retrying once", outcome.err)

    def test_the_report_shows_the_lost_attempt_and_the_retry(self):
        self.drop(WORKSPACE, {1})
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        name = self.cause(outcome.runs()[0], "workspace")
        lines = outcome.err.splitlines()
        self.assertRegex(lines[1], rf"^\[1/1\] FP8 plan-fee #1: CONNECTION ERROR {name} at workspace, retrying once "
                                   r"[0-9.]+s calls - tools -$")
        self.assertEqual(lines[2], "[1/1] FP8 plan-fee #1 (retry): PASS 7.35s calls 2 tools calculator>calculator")
        rows = [[c.strip() for c in line.split("|")[1:-1]] for line in lines if line.startswith("| plan-fee ")]
        self.assertEqual({len(row) for row in rows}, {14})
        self.assertEqual([(row[2], row[3]) for row in rows], [("1", "CONNECTION ERROR"), ("1 (retry)", "PASS")])
        self.assertEqual([row[12] for row in rows],
                         [f"connection: {name} at workspace; completed: transport {name}; retried", ""])

    def test_ctrl_c_while_waiting_for_the_retry_skips_the_whole_plan(self):
        def interrupt(seconds):
            raise KeyboardInterrupt

        self.drop(WORKSPACE, {1})
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=2", sleep=interrupt)
        self.assertEqual([(r["run"], r.get("superseded"), r.get("skipped")) for r in outcome.runs()],
                         [(1, True, None), (1, None, "interrupted"), (2, None, "interrupted")])
        self.assertEqual((outcome.code, outcome.summary["interrupted"]), (1, True))

    def test_ctrl_c_after_a_retry_skips_only_the_runs_not_yet_made(self):
        waits = []

        def interrupt_second(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                raise KeyboardInterrupt

        self.drop(WORKSPACE, {1})
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=3", sleep=interrupt_second)
        self.assertEqual([(r["run"], r.get("attempt"), r.get("superseded"), r.get("skipped")) for r in outcome.runs()],
                         [(1, 1, True, None), (1, 2, None, None), (2, None, None, "interrupted"),
                          (3, None, None, "interrupted")])
        self.assertEqual(outcome.summary["interrupted"], True)


class StepTests(LinkCase):
    def test_a_lost_upload_ends_the_attempt_before_the_turn(self):
        self.drop(UPLOAD, {1})
        outcome = self.run_cli("--only", "contract-review", "--runs", "fp8=1")
        first, second = outcome.runs()
        name = self.cause(first, "upload")
        self.assertEqual(first["http"], {"workspace": 200, "upload": None, "close": 200})
        self.assertEqual(first["failed"], {"connection": f"{name} at upload", "completed": f"upload transport {name}"})
        self.assertEqual(len(self.demo.bodies), 1)  # only the second attempt asked the model
        self.assertTrue(second["ok"], second["failed"])
        self.assertEqual((self.demo.created, self.demo.closed), (["tok001", "tok002"], ["tok001", "tok002"]))

    def test_a_lost_download_names_the_file_and_the_preview_is_not_tried(self):
        self.drop(DOWNLOAD, method="GET")
        outcome = self.run_cli("--only", "ai-chip-news", "--runs", "fp8=1")
        first, second = outcome.runs()
        name = self.cause(first, "download")
        self.assertEqual(first["state"], "completed")
        self.assertEqual(first["failed"], {"connection": f"{name} at download",
                                           "files": "briefing.html could not be downloaded (connection error)"})
        self.assertEqual(first["files"], [{"name": "briefing.html", "download": None, "bytes": None}])
        self.assertEqual(self.cause(second, "download"), second["connection_error"]["error"])
        self.assertEqual((self.hits(DOWNLOAD, "GET"), self.hits(PREVIEW, "GET")), (2, 0))
        self.assertEqual((outcome.code, outcome.summary["connection_errors"]),
                         (1, {"attempts": 2, "retried": 1, "final": 1}))

    def test_the_files_after_a_lost_download_are_not_asked_for(self):
        self.drop(DOWNLOAD, method="GET")
        outcome = self.run_cli("--only", "monthly-sales-report", "--runs", "fp8=1")
        lost = "could not be downloaded (connection error)"
        for rec in outcome.runs():
            self.assertEqual([(f["name"], f["download"]) for f in rec["files"]],
                             [("sales.csv", None), ("report.html", None)])
            self.assertEqual(rec["failed"]["files"], f"sales.csv {lost}; report.html {lost}")
        self.assertEqual(self.hits(DOWNLOAD, "GET"), 2)

    def test_a_lost_preview_is_not_a_page_that_does_not_open(self):
        self.drop(PREVIEW, {1}, method="GET")
        outcome = self.run_cli("--only", "ai-chip-news", "--runs", "fp8=1")
        first, second = outcome.runs()
        name = self.cause(first, "preview")
        self.assertEqual((first["files"][0]["download"], first["files"][0]["preview"]), (200, None))
        self.assertEqual(first["failed"], {"connection": f"{name} at preview",
                                           "files": "briefing.html could not be opened in the preview "
                                                    "(connection error)"})
        self.assertTrue(second["ok"], second["failed"])

    def test_a_lost_close_is_not_a_connection_error(self):
        self.drop(CLOSE)
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        (rec,) = outcome.runs()
        self.assertEqual((rec["ok"], rec["connection_error"], rec["http"]["close"]), (True, None, None))
        self.assertEqual((outcome.code, self.hits(WORKSPACE)), (0, 1))
        self.assertEqual(outcome.summary["connection_errors"], self.no_errors())


class NotLostTests(LinkCase):
    def test_an_http_503_is_not_a_lost_connection(self):
        self.reply(WORKSPACE, 503, {"error": "workspace_capacity"})
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        (rec,) = outcome.runs()
        self.assertEqual((rec["connection_error"], rec["http"], rec["http_5xx"]), (None, {"workspace": 503}, 1))
        self.assertEqual(rec["failed"], {"completed": "http 503 workspace_capacity", "http_5xx": "1"})
        self.assertEqual((self.hits(WORKSPACE), outcome.sleeps), (1, []))
        self.assertEqual(outcome.summary["connection_errors"], self.no_errors())
        self.assertEqual(outcome.summary["totals"]["failed"], 1)

    def test_an_http_500_from_the_agent_route_is_not_a_lost_connection(self):
        self.reply(AGENT, 500, {"error": "upstream"})
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        (rec,) = outcome.runs()
        self.assertEqual((rec["connection_error"], rec["http"]["agent"], rec["http_5xx"]), (None, 500, 1))
        self.assertEqual((self.hits(WORKSPACE), outcome.sleeps), (1, []))
        self.assertEqual(outcome.summary["connection_errors"], self.no_errors())

    def test_a_turn_that_only_runs_over_its_deadline_is_a_failure(self):
        self.script("plan-fee")["hang"] = True
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1", "--timeout", "1")
        (rec,) = outcome.runs()
        self.assertEqual((rec["state"], rec["connection_error"], rec["failed"]),
                         ("deadline", None, {"completed": "deadline"}))
        self.assertEqual((self.hits(WORKSPACE), self.demo.cancelled, outcome.sleeps), (1, ["tok001"], []))
        self.assertEqual(outcome.summary["totals"]["failed"], 1)

    def test_a_lost_cancel_after_a_deadline_does_not_count(self):
        self.script("plan-fee")["hang"] = True
        self.drop(CANCEL)
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1", "--timeout", "1")
        (rec,) = outcome.runs()
        self.assertEqual((rec["state"], rec["connection_error"], rec["failed"]),
                         ("deadline", None, {"completed": "deadline"}))
        self.assertEqual((self.demo.cancelled, self.hits(WORKSPACE)), ([], 1))

    def test_an_event_line_over_the_limit_is_a_failure(self):
        self.script("plan-fee")["raw"] = ["data: " + "x" * (check.MAX_LINE + 10) + "\n\n"]
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        (rec,) = outcome.runs()
        text = "transport oversized line"
        self.assertEqual((rec["state"], rec["connection_error"], rec["failed"]), (text, None, {"completed": text}))
        self.assertEqual(self.hits(WORKSPACE), 1)

    def test_a_stream_that_ends_cleanly_without_an_end_event_is_a_failure(self):
        with mock.patch.object(check, "events", lambda response, deadline: iter(())):
            outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        (rec,) = outcome.runs()
        self.assertEqual((rec["state"], rec["connection_error"], rec["failed"]),
                         ("ended without end", None, {"completed": "ended without end"}))
        self.assertEqual((rec["http"]["agent"], self.hits(WORKSPACE)), (200, 1))


class NoLeakTests(LinkCase):
    """No way out of a request leaves a response open or a socket for the garbage collector to close."""

    def test_a_body_over_the_limit_leaves_nothing_open(self):
        server = serve(self, b"x" * (check.MAX_BODY + 1))
        asking = check.Server(check.Target("http", "127.0.0.1", server.server_address[1]), 30)
        with self.left_open() as left:
            reply = asking.request("GET", "/x")
        self.assertEqual((reply.status, len(reply.body), left), (200, check.MAX_BODY, []))

    def test_a_resolver_error_leaves_nothing_open(self):
        server, seen = serve(self, b"{}", status=503), []
        connection = watched(http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30), seen)
        with self.left_open(seen) as left:
            address = check.doh_query(connection, HOST)
        self.assertEqual((address, left, server.requests[0][0]), (None, [], "/resolve?name=demo.example.com&type=A"))

    def test_a_clean_run_leaves_nothing_open(self):
        with self.left_open() as left:
            outcome = self.run_cli("--only", "ai-chip-news", "--runs", "fp8=1")
        self.assertEqual((outcome.code, left), (0, []))

    def test_a_stream_that_breaks_leaves_nothing_open(self):
        with self.cutting(ConnectionResetError("cut")), self.left_open() as left:
            outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        self.assertEqual((outcome.code, len(outcome.runs()), left), (0, 2, []))

    def test_a_deadline_leaves_nothing_open(self):
        self.script("plan-fee")["hang"] = True
        with self.left_open() as left:
            outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1", "--timeout", "1")
        self.assertEqual((outcome.runs()[0]["state"], left), ("deadline", []))

    def test_an_oversized_line_leaves_nothing_open(self):
        self.script("plan-fee")["raw"] = ["data: " + "x" * (check.MAX_LINE + 10) + "\n\n"]
        with self.left_open() as left:
            outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        self.assertEqual((outcome.runs()[0]["state"], left), ("transport oversized line", []))

    def test_a_refused_request_leaves_nothing_open(self):
        self.reply(AGENT, 429, {"error": "rate_limited"})
        with self.left_open() as left:
            outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        self.assertEqual((outcome.runs()[0]["stop"], left), (429, []))

    def test_a_lost_connection_leaves_nothing_open(self):
        self.drop(WORKSPACE)
        with self.left_open() as left:
            outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        self.assertEqual((outcome.code, len(outcome.runs()), left), (1, 2, []))

    def test_the_check_sees_a_response_that_is_left_open(self):
        server, seen = serve(self, b"x" * 100), []
        connection = watched(http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30), seen)
        with self.left_open(seen) as left:
            connection.request("GET", "/x")
            connection.getresponse().read(10)
        self.assertEqual(left, ["a response with status 200"])
        seen[0].close()


class PreflightTests(LinkCase):
    def test_a_me_request_that_gets_no_answer_is_asked_again(self):
        self.drop(ME, {1, 2}, method="GET")
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        (rec,) = outcome.runs()
        self.assertTrue(rec["ok"], rec["failed"])
        self.assertEqual((outcome.code, outcome.sleeps, self.hits(ME, "GET")), (0, [10.0, 10.0], 3))

    def test_the_wait_between_me_requests_is_the_gap(self):
        self.drop(ME, {1, 2}, method="GET")
        self.assertEqual(self.run_cli("--only", "plan-fee", "--runs", "fp8=1", "--gap", "30").sleeps, [30.0, 30.0])

    def test_a_server_that_never_answers_skips_the_runs_of_the_profile(self):
        self.drop(ME, method="GET")
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=2")
        reasons = {r["skipped"] for r in outcome.runs()}
        self.assertEqual(len(reasons), 1)
        self.assertRegex(reasons.pop(), r"^api/me transport (%s)$" % "|".join(sorted(DROPPED)))
        self.assertEqual((outcome.code, outcome.sleeps, self.hits(ME, "GET"), self.demo.created),
                         (1, [10.0, 10.0], 1 + check.PREFLIGHT_RETRIES, []))

    def test_an_http_refusal_is_not_asked_again(self):
        self.reply(ME, 401, {"error": "login"}, method="GET")
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1")
        (rec,) = outcome.runs()
        self.assertEqual(rec["skipped"], "api/me http 401 login")
        self.assertEqual((outcome.sleeps, self.hits(ME, "GET")), ([], 1))

    def test_a_profile_that_cannot_be_reached_does_not_stop_the_other_one(self):
        self.drop(ME, method="GET", profile="nvfp4")
        outcome = self.run_cli("--only", "plan-fee", "--runs", "fp8=1,nvfp4=1")
        first, second = outcome.runs()
        self.assertTrue(first["ok"], first["failed"])
        self.assertRegex(second["skipped"], r"^api/me transport \w+$")
        self.assertEqual((outcome.code, outcome.sleeps), (1, [10.0, 10.0]))


if __name__ == "__main__":
    unittest.main()
