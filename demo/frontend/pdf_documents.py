"""Native PDF text extraction with explicit outcomes; no OCR or external document transfer."""
import io
import json
import logging

from pypdf import PdfReader, filters
from pypdf.errors import LimitReachedError, PyPdfError

MAX_PAGES = 100
PAGE_STREAM_LIMIT = 2 * 1024 * 1024
PAGE_TEXT_LIMIT = 200_000
PAGE_CHUNK = 10_000
filters.ZLIB_MAX_OUTPUT_LENGTH = PAGE_STREAM_LIMIT
LOG = logging.getLogger(__name__)


class PDFProblem(ValueError):
    def __init__(self, code, message, path, **details):
        self.result = {"status": "error", "error": code, "message": message, "file": path,
                       "method": "pypdf_native_text", **details}
        super().__init__(json.dumps(self.result, ensure_ascii=False))


def _reader(data, path):
    if not data.lstrip().startswith(b"%PDF-"):
        raise PDFProblem("pdf_invalid", "The bytes are not a PDF document; no scan assumption was made.", path)
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PDFProblem("pdf_encrypted", "Password-protected PDFs are not supported. Upload an authorized unencrypted copy.", path)
        total = len(reader.pages)
        if not 1 <= total <= MAX_PAGES:
            raise PDFProblem("pdf_limit", "PDF must contain 1..100 pages.", path, total_pages=total)
        return reader
    except LimitReachedError as exc:
        raise PDFProblem("pdf_limit", "PDF parser resource limit reached.", path) from exc
    except PyPdfError as exc:
        raise PDFProblem("pdf_invalid", "The PDF structure is corrupt or cannot be parsed.", path) from exc


def _page(reader, path, index, offset=0):
    page = reader.pages[index]
    stream = page.get_contents()
    if stream is not None and len(stream.get_data()) > PAGE_STREAM_LIMIT:
        raise PDFProblem("pdf_limit", "Decoded PDF page stream exceeds 2 MiB.", path, page=index + 1)
    text = page.extract_text() or ""
    if len(text) > PAGE_TEXT_LIMIT:
        raise PDFProblem("pdf_limit", "PDF page text exceeds 200000 characters.", path, page=index + 1)
    # Inspect image object descriptors, not decoded image data. Blank pages are not called scanned PDFs.
    resources = page.get("/Resources")
    resources = resources.get_object() if resources is not None else {}
    objects = resources.get("/XObject")
    objects = objects.get_object() if objects is not None else {}
    if len(objects) > 256:
        raise PDFProblem("pdf_limit", "PDF page has too many image/form objects.", path, page=index + 1)
    has_images = any(obj.get_object().get("/Subtype") == "/Image" for obj in objects.values())
    result = {"page": index + 1, "citation": f"{path} [page {index + 1}]",
              "offset": offset, "total_characters": len(text)}
    if not text.strip():
        code = "ocr_not_configured" if has_images else "pdf_no_text"
        return dict(result, status="error", error=code, text="",
                    message=("The native parser found no extractable text and detected an image object. "
                             "OCR is not configured; no OCR or visual chart analysis was performed." if has_images
                             else "The native parser found no extractable text. This can be a blank/graphics-only "
                                  "page or unavailable text encoding; filename/size does not establish a scanned PDF. "
                                  "OCR is not configured."))
    if offset >= len(text) and offset != 0:
        raise PDFProblem("pdf_range", "Text offset is beyond this page's extracted text.", path, page=index + 1)
    return dict(result, status="text_extracted", text=text[offset:offset + PAGE_CHUNK],
                truncated=offset + PAGE_CHUNK < len(text),
                next_offset=offset + PAGE_CHUNK if offset + PAGE_CHUNK < len(text) else None)


def read_pdf(data, path, start_page=1, page_count=3, offset=0):
    if not path.lower().endswith(".pdf"):
        raise PDFProblem("pdf_unsupported", "read_pdf accepts PDF files only.", path)
    if not 1 <= page_count <= 5 or start_page < 1 or not 0 <= offset <= PAGE_TEXT_LIMIT:
        raise PDFProblem("pdf_range", "Use start_page >= 1, page_count 1..5, offset 0..200000.", path)
    reader = _reader(data, path)
    return _read_range(reader, path, start_page, page_count, offset)


def _read_range(reader, path, start_page, page_count, offset):
    total = len(reader.pages)
    if start_page > total:
        raise PDFProblem("pdf_range", "Requested page is beyond the PDF.", path, total_pages=total)
    end = min(total, start_page - 1 + page_count)
    try:
        pages = [_page(reader, path, i, offset) for i in range(start_page - 1, end)]
    except LimitReachedError as exc:
        raise PDFProblem("pdf_limit", "Decoded PDF stream exceeds parser resource limits.", path) from exc
    except (PyPdfError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        if isinstance(exc, PDFProblem):
            raise
        raise PDFProblem("pdf_invalid", "PDF page content could not be parsed; this is not proof of an image PDF.", path) from exc
    errors = [p for p in pages if p["status"] == "error"]
    if len(errors) == len(pages):
        code = "ocr_not_configured" if any(p["error"] == "ocr_not_configured" for p in errors) else "pdf_no_text"
        raise PDFProblem(code, "No extractable text in the requested pages. See per-page parser results. "
                         "OCR is not configured; no text or image contents were invented.", path,
                         total_pages=total, pages=pages)
    if errors:
        LOG.warning("PDF extraction returned partial native text; some pages require attention")
    return {"status": "partial" if errors else "text_extracted", "file": path,
            "method": "pypdf_native_text", "total_pages": total, "pages": pages,
            "next_page": end + 1 if end < total else None,
            "error": "pdf_partial" if errors else None,
            "limitations": "Text layer only; reading order can differ in complex layouts. "
                           "OCR is unavailable and neither text extraction nor OCR understands charts visually."}


def all_text(data, path):
    reader = _reader(data, path)
    refs, size = [], 0
    for index in range(len(reader.pages)):
        batch = _read_range(reader, path, index + 1, 1, 0)
        page = batch["pages"][0]
        text = page["text"]
        offset = page["next_offset"]
        while offset is not None:
            more = _read_range(reader, path, index + 1, 1, offset)["pages"][0]
            text += more["text"]
            offset = more["next_offset"]
        size += len(text)
        if size > PAGE_TEXT_LIMIT:
            raise PDFProblem("pdf_limit", "Full-document extraction exceeds 200000 characters; use read_pdf pagination.", path)
        refs.append(f"[page {index + 1}]\n{text}")
    return "\n\n".join(refs)
