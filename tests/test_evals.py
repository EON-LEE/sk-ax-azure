"""Tests for aml/src/evals.py: answer extraction, summaries, admission control, the request loop against a fake
vLLM server and the demo frontend's link API, and main() end to end. The pinned-data and IFBench verifier tests
read cached files (AXK2_EVALS_DATA, default <tmp>/axk2-evals-check) and skip without them."""
import contextlib
import http.server
import io
import json
import os
import re
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "aml" / "src"))
try:
    import evals
finally:
    sys.path.pop(0)

DATA = Path(os.environ.get("AXK2_EVALS_DATA") or Path(tempfile.gettempdir()) / "axk2-evals-check")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rec(suite, key, rep, correct, meta=None, **extra):
    return dict({"suite": suite, "key": key, "rep": rep, "correct": correct, "meta": meta or {}, "seconds": 1.0,
                 "completion_tokens": 10}, **extra)


def metrics(running=0, waiting=0, kv=0.1, preemptions=0):
    return {"running": running, "waiting": waiting, "kv": kv, "preemptions": preemptions}


class FakeServer:
    """What evals.py calls on vLLM's OpenAI server and on the demo frontend's link API."""

    def __init__(self, behaviors=None, token="tok"):
        self.behaviors, self.token = behaviors or {}, token
        self.lock, self.release = threading.Lock(), threading.Event()
        self.streams, self.link, self.summaries = [], {}, []
        self.metrics = metrics()
        self.healthy, self.reject_priority = True, False
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.httpd.daemon_threads = True
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def prompts(self):
        with self.lock:
            return [body["messages"][-1]["content"] for body in self.streams]

    def chat(self, handler, body):
        if body.get("priority") and self.reject_priority:
            handler.send(400, {"object": "error", "message": "Priority scheduling is not enabled.", "code": 400})
            return
        if not body.get("stream"):
            handler.send(200, {"choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant", "content": "2"}}]})
            return
        prompt = body["messages"][-1]["content"]
        with self.lock:
            self.streams.append(body)
            attempt = sum(1 for b in self.streams if b["messages"][-1]["content"] == prompt)
        behavior = self.behaviors.get(prompt)
        if behavior is None:  # a NIAH prompt: answer with its code
            behavior = {"content": re.search(r"KX-\d+K-\d{4}7", prompt).group(0)}
        if behavior.get("hold"):
            self.release.wait(10)
        if "status" in behavior or (behavior.get("fail_first") and attempt == 1):
            handler.send(behavior.get("status", 500), {"object": "error", "message": "rejected"})
            return
        text = behavior.get("content", "")
        chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}]
        if behavior.get("reasoning"):
            chunks.append({"choices": [{"index": 0, "delta": {"reasoning_content": behavior["reasoning"]}}]})
        if behavior.get("error"):
            chunks.append({"error": {"message": behavior["error"]}})
        chunks += [{"choices": [{"index": 0, "delta": {"content": text[i:i + 3]}}]} for i in range(0, len(text), 3)]
        chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": behavior.get("finish", "stop")}]})
        chunks.append({"choices": [], "usage": {"prompt_tokens": len(prompt), "completion_tokens": 7}})
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        try:
            for chunk in chunks:
                handler.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
            handler.wfile.write(b"data: [DONE]\n\n")
        except ConnectionError:  # the client stops reading at an error chunk
            pass

    def handler(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, code, payload, content_type="application/json"):
                data = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def authorized(self):
                if self.headers.get("Authorization") == "Bearer " + fake.token:
                    return True
                self.send(401, {"detail": "bad token"})
                return False

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                if url.path == "/health":
                    self.send(200 if fake.healthy else 503, {})
                elif url.path == "/metrics":
                    lines = ["# TYPE vllm:num_requests_running gauge"] + [
                        f'{name}{{engine="0",model_name="axk2"}} {float(fake.metrics[key])}'
                        for key, name in evals.METRICS.items()]
                    self.send(200, "\n".join(lines) + "\n", "text/plain")
                elif url.path == "/api/link/evals":
                    if self.authorized():
                        run = urllib.parse.parse_qs(url.query)["run"][0]
                        with fake.lock:
                            records = list(fake.link.get(run, []))
                        self.send(200, {"records": records})
                else:
                    self.send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/tokenize":  # one token per character, plus a chat template
                    count = (sum(len(m["content"]) for m in body["messages"]) + 8 if "messages" in body
                             else len(body["prompt"]))
                    self.send(200, {"count": count, "max_model_len": 262144, "tokens": []})
                elif self.path == "/v1/chat/completions":
                    fake.chat(self, body)
                elif self.path in ("/api/link/evals", "/api/link/evals/summary"):
                    if self.authorized():
                        with fake.lock:
                            if self.path.endswith("/summary"):
                                fake.summaries.append(body)
                            else:
                                fake.link.setdefault(body["run"], []).extend(body["records"])
                        self.send(200, {"ok": True})
                else:
                    self.send(404, {})

        return Handler


