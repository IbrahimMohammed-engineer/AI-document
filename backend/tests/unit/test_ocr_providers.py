"""
Unit tests for the Phase 5 OCR provider layer.

Covers:
  - Tesseract TSV → LineBox parsing (pure function — no binary needed)
  - recognize_with_retry: transient retry with backoff, exhaustion →
    OCRPageError, non-retryable OCRProviderUnavailable
  - NullOCRProvider / provider factory configuration paths
  - The extractor's per-page OCR tolerance marker (contract with extraction)

Docker is NOT required; the real Tesseract contract test auto-skips when the
binary is not installed.
"""
from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.infrastructure.ocr import (
    LineBox,
    NullOCRProvider,
    OCRPageError,
    OCRProviderError,
    OCRProviderUnavailable,
    OCRResult,
    PageContext,
    TesseractOCRProvider,
    _lines_from_tesseract_tsv,
    get_ocr_provider,
    recognize_with_retry,
)
from app.ingestion.extractor import _recognize_page
from app.ingestion.parser import ParsedPage

CONTEXT = PageContext(page_number=1, document_version_id="v-123")
VERSION = SimpleNamespace(id="v-123")  # _recognize_page only reads version.id


# ─── Tesseract TSV parsing (pure function) ────────────────────────────────────

def _tsv(rows: list[dict]) -> dict:
    """Build an image_to_data DICT from compact row specs."""
    keys = ("left", "top", "width", "height", "conf", "text",
            "block_num", "par_num", "line_num", "word_num")
    return {k: [r[k] for r in rows] for k in keys}


class TestTesseractTsvParsing:
    def test_groups_words_into_lines_with_normalized_boxes(self):
        tsv = _tsv([
            # line 1: "Hello World" spanning (10..210, 10..30) on a 200x100 image
            dict(left=10, top=10, width=90, height=20, conf=90.0, text="Hello",
                 block_num=1, par_num=1, line_num=1, word_num=1),
            dict(left=110, top=10, width=100, height=20, conf=80.0, text="World",
                 block_num=1, par_num=1, line_num=1, word_num=2),
            # line 2 in the same paragraph
            dict(left=10, top=40, width=80, height=20, conf=70.0, text="Next",
                 block_num=1, par_num=1, line_num=2, word_num=1),
        ])
        lines = _lines_from_tesseract_tsv(tsv, image_width=200, image_height=100)

        assert [line.text for line in lines] == ["Hello World", "Next"]
        first, second = lines
        assert first.bbox == (0.05, 0.10, 1.0, 0.30)
        assert first.confidence == 85.0  # mean of 90 and 80
        assert second.bbox == (0.05, 0.40, 0.45, 0.60)

    def test_drops_low_confidence_and_empty_entries(self):
        tsv = _tsv([
            dict(left=10, top=10, width=50, height=20, conf=-1, text="",
                 block_num=1, par_num=1, line_num=1, word_num=1),
            dict(left=10, top=10, width=50, height=20, conf=95.0, text="Keep",
                 block_num=1, par_num=1, line_num=2, word_num=1),
        ])
        lines = _lines_from_tesseract_tsv(tsv, 200, 100)
        assert len(lines) == 1
        assert lines[0].text == "Keep"

    def test_clamps_boxes_to_unit_square(self):
        tsv = _tsv([
            dict(left=190, top=90, width=50, height=50, conf=80.0, text="Edge",
                 block_num=1, par_num=1, line_num=1, word_num=1),
        ])
        (line,) = _lines_from_tesseract_tsv(tsv, 200, 100)
        assert line.bbox == (0.95, 0.90, 1.0, 1.0)


# ─── Fake providers ───────────────────────────────────────────────────────────

class FlakyProvider:
    """Fails `failures` times with OCRProviderError, then succeeds."""

    name = "flaky"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def recognize(self, image: bytes, page_context: PageContext) -> OCRResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise OCRProviderError("simulated transient outage")
        return OCRResult(text="recovered text", provider=self.name,
                         confidence=91.0, lines=())


class UnavailableProvider:
    name = "unavailable"

    def __init__(self) -> None:
        self.calls = 0

    async def recognize(self, image: bytes, page_context: PageContext) -> OCRResult:
        self.calls += 1
        raise OCRProviderUnavailable("no engine configured")


# ─── recognize_with_retry ─────────────────────────────────────────────────────

class TestRecognizeWithRetry:
    async def test_recovers_after_transient_failures(self):
        provider = FlakyProvider(failures=2)
        result = await recognize_with_retry(
            provider, b"img", CONTEXT,
            max_attempts=3, backoff_seconds=0.0, timeout_seconds=5.0,
        )
        assert result.text == "recovered text"
        assert provider.calls == 3

    async def test_exhaustion_raises_ocr_page_error_with_attempts(self):
        provider = FlakyProvider(failures=99)
        with pytest.raises(OCRPageError) as exc_info:
            await recognize_with_retry(
                provider, b"img", CONTEXT,
                max_attempts=3, backoff_seconds=0.0, timeout_seconds=5.0,
            )
        assert exc_info.value.attempts == 3
        assert provider.calls == 3

    async def test_unavailable_is_not_retried(self):
        provider = UnavailableProvider()
        with pytest.raises(OCRProviderUnavailable):
            await recognize_with_retry(
                provider, b"img", CONTEXT,
                max_attempts=5, backoff_seconds=0.0, timeout_seconds=5.0,
            )
        assert provider.calls == 1  # single attempt — deterministic failure


