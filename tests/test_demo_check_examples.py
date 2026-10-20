"""Tests of the demo-example verifier (demo/tools/check_agentic_examples.py) against a fake demo web app.

The fake is a small HTTP server on a loopback port. It speaks the routes the verifier uses (/api/me, /api/capabilities,
/api/workspace with its upload, download, preview, cancel and close routes, and /api/agent as server-sent events) and
plays one scripted turn per example prompt, so every run below is offline and instant. Hosts, tokens and numbers are
made up: reserved example hosts, six-character tokens, no clock times.
"""
from __future__ import annotations

import difflib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "demo" / "tools")]
import agentic_examples as catalog  # noqa: E402
import check_agentic_examples as check  # noqa: E402
del sys.path[0]

BY_ID = {example.id: example for example in catalog.EXAMPLES}
PROMPTS = {name: example.prompt for name, example in BY_ID.items()}
NEWS, QUOTES, DOCS, PLACES = (f"https://{name}.example.com/" for name in ("news", "quotes", "docs", "places"))
LEARN = "https://%s/docs/byom-fixture" % BY_ID["foundry-byom-steps"].hosts[0]
HOSTS = {"ai-chip-news": ["news.example.com"], "chip-price-ratio": ["quotes.example.com"],
         "axk2-public-research": ["docs.example.com"], "foundry-byom-steps": [BY_ID["foundry-byom-steps"].hosts[0]],
         "pangyo-lunch-plan": ["places.example.com"]}


# ------------------------------------------------------------------------------------------------- the fake demo
DROP = object()  # an override's answer that closes the connection without a reply


class Space:
    def __init__(self):
        self.files, self.originals = {}, {}


def emit(http, name, payload):
    head = "" if name is None else f"event: {name}\n"
    http.wfile.write((head + "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8"))


def effect(space, name, arguments):
    """What the real tool would return, for the calls a script does not give a result for."""
    path = arguments.get("path")
    if name == "write_file":
        data = arguments["content"].encode("utf-8")
        space.files[path] = data
        return {"file": path, "bytes": len(data), "artifact": {"kind": "download", "path": path}}
    if name == "preview_html":
        return {"artifact": {"kind": "preview", "path": path}}
    if name == "chart_table":
        return {"artifact": {"kind": "chart", "svg": "<svg></svg>"}}
    if name == "read_file":
        return {"path": path, "text": space.files[path].decode("utf-8")}
    if name == "diff_file":
        before = space.originals.get(path, b"").decode("utf-8").splitlines(keepends=True)
        after = space.files.get(path, b"").decode("utf-8").splitlines(keepends=True)
        return "".join(difflib.unified_diff(before, after, "original/" + path, path))
    return {}


def act(http, space, ident, call):
    base = {"id": ident, "name": call["name"], "arguments": call["arguments"]}
    emit(http, "action", {**base, "state": "pending"})
    emit(http, "action", {**base, "state": "running"})
    result = call["result"] if "result" in call else effect(space, call["name"], call["arguments"])
    emit(http, "action", {**base, "state": call.get("state", "success"), "result": result,
                          "milliseconds": call.get("ms", 120)})


