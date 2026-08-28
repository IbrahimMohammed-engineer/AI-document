"""
OCR provider abstraction (roadmap Phase 5; Backend §19).

The pipeline must not care whether OCR comes from a managed cloud service or
a self-hosted engine — the cloud-vs-self-hosted decision is a
deployment/cost/accuracy trade-off that will be revisited. Everything here is
behind one contract:

    OCRProvider.recognize(image: bytes, page_context: PageContext) -> OCRResult

Implementations:
  - TesseractOCRProvider  — self-hosted dev/fallback engine (pytesseract)
  - AzureDiOCRProvider    — Azure Document Intelligence (prebuilt-read, REST)
  - TextractOCRProvider   — AWS Textract DetectDocumentText (boto3)
  - NullOCRProvider       — explicit "no OCR configured" provider; every page
    routed to it fails fast with OCRProviderUnavailable, which the extractor
    turns into the per-page "OCR failed" marker (partial processing, never a
    document failure — Backend §19).

Retry policy lives HERE at the client level (roadmap Phase 5 step 10): a
page's OCR call is retried with exponential backoff (2–3 attempts) before
propagating; per-page failure tolerance is applied by the extractor.

Provider selection is a configuration change only — zero changes to
ingestion/ when swapped (Backend §19).
"""
from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# ── Errors ────────────────────────────────────────────────────────────────────

class OCRProviderError(Exception):
    """A transient OCR failure — a retry may succeed (timeout, 5xx)."""


class OCRProviderUnavailable(OCRProviderError):
    """OCR cannot run at all for this deployment/configuration.

    Deliberately NOT retried: missing engine, missing credentials. The
    extractor converts this into the per-page "OCR failed" marker.
    """


class OCRPageError(Exception):
    """A page's OCR failed permanently (retry budget exhausted).

    The extractor catches this per page — the document PROCEEDS with that
    page marked (empty text + metadata flag), never failing the version
    (Backend §19).
    """

    def __init__(self, message: str, *, attempts: int) -> None:
        super().__init__(message)
        self.message = message
        self.attempts = attempts


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PageContext:
    """Lightweight correlation info — for logging, never sent to the provider."""

    page_number: int
    document_version_id: str


@dataclass(frozen=True)
class LineBox:
    """One recognized text line with its normalized bounding box.

    Coordinates are normalized to 0..1 relative to the page (width/height),
    ready for highlight-coordinate normalization against document_pages'
    width/height (DB §15) in the citation-highlighting phases (10/15).
    """

    text: str
    bbox: tuple[float, float, float, float]  # (x0, y0, x1, y1), 0..1
    confidence: float | None = None


@dataclass(frozen=True)
class OCRResult:
    """Recognized text + geometry (+ optional confidence).

    Confidence is stored in page/chunk metadata as an INTERNAL quality
    signal only — never user-facing in V1 (FE §20 item 8).
    """

    text: str
    lines: tuple[LineBox, ...] = field(default_factory=tuple)
    confidence: float | None = None
    provider: str = ""


# ── The provider contract ─────────────────────────────────────────────────────

@runtime_checkable
class OCRProvider(Protocol):
    """The single OCR contract every engine implements."""

    name: str

    async def recognize(
        self, image: bytes, page_context: PageContext
    ) -> OCRResult: ...


# ── Tesseract implementation (dev / self-hosted fallback) ────────────────────