# ─── Per-page tolerance in the extractor ──────────────────────────────────────

def _scanned_page() -> ParsedPage:
    return ParsedPage(
        page_number=2,
        text="",  # no native text → routes to OCR
        width=612.0,
        height=792.0,
        rasterize=lambda: b"png-bytes",
    )


class TestExtractorPageTolerance:
    async def test_ocr_success_provides_text_and_internal_metadata(self):
        class GoodProvider:
            name = "good"

            async def recognize(self, image, page_context) -> OCRResult:
                assert page_context.page_number == 2
                return OCRResult(
                    text="scanned words",
                    provider=self.name,
                    confidence=88.5,
                    lines=(LineBox(text="scanned words",
                                   bbox=(0.1, 0.1, 0.9, 0.2), confidence=88.5),),
                )

        settings = get_settings()
        page = _scanned_page()
        text, meta, failed = await _recognize_page(
            page, version=VERSION, provider=GoodProvider(),
            settings=settings, base_metadata={},
        )
        assert failed is False
        assert text == "scanned words"
        assert meta["ocr"]["provider"] == "good"
        assert meta["ocr"]["confidence"] == 88.5
        assert meta["ocr"]["lines"][0]["bbox"] == [0.1, 0.1, 0.9, 0.2]

    async def test_ocr_failure_marks_page_and_continues(self):
        class AlwaysDownProvider:
            name = "down"

            async def recognize(self, image, page_context) -> OCRResult:
                raise OCRProviderError("provider outage")

        settings = get_settings()
        page = _scanned_page()
        text, meta, failed = await _recognize_page(
            page, version=VERSION, provider=AlwaysDownProvider(),
            settings=settings, base_metadata={"segmentation": None},
        )
        assert failed is True
        assert text == ""  # explicit empty text
        assert meta["ocr_failed"] is True
        assert meta["ocr_attempts"] == settings.ocr_max_attempts_per_page
        # base metadata preserved
        assert "segmentation" in meta


# ─── Null provider + factory configuration ────────────────────────────────────

class TestNullProviderAndFactory:
    async def test_null_provider_raises_unavailable(self):
        with pytest.raises(OCRProviderUnavailable):
            await NullOCRProvider().recognize(b"img", CONTEXT)

    def test_factory_none_returns_null_provider(self, monkeypatch):
        monkeypatch.setenv("OCR_PROVIDER", "none")
        get_settings.cache_clear()
        assert isinstance(get_ocr_provider(), NullOCRProvider)

    def test_factory_azure_without_keys_raises_unavailable(self, monkeypatch):
        monkeypatch.setenv("OCR_PROVIDER", "azure_di")
        monkeypatch.delenv("OCR_AZURE_ENDPOINT", raising=False)
        monkeypatch.delenv("OCR_AZURE_API_KEY", raising=False)
        get_settings.cache_clear()
        with pytest.raises(OCRProviderUnavailable):
            get_ocr_provider()

    def test_factory_tesseract_without_binary_raises_unavailable(self, monkeypatch):
        if shutil.which("tesseract"):
            pytest.skip("Tesseract is installed — misconfiguration path not testable")
        monkeypatch.setenv("OCR_PROVIDER", "tesseract")
        monkeypatch.setenv("TESSERACT_CMD", "Z:\\definitely\\not\\a\\binary.exe")
        get_settings.cache_clear()
        with pytest.raises(OCRProviderUnavailable):
            get_ocr_provider()


# ─── Real Tesseract contract test (skipped without the binary) ────────────────

class TestTesseractContract:
    @pytest.fixture()
    def provider(self):
        if shutil.which("tesseract") is None:
            pytest.skip("Tesseract binary not installed")
        return TesseractOCRProvider(timeout_seconds=30)

    async def test_recognizes_synthetic_image(self, provider):
        from PIL import Image, ImageDraw

        import io as _io

        image = Image.new("RGB", (300, 120), color="white")
        draw = ImageDraw.Draw(image)
        draw.text((10, 40), "HELLO OCR WORLD", fill="black")
        buf = _io.BytesIO()
        image.save(buf, format="PNG")

        result = await provider.recognize(buf.getvalue(), CONTEXT)
        assert "OCR" in result.text.upper()
        assert result.lines, "line boxes must be present for highlight groundwork"
        for line in result.lines:
            assert 0.0 <= line.bbox[0] <= line.bbox[2] <= 1.0
            assert 0.0 <= line.bbox[1] <= line.bbox[3] <= 1.0
