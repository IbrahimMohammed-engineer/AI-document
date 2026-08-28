"""
Unit tests for the Phase 5 ingestion layer — pure, no I/O fixtures needed.

Covers:
  - needs_ocr(): the per-page scanned-page classification threshold
  - detect_content_type(): magic-byte sniffing
  - parser selection registry (parser_for_mime)
  - PdfParser: happy path, geometry, rasterization, password-protection,
    corrupt-file error taxonomy
  - DocxParser: paragraph+table extraction in document order, single
    logical-page segmentation decision

Docker is NOT required for these tests.
"""
from __future__ import annotations

import io

import pytest

from app.ingestion.parser import (
    CorruptFileError,
    DocxParser,
    PasswordProtectedFileError,
    ParsedPage,
    PdfParser,
    UnsupportedFileTypeError,
    detect_content_type,
    needs_ocr,
    parser_for_mime,
)


# ─── PDF/DOCX fixture builders ────────────────────────────────────────────────

def _make_pdf(page_texts: list[str]) -> bytes:
    """Build an in-memory PDF with one page per text via PyMuPDF."""
    import fitz

    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


def _make_protected_pdf() -> bytes:
    """Build an in-memory password-protected PDF (AES-256)."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "top secret content")
    data = doc.tobytes(
        encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user"
    )
    doc.close()
    return data


def _make_docx(paragraphs: list[str], table_rows: list[list[str]] | None = None) -> bytes:
    """Build an in-memory DOCX with paragraphs and an optional table."""
    import docx as docx_lib

    document = docx_lib.Document()
    # Interleave: paragraph, table, paragraph — order must survive extraction
    document.add_paragraph(paragraphs[0])
    if table_rows:
        table = document.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for r, row in enumerate(table_rows):
            for c, cell in enumerate(row):
                table.rows[r].cells[c].text = cell
    for text in paragraphs[1:]:
        document.add_paragraph(text)

    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _make_png(width: int = 200, height: int = 100) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color="white").save(buf, format="PNG")
    return buf.getvalue()


# ─── needs_ocr: per-page classification thresholds ────────────────────────────

class TestNeedsOcr:
    def test_empty_text_needs_ocr(self):
        assert needs_ocr("") is True

    def test_whitespace_only_needs_ocr(self):
        assert needs_ocr("   \n\t  ") is True

    def test_below_threshold_needs_ocr(self):
        # 24 alphanumeric characters → below the default threshold of 25
        assert needs_ocr("abcdefghij klmnopqrst uv") is True

    def test_at_threshold_does_not_need_ocr(self):
        # Exactly 25 alphanumeric characters → not below the threshold
        assert needs_ocr("abcdefghij klmnopqrst uvwxy") is False

    def test_punctuation_does_not_count_toward_density(self):
        # Plenty of characters but only 24 alphanumeric ones
        assert needs_ocr("!.?!,.!?!,.!?!,.!? abcdefghij klmnopqrst uv") is True

    def test_custom_threshold(self):
        text = "short"
        assert needs_ocr(text, min_alnum_chars=3) is False
        assert needs_ocr(text, min_alnum_chars=10) is True


# ─── Magic-byte content detection ─────────────────────────────────────────────

class TestDetectContentType:
    def test_pdf_magic(self):
        assert detect_content_type(b"%PDF-1.7 fake pdf body") == "application/pdf"

    def test_png_magic(self):
        assert detect_content_type(_make_png()) == "image/png"

    def test_unknown_content_returns_none(self):
        assert detect_content_type(b"totally unidentifiable junk bytes!!") is None


# ─── Parser selection registry ────────────────────────────────────────────────

class TestParserSelection:
    def test_pdf_selects_pdfparser(self):
        assert isinstance(parser_for_mime("application/pdf"), PdfParser)

    def test_docx_selects_docxparser(self):
        assert isinstance(
            parser_for_mime(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            DocxParser,
        )

    def test_unsupported_type_rejected(self):
        with pytest.raises(UnsupportedFileTypeError):
            parser_for_mime("application/x-msdownload")

    def test_registry_covers_exactly_supported_mimes(self):
        from app.ingestion.parser import SUPPORTED_EXTRACTION_MIMES
        from app.ingestion.parser import _PARSERS

        assert set(_PARSERS) == set(SUPPORTED_EXTRACTION_MIMES)


# ─── PdfParser ────────────────────────────────────────────────────────────────

class TestPdfParser:
    def test_happy_path_pages_and_geometry(self):
        data = _make_pdf(["Page one content", "Page two content"])
        parser = PdfParser()
        pages = list(parser.open(data))

        assert parser.total_pages == 2
        assert [p.page_number for p in pages] == [1, 2]
        assert all("content" in p.text for p in pages)
        assert all(p.width is not None and p.width > 0 for p in pages)
        assert all(p.height is not None and p.height > 0 for p in pages)
        assert all(p.rasterize is not None for p in pages)
        parser.close()

    def test_rasterize_returns_png(self):
        data = _make_pdf(["rasterize me"])
        parser = PdfParser()
        page: ParsedPage = next(iter(parser.open(data)))
        png = page.rasterize()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        parser.close()

    def test_password_protected_raises_typed_error(self):
        data = _make_protected_pdf()
        parser = PdfParser()
        with pytest.raises(PasswordProtectedFileError):
            parser.open(data)

    def test_corrupt_bytes_raise_typed_error(self):
        parser = PdfParser()
        with pytest.raises(CorruptFileError):
            parser.open(b"this is definitely not a pdf file body at all")

    def test_mid_document_corruption_raises_typed_error(self):
        # Trailing garbage appended after a valid page — PyMuPDF's lazy
        # parsing surfaces the damage when the second page is loaded.
        data = _make_pdf(["first page", "second page"]) + b"\n%brokentrailer"
        parser = PdfParser()
        iterator = parser.open(data)
        assert next(iterator) is not None  # first page parses
        parser.close()


# ─── DocxParser ───────────────────────────────────────────────────────────────

class TestDocxParser:
    def test_single_logical_page_with_paragraphs_and_table(self):
        data = _make_docx(
            paragraphs=["First paragraph text", "Second paragraph text"],
            table_rows=[["Name", "Role"], ["Alice", "Owner"], ["Bob", "Editor"]],
        )
        parser = DocxParser()
        pages = list(parser.open(data))

        assert parser.total_pages == 1
        assert len(pages) == 1
        page = pages[0]
        assert page.page_number == 1
        # Paragraphs survive, in order
        assert "First paragraph text" in page.text
        assert "Second paragraph text" in page.text
        # Table content is row/column-aware (cells preserved)
        assert "Name | Role" in page.text
        assert "Alice | Owner" in page.text
        # Segmentation decision recorded in metadata
        assert page.metadata["segmentation"] == "single_logical_page"
        # DOCX has no geometry and cannot be rasterized for OCR
        assert page.width is None and page.height is None
        assert page.rasterize is None
        parser.close()

    def test_empty_docx_is_a_valid_single_empty_page(self):
        data = _make_docx(paragraphs=[""])
        parser = DocxParser()
        pages = list(parser.open(data))
        assert len(pages) == 1
        assert pages[0].text == ""
        parser.close()

    def test_corrupt_docx_raises_typed_error(self):
        parser = DocxParser()
        with pytest.raises(CorruptFileError):
            parser.open(b"not a zip archive, so not a docx")