def play(http, space, script):
    try:
        emit(http, "policy", {"tools": True, "external": True, "denied": [], "reason": "enabled"})
        for raw in script.get("raw", ()):
            http.wfile.write(raw.encode("utf-8"))
        if script.get("hang"):
            for _ in range(40):
                http.wfile.write(b": keep-alive\n\n")
                time.sleep(0.1)
            return
        if script.get("error"):
            emit(http, "error", {"code": script["error"], "message": "the turn failed"})
            return
        invoked, rounds = 0, []
        for number, call in enumerate(script.get("pre", ()), 1):
            act(http, space, f"pre_{number}", call)
            invoked += 1
        for index, turn in enumerate(script["rounds"], 1):
            emit(http, "round", {"index": index})
            if turn.get("reasoning"):
                emit(http, None, {"choices": [{"delta": {"reasoning_content": turn["reasoning"]}}]})
            if turn["text"]:
                emit(http, None, {"choices": [{"delta": {"content": turn["text"]}}]})
            for number, call in enumerate(turn["calls"], 1):
                piece = {"index": number - 1, "id": f"call_{index}_{number}",
                         "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}
                emit(http, None, {"choices": [{"delta": {"tool_calls": [piece]}}]})
            emit(http, None, {"choices": [], "usage": {"prompt_tokens": 16000 + index, "completion_tokens": 40}})
            for number, call in enumerate(turn["calls"], 1):
                act(http, space, f"call_{index}_{number}", call)
                invoked += 1
            rounds.append({"index": index, "input_tokens": 16000 + index, "output_tokens": 40,
                           "first_token_seconds": 0.5, "seconds": 1.0, "cached_tokens": 0,
                           "markup_removed": bool(turn.get("markup_removed"))})
        for notice in script.get("notices", ()):
            emit(http, "notice", {"code": notice, "message": "fixture"})
        emit(http, "metrics", {"state": "completed", "ttft_seconds": script.get("ttft", 3.1),
                               "total_seconds": script.get("seconds", 7.35),
                               "input_tokens": sum(r["input_tokens"] for r in rounds),
                               "output_tokens": sum(r["output_tokens"] for r in rounds),
                               "output_tokens_per_second": 5.4, "rate_basis": "fixture", "model_calls": len(rounds),
                               "rounds": rounds, "comparison_id": "cmp001"})
        emit(http, "end", {"state": script.get("end_state", "completed"), "tool_invocations": invoked})
    except OSError:
        pass  # the client went away, as it does after a cancelled turn


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.demo.handle(self, "GET")

    def do_POST(self):
        self.server.demo.handle(self, "POST")


class FakeDemo:
    """The demo's routes. `scripts` maps an example prompt to the turn to play; `overrides` maps (profile, method,
    route) to a function of the hit number that returns (status, payload) to answer instead, DROP to close the
    connection without any reply, or None to carry on normally."""

    def __init__(self, scripts):
        self.scripts, self.overrides, self.hits = scripts, {}, {}
        self.requests, self.bodies, self.uploads = [], [], []
        self.created, self.closed, self.cancelled, self.spaces = [], [], [], {}
        self.hosts = set()
        self.me = {"fp8": {}, "nvfp4": {}}
        self.web_iq = {"fp8": "ready", "nvfp4": "ready"}
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.demo = self
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    @staticmethod
    def send(http, status, payload, kind="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        http.send_response(status)
        http.send_header("Content-Type", kind)
        http.send_header("Content-Length", str(len(data)))
        http.end_headers()
        http.wfile.write(data)

    def handle(self, http, method):
        parts = urlsplit(http.path)
        route, profile = parts.path, "fp8"
        if route.startswith("/models/nvfp4/"):
            profile, route = "nvfp4", route[len("/models/nvfp4"):]
        query = parse_qs(parts.query)
        length = int(http.headers.get("Content-Length") or 0)
        body = http.rfile.read(length) if length else b""
        token = http.headers.get("X-AX-Workspace")
        key = (profile, method, route)
        with self.lock:
            self.requests.append(key)
            self.hosts.add(http.headers.get("Host"))
            hit = self.hits[key] = self.hits.get(key, 0) + 1
        override = self.overrides.get(key)
        replaced = override(hit) if override else None
        if replaced is DROP:
            return
        if replaced:
            return self.send(http, *replaced)
        action = (method, route)
        if action == ("GET", "/api/me"):
            return self.send(http, 200, {"demo": True, "admin": False, "open": True, **self.me[profile]})
        if action == ("GET", "/api/capabilities"):
            return self.send(http, 200, {"web_iq": {"provider": "fixture", "state": self.web_iq[profile]}})
        if action == ("POST", "/api/workspace"):
            with self.lock:
                token = "tok%03d" % (len(self.created) + 1)
                self.created.append(token)
                self.spaces[(profile, token)] = Space()
            return self.send(http, 200, {"token": token, "expires_after_seconds": 1800})
        space = self.spaces.get((profile, token))
        if space is None:
            return self.send(http, 410, {"error": "expired", "message": "gone"})
        if action == ("POST", "/api/workspace/upload"):
            name = query["name"][0]
            space.files[name] = space.originals[name] = body
            self.uploads.append((profile, name, body))
            return self.send(http, 200, {"files": sorted(space.files), "bytes": len(body)})
        if action == ("GET", "/api/workspace/download"):
            data = space.files.get(query["path"][0])
            if data is None:
                return self.send(http, 404, {"error": "not_found"})
            return self.send(http, 200, data, "application/octet-stream")
        if action == ("GET", "/api/workspace/preview"):
            path = query["path"][0]
            if not path.endswith(".html"):
                return self.send(http, 400, {"error": "preview"})
            if path not in space.files:
                return self.send(http, 404, {"error": "not_found"})
            return self.send(http, 200, space.files[path], "text/plain; charset=utf-8")
        if action == ("POST", "/api/workspace/cancel"):
            self.cancelled.append(token)
            return self.send(http, 200, {"ok": True})
        if action == ("POST", "/api/workspace/close"):
            self.closed.append(token)
            self.spaces.pop((profile, token), None)
            return self.send(http, 200, {"ok": True})
        if action == ("POST", "/api/agent"):
            payload = json.loads(body)
            self.bodies.append((profile, payload))
            script = self.scripts.get(payload["messages"][0]["content"])
            if script is None:
                return self.send(http, 400, {"error": "bad_messages", "message": "no script for this prompt"})
            http.send_response(200)
            http.send_header("Content-Type", "text/event-stream")
            http.end_headers()
            return play(http, space, script)
        return self.send(http, 404, {"error": "not_found"})


# --------------------------------------------------------------------------------------------- the ideal turns
def call(name, arguments=None, **more):
    return {"name": name, "arguments": arguments or {}, **more}


def calc(expression, result):
    return call("calculator", {"expression": expression}, result={"expression": expression, "result": result})


def step(*calls, text=""):
    return {"text": text, "calls": list(calls)}


def final(text):
    return {"text": text, "calls": []}


def item(kind, title, url, **more):
    return {"kind": kind, "title": title, "url": url, "snippet": "fixture", **more}


def found(vertical, *items):
    return {"provider": "fixture", "vertical": vertical, "public_query": True, "retrieved_at": "fixture",
            "items": list(items), "result_status": "ok",
            "citations": [{"url": i["url"], "title": i["title"]} for i in items], "limitations": []}


def page(url):
    return {"provider": "fixture", "vertical": "browse", "url": url, "text": "fixture page", "result_status": "ok",
            "limitations": []}


def quote(symbol, name, price):
    return item("quote", name, QUOTES + symbol, quote={
        "symbol": symbol, "displayName": name, "price": price, "currency": "KRW", "exchange": "KRX",
        "lastTradedAt": "fixture", "delay": "fixture", "isDelayed": True, "delay_status": "delayed"})


def corrected_discount():
    text = catalog.discount_py().decode("utf-8").replace("total += price * rate", "total += price * (1 - rate)")
    return text.replace("    return sum(values) / len(values)",
                        "    if not values:\n        return 0\n    return sum(values) / len(values)")


def played(script):
    return [c["name"] for c in script.get("pre", ())] + [c["name"] for r in script["rounds"] for c in r["calls"]]


def ideal():
    """One good turn per example: the tools in the planned order, the numbers right, only returned links cited."""
    scripts = {}

    def add(name, *rounds, **more):
        scripts[PROMPTS[name]] = {"rounds": list(rounds), **more}

    zones = ("Asia/Seoul", "America/New_York", "Europe/London")
    add("time-zones",
        step(*(call("get_current_time", {"timezone": z}, result={"timezone": z, "time": "fixture"}) for z in zones)),
        final("서울, 뉴욕, 런던의 현재 시각입니다. 서울 저녁 시간대가 세 도시에 덜 불편합니다."))
    add("plan-fee",
        step(calc("69000*24*75/100", "1242000"), calc("69000*24-69000*24*75/100", "414000")),
        final("할인 후 총액은 1,242,000원이고 할인 전보다 414,000원 적습니다."))
    add("code-baseline", final("```python\ndef monthly_summary(usage_mb):\n    return sum(usage_mb)\n```"))

    headlines = [item("news", "기사 %d" % n, NEWS + "a%d" % n, source="fixture", date="fixture") for n in (1, 2, 3)]
    table = ("<h1>AI 반도체 뉴스</h1><table><tr><th>제목</th><th>언론사</th><th>날짜</th></tr>"
             + "".join("<tr><td>기사 %d</td><td>fixture</td><td>fixture</td></tr>" % n for n in (1, 2, 3))
             + "</table>")
    add("ai-chip-news",
        step(call("web_iq_news", {"query": "AI 반도체"}, result=found("news", *headlines))),
        step(call("write_file", {"path": "briefing.html", "content": table})),
        step(call("preview_html", {"path": "briefing.html"})),
        final("기사 3건의 출처입니다. " + " ".join(h["url"] for h in headlines)))

    add("chip-price-ratio",
        step(call("web_iq_finance", {"query": "000660"}, result=found("finance", quote("000660", "SK하이닉스", 210000)))),
        step(call("web_iq_finance", {"query": "005930"}, result=found("finance", quote("005930", "삼성전자", 70000)))),
        step(calc("210000/70000", "3")),
        final("SK하이닉스 ÷ 삼성전자 = 3 (210,000원 ÷ 70,000원, KRW, 지연 시세). 출처: " + QUOTES + "000660"))

    add("axk2-public-research",
        step(call("web_iq_web", {"query": "A.X K2"}, result=found("web", item("web", "A.X K2 소개", DOCS + "axk2")))),
        step(call("web_iq_browse", {"url": DOCS + "axk2"}, result=page(DOCS + "axk2"))),
        final("핵심 3가지를 요약했습니다.\n\n| 제목 | URL |\n| --- | --- |\n| A.X K2 소개 | " + DOCS + "axk2 |\n"))

    add("foundry-byom-steps",
        step(call("web_iq_web", {"query": "Foundry BYOM"}, result=found("web", item("web", "BYOM 연결", LEARN)))),
        step(call("web_iq_browse", {"url": LEARN}, result=page(LEARN))),
        final("1. 모델을 등록합니다.\n2. 연결을 만듭니다.\n\n출처: " + LEARN))

    sales = "월,매출(만원)\n1월,1200\n2월,1350\n3월,1280\n4월,1520\n5월,1610\n6월,1700\n"
    report = "<h1>월별 매출</h1><table><tr><th>합계</th><th>증감률</th></tr><tr><td>8660</td><td>41.7%</td></tr></table>"
    add("monthly-sales-report",
        step(call("write_file", {"path": "sales.csv", "content": sales}),
             calc("1200+1350+1280+1520+1610+1700", "8660"), calc("(1700-1200)/1200", "5/12 (≈ 0.4167)")),
        step(call("write_file", {"path": "report.html", "content": report})),
        step(call("preview_html", {"path": "report.html"})),
        final("매출 합계는 8,660만원이고 1월 대비 6월 증감률은 41.7%입니다."))

    first_page = {"status": "text_extracted", "file": "contract.pdf", "total_pages": 3, "next_page": 2,
                  "pages": [{"page": 1, "citation": "contract.pdf [page 1]", "text": "제1조 제2조 제3조"}]}
    other_pages = {"status": "text_extracted", "file": "contract.pdf", "total_pages": 3, "next_page": None,
                   "pages": [{"page": 2, "text": "제4조 제5조"}, {"page": 3, "text": "제6조 제7조"}]}
    checklist = ("# 계약서 점검\n- 제4조 해지: page 2\n- 제5조 위약금: page 2\n- 제6조 비밀유지: page 3\n"
                 "- 위약금: 12 × 330,000 × 30% = 1,188,000원\n")
    add("contract-review",
        step(call("read_pdf", {"path": "contract.pdf", "start_page": 2, "page_count": 2}, result=other_pages)),
        step(calc("12*330000*30/100", "1188000")),
        step(call("write_file", {"path": "contract_check.md", "content": checklist})),
        final("12개월 뒤 해지하면 위약금은 1,188,000원입니다. 제4조, 제5조, 제6조를 contract_check.md에 정리했습니다."),
        pre=[call("read_pdf", {"path": "contract.pdf", "start_page": 1, "page_count": 1}, result=first_page)])

    places = [item("place", "가상식당 " + n, PLACES + n, address="가상주소 " + n) for n in ("a", "b")]
    plan = "# 점심 일정\n- 12시 30분 점심\n" + "".join(f"- {p['title']}, {p['address']}, {p['url']}\n" for p in places)
    add("pangyo-lunch-plan",
        step(call("web_iq_places", {"query": "판교역 점심"}, result=found("places", *places))),
        step(call("get_current_time", {"timezone": "Asia/Seoul"}, result={"timezone": "Asia/Seoul", "time": "fixture"})),
        step(call("write_file", {"path": "lunch_plan.md", "content": plan})),
        final("lunch_plan.md에 기록했습니다. 출처: " + PLACES + "a"))

    add("code-fix-diff",
        step(call("read_file", {"path": "discount.py"})),
        step(call("write_file", {"path": "discount.py", "content": corrected_discount()})),
        step(call("diff_file", {"path": "discount.py"})),
        final("고친 이유: 할인율은 1 - rate로 곱해야 하고, 빈 목록은 0을 돌려줍니다."))

    traffic = "월,사용량(TB)\n1월,410\n2월,395\n3월,430\n4월,465\n5월,500\n6월,520\n"
    add("csv-chart",
        step(call("write_file", {"path": "traffic.csv", "content": traffic})),
        step(call("analyze_table", {"path": "traffic.csv", "operation": "sum", "column": "사용량(TB)"},
                  result={"column": "사용량(TB)", "operation": "sum", "count": 6, "result": "2720"}),
             call("analyze_table", {"path": "traffic.csv", "operation": "mean", "column": "사용량(TB)"},
                  result={"column": "사용량(TB)", "operation": "mean", "count": 6, "result": "1360/3"})),
        step(call("chart_table", {"path": "traffic.csv", "label_column": "월", "value_column": "사용량(TB)"})),
        final("합계는 2,720TB, 월 평균은 약 453.3TB입니다. 시연용 가상 데이터입니다."))
    return scripts


# ------------------------------------------------------------------------------------------------- the harness
class Outcome:
    def __init__(self, code, out, err, sleeps):
        self.code, self.out, self.err, self.sleeps = code, out, err, sleeps

    @property
    def summary(self):
        return json.loads(self.out)

    def runs(self):
        return self.summary["runs"]

    def run(self, example, profile="fp8", number=1):
        (rec,) = [r for r in self.runs() if (r["id"], r["profile"], r["run"]) == (example, profile, number)]
        return rec


class DemoCase(unittest.TestCase):
    def setUp(self):
        self.scripts = ideal()
        self.demo = FakeDemo(self.scripts)
        self.demo.start()
        self.addCleanup(self.demo.stop)

    def script(self, name):
        return self.scripts[PROMPTS[name]]

    def cli(self, *argv, url=None, sleep=None):
        sleeps, out, err = [], io.StringIO(), io.StringIO()
        code = check.main(list(argv), environ={"AXK2_DEMO_URL": url or self.demo.url}, stdout=out, stderr=err,
                          sleep=sleep or sleeps.append)
        return Outcome(code, out.getvalue(), err.getvalue(), sleeps)

    def go(self, *argv, **more):
        return self.cli("--go", *argv, **more)

    def one(self, name, *argv, profile="fp8"):
        """One run of one example: (the whole outcome, the run record)."""
        outcome = self.go("--only", name, "--runs", profile + "=1", *argv)
        return outcome, outcome.run(name, profile)


# ------------------------------------------------------------------------------------- the good turns pass
class IdealRunTests(DemoCase):
    def test_every_example_passes_on_both_profiles(self):
        outcome = self.go("--runs", "fp8=1,nvfp4=1")
        self.assertEqual(outcome.code, 0, outcome.err)
        runs = outcome.runs()
        self.assertEqual(len(runs), 2 * len(catalog.EXAMPLES))
        web_iq = 0
        for rec in runs:
            with self.subTest(example=rec["id"], profile=rec["profile"]):
                script = self.scripts[PROMPTS[rec["id"]]]
                names = played(script)
                web_iq += sum(1 for name in names if name.startswith("web_iq_"))
                self.assertEqual((rec["ok"], rec["failed"], rec["flags"]), (True, {}, {}))
                self.assertEqual(rec["state"], "completed")
                self.assertEqual([t["name"] for t in rec["tools"]], names)
                self.assertEqual({t["state"] for t in rec["tools"]}, {"success"} if names else set())
                self.assertEqual(rec["first_tool"], names[0] if names else None)
                self.assertEqual(rec["web_iq_calls"], sum(1 for name in names if name.startswith("web_iq_")))
                written = [c["arguments"]["path"] for r in script["rounds"] for c in r["calls"]
                           if c["name"] == "write_file"]
                self.assertEqual([f["name"] for f in rec["files"]], written)
                for entry in rec["files"]:
                    html = entry["name"].endswith(".html")
                    self.assertEqual(entry["download"], 200)
                    self.assertGreater(entry["bytes"], 0)
                    self.assertEqual(entry.get("preview"), True if html else None)
                    self.assertEqual("preview_status" in entry, html)
                    self.assertEqual(entry.get("preview_status"), 200 if html else None)
                    self.assertEqual(entry.get("preview_bytes"), entry["bytes"] if html else None)
                self.assertEqual(rec.get("source_hosts"), HOSTS.get(rec["id"]))
                count = len(script["rounds"])
                self.assertEqual((rec["model_calls"], len(rec["rounds"])), (count, count))
                self.assertEqual((rec["ttft_seconds"], rec["total_seconds"]), (3.1, 7.35))
                self.assertEqual((rec["input_tokens"], rec["output_tokens"]),
                                 (sum(16000 + i for i in range(1, count + 1)), 40 * count))
                self.assertEqual(rec["http"].get("close"), 200)
                self.assertEqual((rec["http_5xx"], rec["stop"], rec["notices"]), (0, None, []))
        self.assertEqual(outcome.summary["totals"],
                         {"passed": len(runs), "failed": 0, "skipped": 0, "http_5xx": 0, "web_iq_calls": web_iq})

    def test_requests_are_made_the_way_the_page_makes_them(self):
        outcome = self.go("--runs", "fp8=1,nvfp4=1")
        self.assertEqual(outcome.code, 0, outcome.err)
        bodies = {}
        for profile, payload in self.demo.bodies:
            bodies[(profile, payload["messages"][0]["content"])] = payload
        for profile in ("fp8", "nvfp4"):
            for example in catalog.EXAMPLES:
                with self.subTest(example=example.id, profile=profile):
                    expected = {"messages": [{"role": "user", "content": example.prompt}], "thinking": False,
                                "tools": True}
                    if example.upload:
                        expected["attachments"] = [example.upload]
                    self.assertEqual(bodies[(profile, example.prompt)], expected)
        self.assertEqual(sorted((p, n) for p, n, _ in self.demo.uploads),
                         [("fp8", "contract.pdf"), ("fp8", "discount.py"), ("nvfp4", "contract.pdf"),
                          ("nvfp4", "discount.py")])
        for _, name, data in self.demo.uploads:
            self.assertEqual(data, catalog.FIXTURES[name]())
        self.assertIn(("nvfp4", "POST", "/api/agent"), self.demo.requests)
        self.assertIn(("fp8", "POST", "/api/agent"), self.demo.requests)
        self.assertEqual(len(self.demo.created), 2 * len(catalog.EXAMPLES))
        self.assertEqual(sorted(self.demo.closed), sorted(self.demo.created))
        self.assertEqual(self.demo.cancelled, [])
        self.assertEqual({route for _, _, route in self.demo.requests},
                         {"/api/me", "/api/capabilities", "/api/workspace", "/api/workspace/upload",
                          "/api/workspace/download", "/api/workspace/preview", "/api/workspace/close", "/api/agent"})

    def test_runs_are_spaced_by_the_gap(self):
        outcome = self.go("--runs", "fp8=2,nvfp4=1")
        self.assertEqual(outcome.code, 0, outcome.err)
        self.assertEqual(outcome.sleeps, [10.0] * (3 * len(catalog.EXAMPLES) - 1))
        outcome = self.go("--only", "plan-fee,code-baseline", "--runs", "fp8=2", "--gap", "12")
        self.assertEqual(outcome.sleeps, [12.0] * 3)

    def test_summary_and_table_name_neither_host_nor_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            result, table = Path(folder, "result.json"), Path(folder, "result.md")
            outcome = self.go("--only", "plan-fee,ai-chip-news", "--runs", "fp8=1,nvfp4=1",
                              "--out", str(result), "--table", str(table))
            saved, saved_table = result.read_text("utf-8"), table.read_text("utf-8")
        self.assertEqual(outcome.code, 0, outcome.err)
        self.assertEqual(saved, outcome.out)
        self.assertTrue(outcome.err.endswith(saved_table))
        for text in (outcome.out, outcome.err, saved, saved_table):
            self.assertNotIn("127.0.0.1", text)
            self.assertNotIn("tok0", text)
        self.assertEqual(outcome.summary["scheme"], "http")
        self.assertEqual(outcome.summary["profiles"],
                         {"fp8": {"web_iq": "ready", "problem": None}, "nvfp4": {"web_iq": "ready", "problem": None}})
        self.assertIn("| plan-fee | FP8 | 1 | PASS |", saved_table)
        self.assertIn("| ai-chip-news | NVFP4 | 1 | PASS |", saved_table)
        self.assertIn("web_iq_news>write_file>preview_html", saved_table)
        self.assertIn("news.example.com", saved_table)
        self.assertEqual(saved_table.count("\n"), 2 + 4)


if __name__ == "__main__":
    unittest.main()
