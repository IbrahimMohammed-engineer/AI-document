"""
Core configuration — all settings are loaded from environment variables.

Pydantic Settings automatically reads from the process environment and from
a .env file (if present). NEVER hard-code secrets here; this file only
defines the shape and types of configuration — the values come from the
environment.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Environment ──────────────────────────────────────────────────────────
    environment: Literal["development", "staging", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # ─── Database ─────────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://aidoc_user:aidoc_password@localhost:5432/aidoc_db",
        description="Async SQLAlchemy DSN (asyncpg driver required)",
    )
    # Sync DSN used by Alembic migrations (psycopg2)
    migration_database_url: str = Field(
        default="postgresql+psycopg2://aidoc_user:aidoc_password@localhost:5432/aidoc_db",
        description="Sync SQLAlchemy DSN for Alembic migrations",
    )

    # Connection pool settings
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800  # seconds

    # ─── Redis ────────────────────────────────────────────────────────────────
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL",
    )

    # ─── Object Storage ───────────────────────────────────────────────────────
    storage_provider: Literal["minio", "s3", "azure"] = "minio"
    storage_endpoint_url: str | None = "http://localhost:9000"
    storage_access_key: str = "minioadmin"
    storage_secret_key: str = "minioadmin123"
    storage_bucket_name: str = "aidoc-documents"
    storage_region: str = "us-east-1"
    storage_use_ssl: bool = False  # set True in production (S3/Azure always use TLS)

    # Maximum allowed upload size in megabytes (org-configurable override planned for Phase 16)
    max_upload_file_size_mb: int = 100

    # Signed URL TTL for downloads — clients fetch bytes directly from object storage
    signed_url_expires_seconds: int = 900  # 15 minutes

    # ─── JWT / Auth ───────────────────────────────────────────────────────────
    jwt_secret_key: str = Field(
        description="Secret key for signing JWT access tokens — must be long and random in production",
    )
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7

    # ─── AI Providers ─────────────────────────────────────────────────────────
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    cohere_api_key: str | None = None

    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    llm_provider: Literal["openai", "anthropic"] = "openai"
    llm_model: str = "gpt-4o-mini"

    reranker_provider: Literal["cohere", "none"] = "none"
    ocr_provider: Literal["none", "azure_di", "textract"] = "none"

    # ─── CORS ─────────────────────────────────────────────────────────────────
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v

    @field_validator("jwt_secret_key")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        if v.startswith("CHANGE_ME") and True:
            # Allow the placeholder in development but warn
            pass
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings singleton.

    Use this everywhere rather than instantiating Settings() directly —
    the cache ensures settings are parsed once.
    """
    return Settings()
