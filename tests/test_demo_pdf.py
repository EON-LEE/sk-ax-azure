"""Actual PDF parser, upload, MAF registration/replay and typed outcome regression tests."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo" / "frontend"))
from agent import agent_response
from app import Config, create_app
from hub import Gate
from pdf_documents import PAGE_CHUNK, PDFProblem, read_pdf
from workspace import Workspace, extract
sys.path.pop(0)
from tests.pdf_samples import image_pdf, text_pdf
from tests.test_demo_agent import FixtureHub, FixtureLink


class PDFTests(unittest.TestCase):
    def test_korean_positioned_text_page_citations_and_size(self):
        data = text_pdf(padding=740_000)
        self.assertGreater(len(data), 740_000)
        first = read_pdf(data, "한국어-대시보드.pdf", page_count=1)
        self.assertEqual(first["status"], "text_extracted")
        self.assertEqual(first["total_pages"], 2)
        self.assertEqual(first["next_page"], 2)
        self.assertIn("매출 120", first["pages"][0]["text"])
        self.assertIn("방문자 42", first["pages"][0]["text"])
        self.assertEqual(first["pages"][0]["citation"], "한국어-대시보드.pdf [page 1]")
        second = read_pdf(data, "한국어-대시보드.pdf", start_page=2, page_count=1)
        self.assertIn("두 번째 페이지", second["pages"][0]["text"])
        self.assertIsNone(second["next_page"])
        self.assertIn("[page 2]", extract("한국어-대시보드.pdf", data))

    def test_character_pagination_is_not_silent_truncation(self):
        data = text_pdf(["가나다" * 4000])
        first = read_pdf(data, "large.pdf", page_count=1)
        page = first["pages"][0]
        self.assertEqual(len(page["text"]), PAGE_CHUNK)
        self.assertTrue(page["truncated"])
        second = read_pdf(data, "large.pdf", page_count=1, offset=page["next_offset"])
        combined = page["text"] + second["pages"][0]["text"]
        self.assertEqual(combined.strip(), "가나다" * 4000)

    def test_corrupt_encrypted_unsupported_and_range_are_distinct(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("test-password")
        encrypted = io.BytesIO()
        writer.write(encrypted)
        cases = [(b"not pdf", "a.pdf", {}, "pdf_invalid"),
                 (b"%PDF-1.7\ntruncated", "a.pdf", {}, "pdf_invalid"),
                 (encrypted.getvalue(), "a.pdf", {}, "pdf_encrypted"),
                 (text_pdf(), "a.txt", {}, "pdf_unsupported"),
                 (text_pdf(), "a.pdf", {"start_page": 99}, "pdf_range"),
                 (text_pdf(), "a.pdf", {"page_count": 6}, "pdf_range")]
        for data, name, options, code in cases:
            with self.subTest(code=code), self.assertRaises(PDFProblem) as caught:
                read_pdf(data, name, **options)
            self.assertEqual(caught.exception.result["error"], code)

    def test_image_only_and_blank_are_not_zero_text_success(self):
        with self.assertRaises(PDFProblem) as caught:
            read_pdf(image_pdf(), "scan.pdf")
        self.assertEqual(caught.exception.result["error"], "ocr_not_configured")
        self.assertEqual(caught.exception.result["pages"][0]["page"], 1)
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        blank = io.BytesIO()
        writer.write(blank)
        with self.assertRaises(PDFProblem) as caught:
            read_pdf(blank.getvalue(), "blank.pdf")
        self.assertEqual(caught.exception.result["error"], "pdf_no_text")
        with self.assertRaises(PDFProblem):
            extract("scan.pdf", image_pdf())

    def test_page_limit_and_partial_pages(self):
        writer = PdfWriter()
        for _ in range(101):
            writer.add_blank_page(width=100, height=100)
        out = io.BytesIO()
        writer.write(out)
        with self.assertRaises(PDFProblem) as caught:
            read_pdf(out.getvalue(), "many.pdf")
        self.assertEqual(caught.exception.result["error"], "pdf_limit")
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(text_pdf(["한국어 페이지"]))))
        writer.append(PdfReader(io.BytesIO(image_pdf())))
        out = io.BytesIO()
        writer.write(out)
        result = read_pdf(out.getvalue(), "mixed.pdf")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["pages"][1]["error"], "ocr_not_configured")
        self.assertIn("한국어", result["pages"][0]["text"])


class PDFIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_genuine_maf_forced_pdf_read_and_replay(self):
        path = "한국어-대시보드.pdf"
        space = Workspace()
        space.upload(path, text_pdf())
        link = FixtureLink("read_pdf", {"path": path, "start_page": 1, "page_count": 1})
        response = agent_response(FixtureHub(link), Gate(1, 2), {
            "messages": [{"role": "user", "content": "첨부 문서를 요약해 줘."}], "model": "axk2",
        }, space, True, attachment_paths=[path])
        output = b"".join([part async for part in response.body_iterator]).decode()
        self.assertIn('"state": "success"', output)
        self.assertIn("매출 120", output)
        self.assertIn("[page 1]", output)
        self.assertIn(path, space.pdf_checked)
        self.assertEqual(link.requests[0]["tool_choice"], "auto")
        self.assertIn("text_extracted", next(m["content"] for m in link.requests[0]["messages"] if m["role"] == "tool"))
        self.assertEqual(link.requests[1]["tool_choice"], "auto")
        tools = {t["function"]["name"]: t["function"] for t in link.requests[0]["tools"]}
        self.assertIn("Korean", tools["read_pdf"]["description"])
        self.assertIn("page_count", tools["read_pdf"]["parameters"]["properties"])
        self.assertIn(path, link.requests[0]["messages"][0]["content"])
        replay = next(m["content"] for m in link.requests[1]["messages"] if m["role"] == "tool")
        self.assertIn("text_extracted", replay)
        self.assertIn(path, replay)

    async def test_actual_parser_failure_is_a_typed_error_action(self):
        for name, data, code in [("scan.pdf", image_pdf(), "ocr_not_configured"),
                                 ("corrupt.pdf", b"broken", "pdf_invalid")]:
            with self.subTest(code=code):
                space = Workspace()
                space.upload(name, data)
                link = FixtureLink("read_pdf", {"path": name})
                response = agent_response(FixtureHub(link), Gate(1, 2), {
                    "messages": [{"role": "user", "content": "read attachment"}], "model": "axk2",
                }, space, True, attachment_paths=[name])
                output = b"".join([part async for part in response.body_iterator]).decode()
                self.assertIn('"state": "error"', output)
                self.assertIn(f'"error": "{code}"', output)
                self.assertNotIn('"state": "success"', output)

    async def test_upload_metadata_and_cross_chat_pdf_access(self):
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(Config(data=Path(folder), aml_dir=ROOT / "aml", session_secret="test", open_demo=True),
                             start_supervisor=False)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                a = (await client.post("/api/workspace")).json()["token"]
                b = (await client.post("/api/workspace")).json()["token"]
                path = "한국어-대시보드.pdf"
                upload = await client.post("/api/workspace/upload", params={"name": path},
                                           headers={"X-AX-Workspace": a}, content=text_pdf())
                self.assertEqual(upload.json()["files"], [path])
                request = {"messages": [{"role": "user", "content": "요약"}], "attachments": [path], "tools": True}
                denied = await client.post("/api/agent", headers={"X-AX-Workspace": b}, json=request)
                self.assertEqual(denied.status_code, 400)
                disabled = await client.post("/api/agent", headers={"X-AX-Workspace": a},
                                             json=dict(request, tools=False))
                self.assertEqual(disabled.json()["error"], "tools_disabled")


if __name__ == "__main__":
    unittest.main()