class ExtractionTests(unittest.TestCase):
    def test_last_boxed(self):
        self.assertEqual(evals.last_boxed(r"\boxed{1} then \boxed{\frac{1}{2}}"), r"\frac{1}{2}")
        self.assertEqual(evals.last_boxed(r"$\boxed {12}$"), "12")
        self.assertEqual(evals.last_boxed(r"\boxed{7}, not a bare \boxed"), "7")
        self.assertIsNone(evals.last_boxed(r"\boxed{open"))
        self.assertIsNone(evals.last_boxed("no answer"))

    def test_aime_value(self):
        cases = {"277": 277, "077": 77, "m+n=277": 277, r"\text{277}": 277, r"90^\circ": 90, r"90^{\circ}": 90,
                 "1{,}000": 1000, "1,000": 1000, "12.": 12, "3.0": 3, "$45$": 45, r"\frac{1}{2}": None,
                 "3.5": None, "": None, None: None}
        for boxed, expected in cases.items():
            with self.subTest(boxed=boxed):
                self.assertEqual(evals.aime_value(boxed), expected)
        item = {"gold": 277}
        self.assertEqual(evals.score_aime(item, r"so $\boxed{277}$. I hope it is correct"),
                         {"correct": True, "pred": 277})
        self.assertEqual(evals.score_aime(item, "277"), {"correct": False, "pred": None})

    def test_kobalt(self):
        cases = [("따라서 정답은 C입니다.", "C"), ("정답은 [C]입니다", "C"), ("정답은 (C)입니다", "C"),
                 ("정답은 **C**입니다.", "C"), ("정답은 A입니다. 다시 보면 정답은 C입니다.", "C"),
                 ("정답은 K입니다.", None), ("The answer is C.", None)]
        for text, pred in cases:
            with self.subTest(text=text):
                self.assertEqual(evals.score_kobalt({"gold": "C"}, text), {"correct": pred == "C", "pred": pred})

    def test_click_letter(self):
        cases = [("A", 4, "A"), (" (B) ", 4, "B"), ("**C**", 4, "C"), ("D.", 4, "D"), ("정답: A", 4, "A"),
                 ("정답은 B입니다.", 4, "B"), ("The answer is C.", 4, "C"), ("Answer: (D)", 4, "D"),
                 ("정답: A ... 다시 보면 정답: C", 4, "C"), ("정답: E", 4, None), ("정답: E", 5, "E"),
                 ("A 또는 B", 4, None), ("보기 중 B가 맞다", 4, "B"), ("", 4, None)]
        for text, n_choices, letter in cases:
            with self.subTest(text=text, n_choices=n_choices):
                self.assertEqual(evals.click_letter(text, n_choices), letter)
        item = {"gold": "B", "meta": {"n_choices": 4}}
        self.assertEqual(evals.score_click(item, "B"), {"correct": True, "pred": "B"})

    def test_final_answer_and_niah(self):
        self.assertEqual(evals.final_answer("<think>A? no</think>\n\n정답은 B입니다."), "\n\n정답은 B입니다.")
        self.assertEqual(evals.final_answer("plain"), "plain")
        item = {"gold": "KX-8K-00087"}
        self.assertTrue(evals.score_niah(item, " 코드는 KX-8K-00087 입니다")["correct"])
        self.assertFalse(evals.score_niah(item, "KX-8K-0008")["correct"])


