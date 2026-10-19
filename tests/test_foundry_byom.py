"""Offline tests for the Foundry BYOM tooling (demo/foundry-byom): no network, no Azure, no model calls."""
import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "demo" / "foundry-byom"
sys.path.insert(0, str(HERE))

import byom  # noqa: E402
import observe_requests  # noqa: E402
import verify_gateway  # noqa: E402

SUB = "11111111-1111-4111-8111-111111111111"
TENANT = "22222222-2222-4222-8222-222222222222"
NAME = "demoprefix1"
KEY = "fake-subscription-key-" + "x" * 20
BACKEND_KEY = "fake-backend-key-" + "y" * 24
ADMITTED = [f"ax-k2-nvfp4-{check}" for check in ("chat", "stream", "tool", "tool_result")]

# Stands in for demo/tools/model_api_smoke.py: lists the flags named in FAKE_FLAGS for --help, otherwise records
# its argv in FAKE_LOG and prints and writes a JSON summary shaped like the real tool's: one entry per check with
# sent_request_id and x_request_id, one such pair per rejected call under auth, and the admissions count.
FAKE_TOOL = """
import json, os, sys
args = sys.argv[1:]
if "--help" in args:
    choices = os.environ.get("FAKE_CHOICES", "{bearer,api-key}")
    print("usage: fake_smoke " + " ".join(os.environ["FAKE_FLAGS"].split()) + " " + choices)
    sys.exit(0)
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + chr(10))
value = lambda flag: args[args.index(flag) + 1]
model = value("--model")
sent = lambda label: {"status": 200, "x_request_id": label, "sent_request_id": label, "request_id_echoed": True}
checks = {}
for name in value("--only").split(","):
    label = model + "-" + name
    if name == "auth":
        checks[name] = {"ok": True, "no_key": dict(sent(label + "-no_key"), status=401),
                        "wrong_key": dict(sent(label + "-wrong_key"), status=401)}
    else:
        checks[name] = dict(sent(label), ok=True)
if "stream" in checks:
    checks["stream"].update({"ttft_s": 0.5, "chunks": 7})
result = {"tool": "model_api_smoke", "model": model, "target": value("--base-url"), "ok": True, "checks": checks,
          "started_at": "2026-01-01T00:00:00Z",
          "admissions": len({"chat", "stream", "tool", "tool_result"}.intersection(checks))}
override = os.environ.get("FAKE_ADMISSIONS", "")
if override == "omit":
    del result["admissions"]
elif override:
    result["admissions"] = int(override)
text = json.dumps(result, indent=2)
print(text)
with open(value("--out"), "w") as out:
    out.write(text)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


def trace_row(request_id):
    shape = {"request_id": request_id, "path": "/models/nvfp4/v1/chat/completions", "stream": request_id.endswith("stream"),
             "has_max_tokens": True, "has_max_completion_tokens": False}
    return {"TimeGenerated": "2026-01-01T00:00:00Z", "OperationId": "op-" + request_id, "Message": json.dumps(shape)}


def request_row(request_id):
    return {"TimeGenerated": "2026-01-01T00:00:00Z", "OperationId": "op-" + request_id,
            "Name": "POST /nvfp4/v1/chat/completions", "ResultCode": "200", "DurationMs": 1234.56, "Success": True,
            "Api": "axk2-nvfp4", "Operation": "chat", "Subscription": "foundry-byom",
            "ApimRequestId": "apim-" + request_id}


def dependency_row(request_id):
    return {"TimeGenerated": "2026-01-01T00:00:00Z", "OperationId": "op-" + request_id,
            "Name": "POST /models/nvfp4/v1/chat/completions", "ResultCode": "200", "DurationMs": 1200.0,
            "Success": True}


def forwarded_row(request_id, **flags):
    """One row of the forwarded-headers query: the gateway removed every client credential and added its own."""
    row = {"TimeGenerated": "2026-01-01T00:00:00Z", "OperationId": "op-" + request_id, "request_id": request_id,
           "method": "POST", "api_key_header": False, "subscription_key_header": False,
           "api_key_query": False, "subscription_key_query": False, "authorization_header": True}
    return row | flags


def forwarded_trace_row(request_id, **flags):
    """The same record as the trace message the policy writes."""
    row = forwarded_row(request_id, **flags)
    record = {"kind": verify_gateway.FORWARDED_KIND} | {
        key: value for key, value in row.items() if key not in ("TimeGenerated", "OperationId")}
    return {"TimeGenerated": row["TimeGenerated"], "OperationId": row["OperationId"], "Message": json.dumps(record)}


class FakeLogs:
    """Canned Log Analytics answers, selected by the table and the shape of the query."""

    def __init__(self, traced=ADMITTED, scan=(), forwarded=None, backend_code="200"):
        self.queries = []
        self.traced = list(traced)
        self.scan = list(scan)
        self.forwarded = forwarded
        self.backend_code = backend_code

    def __call__(self, text):
        self.queries.append(text)
        if text.startswith("union"):
            return list(self.scan)
        if text.startswith("AppTraces") and f"'{verify_gateway.FORWARDED_KIND}'" in text:
            return [forwarded_row(item) for item in self.traced] if self.forwarded is None else list(self.forwarded)
        if text.startswith("AppTraces"):
            # Like the real table, the request-id lookup also returns the gateway's header trace.
            return [trace_row(item) for item in self.traced] + [forwarded_trace_row(item) for item in self.traced]
        if text.startswith("AppRequests") and "'401'" in text:
            return [{"Name": "POST /nvfp4/v1/chat/completions", "Count": 2}]
        if text.startswith("AppRequests") and "summarize" in text:
            return [{"Name": "POST /nvfp4/v1/chat/completions", "ResultCode": "200", "Subscription": "foundry-byom",
                     "Count": len(self.traced), "AvgMs": 1.0, "MaxMs": 2.0}]
        if text.startswith("AppRequests"):
            return [request_row(item) for item in self.traced]
        if text.startswith("AppDependencies"):
            return [dict(dependency_row(item), ResultCode=self.backend_code) for item in self.traced]
        raise AssertionError("unexpected query: " + text)


class ClientCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.core = self.tmp / "core"
        self.core.mkdir()
        (self.core / "target.json").write_text(json.dumps(
            {"subscription_id": SUB, "tenant_id": TENANT, "name": NAME, "web_location": "uksouth"}))
        self.private = self.tmp / "private"
        self.private.mkdir()
        (self.private / byom.SUBSCRIPTION_KEY_FILE).write_text(KEY + "\n")
        (self.core / byom.BACKEND_KEY_FILE).write_text(BACKEND_KEY + "\n")
        self.client = byom.Byom(self.core, self.private)
        self.client.verify_identity = lambda: None
        self.evidence = self.tmp / "evidence"
        self.tool = self.tmp / "fake_smoke.py"
        self.tool.write_text(FAKE_TOOL)
        self.log = self.tmp / "calls.log"
        for patcher in (
                mock.patch.object(byom, "EVIDENCE", self.evidence),
                # POSIX permission bits do not exist on Windows; the real check runs under Linux.
                mock.patch.object(byom, "read_secret_file", lambda path: Path(path).read_text().strip()),
                mock.patch.dict(os.environ, {"FAKE_FLAGS": " ".join(verify_gateway.FLAGS.values()),
                                             "FAKE_LOG": str(self.log), "FAKE_EXIT": "0"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.options = argparse.Namespace(smoke_tool=self.tool, smoke_python=sys.executable, go=True, phase="all",
                                          ingest_wait=0, telemetry_timeout=0, label="test")

    def run_verify(self, logs=None, **changes):
        vars(self.options).update(changes)
        self.client.log_query = logs or FakeLogs()
        ticks = iter(range(1, 10000))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify_gateway.verify(self.client, self.options, sleep=lambda seconds: None,
                                         clock=lambda: next(ticks))
        return code, output.getvalue()

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def evidence_json(self, name):
        return json.loads((self.evidence / f"{name}.json").read_text())


class ScrubTests(ClientCase):
    def test_scrub_removes_hosts_paths_and_prefix(self):
        value = {"gateway": f"https://{NAME}-apim.azure-api.net/nvfp4/v1", "web": f"https://{NAME}.azurewebsites.net",
                 "foundry": f"https://{NAME}-foundry.services.ai.azure.com/api/projects/axk2-demo",
                 "other": "https://someone-else.azure-api.net", f"{NAME}-key": "Prefix: DemoPrefix1",
                 "paths": ["/home/alice/.keys/file", "/mnt/c/Users/bob/repo"], "number": 3}
        text = json.dumps(byom.scrub(value, NAME))
        for leaked in (NAME, "someone-else", "alice", "bob"):
            self.assertNotIn(leaked, text.lower())
        for kept in ("<apim-gateway>/nvfp4/v1", "<web-app>", "<foundry-endpoint>/api/projects/axk2-demo",
                     "<deployment>-key", "/home/<user>/", "/Users/<user>/", '"number": 3'):
            self.assertIn(kept, text)

    def test_save_evidence_scrubs_before_writing(self):
        self.client.save_evidence("probe-test", {"gateway": self.client.gateway}, (KEY,))
        text = (self.evidence / "probe-test.json").read_text()
        self.assertIn("https://<apim-gateway>", text)
        self.assertNotIn(NAME, text)

    def test_save_evidence_refuses_secrets(self):
        jwt = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 5
        for value in ({"leak": KEY}, {"leak": SUB}, {"leak": jwt}):
            with self.assertRaises(ValueError):
                self.client.save_evidence("leaky", value, (KEY, SUB, TENANT))
        self.assertFalse((self.evidence / "leaky.json").exists())


class ProtocolTests(ClientCase):
    def test_command_passes_the_key_by_file_name_only(self):
        argv = verify_gateway.command("python", Path("tool.py"), "https://gw/nvfp4/v1", "ax-k2-nvfp4",
                                      ("models", "auth"), self.private / byom.SUBSCRIPTION_KEY_FILE, Path("out.json"))
        self.assertNotIn(KEY, " ".join(argv))
        for flag, value in (("--auth-header", "api-key"), ("--max-tokens", "64"), ("--only", "models,auth"),
                            ("--base-url", "https://gw/nvfp4/v1"), ("--out", "out.json"),
                            ("--key-file", str(self.private / byom.SUBSCRIPTION_KEY_FILE))):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        # The real tool sends one request at a time and never retries; it has no option for either.
        self.assertFalse({"--concurrency", "--retries"}.intersection(argv))

    def test_plan_allows_four_model_calls_and_no_more(self):
        runs = verify_gateway.build_runs()
        self.assertEqual([run["admissions"] for run in runs], [4, 0])
        too_many = (("nvfp4", "m", ("chat", "stream", "tool", "tool_result")), ("fp8", "n", ("chat",)))
        with mock.patch.object(verify_gateway, "RUNS", too_many):
            with self.assertRaises(ValueError):
                verify_gateway.build_runs()

    def test_request_ids_come_from_any_depth_and_must_be_well_formed(self):
        value = {"checks": {
            "chat": {"ok": True, "x_request_id": "ok-1", "sent_request_id": "ok-1", "ttft_s": 0.4},
            "auth": {"no_key": {"x_request_id": None, "sent_request_id": "sent-2"},
                     "wrong_key": {"sent_request_id": "has space"}},
            "other": [{"request_id": "x" * 200}, {"request_id": 12}],
            "headers": {"x-apim-request-id": "apim-5"}}}
        found = verify_gateway.observations(value, "x-request-id: text-3\nX-APIM-Request-Id=text-4")
        self.assertEqual({(item["kind"], item["id"]) for item in found},
                         {("request", "ok-1"), ("request", "sent-2"), ("apim", "apim-5"), ("request", "text-3"),
                          ("apim", "text-4")})
        chat = next(item for item in found if item["id"] == "ok-1")["client"]
        self.assertEqual((chat["source"], chat["ttft_s"]), ("checks/chat", 0.4))
        self.assertEqual(next(item for item in found if item["id"] == "sent-2")["client"]["source"],
                         "checks/auth/no_key")
        self.assertEqual(verify_gateway.observations(None, ""), [])

    def test_query_lists_reject_anything_but_identifiers(self):
        self.assertEqual(verify_gateway.kql_list(["b", "a", "a"]), 'dynamic(["a", "b"])')
        with self.assertRaises(ValueError):
            verify_gateway.kql_list(['a"]) | take 1 //'])

    def test_correlate_ties_trace_gateway_row_and_backend_call(self):
        found = [{"kind": "request", "id": "r1", "client": {"name": "chat"}},
                 {"kind": "apim", "id": "a2", "client": {}}, {"kind": "request", "id": "r3", "client": {}}]
        traces = [{"OperationId": "op1", "Message": json.dumps({"request_id": "r1", "stream": False})},
                  {"OperationId": "op9", "Message": "not json"}]
        requests = [{"OperationId": "op1", "Name": "n1", "ResultCode": "200", "DurationMs": 10.04,
                     "Subscription": "s", "ApimRequestId": "a1"},
                    {"OperationId": "op2", "Name": "n2", "ResultCode": "401", "DurationMs": 5,
                     "Subscription": "", "ApimRequestId": "a2"}]
        dependencies = [{"OperationId": "op1", "Name": "b", "ResultCode": "200", "DurationMs": 8.0}]
        first, second, third = verify_gateway.correlate(found, traces, requests, dependencies)
        self.assertEqual((first["matchedBy"], first["correlated"], first["trace"]["stream"]), ("trace", True, False))
        self.assertEqual(first["backend"], {"name": "b", "resultCode": "200", "durationMs": 8.0})
        self.assertEqual((second["matchedBy"], second["backend"], second["trace"]), ("apim-request-id", None, None))
        self.assertEqual((third["correlated"], third["apim"]), (False, None))


class VerifyTests(ClientCase):
    def test_dry_run_checks_the_tool_but_sends_nothing(self):
        self.client.log_query = mock.Mock(side_effect=AssertionError("no telemetry in a dry run"))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = verify_gateway.verify(self.client, argparse.Namespace(**{**vars(self.options), "go": False}))
        self.assertEqual(code, 0)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.evidence.exists())
        self.assertIn("Dry run", output.getvalue())
        self.assertNotIn(NAME, output.getvalue())

    def test_go_runs_both_apis_and_writes_redacted_evidence(self):
        code, output = self.run_verify()
        self.assertEqual(code, 0, output)
        nvfp4, fp8 = self.calls()
        self.assertEqual(nvfp4[nvfp4.index("--only") + 1], "models,chat,stream,tool,tool_result,auth")
        self.assertEqual(nvfp4[nvfp4.index("--base-url") + 1], f"https://{NAME}-apim.azure-api.net/nvfp4/v1")
        self.assertEqual(nvfp4[nvfp4.index("--model") + 1], "ax-k2-nvfp4")
        self.assertEqual(fp8[fp8.index("--only") + 1], "models,auth")
        self.assertEqual(fp8[fp8.index("--base-url") + 1], f"https://{NAME}-apim.azure-api.net/fp8/v1")
        for argv in (nvfp4, fp8):
            self.assertEqual(argv[argv.index("--auth-header") + 1], "api-key")
            self.assertEqual(argv[argv.index("--key-file") + 1], str(self.private / byom.SUBSCRIPTION_KEY_FILE))
            self.assertEqual(argv[argv.index("--max-tokens") + 1], "64")
            self.assertTrue(argv[argv.index("--out") + 1].endswith(".json"))
        self.assertNotIn(KEY, json.dumps(self.calls()))
        self.assertNotIn(BACKEND_KEY, json.dumps(self.calls()))
        self.assertEqual(sorted(path.name for path in self.evidence.iterdir()),
                         ["apim-smoke-correlation-test.json", "apim-smoke-fp8-test.json",
                          "apim-smoke-nvfp4-test.json"])
        for text in [path.read_text() for path in self.evidence.iterdir()] + [output]:
            for leaked in (NAME, KEY, BACKEND_KEY, SUB, TENANT):
                self.assertNotIn(leaked, text.lower() if leaked == NAME else text)
        report = self.evidence_json("apim-smoke-correlation-test")
        self.assertEqual(report["verdict"], {
            "smokeExitCodesZero": True, "admissionsReported": 4, "admissionsWithinCap": True, "tracesExpected": 4,
            "tracesFound": 4, "traceRows": 4, "traceRowsWithinCap": True, "backendCalls": 4,
            "tracedRequestsCorrelated": True, "backendAcceptedTracedCalls": True,
            "clientCredentialsNotForwarded": True, "gatewayAuthorizationOnEveryCall": True,
            "logsFreeOfKeysAndPromptText": True, "passed": True})
        self.assertEqual(report["gateway"], "https://<apim-gateway>")
        self.assertEqual(report["forwardedHeaders"]["present"], {
            "api_key_header": 0, "subscription_key_header": 0, "api_key_query": 0, "subscription_key_query": 0,
            "authorization_header": 4})
        self.assertEqual(report["logScan"]["keysChecked"], ["backendKey", "subscriptionKey"])
        traced = [entry for entry in report["correlation"]["entries"] if entry["trace"]]
        self.assertEqual(len(traced), 4)
        self.assertEqual({entry["matchedBy"] for entry in traced}, {"trace"})
        self.assertTrue(all(entry["forwarded"]["authorization_header"] for entry in traced))
        self.assertEqual({entry["client"]["source"] for entry in traced},
                         {f"checks/{check}" for check in ("chat", "stream", "tool", "tool_result")})
        self.assertEqual(next(entry for entry in traced if entry["client"]["source"] == "checks/stream")
                         ["client"]["ttft_s"], 0.5)
        self.assertIn("ttft_s=0.5 chunks=7", output)
        self.assertEqual(report["correlation"]["rejectedAtGateway"][0]["Count"], 2)
        smoke = self.evidence_json("apim-smoke-nvfp4-test")
        self.assertEqual((smoke["phase"], smoke["exitCode"], smoke["result"]["admissions"]), ("all", 0, 4))
        self.assertEqual(smoke["result"]["target"], "https://<apim-gateway>/nvfp4/v1")
        self.assertEqual(smoke["smokeToolSha256"], hashlib.sha256(self.tool.read_bytes()).hexdigest())

    def test_queries_name_the_ids_the_smoke_tool_reported(self):
        logs = FakeLogs()
        self.run_verify(logs)
        lookups = [query for query in logs.queries if "shape.request_id" in query]
        self.assertEqual(len(lookups), 1)
        for request_id in ADMITTED + ["ax-k2-nvfp4-auth-no_key", "ax-k2-fp8-auth-wrong_key"]:
            self.assertIn(f'"{request_id}"', lookups[0])
        self.assertIn("datetime(20", lookups[0])

    def test_incomplete_telemetry_fails_but_keeps_the_evidence(self):
        code, _ = self.run_verify(FakeLogs(ADMITTED[:3]))
        self.assertEqual(code, 1)
        report = self.evidence_json("apim-smoke-correlation-test")
        self.assertEqual((report["verdict"]["tracesFound"], report["verdict"]["passed"]), (3, False))
        self.assertTrue((self.evidence / "apim-smoke-nvfp4-test.json").exists())

    def test_more_trace_rows_than_the_call_cap_fail(self):
        code, output = self.run_verify(FakeLogs(ADMITTED + [ADMITTED[0]]))
        self.assertEqual(code, 1, output)
        verdict = self.evidence_json("apim-smoke-correlation-test")["verdict"]
        self.assertEqual((verdict["traceRows"], verdict["traceRowsWithinCap"], verdict["passed"]), (5, False, False))

    def test_more_admissions_than_the_cap_or_none_reported_fail(self):
        # The override applies to both runs, so "3" reports 6 in total.
        for override, total in (("3", 6), ("omit", None)):
            with self.subTest(override=override), mock.patch.dict(os.environ, {"FAKE_ADMISSIONS": override}):
                code, _ = self.run_verify()
                verdict = self.evidence_json("apim-smoke-correlation-test")["verdict"]
                self.assertEqual((code, verdict["admissionsReported"], verdict["admissionsWithinCap"],
                                  verdict["passed"]), (1, total, False, False))

    def test_polling_waits_for_ingestion_until_every_trace_is_visible(self):
        sleeps = []

        class Late(FakeLogs):
            def __call__(self, text):
                if text.startswith("AppTraces"):
                    self.traced = ADMITTED[:2] if not any(q.startswith("AppTraces") for q in self.queries) else ADMITTED
                return super().__call__(text)

        self.client.log_query = Late()
        ticks = iter(range(1, 10000))
        self.options.ingest_wait, self.options.telemetry_timeout = 90, 1000
        with contextlib.redirect_stdout(io.StringIO()):
            code = verify_gateway.verify(self.client, self.options, sleep=sleeps.append, clock=lambda: next(ticks))
        self.assertEqual((code, sleeps), (0, [90, verify_gateway.POLL_SECONDS]))

    def test_smoke_failure_is_a_failure_even_when_telemetry_is_complete(self):
        with mock.patch.dict(os.environ, {"FAKE_EXIT": "3"}):
            code, output = self.run_verify()
        self.assertEqual(code, 1)
        # The first run failed, so the second was never started.
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("not started, an earlier run failed", output)
        report = self.evidence_json("apim-smoke-correlation-test")
        self.assertEqual([run["exitCode"] for run in report["runs"]], [3])
        self.assertFalse(report["verdict"]["smokeExitCodesZero"])

    def test_missing_flag_stops_before_any_request(self):
        flags = " ".join(flag for flag in verify_gateway.FLAGS.values() if flag != "--out")
        with mock.patch.dict(os.environ, {"FAKE_FLAGS": flags}):
            with self.assertRaisesRegex(RuntimeError, "--out"):
                self.run_verify()
        self.assertFalse(self.log.exists())
        self.assertFalse(self.evidence.exists())

    def test_tool_without_the_api_key_header_choice_stops_before_any_request(self):
        with mock.patch.dict(os.environ, {"FAKE_CHOICES": "{bearer}"}):
            with self.assertRaisesRegex(RuntimeError, "api-key"):
                self.run_verify()
        self.assertFalse(self.log.exists())

    def test_missing_smoke_tool_or_key_file_stops_before_any_request(self):
        with self.assertRaises(FileNotFoundError):
            self.run_verify(smoke_tool=self.tmp / "absent.py")
        (self.private / byom.SUBSCRIPTION_KEY_FILE).unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_verify(smoke_tool=self.tool)
        self.assertFalse(self.log.exists())

    def test_timeout_is_reported_not_raised(self):
        run = dict(verify_gateway.build_runs()[0])
        with mock.patch.object(verify_gateway.subprocess, "run", side_effect=subprocess.TimeoutExpired("smoke", 1)):
            result = verify_gateway.execute(self.client, self.options, run, self.private / "key", lambda: 0)
        self.assertIsNone(result["exitCode"])
        self.assertIn("timed out", result["stderr"])

    def test_smoke_phase_sends_requests_and_keeps_raw_results_without_reading_logs(self):
        logs = mock.Mock(side_effect=AssertionError("no log queries"))
        code, output = self.run_verify(logs, phase="smoke")
        self.assertEqual((code, len(self.calls())), (0, 2))
        self.assertEqual(sorted(path.name for path in self.private.glob("apim-*-smoke.json")),
                         ["apim-fp8-smoke.json", "apim-nvfp4-smoke.json"])
        self.assertEqual(sorted(path.name for path in self.evidence.iterdir()),
                         ["apim-smoke-fp8-test.json", "apim-smoke-nvfp4-test.json"])
        self.assertIn("apim-smoke-<api>-test.json", output)

    def test_correlate_phase_reads_the_raw_results_and_sends_nothing(self):
        self.run_verify(mock.Mock(side_effect=AssertionError("no log queries")), phase="smoke")
        sent = self.calls()
        code, output = self.run_verify(phase="correlate", go=False)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.calls(), sent)
        report = self.evidence_json("apim-smoke-correlation-test")
        self.assertEqual((report["phase"], report["verdict"]["passed"], report["since"]),
                         ("correlate", True, "2025-12-31T23:59:00Z"))
        self.assertEqual([run["exitCode"] for run in report["runs"]], [0, 0])

    def test_correlate_phase_without_raw_results_stops(self):
        with self.assertRaises(FileNotFoundError):
            self.run_verify(phase="correlate", go=False)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.evidence.exists())

    def test_the_private_copy_of_the_smoke_tool_is_used_unless_one_is_named(self):
        self.assertEqual(verify_gateway.resolve_tool(self.client, self.options), self.tool)
        self.assertEqual(verify_gateway.resolve_tool(self.client, argparse.Namespace(smoke_tool=None)),
                         verify_gateway.TOOL)
        (self.private / "tools").mkdir()
        private = self.private / "tools" / verify_gateway.TOOL.name
        private.write_text("# copy")
        self.assertEqual(verify_gateway.resolve_tool(self.client, argparse.Namespace(smoke_tool=None)), private)

    def test_a_client_credential_still_on_the_forwarded_request_fails(self):
        for flag in verify_gateway.CLIENT_CREDENTIAL_FLAGS:
            with self.subTest(flag=flag):
                rows = [forwarded_row(item, **{flag: True}) for item in ADMITTED]
                code, _ = self.run_verify(FakeLogs(forwarded=rows))
                verdict = self.evidence_json("apim-smoke-correlation-test")["verdict"]
                self.assertEqual((code, verdict["clientCredentialsNotForwarded"], verdict["passed"]),
                                 (1, False, False))

    def test_without_forwarded_header_traces_nothing_is_proven(self):
        code, _ = self.run_verify(FakeLogs(forwarded=[]))
        verdict = self.evidence_json("apim-smoke-correlation-test")["verdict"]
        self.assertEqual((code, verdict["clientCredentialsNotForwarded"], verdict["gatewayAuthorizationOnEveryCall"],
                          verdict["passed"]), (1, False, False, False))

    def test_a_request_without_the_gateways_authorization_fails(self):
        rows = [forwarded_row(item, authorization_header=item != ADMITTED[0]) for item in ADMITTED]
        code, _ = self.run_verify(FakeLogs(forwarded=rows))
        verdict = self.evidence_json("apim-smoke-correlation-test")["verdict"]
        self.assertEqual((code, verdict["gatewayAuthorizationOnEveryCall"], verdict["passed"]), (1, False, False))

    def test_a_backend_that_rejects_a_traced_call_fails(self):
        code, _ = self.run_verify(FakeLogs(backend_code="401"))
        verdict = self.evidence_json("apim-smoke-correlation-test")["verdict"]
        self.assertEqual((code, verdict["backendAcceptedTracedCalls"], verdict["passed"]), (1, False, False))

    def test_a_key_in_any_log_row_fails_and_is_named_never_shown(self):
        for name, value in (("subscriptionKey", KEY), ("backendKey", BACKEND_KEY)):
            with self.subTest(key=name):
                code, output = self.run_verify(FakeLogs(scan=[{"TimeGenerated": "t", "Message": "debug " + value}]))
                evidence = self.evidence_json("apim-smoke-correlation-test")
                self.assertEqual((code, evidence["logScan"]["keysFound"], evidence["verdict"]["passed"]),
                                 (1, [name], False))
                self.assertNotIn(value, output)

    def test_prompt_text_in_the_logs_fails_and_names_its_column(self):
        rows = [{"Message": "What is the weather in Seoul right now?", "ClientCity": "Seoul"},
                {"Message": "no request content", "ClientCity": "Seoul"}]
        code, _ = self.run_verify(FakeLogs(scan=rows))
        scan = self.evidence_json("apim-smoke-correlation-test")["logScan"]
        self.assertEqual(code, 1)
        self.assertEqual(scan["promptTextHits"], {
            "weather in Seoul": {"occurrences": 1, "columns": {"Message": 1}},
            "right now": {"occurrences": 1, "columns": {"Message": 1}}})

    def test_a_place_name_in_the_platform_geolocation_columns_is_not_prompt_text(self):
        rows = [{"ClientCity": "Seoul", "ClientStateOrProvince": "Seoul"}] * 3
        code, output = self.run_verify(FakeLogs(scan=rows))
        self.assertEqual(code, 0, output)
        self.assertEqual(self.evidence_json("apim-smoke-correlation-test")["logScan"], {
            "rowsScanned": 3, "truncated": False, "keysChecked": ["backendKey", "subscriptionKey"],
            "keysFound": [], "promptTextHits": {}})


class ForwardedHeaderTests(unittest.TestCase):
    def test_summary_counts_flags_and_methods_and_holds_no_values(self):
        rows = [forwarded_row("a"), forwarded_row("b", method="GET", api_key_header=True)]
        summary = verify_gateway.forwarded_summary(rows)
        self.assertEqual((summary["rows"], summary["methods"], summary["requestIds"]),
                         (2, {"GET": 1, "POST": 1}, ["a", "b"]))
        self.assertEqual(summary["present"], {"api_key_header": 1, "subscription_key_header": 0,
                                              "api_key_query": 0, "subscription_key_query": 0,
                                              "authorization_header": 2})

    def test_credentials_count_as_dropped_only_with_a_trace_and_no_credential_flag(self):
        empty = verify_gateway.forwarded_summary([])
        clean = verify_gateway.forwarded_summary([forwarded_row("a"), forwarded_row("b", method="GET")])
        leaky = verify_gateway.forwarded_summary([forwarded_row("a"), forwarded_row("b", subscription_key_query=True)])
        self.assertEqual([verify_gateway.credentials_dropped(item) for item in (empty, clean, leaky)],
                         [False, True, False])

    def test_authorization_counts_as_injected_only_when_every_request_had_it(self):
        empty = verify_gateway.forwarded_summary([])
        every = verify_gateway.forwarded_summary([forwarded_row("a"), forwarded_row("b")])
        partial = verify_gateway.forwarded_summary([forwarded_row("a"), forwarded_row("b", authorization_header=False)])
        self.assertEqual([verify_gateway.authorization_injected(item) for item in (empty, every, partial)],
                         [False, True, False])

    def test_parse_trace_ignores_messages_that_are_not_trace_records(self):
        for message in ("not json", "request-shape-unavailable", json.dumps([1]), json.dumps({"no": "id"})):
            self.assertIsNone(verify_gateway.parse_trace({"Message": message}))
        self.assertIsNone(verify_gateway.parse_trace({}))
        self.assertEqual(verify_gateway.parse_trace({"Message": json.dumps({"request_id": "r"})}), {"request_id": "r"})

    def test_correlate_keeps_the_header_trace_apart_from_the_request_shape(self):
        found = [{"kind": "request", "id": "r1", "client": {}}, {"kind": "request", "id": "g1", "client": {}}]
        traces = [trace_row("r1"), forwarded_trace_row("r1"), forwarded_trace_row("g1", method="GET")]
        first, second = verify_gateway.correlate(
            found, traces, [request_row("r1"), request_row("g1")], [dependency_row("r1")])
        self.assertEqual((first["trace"]["request_id"], first["forwarded"]["method"]), ("r1", "POST"))
        # A GET has the header trace but no request-shape trace.
        self.assertIsNone(second["trace"])
        self.assertEqual((second["forwarded"]["method"], second["forwarded"]["authorization_header"],
                          second["matchedBy"], second["correlated"]), ("GET", True, "trace", True))

    def test_the_header_query_selects_flags_only(self):
        query = verify_gateway.forwarded_query("2026-01-01T00:00:00Z")
        self.assertIn("kind) == 'forwarded-headers'", query)
        for flag in (*verify_gateway.CLIENT_CREDENTIAL_FLAGS, verify_gateway.INJECTED_FLAG):
            self.assertIn(f"{flag} = tobool(f.{flag})", query)
        self.assertNotIn("Message", query.split("| project", 1)[1])


class DeploymentTests(ClientCase):
    def test_state_directory_must_be_outside_the_checkout(self):
        target = byom.ROOT / "state-that-must-not-exist"
        with self.assertRaises(ValueError):
            byom.open_deployment(self.core, target)
        self.assertFalse(target.exists())

    @unittest.skipIf(os.name == "nt", "needs POSIX permission bits")
    def test_private_state_directory_is_created_next_to_the_core_directory(self):
        opened = byom.open_deployment(self.core)
        self.assertEqual(opened.directory, self.core.with_name("core-byom").resolve())
        self.assertEqual(opened.directory.stat().st_mode & 0o777, 0o700)

    def test_core_directory_has_no_default(self):
        saved = os.umask(0o022)
        self.addCleanup(os.umask, saved)
        environment = {key: value for key, value in os.environ.items() if key != "AXK2_CORE_DIRECTORY"}
        for module, argv in ((byom, ["byom.py", "status"]), (verify_gateway, ["verify_gateway.py"])):
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(sys, "argv", argv):
                with contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as raised:
                    module.main()
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("AXK2_CORE_DIRECTORY", errors.getvalue())

    def test_every_stage_has_a_template(self):
        for stage in byom.STAGES:
            self.assertTrue((HERE / f"{stage}.bicep").is_file(), stage)


class FakeResponse:
    status = 200

    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class TracingTests(ClientCase):
    def listing(self, *names):
        return {"value": [{"name": name, "properties": {"category": "AppInsights"}} for name in names]
                + [{"name": "axk2-nvfp4", "properties": {"category": "ApiManagement"}}]}

    def fake_arm(self, *names):
        self.client.arm = lambda *args, **kwargs: (200, self.listing(*names))

    def fake_az(self, changes):
        calls = []

        def az(*args, output="json", timeout=900):
            calls.append(args)
            if args[:2] == ("group", "exists"):
                return "true"
            if args[:3] == ("deployment", "group", "what-if"):
                return {"changes": changes}
            raise AssertionError("unexpected az call: " + " ".join(args[:3]))

        self.client.az = az
        return calls

    def change(self, kind):
        return {"changeType": kind, "resourceId": f"/subscriptions/{SUB}/resourceGroups/rg/providers/"
                f"Microsoft.CognitiveServices/accounts/{NAME}-foundry/connections/{NAME}-foundry-appinsights"}

    def check(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.client.check_tracing_free()

    def test_parameters_are_resource_names_only(self):
        self.assertEqual(self.client.params("tracing"),
                         {"accountName": f"{NAME}-foundry", "projectName": "axk2-demo", "aiName": f"{NAME}-byom-ai"})

    def test_template_creates_two_connections_and_exposes_no_secret(self):
        text = (HERE / "tracing.bicep").read_text()
        self.assertEqual(text.count("category: 'AppInsights'"), 2)
        self.assertIn("Microsoft.CognitiveServices/accounts/connections@", text)
        self.assertIn("Microsoft.CognitiveServices/accounts/projects/connections@", text)
        self.assertIn("Microsoft.Insights/components@2020-02-02' existing", text)
        self.assertNotIn("roleAssignments", text)
        self.assertNotIn("@secure", text)
        self.assertEqual([line for line in text.splitlines() if line.startswith("output ")],
                         ["output connectionNames array = ["])

    def test_a_project_without_app_insights_connections_is_free(self):
        self.fake_arm()
        self.check()

    def test_a_second_run_accepts_its_own_two_connections_on_either_list(self):
        self.fake_arm(f"{NAME}-foundry-appinsights", f"{NAME}-byom-ai")
        self.check()

    def test_another_app_insights_connection_is_never_replaced(self):
        self.fake_arm(f"{NAME}-foundry-appinsights", "someone-elses-appinsights")
        with self.assertRaises(ValueError) as raised:
            self.check()
        self.assertIn("only one", str(raised.exception))

    def test_a_listing_error_stops_the_check(self):
        self.client.arm = lambda *args, **kwargs: (403, {"error": "AuthorizationFailed"})
        with self.assertRaises(RuntimeError):
            self.check()

    def test_plan_only_writes_names_and_creates_nothing(self):
        self.fake_arm()
        calls = self.fake_az([self.change("Create")])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.client.deploy("tracing", True)
        written = json.loads((self.private / "params-tracing.json").read_text())
        self.assertEqual(set(written["parameters"]), {"accountName", "projectName", "aiName"})
        self.assertIn("what-if Create: 1", output.getvalue())
        self.assertFalse([call for call in calls if call[:3] == ("deployment", "group", "create")])

    def test_a_deletion_in_the_plan_stops_before_any_deployment(self):
        self.fake_arm()
        calls = self.fake_az([self.change("Delete")])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            self.client.deploy("tracing", False)
        self.assertFalse([call for call in calls if call[:3] == ("deployment", "group", "create")])

    def test_connection_evidence_lists_both_scopes_and_hides_secrets(self):
        resource = (f"/subscriptions/{SUB}/resourceGroups/rg-{NAME}-byom/providers/Microsoft.Insights/components/"
                    f"{NAME}-byom-ai")
        listing = {"value": [{"name": f"{NAME}-byom-ai", "properties": {
            "category": "AppInsights", "authType": "ApiKey", "target": resource,
            "credentials": {"key": "SECRET-CONNECTION-STRING"}, "metadata": {"ResourceId": resource}}}]}
        created = [f"{resource.split('/providers/')[0]}/providers/Microsoft.CognitiveServices/accounts/"
                   f"{NAME}-foundry/connections/{NAME}-foundry-appinsights"]
        self.client.arm = lambda *args, **kwargs: (200, listing)
        self.client.state = {"deployments": {"tracing": {"name": "byom-tracing-1"}}}

        def az(*args, output="json", timeout=900):
            if args[:2] == ("account", "get-access-token"):
                return {"accessToken": "opaque"}
            if args[:3] == ("deployment", "group", "show"):
                return created
            raise AssertionError("unexpected az call: " + " ".join(args[:3]))

        self.client.az = az
        with mock.patch.object(byom.urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse(listing)):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.client.connections("test")
        text = (self.evidence / "connections-test.json").read_text()
        for leaked in (SUB, "SECRET-CONNECTION-STRING", NAME):
            self.assertNotIn(leaked, text)
        evidence = json.loads(text)
        self.assertEqual(evidence["armAccount"][0]["category"], "AppInsights")
        self.assertEqual(evidence["createdByDeployment"]["tracing"], [
            "Microsoft.CognitiveServices/accounts/<deployment>-foundry/connections/<deployment>-foundry-appinsights"])
        self.assertIn("[account]", output.getvalue())
        self.assertIn("[project]", output.getvalue())


# Arrival times, gateway and backend durations (ms) and the caller-side span start of a failed Foundry call with its
# three retries, as the logs reported them.
FOUNDRY_CALLS = (("2026-10-19T11:45:12.1307678Z", 77.7, 72.0, "2026-10-19T11:45:12.0841519Z"),
                 ("2026-10-19T11:45:12.632663Z", 35.9, 34.6, "2026-10-19T11:45:12.6100348Z"),
                 ("2026-10-19T11:45:18.1192271Z", 14.4, 13.2, "2026-10-19T11:45:18.0631354Z"),
                 ("2026-10-19T11:45:23.5991794Z", 7.3, 5.9, "2026-10-19T11:45:23.5250538Z"))
BACKEND_NAME = "POST /models/nvfp4/v1/chat/completions"


def shape_message(request_id, **changes):
    record = {"request_id": request_id, "path": "/models/nvfp4/v1/chat/completions", "user_agent": "",
              "keys": "messages,stream,max_completion_tokens,stream_options,model,temperature,top_p",
              "model": "ax-k2-nvfp4", "stream": "True", "has_max_tokens": False, "has_max_completion_tokens": True,
              "message_count": 2, "roles": "system,user", "tool_names": "", "tool_choice": "absent"}
    return json.dumps(record | changes)


def policy_traces(operation, request_id, shape=None, **flags):
    """The two trace rows the policy writes for one request."""
    forwarded = {"kind": verify_gateway.FORWARDED_KIND, "request_id": request_id, "method": "POST",
                 "api_key_header": False, "subscription_key_header": False, "api_key_query": False,
                 "subscription_key_query": False, "authorization_header": True} | flags
    return [{"TimeGenerated": "2026-10-19T11:45:12Z", "OperationId": operation,
             "Message": json.dumps(forwarded)},
            {"TimeGenerated": "2026-10-19T11:45:12Z", "OperationId": operation,
             "Message": shape_message(request_id) if shape is None else shape}]


def gateway_request(arrived, total, operation, code="503", url="https://gw/nvfp4/v1/chat/completions",
                    subscription="foundry-byom"):
    return {"TimeGenerated": arrived, "OperationId": operation, "Name": "POST /nvfp4/v1/chat/completions",
            "ResultCode": code, "DurationMs": total, "Success": code == "200", "Url": url, "Subscription": subscription}


def backend_call(arrived, millis, operation, code="503"):
    return {"TimeGenerated": arrived, "OperationId": operation, "Name": BACKEND_NAME, "ResultCode": code,
            "DurationMs": millis, "Success": code == "200"}


def foundry_tables():
    requests, dependencies, traces = [], [], []
    for index, (arrived, total, backend, span_start) in enumerate(FOUNDRY_CALLS):
        operation = f"gateway-op-{index}"
        requests.append(gateway_request(arrived, total, operation))
        dependencies += [backend_call(arrived, backend, operation),
                         {"TimeGenerated": span_start, "OperationId": f"agent-op-{index}", "ResultCode": "500",
                          "Name": "invoke_agent some-agent:1", "DurationMs": 150.0, "Success": False}]
        traces += policy_traces(operation, f"00000000-0000-4000-8000-00000000000{index}")
    return {"requests": requests, "dependencies": dependencies, "traces": traces}


class FakeTables:
    """Canned Log Analytics answers keyed by the table a query reads."""

    def __init__(self, requests=(), dependencies=(), traces=()):
        self.queries = []
        self.tables = {"AppRequests": list(requests), "AppDependencies": list(dependencies),
                       "AppTraces": list(traces)}

    def __call__(self, text):
        self.queries.append(text)
        return [dict(row) for row in self.tables[text.split(" ", 1)[0]]]


class ObserveTests(ClientCase):
    NOW = datetime(2026, 10, 19, 12, 30, tzinfo=timezone.utc)

    def observe(self, tables=None, **changes):
        options = argparse.Namespace(since="2026-10-19T20:44:30+09:00", until="2026-10-19T20:46:30+09:00",
                                     include_smoke=False, label=None)
        vars(options).update(changes)
        self.client.log_query = FakeTables(**(foundry_tables() if tables is None else tables))
        self.client.http = mock.Mock(side_effect=AssertionError("the observer must send nothing"))
        self.output = io.StringIO()
        with contextlib.redirect_stdout(self.output):
            code = observe_requests.observe(self.client, options, clock=lambda: self.NOW)
        self.assertEqual(code, 0)
        return self.output.getvalue()

    def saved(self, label="test"):
        return self.evidence_json(f"gateway-requests-{label}")

    def test_failed_foundry_call_with_retries_is_reported_with_its_origin(self):
        text = self.observe(label="test")
        report = self.saved()
        summary = report["summary"]
        self.assertEqual(summary["requests"], 4)
        self.assertEqual(summary["status"], {"503": 4})
        self.assertEqual(summary["answeredBy"], {"backend": 4})
        self.assertEqual(summary["callers"], {"other": 4})
        self.assertEqual(summary["gapsSecondsBetweenCallerRequests"], [0.5, 5.49, 5.48])
        self.assertEqual(summary["gatewayOverheadMs"], {"min": 1.2, "max": 5.7})
        self.assertTrue(summary["identicalShapes"])
        self.assertEqual(summary["clientCredentialsForwarded"], 0)
        self.assertEqual(summary["authorizationInjected"], 4)
        self.assertEqual(summary["callerSideSpans"], {"invoke_agent": {"count": 4, "status": {"500": 4}}})
        first = report["requests"][0]
        self.assertEqual(first["backend"], {"call": BACKEND_NAME, "status": "503", "ms": 72.0})
        self.assertEqual(first["duringSpan"], "invoke_agent")
        self.assertEqual(first["url"], {"path": "/nvfp4/v1/chat/completions", "queryNames": [], "apiVersion": None})
        shape = first["shape"]
        self.assertIs(shape["stream"], True)
        self.assertEqual(shape["tokenLimit"], ["max_completion_tokens"])
        self.assertEqual(shape["fields"][:3], ["messages", "stream", "max_completion_tokens"])
        self.assertEqual((shape["roles"], shape["tools"], shape["userAgent"]), (["system", "user"], [], ""))
        self.assertEqual(text.count("answered by backend"), 4)
        self.assertIn("token limit max_completion_tokens", text)
        self.assertIn("client credentials forwarded none; authorization injected yes", text)
        self.assertEqual(len(self.client.log_query.queries), 3)

    def test_gateway_answers_for_itself_when_no_backend_call_exists(self):
        tables = {"requests": [gateway_request("2026-10-19T11:45:12Z", 3.0, "op-limit", code="429")]}
        text = self.observe(tables, label="test")
        entry = self.saved()["requests"][0]
        self.assertEqual((entry["answeredBy"], entry["backend"], entry["gatewayOverheadMs"]), ("gateway", None, None))
        self.assertIn("no backend call; answered by gateway", text)

    def test_a_status_the_gateway_changed_is_flagged(self):
        tables = {"requests": [gateway_request("2026-10-19T11:45:12Z", 230000.0, "op-timeout", code="504")],
                  "dependencies": [backend_call("2026-10-19T11:45:12Z", 229990.0, "op-timeout", code="502")]}
        self.observe(tables, label="test")
        self.assertEqual(self.saved()["requests"][0]["answeredBy"], "gateway-translated")

    def test_smoke_tool_requests_are_left_out_unless_asked(self):
        smoke_by_id = policy_traces("op-smoke", "smoke-abc123-chat", shape_message("smoke-abc123-chat"))
        smoke_by_agent = policy_traces("op-agent", "plain-id", shape_message("plain-id",
                                                                             user_agent="axk2-model-api-smoke/1"))
        tables = {"requests": [gateway_request("2026-10-19T11:45:10Z", 5.0, "op-smoke", "200"),
                               gateway_request("2026-10-19T11:45:11Z", 5.0, "op-agent", "200"),
                               gateway_request("2026-10-19T11:45:12Z", 5.0, "op-real", "200")],
                  "traces": smoke_by_id + smoke_by_agent + policy_traces("op-real", "real-id")}
        self.observe(tables, label="default")
        report = self.saved("default")
        self.assertEqual([item["operation"] for item in report["requests"]], ["op-real"])
        self.assertEqual(report["summary"]["smokeLeftOut"], 2)
        self.observe(tables, label="all", include_smoke=True)
        report = self.saved("all")
        self.assertEqual([item["caller"] for item in report["requests"]], ["smoke", "smoke", "other"])
        self.assertEqual(report["summary"]["smokeLeftOut"], 0)

    def test_requests_that_never_reached_the_policy_are_labelled(self):
        tables = {"requests": [gateway_request("2026-10-19T11:45:10Z", 2.0, "op-401", "401", subscription=""),
                               gateway_request("2026-10-19T11:45:11Z", 2.0, "op-lag", "200")]}
        self.observe(tables, label="test")
        entries = self.saved()["requests"]
        self.assertEqual([(item["caller"], item["subscription"], item["shape"]) for item in entries],
                         [("unauthenticated", None, None), ("unknown", "foundry-byom", None)])
        self.assertEqual(self.saved()["summary"]["gapsSecondsBetweenCallerRequests"], [])

    def test_a_credential_that_reaches_the_backend_is_called_out(self):
        tables = foundry_tables()
        tables["traces"] = policy_traces("gateway-op-0", "id-0", api_key_header=True)
        text = self.observe(tables, label="test")
        self.assertEqual(self.saved()["requests"][0]["clientCredentialsForwarded"], ["api_key_header"])
        self.assertEqual(self.saved()["summary"]["clientCredentialsForwarded"], 1)
        self.assertIn("client credentials forwarded api_key_header", text)

    def test_unreadable_shape_is_reported(self):
        tables = {"requests": [gateway_request("2026-10-19T11:45:12Z", 5.0, "op-bad", "400")],
                  "traces": policy_traces("op-bad", "id-bad", shape="request-shape-unavailable")}
        text = self.observe(tables, label="test")
        self.assertEqual(self.saved()["requests"][0]["shape"], {"unavailable": True})
        self.assertIn("shape: unavailable", text)

    def test_url_shape_keeps_names_but_no_host_or_values(self):
        shape = observe_requests.url_shape("https://gw/nvfp4/v1/models?subscription-key=SECRET-VALUE&api-version=2024-10-21")
        self.assertEqual(shape, {"path": "/nvfp4/v1/models", "queryNames": ["api-version", "subscription-key"],
                                 "apiVersion": "2024-10-21"})
        self.assertNotIn("SECRET-VALUE", json.dumps(shape))
        self.assertEqual(observe_requests.url_shape("https://gw/x?api-version=a%20b")["apiVersion"], "<other>")
        self.assertEqual(observe_requests.url_shape(None), {"path": "", "queryNames": [], "apiVersion": None})

    def test_times_are_read_as_utc_from_every_supported_form(self):
        clock = lambda: self.NOW
        moment = observe_requests.moment
        self.assertEqual(moment("30m", clock), datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(moment("2h", clock), datetime(2026, 10, 19, 10, 30, tzinfo=timezone.utc))
        expected = datetime(2026, 10, 19, 11, 44, 30, tzinfo=timezone.utc)
        self.assertEqual(moment("2026-10-19T20:44:30+09:00", clock), expected)
        self.assertEqual(moment("2026-10-19T11:44:30Z", clock), expected)
        for bad in ("2026-10-19T11:44:30", "yesterday", "5d", "2026-10-19T00:00:00Z') | take 1 //"):
            with self.assertRaises(ValueError):
                moment(bad, clock)

    def test_window_must_be_ordered_and_at_most_two_days(self):
        for changes in ({"since": "10m", "until": "20m"}, {"since": "49h", "until": "1m"}):
            with self.assertRaises(ValueError):
                self.observe(**changes)

    def test_queries_read_only_the_requested_window(self):
        start = datetime(2026, 10, 19, 11, 44, 30, tzinfo=timezone.utc)
        found = observe_requests.queries(start, datetime(2026, 10, 19, 11, 46, 30, tzinfo=timezone.utc))
        self.assertEqual({name: text.split(" ", 1)[0] for name, text in found.items()},
                         {"requests": "AppRequests", "dependencies": "AppDependencies", "traces": "AppTraces"})
        for text in found.values():
            self.assertIn("TimeGenerated between (datetime(2026-10-19T11:44:30Z) .. datetime(2026-10-19T11:46:30Z))",
                          text)

    def test_evidence_and_output_name_no_deployment(self):
        tables = foundry_tables()
        tables["traces"] = policy_traces("gateway-op-0", "id-0", shape_message("id-0", user_agent=f"agent-{NAME}/1"))
        text = self.observe(tables, label="test")
        saved = (self.evidence / "gateway-requests-test.json").read_text()
        for output in (text, saved):
            self.assertNotIn(NAME, output.lower())
        self.assertIn("agent-<deployment>/1", saved)

    def test_a_key_inside_the_logs_is_masked_on_screen_and_refused_in_evidence(self):
        tables = foundry_tables()
        tables["traces"] = policy_traces("gateway-op-0", "id-0", shape_message("id-0", user_agent=KEY))
        with self.assertRaises(ValueError):
            self.observe(tables, label="test")
        self.assertNotIn(KEY, self.output.getvalue())
        self.assertIn("user agent <KEY>", self.output.getvalue())
        self.assertFalse((self.evidence / "gateway-requests-test.json").exists())

    def test_late_logs_are_announced_when_the_window_ends_now(self):
        self.assertIn("run it again later", self.observe(since="10m", until=None))
        self.assertNotIn("run it again later", self.observe(since="2h", until="1h"))

    def test_command_line_help_lists_the_window_options(self):
        run = subprocess.run([sys.executable, str(HERE / "observe_requests.py"), "--help"], capture_output=True,
                             text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(run.returncode, 0, run.stderr)
        for option in ("--since", "--until", "--include-smoke", "--label"):
            self.assertIn(option, run.stdout)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.text = (HERE / "policies" / "model-api.xml").read_text(encoding="utf-8")
        self.trace = self.text.index('<trace source="axk2-forwarded-headers"')

    def test_client_credentials_are_removed_before_the_header_trace_and_the_backend_call(self):
        removed = ([f'<set-header name="{name}" exists-action="delete" />' for name in ("api-key", "Ocp-Apim-Subscription-Key")]
                   + [f'<set-query-parameter name="{name}" exists-action="delete" />'
                      for name in ("api-key", "subscription-key")])
        for item in removed:
            self.assertIn(item, self.text)
            self.assertLess(self.text.index(item), self.trace)
        self.assertLess(self.trace, self.text.index("<forward-request"))

    def test_the_header_trace_records_presence_flags_only(self):
        message = self.text[self.trace:self.text.index("</trace>", self.trace)]
        self.assertEqual(message.count("ContainsKey("), 5)
        for forbidden in ("GetValueOrDefault", "Body", "Headers[", "Query["):
            self.assertNotIn(forbidden, message)

    def test_the_backend_sees_the_gateway_key_and_the_stream_is_not_buffered(self):
        self.assertIn("{{axk2-backend-key}}", self.text)
        self.assertIn('<forward-request timeout="230" buffer-response="false" />', self.text)

    def test_the_gateway_template_deploys_this_policy_file(self):
        self.assertIn("loadTextContent('policies/model-api.xml')", (HERE / "gateway.bicep").read_text())


class RepositoryTests(unittest.TestCase):
    def test_committed_files_name_no_real_host(self):
        real_host = r"[A-Za-z0-9-]+\.(azure-api\.net|azurewebsites\.net|services\.ai\.azure\.com)"
        import re
        files = [path for folder in (HERE, ROOT / "evidence" / "foundry-byom") if folder.exists()
                 for path in folder.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
        self.assertTrue(files)
        for path in files:
            self.assertIsNone(re.search(real_host, path.read_text(encoding="utf-8")), path.name)


if __name__ == "__main__":
    unittest.main()
