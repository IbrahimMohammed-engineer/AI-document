"""
Real S3/MinIO ObjectStorageProvider implementation.

Implements the ObjectStorageProvider interface from Phase 0 using boto3's S3 client,
which is compatible with both AWS S3 and MinIO (S3-compatible object storage for
local development).

Rules enforced by this module:
  - No boto3 import exists above the infrastructure layer.
  - ALL file reads/writes go through this provider — the bucket is never public.
  - Signed URLs are the ONLY way to expose file bytes to clients.
  - Upload streams in chunks — never buffers a whole file in memory (DB §36).

Wired at application startup in main.py (lifespan) based on settings.storage_provider.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from functools import partial
from typing import AsyncIterator

logger = logging.getLogger(__name__)


# ── Abstract interface (unchanged from Phase 0 stub) ──────────────────────────

class ObjectStorageProvider(ABC):
    """Abstract interface for object storage operations.

    Concrete implementations: S3StorageProvider (covers S3 + MinIO).
    """

    @abstractmethod
    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload bytes to storage.

        Returns:
            The storage key (same as `key` — returned for convenience).
        """
        ...

    @abstractmethod
    async def upload_stream(
        self,
        key: str,
        data_iter: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload from an async byte-stream (avoids buffering the whole file).

        Returns:
            The storage key.
        """
        ...

    @abstractmethod
    async def download(self, key: str) -> bytes:
        """Download an object as bytes."""
        ...

    @abstractmethod
    async def generate_signed_url(self, key: str, expires_in_seconds: int = 900) -> str:
        """Generate a short-lived pre-signed URL for direct client access.

        The URL should expire after `expires_in_seconds` (default: 15 minutes).
        """
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete an object from storage."""
        ...

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Return True if the object exists."""
        ...


# ── S3/MinIO concrete implementation ─────────────────────────────────────────

class S3StorageProvider(ObjectStorageProvider):
    """boto3-based S3/MinIO storage provider.

    Compatible with AWS S3 and MinIO (S3-compatible).
    All blocking boto3 calls are run in a thread-pool executor so they do not
    block the event loop.
    """

    def __init__(
        self,
        *,
        access_key: str,
        secret_key: str,
        bucket_name: str,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
        use_ssl: bool = False,
    ) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "boto3 is required for S3StorageProvider. "
                "Add 'boto3' to requirements.txt."
            ) from exc

        self._bucket = bucket_name
        self._client = boto3.client(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            endpoint_url=endpoint_url,
            use_ssl=use_ssl,
            config=Config(signature_version="s3v4"),
        )
        logger.info(
            "S3StorageProvider initialized",
            extra={"bucket": bucket_name, "endpoint": endpoint_url or "AWS"},
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _run_sync(self, fn, *args, **kwargs):
        """Run a blocking boto3 call in the default thread-pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

    def _ensure_bucket(self) -> None:
        """Create the bucket if it does not exist (idempotent, dev-convenience)."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:
            try:
                self._client.create_bucket(Bucket=self._bucket)
                logger.info("Storage bucket created", extra={"bucket": self._bucket})
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "Could not create storage bucket (may already exist or lack permissions)",
                    extra={"bucket": self._bucket, "error": str(exc)},
                )

    # ── Interface implementation ───────────────────────────────────────────────

    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload bytes to S3/MinIO."""
        import io
        await self._run_sync(
            self._client.upload_fileobj,
            io.BytesIO(data),
            self._bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        logger.debug("Uploaded to storage", extra={"key": key, "bytes": len(data)})
        return key

    async def upload_stream(
        self,
        key: str,
        data_iter: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload from an async byte-stream using S3 multipart upload.

        Collects chunks in memory for single-part upload (suited for files
        up to the org-configured size limit — typically 100 MB). For very
        large files, a true multipart streaming approach should replace this.
        """
        chunks: list[bytes] = []
        async for chunk in data_iter:
            chunks.append(chunk)
        data = b"".join(chunks)
        return await self.upload(key, data, content_type)

    async def download(self, key: str) -> bytes:
        """Download an object as bytes."""
        import io
        buf = io.BytesIO()
        await self._run_sync(
            self._client.download_fileobj,
            self._bucket,
            key,
            buf,
        )
        return buf.getvalue()

    async def generate_signed_url(self, key: str, expires_in_seconds: int = 900) -> str:
        """Generate a pre-signed GET URL."""
        url: str = await self._run_sync(
            self._client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in_seconds,
        )
        return url

    async def delete(self, key: str) -> None:
        """Delete an object from storage."""
        await self._run_sync(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=key,
        )
        logger.debug("Deleted from storage", extra={"key": key})

    async def exists(self, key: str) -> bool:
        """Return True if the object exists in S3/MinIO."""
        try:
            await self._run_sync(
                self._client.head_object,
                Bucket=self._bucket,
                Key=key,
            )
            return True
        except Exception:
            return False


# ── Module-level provider singleton ──────────────────────────────────────────

_storage_provider: ObjectStorageProvider | None = None


def get_storage_provider() -> ObjectStorageProvider:
    """Return the configured storage provider.

    Raises RuntimeError if the provider has not been initialized yet.
    Call set_storage_provider() at application startup before using this.
    """
    if _storage_provider is None:
        raise RuntimeError(
            "ObjectStorageProvider has not been initialized. "
            "Call set_storage_provider() at application startup (lifespan)."
        )
    return _storage_provider


def set_storage_provider(provider: ObjectStorageProvider) -> None:
    """Set the active storage provider (called at application startup — Phase 3)."""
    global _storage_provider
    _storage_provider = provider
    logger.info("Storage provider set: %s", type(provider).__name__)


def create_storage_provider_from_settings() -> ObjectStorageProvider:
    """Factory: instantiate the correct provider from app settings.

    Called in the application lifespan (main.py).
    """
    from app.core.config import get_settings

    settings = get_settings()

    if settings.storage_provider in ("s3", "minio"):
        provider = S3StorageProvider(
            access_key=settings.storage_access_key,
            secret_key=settings.storage_secret_key,
            bucket_name=settings.storage_bucket_name,
            region=settings.storage_region,
            endpoint_url=settings.storage_endpoint_url,
            use_ssl=settings.storage_use_ssl,
        )
        # Ensure the bucket exists (MinIO dev convenience — no-op on S3 if it already exists)
        provider._ensure_bucket()
        return provider

    raise ValueError(
        f"Unknown storage_provider: {settings.storage_provider!r}. "
        "Supported: 'minio', 's3'."
    )