class LoaderTests(unittest.TestCase):
    def test_json_records(self):
        self.assertEqual(evals.json_records('[{"a": 1}, {"a": 2}]'), [{"a": 1}, {"a": 2}])
        self.assertEqual(evals.json_records('{"a": 1}\n\n{"a": 2}\n'), [{"a": 1}, {"a": 2}])
        self.assertEqual(evals.json_records('{"a": 1}'), [{"a": 1}])

    def test_click_items(self):
        rows = [{"id": "q", "question": "수도는?", "choices": ["서울", "부산", "대구", "광주"], "answer": "대구"},
                {"id": "q", "paragraph": "지문", "question": "무엇인가?", "choices": list("12345"), "answer": "5"},
                {"id": "r", "question": "보기가 겹친다", "choices": ["가", "가", "나", "다"], "answer": "나"}]
        items = evals.click_items("Culture/Korean History/History_KHB", rows)
        self.assertEqual([i["key"] for i in items], ["History_KHB:0", "History_KHB:1", "History_KHB:2"])
        self.assertEqual([i["gold"] for i in items], ["C", "E", "C"])
        self.assertEqual([(i["meta"]["group"], i["meta"]["category"], i["meta"]["n_choices"],
                           i["meta"]["duplicate_choices"]) for i in items],
                         [("Culture", "History", 4, False), ("Culture", "History", 5, False),
                          ("Culture", "History", 4, True)])
        four, five = (i["messages"][0]["content"] for i in items[:2])
        self.assertEqual(four, "주어진 질문을 천천히 읽고, 적절한 정답을 A, B, C, D 중에 골라 알파벳 하나로 답하시오.\n\n"
                               "질문: 수도는?\n보기:\nA:서울, B: 부산, C: 대구, D: 광주\n정답:")
        self.assertIn("정답을 A, B, C, D, E 중에", five)
        self.assertIn("맥락: 지문\n질문: 무엇인가?\n보기:\nA:1, B: 2, C: 3, D: 4, E: 5\n정답:", five)

    def test_niah_items(self):
        items = evals.niah_items(evals.NIAH_LENGTHS, 262144)
        self.assertEqual(len(items), 66)
        self.assertEqual(len({i["gold"] for i in items}), 66)
        self.assertEqual(len({i["key"] for i in items}), 66)
        self.assertTrue(all(re.fullmatch(r"KX-\d+K-\d{4}7", i["gold"]) for i in items))
        self.assertEqual(max(i["target"] for i in items), 262144 - 48)
        self.assertEqual({i["target"] for i in evals.niah_items([8192], 4096)}, {4048})

    def test_run_configuration(self):
        self.assertEqual(evals.parse_repeats("", ["aime"]), {})
        self.assertEqual(evals.parse_repeats("2", ["aime", "click"]), {"aime": 2, "click": 2})
        self.assertEqual(evals.parse_repeats("aime=4,click=1", ["aime", "click"]), {"aime": 4, "click": 1})
        suites = [evals.Suite("aime", [{"key": "1"}], evals.score_aime, 10, 8)]
        run = evals.default_run(suites, SimpleNamespace(seed=1))
        self.assertRegex(run, r"^ev-[0-9a-f]{10}$")
        self.assertEqual(run, evals.default_run(suites, SimpleNamespace(seed=1)))
        self.assertNotEqual(run, evals.default_run(suites, SimpleNamespace(seed=2)))
        self.assertEqual(evals.plan_of(suites)["aime"]["repeats"], 8)
        self.assertNotIn("keys", evals.plan_of(suites)["aime"])

    def test_limit_takes_a_reproducible_random_sample(self):
        def suite():
            return evals.Suite("click", [{"key": str(k)} for k in range(100)], evals.score_aime, 10, 1)
        first, again, other = suite(), suite(), suite()
        evals.sample(first, 10, 1)
        evals.sample(again, 10, 1)
        evals.sample(other, 10, 2)
        keys = [i["key"] for i in first.items]
        self.assertEqual(len(keys), 10)
        self.assertEqual(keys, [i["key"] for i in again.items])
        self.assertEqual(keys, sorted(keys, key=int))  # original order kept
        self.assertNotEqual(keys, [str(k) for k in range(10)])
        self.assertNotEqual(keys, [i["key"] for i in other.items])
        self.assertEqual(evals.plan_of([first])["click"]["keys"], keys)
        small = evals.Suite("aime", [{"key": "1"}], evals.score_aime, 10, 1)
        evals.sample(small, 10, 1)
        self.assertFalse(small.sampled)

    def test_summary_keeps_only_the_planned_sample_and_repeats(self):
        records = [rec("click", "1", 0, True), rec("click", "2", 0, False), rec("aime", "1", 0, True),
                   rec("aime", "1", 3, False)]
        plan = {"click": {"items": 1, "repeats": 1, "keys": ["1"]}, "aime": {"items": 1, "repeats": 2}}
        summary = evals.summarize(records, plan)
        self.assertEqual((summary["click"]["n_generations"], summary["click"]["score"]), (1, 1.0))
        self.assertEqual((summary["aime"]["n_generations"], summary["aime"]["score"]), (1, 1.0))


