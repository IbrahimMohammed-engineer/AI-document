"""
Document parsers — the content-extraction boundary of the ingestion stage
(roadmap Phase 5; Backend §18).

Contains:
  - ExtractionError taxonomy (typed, deterministic — a corrupt or
    password-protected file will NEVER succeed on retry, so it maps to the
    worker's DeterministicJobError → terminal FAILED, Backend §48).
  - needs_ocr(): the per-page scanned-page classification heuristic. OCR need
    is decided PER PAGE, not per document, because a scanned exhibit can sit
    inside an otherwise born-digital contract (Backend §18).
  - detect_content_type(): magic-byte re-verification of the downloaded
    bytes. Upload-time validation is not trusted twice (Phase 5 security
    note) — the worker re-sniffs before parsing.
  - DocumentParser implementations:
      * PdfParser  (PyMuPDF)   — lazy per-page extraction + rasterization for
        OCR at OCR-tuned DPI; page-level and streamed so worker memory is
        bounded independent of document size (Backend §18).
      * DocxParser (python-docx) — paragraphs/tables in document order;
        DOCX has no pages, so a single logical page is synthesized and the
        decision recorded in page metadata (roadmap Phase 5 step 7).

The stage orchestrator (ingestion/extractor.py) selects the implementation
by DETECTED mime type — file-type branching lives here and nowhere else
(Backend §18: adding a format means adding one parser, not touching stages).
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Callable, ClassVar, Iterator

logger = logging.getLogger(__name__)

# Canonical MIME types the extraction stage supports (parser registry keys).
SUPPORTED_EXTRACTION_MIMES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)


# ── Error taxonomy (deterministic — never retry-worthy) ──────────────────────

class ExtractionError(Exception):
    """A file cannot be extracted, and retrying will not change that.

    Raised by parsers; the worker converts this into DeterministicJobError →
    terminal FAILED + version FAILED (recovery = corrected re-upload or the
    explicit retry action, never automatic retries — Backend §48/§49).
    """

    code: str = "EXTRACTION_FAILED"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class CorruptFileError(ExtractionError):
    """The bytes are not a readable file of the detected type."""

    code = "FILE_CORRUPT"


class PasswordProtectedFileError(ExtractionError):
    """The file is encrypted/password-protected — cannot be processed."""

    code = "FILE_PASSWORD_PROTECTED"


class UnsupportedFileTypeError(ExtractionError):
    """Magic-byte detection did not yield a supported extraction type."""

    code = "FILE_TYPE_UNSUPPORTED"


# ── Per-page OCR-need classification ─────────────────────────────────────────

def needs_ocr(text: str, *, min_alnum_chars: int = 25) -> bool:
    """Classify a page as needing OCR from its natively-extracted text.

    A page needs OCR when its extracted text is empty or below a minimal
    density threshold (alphanumeric character count — whitespace/punctuation
    excluded so stray headers/footers don't mask a scanned body).

    This is a PER-PAGE decision (Backend §18): a scanned exhibit inside an
    otherwise born-digital contract gets OCR while the rest does not.
    """
    if not text:
        return True
    alnum = sum(1 for ch in text if ch.isalnum())
    return alnum < min_alnum_chars


# ── Magic-byte content detection (worker-side re-verification) ───────────────

def detect_content_type(data: bytes) -> str | None:
    """Detect the MIME type from file magic bytes.

    Returns the canonical MIME string, or None when the content cannot be
    identified. This is the authoritative check at processing time — the
    declared mime_type from upload is advisory (defense-in-depth vs
    upload-time spoofing).
    """
    try:
        import filetype as ft

        detected = ft.guess(data)
    except ImportError:  # pragma: no cover — filetype is a pinned dependency
        return None
    return detected.mime if detected is not None else None


# ── Parser output types ──────────────────────────────────────────────────────

@dataclass
class ParsedPage:
    """One page yielded by a parser (streamed — one exists at a time).

    `rasterize` is present only for formats that support page rendering
    (PDF): a zero-arg callable returning PNG bytes at the requested DPI,
    invoked lazily ONLY when the page is routed to OCR.
    """

    page_number: int  # 1-indexed
    text: str
    width: float | None = None   # points — normalizes highlight coordinates
    height: float | None = None
    rasterize: Callable[[], bytes] | None = None
    metadata: dict | None = None


# ── DocumentParser protocol + implementations ────────────────────────────────

class DocumentParser:
    """Base class for format-specific parsers.

    `open()` returns a LAZY iterator — pages are extracted one at a time so
    an 800-page document never sits fully extracted in worker memory
    (Backend §18). Callers must `close()` when done. `total_pages` is set by
    `open()` when the format exposes its page count up front (PDF), or None
    when it does not.
    """

    mime_type: ClassVar[str] = ""

    def __init__(self, *, ocr_dpi: int = 300) -> None:
        self._ocr_dpi = ocr_dpi
        self._document: object | None = None
        self.total_pages: int | None = None

    def open(self, data: bytes) -> Iterator[ParsedPage]:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        raise NotImplementedError


class PdfParser(DocumentParser):
    """PDF extraction via PyMuPDF (fitz).

    Per-page: native `get_text()`; when the orchestrator classifies the page
    as needing OCR it calls the page's `rasterize()` to get a ~300 DPI PNG
    for the OCR provider (roadmap Phase 5 steps 3–4).

    Error mapping: encrypted → PasswordProtectedFileError; unreadable →
    CorruptFileError (both deterministic, Backend §18).

    NOTE: open() validates EAGERLY (container-level corruption and
    password-protection surface immediately, before iteration) while page
    extraction stays lazy — one page exists in memory at a time.
    """

    mime_type = "application/pdf"

    def open(self, data: bytes) -> Iterator[ParsedPage]:
        import fitz  # PyMuPDF

        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise CorruptFileError(
                f"The PDF could not be opened (it may be corrupt): {exc}"
            ) from exc

        if doc.needs_pass:
            doc.close()
            raise PasswordProtectedFileError(
                "The PDF is password-protected and cannot be processed. "
                "Upload an unlocked copy."
            )

        if not doc.is_pdf or doc.page_count == 0:
            doc.close()
            raise CorruptFileError("The file does not contain readable PDF pages.")

        self.total_pages = doc.page_count
        self._document = doc
        return self._iter_pages(doc)

    def _iter_pages(self, doc) -> Iterator[ParsedPage]:
        try:
            for index in range(doc.page_count):
                page = doc.load_page(index)
                # get_text("text") → plain text in reading order
                text = page.get_text("text")
                rect = page.rect
                yield ParsedPage(
                    page_number=index + 1,
                    text=text,
                    width=float(rect.width),
                    height=float(rect.height),
                    rasterize=lambda p=page: self._rasterize(p),
                )
        except Exception as exc:
            # A mid-document parser crash on untrusted input is deterministic
            # for this file — surface as corrupt rather than retry-forever.
            raise CorruptFileError(
                f"The PDF could not be fully parsed: {exc}"
            ) from exc

    def _rasterize(self, page) -> bytes:
        """Render one page to PNG at the OCR-tuned DPI (~300)."""
        pix = page.get_pixmap(dpi=self._ocr_dpi)
        return pix.tobytes("png")

    def close(self) -> None:
        if self._document is not None:
            try:
                self._document.close()
            except Exception:  # pragma: no cover — close is best-effort
                pass
            self._document = None


class DocxParser(DocumentParser):
    """DOCX extraction via python-docx.

    Extracts paragraphs and tables IN document order (walking the XML body
    children, not the paragraph-only API, so interleaved tables keep their
    place in reading order). Table content is emitted row/column-aware.

    DOCX has no page concept: a single logical page is synthesized and the
    segmentation decision recorded in page metadata (roadmap Phase 5 step 7).
    """

    mime_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

    def open(self, data: bytes) -> Iterator[ParsedPage]:
        # DOCX is small and has exactly one (virtual) page — parse eagerly so
        # container-level corruption surfaces immediately.
        self.total_pages = 1
        return iter([self._parse_single_page(data)])

    def _parse_single_page(self, data: bytes) -> ParsedPage:
        try:
            import docx as docx_lib
            from docx.table import Table
            from docx.text.paragraph import Paragraph
        except ImportError as exc:  # pragma: no cover — pinned dependency
            raise CorruptFileError("DOCX support is not installed.") from exc

        try:
            document = docx_lib.Document(io.BytesIO(data))
        except Exception as exc:
            raise CorruptFileError(
                f"The DOCX could not be opened (it may be corrupt): {exc}"
            ) from exc

        lines: list[str] = []
        body = document.element.body
        for child in body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                paragraph = Paragraph(child, document)
                text = paragraph.text.strip()
                if text:
                    lines.append(text)
            elif tag == "tbl":
                table = Table(child, document)
                lines.extend(self._table_lines(table))

        return ParsedPage(
            page_number=1,
            text="\n".join(lines),
            width=None,   # DOCX has no page geometry — highlight normalization
            height=None,  # is N/A until/unless DOCX→PDF rendering is added
            rasterize=None,
            metadata={
                # Record the segmentation decision (roadmap Phase 5 step 7):
                # DOCX maps to a single logical page.
                "segmentation": "single_logical_page",
                "segmentation_note": (
                    "DOCX has no native pages; content is one logical page."
                ),
            },
        )

    @staticmethod
    def _table_lines(table) -> Iterator[str]:
        """Emit table content row/column-aware (structure detection in
        Phase 6 re-detects tables; here we just keep cell boundaries)."""
        for row in table.rows:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            yield " | ".join(cells)

    def close(self) -> None:
        self._document = None


# ── Registry / selection (the ONLY place file types branch) ──────────────────

_PARSERS: dict[str, type[DocumentParser]] = {
    parser.mime_type: parser
    for parser in (PdfParser, DocxParser)
}


def parser_for_mime(mime_type: str, *, ocr_dpi: int = 300) -> DocumentParser:
    """Instantiate the parser registered for a (detected) MIME type.

    Raises UnsupportedFileTypeError for types outside the extraction
    allow-list — the orchestrator selects by DETECTED type only.
    """
    parser_cls = _PARSERS.get(mime_type)
    if parser_cls is None:
        raise UnsupportedFileTypeError(
            f"Content type '{mime_type}' is not supported for text extraction. "
            f"Supported types: {', '.join(sorted(_PARSERS))}."
        )
    return parser_cls(ocr_dpi=ocr_dpi)