def _lines_from_tesseract_tsv(
    tsv: dict, image_width: int, image_height: int
) -> tuple[LineBox, ...]:
    """Build normalized LineBoxes from pytesseract's image_to_data DICT.

    Pure function (unit-testable without the Tesseract binary): groups word
    entries by (block, paragraph, line); drops confidences of -1 (pytesseract's
    "not a word" marker) and empty strings.
    """
    groups: dict[tuple[int, int, int], list[dict]] = {}
    order: list[tuple[int, int, int]] = []
    n = len(tsv.get("text", []))
    for i in range(n):
        text = (tsv["text"][i] or "").strip()
        conf = float(tsv["conf"][i]) if tsv["conf"][i] not in ("-1", None) else -1.0
        if not text or conf < 0:
            continue
        key = (
            int(tsv["block_num"][i]),
            int(tsv["par_num"][i]),
            int(tsv["line_num"][i]),
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(
            {
                "text": text,
                "conf": conf,
                "left": int(tsv["left"][i]),
                "top": int(tsv["top"][i]),
                "width": int(tsv["width"][i]),
                "height": int(tsv["height"][i]),
            }
        )

    lines: list[LineBox] = []
    for key in order:
        words = groups[key]
        left = min(w["left"] for w in words)
        top = min(w["top"] for w in words)
        right = max(w["left"] + w["width"] for w in words)
        bottom = max(w["top"] + w["height"] for w in words)
        confidence = sum(w["conf"] for w in words) / len(words)
        bbox = (
            max(0.0, left / image_width),
            max(0.0, top / image_height),
            min(1.0, right / image_width),
            min(1.0, bottom / image_height),
        )
        lines.append(
            LineBox(
                text=" ".join(w["text"] for w in words),
                bbox=bbox,
                confidence=round(confidence, 2),
            )
        )
    return tuple(lines)


class TesseractOCRProvider:
    """Self-hosted OCR via pytesseract — dev environments and constrained
    deployments where page images must not leave the perimeter."""

    name = "tesseract"

    def __init__(self, *, cmd: str | None = None, language: str = "eng",
                 timeout_seconds: float = 20.0) -> None:
        try:
            import pytesseract
        except ImportError as exc:  # pragma: no cover — pinned dependency
            raise OCRProviderUnavailable(
                "pytesseract is not installed (add it to requirements.txt)."
            ) from exc

        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        self._pytesseract = pytesseract
        self._language = language
        self._timeout_seconds = timeout_seconds

        # Fail fast on a misconfigured engine — a broken OCR binary is a
        # deterministic configuration error, not a per-page transient.
        try:
            pytesseract.get_tesseract_version()
        except Exception as exc:
            raise OCRProviderUnavailable(
                "Tesseract binary is not available "
                "(install it or set TESSERACT_CMD)."
            ) from exc

    async def recognize(
        self, image: bytes, page_context: PageContext
    ) -> OCRResult:
        return await asyncio.get_event_loop().run_in_executor(
            None, self._recognize_sync, image, page_context
        )

    def _recognize_sync(
        self, image: bytes, page_context: PageContext
    ) -> OCRResult:
        from PIL import Image

        try:
            pil_image = Image.open(io.BytesIO(image))
            width, height = pil_image.size
            tsv = self._pytesseract.image_to_data(
                pil_image,
                language=self._language,
                output_type=self._pytesseract.Output.DICT,
                timeout=self._timeout_seconds,
            )
        except self._pytesseract.TesseractError as exc:
            raise OCRProviderError(f"Tesseract failed: {exc}") from exc
        except RuntimeError as exc:
            # pytesseract raises RuntimeError on timeout
            raise OCRProviderError(f"Tesseract timed out: {exc}") from exc

        lines = _lines_from_tesseract_tsv(tsv, width, height)
        text = "\n".join(line.text for line in lines)
        confidences = [
            line.confidence for line in lines if line.confidence is not None
        ]
        mean_conf = (
            round(sum(confidences) / len(confidences), 2) if confidences else None
        )
        return OCRResult(
            text=text, lines=lines, confidence=mean_conf, provider=self.name
        )


# ── Azure Document Intelligence implementation ────────────────────────────────

class AzureDiOCRProvider:
    """Azure Document Intelligence `prebuilt-read` via the REST API.

    Uses httpx (already a dependency) rather than the Azure SDK to keep the
    worker image lean. Analyze is an async operation: POST → poll the
    Operation-Location URL until terminal status.
    """

    name = "azure_di"
    _API_VERSION = "2023-07-31"
    _POLL_INTERVAL_SECONDS = 1.0

    def __init__(self, *, endpoint: str, api_key: str,
                 timeout_seconds: float = 20.0) -> None:
        if not endpoint or not api_key:
            raise OCRProviderUnavailable(
                "OCR_PROVIDER=azure_di requires OCR_AZURE_ENDPOINT and "
                "OCR_AZURE_API_KEY."
            )
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def recognize(
        self, image: bytes, page_context: PageContext
    ) -> OCRResult:
        import httpx

        url = (
            f"{self._endpoint}/formrecognizer/documentModels/prebuilt-read:analyze"
            f"?api-version={self._API_VERSION}"
        )
        headers = {
            "Ocp-Apim-Subscription-Key": self._api_key,
            "Content-Type": "application/octet-stream",
        }
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            try:
                resp = await client.post(url, headers=headers, content=image)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise OCRProviderError(f"Azure DI analyze failed: {exc}") from exc

            operation_url: str | None = resp.headers.get("operation-location")
            if not operation_url:
                raise OCRProviderError(
                    "Azure DI did not return an operation-location header."
                )

            deadline = asyncio.get_event_loop().time() + self._timeout_seconds
            while True:
                poll = await client.get(
                    operation_url, headers={"Ocp-Apim-Subscription-Key": self._api_key}
                )
                poll.raise_for_status()
                payload = poll.json()
                status = payload.get("status")
                if status == "succeeded":
                    break
                if status in ("failed", "canceled"):
                    raise OCRProviderError(
                        f"Azure DI analyze {status}: "
                        f"{payload.get('error', {}).get('message', 'unknown')}"
                    )
                if asyncio.get_event_loop().time() > deadline:
                    raise OCRProviderError("Azure DI polling timed out.")
                await asyncio.sleep(self._POLL_INTERVAL_SECONDS)

        return self._parse_result(payload)

    def _parse_result(self, payload: dict) -> OCRResult:
        analyze = payload.get("analyzeResult", {})
        text = analyze.get("content", "")
        lines: list[LineBox] = []
        confidences: list[float] = []

        for page in analyze.get("pages", []):
            page_w = float(page.get("width") or 1)
            page_h = float(page.get("height") or 1)
            for line in page.get("lines", []):
                polygon = line.get("polygon") or []
                if len(polygon) >= 8:
                    xs = [float(polygon[i]) for i in range(0, len(polygon), 2)]
                    ys = [float(polygon[i]) for i in range(1, len(polygon), 2)]
                    bbox = (
                        max(0.0, min(xs) / page_w),
                        max(0.0, min(ys) / page_h),
                        min(1.0, max(xs) / page_w),
                        min(1.0, max(ys) / page_h),
                    )
                else:  # degenerate polygon — whole-page fallback box
                    bbox = (0.0, 0.0, 1.0, 1.0)
                # per-word confidences live on words; spans map line → words
                line_conf = self._line_confidence(analyze, page, line)
                if line_conf is not None:
                    confidences.append(line_conf)
                lines.append(
                    LineBox(text=line.get("content", ""), bbox=bbox,
                            confidence=line_conf)
                )

        mean_conf = (
            round(sum(confidences) / len(confidences), 2) if confidences else None
        )
        return OCRResult(
            text=text, lines=tuple(lines), confidence=mean_conf, provider=self.name
        )

    @staticmethod
    def _line_confidence(analyze_result: dict, page: dict, line: dict) -> float | None:
        """Average the confidences of the words covered by this line's span."""
        words = page.get("words") or []
        if not words:
            return None
        span = (line.get("span") or {})
        start = span.get("offset", 0)
        end = start + span.get("length", 0)
        confs = [
            float(w["confidence"])
            for w in words
            if "confidence" in w
            and w.get("span", {}).get("offset", -1) >= start
            and w.get("span", {}).get("offset", -1) < end
        ]
        return round(sum(confs) / len(confs), 2) if confs else None


# ── AWS Textract implementation ────────────────────────────────────────────────

class TextractOCRProvider:
    """AWS Textract `detect_document_text` (synchronous single-image API).

    Bounding boxes arrive already normalized to 0..1 (Geometry.BoundingBox).
    """

    name = "textract"

    def __init__(self, *, region: str = "us-east-1",
                 timeout_seconds: float = 20.0) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover — boto3 is a dependency
            raise OCRProviderUnavailable("boto3 is not installed.") from exc

        self._client = boto3.client(
            "textract",
            region_name=region,
            config=Config(
                retries={"max_attempts": 0},  # retry policy is owned HERE
                read_timeout=timeout_seconds,
                connect_timeout=min(10.0, timeout_seconds),
            ),
        )

    async def recognize(
        self, image: bytes, page_context: PageContext
    ) -> OCRResult:
        return await asyncio.get_event_loop().run_in_executor(
            None, self._recognize_sync, image
        )

    def _recognize_sync(self, image: bytes) -> OCRResult:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client.detect_document_text(
                Document={"Bytes": image}
            )
        except (BotoCoreError, ClientError) as exc:
            raise OCRProviderError(f"Textract failed: {exc}") from exc

        lines: list[LineBox] = []
        confidences: list[float] = []
        for block in response.get("Blocks", []):
            if block.get("BlockType") != "LINE":
                continue
            geometry = block.get("Geometry", {}).get("BoundingBox", {})
            left = float(geometry.get("Left", 0))
            top = float(geometry.get("Top", 0))
            width = float(geometry.get("Width", 1))
            height = float(geometry.get("Height", 1))
            conf = block.get("Confidence")
            if conf is not None:
                confidences.append(float(conf))
            lines.append(
                LineBox(
                    text=block.get("Text", ""),
                    bbox=(left, top, min(1.0, left + width), min(1.0, top + height)),
                    confidence=round(float(conf), 2) if conf is not None else None,
                )
            )

        text = "\n".join(line.text for line in lines)
        mean_conf = (
            round(sum(confidences) / len(confidences), 2) if confidences else None
        )
        return OCRResult(
            text=text, lines=tuple(lines), confidence=mean_conf, provider=self.name
        )


# ── Explicit no-provider fallback ──────────────────────────────────────────────

class NullOCRProvider:
    """OCR_PROVIDER=none — every OCR-routed page fails immediately.

    This is the graceful degradation path for deployments without any OCR
    engine: pages needing OCR get the explicit "OCR failed" marker and the
    document proceeds (partial processing — Backend §19).
    """

    name = "none"

    async def recognize(
        self, image: bytes, page_context: PageContext
    ) -> OCRResult:
        raise OCRProviderUnavailable(
            "No OCR provider is configured (OCR_PROVIDER=none). "
            "Scanned pages cannot be read — set OCR_PROVIDER to "
            "'tesseract', 'azure_di', or 'textract'."
        )


# ── Client-level retry wrapper (roadmap Phase 5 step 10) ──────────────────────

async def recognize_with_retry(
    provider: OCRProvider,
    image: bytes,
    page_context: PageContext,
    *,
    max_attempts: int = 3,
    backoff_seconds: float = 2.0,
    timeout_seconds: float = 20.0,
) -> OCRResult:
    """Recognize one page image with exponential-backoff retries.

    - OCRProviderUnavailable is NOT retried (deterministic — no engine/keys).
    - OCRProviderError / timeout are retried up to `max_attempts`.
    - Raises OCRPageError when the budget is exhausted (the extractor marks
      the page and continues — never fails the document).
    """
    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            return await asyncio.wait_for(
                provider.recognize(image, page_context),
                timeout=timeout_seconds,
            )
        except OCRProviderUnavailable:
            raise
        except OCRProviderError as exc:
            last_error = exc
            logger.warning(
                "OCR attempt %d/%d failed for version %s page %d: %s",
                attempt, max_attempts,
                page_context.document_version_id, page_context.page_number, exc,
            )
        except asyncio.TimeoutError:
            last_error = OCRProviderError(
                f"OCR timed out after {timeout_seconds:.0f}s"
            )
            logger.warning(
                "OCR attempt %d/%d timed out for version %s page %d",
                attempt, max_attempts,
                page_context.document_version_id, page_context.page_number,
            )
        if attempt < max_attempts:
            await asyncio.sleep(backoff_seconds * (2 ** (attempt - 1)))

    raise OCRPageError(
        str(last_error or "OCR failed"), attempts=max_attempts
    )


# ── Provider factory (configuration-only switching, Backend §19) ──────────────

def get_ocr_provider() -> OCRProvider:
    """Instantiate the configured OCR provider from settings.

    Real providers validate their configuration here so a broken deployment
    fails fast (deterministic job failure) rather than silently marking every
    scanned page as OCR-failed. OCR_PROVIDER=none intentionally does NOT
    validate — that is the documented graceful-degradation mode.
    """
    settings = get_settings()
    provider = settings.ocr_provider

    if provider == "none":
        return NullOCRProvider()
    if provider == "tesseract":
        return TesseractOCRProvider(
            cmd=settings.tesseract_cmd,
            language=settings.tesseract_language,
            timeout_seconds=settings.ocr_timeout_seconds,
        )
    if provider == "azure_di":
        return AzureDiOCRProvider(
            endpoint=settings.ocr_azure_endpoint or "",
            api_key=settings.ocr_azure_api_key or "",
            timeout_seconds=settings.ocr_timeout_seconds,
        )
    if provider == "textract":
        return TextractOCRProvider(
            region=settings.storage_region,
            timeout_seconds=settings.ocr_timeout_seconds,
        )
    raise OCRProviderUnavailable(f"Unknown OCR provider: {provider!r}")