class SummaryTests(unittest.TestCase):
    def test_mean_ci(self):
        self.assertEqual(evals.mean_ci([]), (None, None))
        self.assertEqual(evals.mean_ci([1.0]), (1.0, None))
        mean, half = evals.mean_ci([1.0, 0.0, 1.0, 1.0])
        self.assertAlmostEqual(mean, 0.75)
        self.assertAlmostEqual(half, 0.49)

    def test_latest_records_prefer_scored_ones(self):
        scored, error, newer = rec("a", "1", 0, True), rec("a", "1", 0, None, error="HTTP 500"), rec("a", "1", 0, False)
        self.assertEqual(evals.latest_records([scored, error]), [scored])
        self.assertEqual(evals.latest_records([error, scored]), [scored])
        self.assertEqual(evals.latest_records([scored, newer]), [newer])
        self.assertEqual(evals.completed([error]), set())
        self.assertEqual(evals.completed([error, scored]), {"a|1|0"})

    def test_summarize(self):
        records = [rec("aime", "1", 0, True), rec("aime", "1", 1, False, hit_limit=True),
                   rec("aime", "2", 0, True), rec("aime", "2", 1, None, error="HTTP 400"),
                   rec("aime", "3", 0, False, score_error="ValueError: x"),
                   rec("niah", "8192@0.0", 0, True, {"length": 8192, "depth": 0.0}),
                   rec("niah", "8192@0.5", 0, False, {"length": 8192, "depth": 0.5})]
        plan = {"aime": {"items": 3, "repeats": 2}, "niah": {"items": 2, "repeats": 1, "breakdown": ["length"]}}
        out = evals.summarize(records, plan)
        aime = out["aime"]
        self.assertAlmostEqual(aime["score"], 0.5)  # item means 0.5, 1.0, 0.0
        self.assertEqual((aime["n_items"], aime["n_generations"], aime["generations_expected"]), (3, 4, 6))
        self.assertEqual((aime["errors"], aime["missing"], aime["hit_limit"], aime["score_errors"]), (1, 1, 1, 1))
        self.assertEqual(aime["by_rep"], {"0": 2 / 3, "1": 0.0})
        self.assertEqual(out["niah"]["grid"], {"8192": {"0.0": True, "0.5": False}})
        self.assertEqual(out["niah"]["by_length"], {"8192": {"n_items": 2, "score": 0.5}})
        self.assertNotIn("by_rep", out["niah"])

    def test_ifbench_levels(self):
        records = [rec("ifbench", "0", 0, True, strict=[True, False], loose=[True, True]),
                   rec("ifbench", "1", 0, True, strict=[True], loose=[True]),
                   rec("ifbench", "2", 0, None, error="HTTP 500")]
        self.assertEqual(evals.ifbench_levels(records),
                         {"prompt_strict": 0.5, "prompt_loose": 1.0, "instruction_strict": 2 / 3,
                          "instruction_loose": 1.0, "n_instructions": 3})
        self.assertEqual(evals.ifbench_levels(records[2:]), {})

    def test_order_is_repetition_major_and_reproducible(self):
        suite = evals.Suite("aime", [{"key": str(k)} for k in range(10)], evals.score_aime, 10, 3)
        units = [(suite, item, rep) for rep in range(3) for item in suite.items]
        first = evals.order(units, 7)
        self.assertEqual([u[2] for u in first], [0] * 10 + [1] * 10 + [2] * 10)
        self.assertEqual(first, evals.order(list(reversed(units)), 7))
        keys = [u[1]["key"] for u in first[:10]]
        self.assertNotEqual(keys, [str(k) for k in range(10)])
        self.assertNotEqual(keys, [u[1]["key"] for u in evals.order(units, 8)[:10]])

    def test_order_puts_niah_last(self):
        niah = evals.Suite("niah", [{"key": str(k)} for k in range(5)], evals.score_aime, 32, 1)
        aime = evals.Suite("aime", [{"key": str(k)} for k in range(5)], evals.score_aime, 10, 2)
        units = [(niah, item, 0) for item in niah.items]
        units += [(aime, item, rep) for rep in range(2) for item in aime.items]
        self.assertEqual([u[0].name for u in evals.order(units, 3)], ["aime"] * 10 + ["niah"] * 5)

    def test_compact_and_report(self):
        rows = evals.compact([rec("aime", "1", 0, True, pred=277, gold=277, finish_reason="stop", prompt_tokens=5)])
        self.assertEqual(rows, [["aime", "1", 0, True, 277, 277, "stop", 5, 10, 1.0, None]])
        calls = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(evals.subprocess, "run", lambda command, **kw: calls.append((command, kw))), \
                mock.patch.dict(os.environ, {"AXK2_REPORT_PKGS": "/pkgs"}):
            evals.report(tmp, "evals.r.records", rows, series=True)
            path = Path(tmp) / "evals.r.records.json"
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), rows)
        command, kw = calls[0]
        self.assertEqual(command[1:], [str(evals.REPORT), "evals.r.records", "--file", str(path), "--series"])
        self.assertEqual(kw["env"]["PYTHONPATH"], "/pkgs")


class AdmissionTests(unittest.TestCase):
    def test_parse_metrics(self):
        text = "\n".join([
            "# HELP vllm:num_requests_running Number of requests in model execution batches.",
            "# TYPE vllm:num_requests_running gauge",
            'vllm:num_requests_running{engine="0",model_name="axk2"} 3.0',
            'vllm:num_requests_running{engine="1",model_name="axk2"} 2.0',
            'vllm:num_requests_waiting{engine="0",model_name="axk2"} 1.0',
            'vllm:kv_cache_usage_perc{engine="0",model_name="axk2"} 0.25',
            'vllm:kv_cache_usage_perc{engine="1",model_name="axk2"} 0.5',
            'vllm:num_preemptions_total{engine="0",model_name="axk2"} 7.0',
            'vllm:num_preemptions_created{engine="0",model_name="axk2"} 1.7e9',
            "vllm:num_requests_running_total 99"])
        self.assertEqual(evals.parse_metrics(text), {"running": 5.0, "waiting": 1.0, "kv": 0.5, "preemptions": 7.0})

    def test_limit_follows_the_server(self):
        a = evals.Admission("http://unused", start=10, cap=14, demo_cap=4, floor=4)
        self.assertTrue(a.paused)
        a.update(metrics(), 1000)
        self.assertEqual((a.paused, a.limit), (False, 10))  # growth waits a minute from the first sample
        a.update(metrics(), 1059)
        self.assertEqual(a.limit, 10)
        a.update(metrics(), 1060)
        self.assertEqual(a.limit, 12)
        a.update(metrics(), 1120)
        a.update(metrics(), 1300)
        self.assertEqual(a.limit, 14)  # cap
        a.update(metrics(preemptions=2), 1301)
        self.assertEqual(a.limit, 10)  # x0.75
        a.update(metrics(preemptions=2, kv=0.6), 1400)
        self.assertEqual((a.paused, a.limit), (False, 10))  # no growth above 50 % KV
        a.update(metrics(preemptions=2, kv=0.85), 1401)
        self.assertTrue(a.paused)
        a.update(metrics(preemptions=2, waiting=1), 1500)
        self.assertEqual((a.paused, a.limit), (True, 10))
        for count in range(3, 9):
            a.update(metrics(preemptions=count), 1600 + count)
        self.assertEqual((a.paused, a.limit), (False, 4))  # floor

    def test_demo_users_shrink_the_limit(self):
        a = evals.Admission("http://unused", start=20, cap=56, demo_cap=16)
        a.in_flight = 10
        a.update(metrics(running=12), 0)
        self.assertEqual((a.demo, a.effective()), (2, 16))
        a.update(metrics(running=10), 1)
        self.assertEqual((a.demo, a.effective()), (0, 20))

    def test_acquire(self):
        a = evals.Admission("http://unused", start=1, cap=1)
        self.assertFalse(a.acquire(lambda: True))  # paused until the first sample; gives up without a slot
        self.assertEqual(a.in_flight, 0)
        a.update(metrics(), 0)
        self.assertTrue(a.acquire())
        got = threading.Event()
        thread = threading.Thread(target=lambda: a.acquire() and got.set(), daemon=True)
        thread.start()
        self.assertFalse(got.wait(0.3))
        a.release()
        self.assertTrue(got.wait(5))
        a.release()
        a.stop.set()
        a.paused = True
        self.assertFalse(a.acquire())


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer({"hi": {"reasoning": "생각 중", "content": "안녕하세요"},
                                  "dead": {"content": "x", "error": "engine dead"}})
        self.addCleanup(self.server.close)

    def body(self, prompt):
        return {"model": "axk2", "messages": [{"role": "user", "content": prompt}], "stream": True}

    def test_stream_chat(self):
        reply = evals.stream_chat(self.server.base, self.body("hi"))
        self.assertEqual((reply["content"], reply["reasoning_chars"], reply["finish_reason"]),
                         ("안녕하세요", len("생각 중"), "stop"))
        self.assertEqual(reply["usage"], {"prompt_tokens": 2, "completion_tokens": 7})
        self.assertIsNotNone(reply["ttft"])
        with self.assertRaisesRegex(RuntimeError, "engine dead"):
            evals.stream_chat(self.server.base, self.body("dead"))
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            evals.stream_chat(self.server.base, self.body("hi"), stop=stop)

    def test_health_and_priority(self):
        self.assertTrue(evals.healthy(self.server.base))
        self.assertTrue(evals.priority_accepted(self.server.base, 10))
        self.server.reject_priority = True
        self.assertFalse(evals.priority_accepted(self.server.base, 10))
        self.server.healthy = False
        self.assertFalse(evals.healthy(self.server.base))
        stop = threading.Event()
        stop.set()
        self.assertFalse(evals.wait_healthy(self.server.base, limit=60, stop=stop))


class SinkTests(unittest.TestCase):
    def test_upload_and_resume_through_the_link(self):
        server = FakeServer()
        self.addCleanup(server.close)
        record = rec("aime", "1", 0, True)
        with tempfile.TemporaryDirectory() as tmp:
            sink = evals.Sink(Path(tmp) / "a" / "records.jsonl", "run-1", server.base + "/", "tok")
            sink.add(record)
            self.assertTrue(sink.flush())
            self.assertEqual((server.link["run-1"], sink.pending), ([record], []))
            self.assertTrue(sink.flush())
            sink.summary({"state": "running"})
            self.assertEqual(server.summaries, [{"state": "running", "run": "run-1"}])
            sink.close()
            fresh = evals.Sink(Path(tmp) / "b" / "records.jsonl", "run-1", server.base, "tok")
            self.assertEqual(fresh.previous(), [record])
            fresh.close()
            local = evals.Sink(Path(tmp) / "a" / "records.jsonl", "run-1")
            self.assertEqual(local.previous(), [record])
            self.assertTrue(local.flush())  # no link: nothing to upload
            local.close()

    def test_failed_uploads_are_kept(self):
        server = FakeServer(token="other")
        self.addCleanup(server.close)
        with tempfile.TemporaryDirectory() as tmp:
            for link in (server.base, "http://127.0.0.1:%d" % free_port()):
                with self.subTest(link=link):
                    sink = evals.Sink(Path(tmp) / "records.jsonl", "run-1", link, "tok")
                    sink.add(rec("aime", "1", 0, True))
                    with contextlib.redirect_stdout(io.StringIO()) as log:
                        self.assertFalse(sink.flush())
                    self.assertIn("AXK2_EVALS_WARN upload of 1 records failed", log.getvalue())
                    self.assertEqual(len(sink.pending), 1)
                    sink.close()
        self.assertEqual(server.link, {})


class RunnerTests(unittest.TestCase):
    def items(self, *keys):
        return [{"key": key, "gold": "B", "messages": [{"role": "user", "content": key}], "meta": {}} for key in keys]

    def test_records_cover_every_outcome(self):
        behaviors = {"ok": {"reasoning": "B가 맞다", "content": "정답은 B입니다."},
                     "wrong": {"content": "정답은 C입니다."},
                     "long": {"reasoning": "...", "content": "정답은 B입니다.", "finish": "length"},
                     "bad": {"status": 400},
                     "boom": {"content": "정답은 B입니다."},
                     "flaky": {"content": "정답은 B입니다.", "fail_first": True}}
        server = FakeServer(behaviors)
        self.addCleanup(server.close)

        def score(item, content):
            if item["key"] == "boom":
                raise ValueError("verifier exploded")
            return evals.score_kobalt(item, content)

        items = self.items(*behaviors)
        suite = evals.Suite("mini", items, score, 64, 1)
        args = SimpleNamespace(base=server.base, retries=3, cap=4, niah_parallel=1)
        with tempfile.TemporaryDirectory() as tmp:
            sink = evals.Sink(Path(tmp) / "records.jsonl", "t-run")
            admission = evals.Admission(server.base, start=4, cap=4, interval=0.05)
            threading.Thread(target=admission.run, daemon=True).start()
            runner = evals.Runner(args, "t-run", sink, admission, priority=10)
            with mock.patch.object(evals, "RETRY_BACKOFF", 0.0), contextlib.redirect_stdout(io.StringIO()) as log:
                runner.run(evals.order([(suite, item, 0) for item in items], 1))
            admission.stop.set()
            sink.close()
            records = {r["key"]: r for r in evals.read_jsonl(sink.path)}
        self.assertEqual(set(records), set(behaviors))
        ok = records["ok"]
        self.assertEqual((ok["correct"], ok["pred"], ok["finish_reason"], ok["attempts"], ok["hit_limit"]),
                         (True, "B", "stop", 1, False))
        self.assertEqual((ok["reasoning_chars"], ok["completion_tokens"], ok["prompt_tokens"]), (len("B가 맞다"), 7, 2))
        self.assertIsNotNone(ok["ttft"])
        self.assertEqual((records["wrong"]["correct"], records["wrong"]["pred"]), (False, "C"))
        self.assertEqual((records["long"]["correct"], records["long"]["pred"], records["long"]["hit_limit"]),
                         (False, "B", True))
        self.assertEqual((records["bad"]["correct"], records["bad"]["attempts"]), (None, 1))
        self.assertTrue(records["bad"]["error"].startswith("HTTP 400"))
        self.assertEqual((records["boom"]["correct"], records["boom"]["pred"]), (False, None))
        self.assertIn("verifier exploded", records["boom"]["score_error"])
        self.assertEqual((records["flaky"]["correct"], records["flaky"]["attempts"]), (True, 2))
        self.assertIn("AXK2_EVALS_WARN mini|flaky|0 attempt 1: HTTP 500", log.getvalue())
        for record in records.values():
            self.assertEqual(record["run"], "t-run")
            self.assertNotIn("messages", record)
        self.assertEqual(sorted(server.prompts()), sorted(list(behaviors) + ["flaky"]))
        for body in server.streams:
            self.assertEqual((body["priority"], body["temperature"], body["top_p"], body["max_tokens"]),
                             (10, 0.6, 0.95, 64))
            self.assertEqual((body["chat_template_kwargs"], body["stream_options"]),
                             ({"enable_thinking": True}, {"include_usage": True}))
        self.assertEqual(len({body["seed"] for body in server.streams}), len(behaviors))
        summary = evals.summarize(list(records.values()), {"mini": {"items": 6, "repeats": 1}})["mini"]
        self.assertAlmostEqual(summary["score"], 0.4)
        self.assertEqual((summary["errors"], summary["hit_limit"], summary["score_errors"]), (1, 1, 1))

    def test_stop_drops_interrupted_generations(self):
        server = FakeServer({"drop": {"hold": True, "status": 500}, "later": {"content": "정답은 B입니다."}})
        self.addCleanup(server.close)
        items = self.items("drop", "later")
        suite = evals.Suite("mini", items, evals.score_kobalt, 64, 1)
        args = SimpleNamespace(base=server.base, retries=3, cap=1, niah_parallel=1)
        with tempfile.TemporaryDirectory() as tmp:
            sink = evals.Sink(Path(tmp) / "records.jsonl", "t-run")
            admission = evals.Admission(server.base, start=1, cap=1)
            admission.update(evals.parse_metrics(""), time.time())
            runner = evals.Runner(args, "t-run", sink, admission, priority=0)
            thread = threading.Thread(target=runner.run, args=([(suite, item, 0) for item in items],))
            thread.start()
            deadline = time.time() + 10
            while not server.prompts() and time.time() < deadline:
                time.sleep(0.02)
            runner.stop.set()
            server.release.set()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            sink.close()
            self.assertEqual(evals.read_jsonl(sink.path), [])
        self.assertEqual(server.prompts(), ["drop"])
        self.assertNotIn("priority", server.streams[0])
        self.assertEqual(admission.in_flight, 0)

    def test_request_body(self):
        suite = evals.Suite("aime", [], evals.score_aime, 131072, 8)
        runner = evals.Runner(SimpleNamespace(base="http://unused"), "r", None, None, priority=10)
        messages = [{"role": "user", "content": "x"}]
        first, second = (runner.body(suite, {"key": "1"}, rep, messages) for rep in (0, 1))
        self.assertEqual({k: first[k] for k in ("model", "max_tokens", "stream", "temperature", "top_p", "priority")},
                         {"model": "axk2", "max_tokens": 131072, "stream": True, "temperature": 0.6, "top_p": 0.95,
                          "priority": 10})
        self.assertEqual(first["seed"], runner.body(suite, {"key": "1"}, 0, messages)["seed"])
        self.assertNotEqual(first["seed"], second["seed"])
        quiet = evals.Runner(SimpleNamespace(base="http://unused"), "r", None, None, priority=0)
        self.assertNotIn("priority", quiet.body(suite, {"key": "1"}, 0, messages))


class MainTests(unittest.TestCase):
    def test_niah_run_then_resume_from_the_link(self):
        server = FakeServer()
        self.addCleanup(server.close)
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        reports = []
        argv = ["--base", server.base, "--suites", "niah", "--niah-lengths", "2048", "--max-model-len", "2000",
                "--limit", "4", "--link", server.base, "--run", "t-run"]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"AXK2_LINK_TOKEN": "tok"}), \
                mock.patch.object(evals, "report", lambda out, key, data, series=False: reports.append(key)), \
                contextlib.redirect_stdout(io.StringIO()) as log:
            self.assertEqual(evals.main(argv + ["--out-dir", str(Path(tmp) / "a")]), 0)
            records = evals.read_jsonl(Path(tmp) / "a" / "evals" / "t-run" / "records.jsonl")
            self.assertEqual(len(server.prompts()), 4)
            self.assertEqual(evals.main(argv + ["--out-dir", str(Path(tmp) / "b")]), 0)
        self.assertEqual(len(server.prompts()), 4, "the second job resumes everything from the link")
        self.assertEqual(len(records), 4)
        for record in records:
            self.assertTrue(record["correct"])
            self.assertEqual(record["meta"]["target_tokens"], 1952)
            self.assertTrue(1800 < record["meta"]["built_tokens"] <= 1952, record["meta"])
        self.assertEqual(server.link["t-run"], records)
        for body in server.streams:
            self.assertEqual((body["temperature"], body["max_tokens"], body["priority"]), (0.0, 32, 10))
            self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        states = [summary["state"] for summary in server.summaries]
        self.assertEqual(states, ["running", "done", "running", "done"])
        for final in (server.summaries[1], server.summaries[3]):
            niah = final["summary"]["niah"]
            self.assertEqual((final["run"], niah["score"], niah["n_generations"]), ("t-run", 1.0, 4))
            depths = set(niah["grid"]["2048"])
            self.assertEqual((len(depths), set(niah["grid"]["2048"].values())), (4, {True}))
            self.assertEqual(final["plan"]["niah"]["items"], 4)
            self.assertEqual(len(final["plan"]["niah"]["keys"]), 4)
        self.assertEqual(reports, ["evals.t-run.summary", "evals.t-run.records"] * 2)
        self.assertIn('"resumed": 4, "todo": 0', log.getvalue())


class PinnedDataTests(unittest.TestCase):
    """The loaders on the pinned files when they are cached; nothing is downloaded here."""

    def require(self, *paths):
        missing = [str(path) for path in paths if not (DATA / path).exists()]
        if missing:
            self.skipTest(f"not cached under {DATA}: {missing[:3]}")

    def test_aime(self):
        self.require("aime_2026.parquet")
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            self.skipTest("pyarrow is not installed")
        items = evals.load_aime(DATA)
        gold = {item["key"]: item["gold"] for item in items}
        self.assertEqual((len(items), gold["1"], gold["30"]), (30, 277, 393))
        self.assertTrue(items[0]["messages"][0]["content"].startswith(evals.FIG10 + "\n\n"))

    def test_kobalt(self):
        self.require("kobalt_700.json")
        items = evals.load_kobalt(DATA)
        self.assertEqual(len({item["key"] for item in items}), 700)
        self.assertTrue(all(re.fullmatch(r"[A-J]", item["gold"]) for item in items))
        self.assertEqual(Counter(item["meta"]["class"] for item in items),
                         {"Syntax": 300, "Semantics": 215, "Pragmatics": 81, "Phonetics/Phonology": 62,
                          "Morphology": 42})
        self.assertEqual(Counter(str(item["meta"]["level"]) for item in items), {"1": 182, "2": 220, "3": 298})
        user = items[0]["messages"][1]["content"]
        self.assertTrue(user.startswith(evals.KOBALT_USER.split("<QUESTION>")[0]))
        self.assertTrue(user.endswith(evals.KOBALT_USER.split("<QUESTION>")[1]))

    def test_click(self):
        self.require(*(Path("click") / (name.replace("/", "__") + ".json") for name in evals.CLICK["files"]))
        items = evals.load_click(DATA)
        self.assertEqual((len(items), len({item["key"] for item in items})), (1995, 1995))
        self.assertEqual(Counter(item["meta"]["n_choices"] for item in items), {4: 1739, 5: 256})
        self.assertEqual(Counter(item["meta"]["group"] for item in items), {"Culture": 1345, "Language": 650})
        self.assertEqual(Counter(item["gold"] for item in items), {"A": 599, "B": 487, "C": 465, "D": 395, "E": 49})

    def test_ifbench_items_and_official_verifier(self):
        root = Path(f"ifbench-{evals.IFBENCH['rev'][:12]}")
        self.require(*(root / rel for rel in evals.IFBENCH["files"]))
        root, items = evals.load_ifbench(DATA)
        self.assertEqual(len(items), 300)
        try:
            verifier = evals.IFBenchVerifier(root)
        except Exception as exc:  # nltk, langdetect, ... or their data unavailable
            self.skipTest(f"IFBench verifier unavailable: {exc}")
        item = next(item for item in items if item["key"] == "0")
        self.assertEqual(item["example"]["instruction_id_list"], ["count:keywords_multiple"])
        words = [item["example"]["kwargs"][0][f"keyword{i}"] for i in range(1, 6)]
        before = json.dumps(item["example"])

        def response(counts):
            return "\n".join(" ".join([word] * count) + "." for word, count in zip(words, counts))

        score = evals.ifbench_scorer(verifier)
        self.assertEqual(score(item, response([1, 2, 3, 5, 7])),
                         {"correct": True, "pred": None, "strict": [True], "loose": [True]})
        self.assertEqual(score(item, response([1, 2, 3, 5, 6])),
                         {"correct": False, "pred": None, "strict": [False], "loose": [False]})
        self.assertEqual(json.dumps(item["example"]), before, "the verifier works on a copy of the row")


if __name__ == "__main__":
    unittest.main()
