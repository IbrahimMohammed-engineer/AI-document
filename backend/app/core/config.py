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

from pydantic import Field, field_validator
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

    # ─── Background jobs / workers (Phase 4) ─────────────────────────────────
    worker_queue_name: str = Field(
        default="arq:queue",
        description="Which Arq queue this worker process consumes: 'arq:queue' (ingestion) or 'arq:low' (maintenance)",
    )
    worker_max_jobs: int = Field(
        default=4,
        description="Max concurrent jobs per worker process",
    )
    job_default_max_attempts: int = Field(
        default=3,
        description="Fallback retry budget when a job type has no specific policy",
    )
    job_backoff_base_seconds: int = Field(
        default=5,
        description="Base delay for exponential retry backoff (base * 2^(attempt-1))",
    )
    job_backoff_max_seconds: int = Field(
        default=300,
        description="Upper bound on a single retry backoff delay",
    )
    job_stuck_threshold_seconds: int = Field(
        default=600,
        description="PROCESSING jobs untouched for this long are considered crashed and re-enqueued by the sweep",
    )
    reconciliation_sweep_interval_seconds: int = Field(
        default=60,
        description="How often the periodic reconciliation sweep runs",
    )

    # ─── Conflict detection (Phase 13) ────────────────────────────────────────
    # Nightly org-wide conflict-scan cron hour (0-23, server-local wall clock).
    # Settings-driven like every other cron parameter (mirrors
    # reconciliation_sweep_interval_seconds).
    conflict_scan_hour: int = Field(
        default=2,
        ge=0,
        le=23,
        description="Hour of day the nightly CONFLICT_SCAN cron fires",
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

    llm_provider: Literal["openai", "anthropic", "stub"] = "openai"
    llm_model: str = "gpt-4o-mini"

    # ─── LLM generation (Phase 9) ─────────────────────────────────────────────
    # Per-attempt timeouts (Backend §51): ~30 s generation, ~5 s fast
    # classification/rewriting calls.  2 attempts, transient-only (4xx never
    # retried); after N consecutive failures the circuit breaker
    # short-circuits for a cooldown instead of paying for doomed calls.
    llm_generation_timeout_seconds: float = 30.0
    llm_fast_timeout_seconds: float = 5.0
    llm_max_retries: int = 2
    llm_circuit_breaker_threshold: int = 5
    llm_circuit_breaker_cooldown_seconds: int = 60
    # Factual-grounding task, not creative generation — low temperature
    # measurably reduces embellishment beyond evidence (Backend §34).
    llm_generation_temperature: float = 0.1
    # Bounded per response (600–1000 documented band) — cost control + the
    # product favors concise cited answers (Backend §34).
    llm_generation_max_tokens: int = 800
    # Bounded recent history (turns; the generator consumes the last N*2
    # messages) — same rationale as the rewriter's bounded window (Backend §28).
    llm_history_turns: int = 2

    # ─── Context assembly + query rewriting (Phase 9) ─────────────────────────
    # Fixed context token budget for SOURCE content (documented 4,000–6,000
    # band; Backend §33) — sources are added in relevance order until it is
    # reached; the LLM call is never made with an unbounded context.
    context_token_budget: int = 5000
    # Rewriter trigger heuristic (Backend §28): messages with at most this
    # many words are considered potentially context-dependent (prior context
    # existing); longer messages trigger only on demonstrative markers.
    rewriter_trigger_max_words: int = 12
    # Drift guard floor: cosine similarity between the original message and
    # the rewrite below this falls back to the raw message (Backend §28).
    rewriter_similarity_floor: float = 0.5
    # Final sources targeted for context (the documented top 5–8, Backend §31).
    rag_top_k_default: int = 8

    reranker_provider: Literal["cohere", "stub", "none"] = "none"
    reranker_model: str = "rerank-v3.5"
    # Reranker latency is bounded by the candidate count (20–30) — 5 s absorbs
    # provider load spikes; 1 retry then graceful fallback to RRF ordering
    # (Backend §32/§51 — reranker outage degrades ordering, never the request).
    reranker_timeout_seconds: float = 5.0
    reranker_max_retries: int = 1
    # Minimum normalized rerank score ([0,1]) for a candidate to survive.
    # Initial value 0.35 (midpoint of the documented 0.3–0.4 starting band);
    # tuned by Phase 18's evaluation evidence — recorded here per roadmap
    # Phase 8 risk note ("record the initial value and its evidence").
    rerank_score_threshold: float = 0.35
    ocr_provider: Literal["none", "tesseract", "azure_di", "textract"] = "none"

    # ─── Ingestion: extraction + OCR (Phase 5) ────────────────────────────────
    # Per-page scanned-PDF detection: a page whose native text has fewer than
    # this many alphanumeric characters is classified as needing OCR
    # (per-page decision, never per-document — Backend §18).
    ocr_min_page_alnum_chars: int = 25
    # Rasterization resolution for OCR input (Backend §19 recommends ~300 DPI)
    ocr_dpi: int = 300
    # Per-OCR-call timeout in seconds (roadmap Phase 5 infra: ~20s/page)
    ocr_timeout_seconds: float = 20.0
    # Client-level per-page retry budget (roadmap Phase 5 step 10: 2–3×)
    ocr_max_attempts_per_page: int = 3
    # Base delay for the per-page exponential retry backoff (2^attempt * base)
    ocr_retry_backoff_seconds: float = 2.0
    # Tesseract binary path (auto-detected from PATH when None)
    tesseract_cmd: str | None = None
    tesseract_language: str = "eng"
    # Azure Document Intelligence (cloud OCR) — required when ocr_provider=azure_di
    ocr_azure_endpoint: str | None = None
    ocr_azure_api_key: str | None = None
    # Page persistence batching: document_pages rows are written incrementally
    # (every N pages) and committed, so a crash resumes from the last
    # persisted page instead of restarting (Backend §18/§49).
    extraction_page_batch_size: int = 20
    # Size bound on per-page OCR metadata (line boxes) — pathological OCR
    # output must not produce unbounded JSONB (Backend Phase 5 security note)
    ocr_max_metadata_lines: int = 300

    # ─── Ingestion: structure detection + chunking (Phase 6) ─────────────────
    # Tokenizer for chunk budgeting — the encoding matching the platform's
    # default LLM family (Backend §21). Sizes are set conservatively because
    # budget-counting and embedding may tokenize slightly differently.
    tokenizer_encoding: str = Field(
        default="cl100k_base",
        description="tiktoken encoding used for chunk token counting (cl100k_base = OpenAI family)",
    )
    # Target chunk size ~500–800 tokens with a 1,000-token hard maximum
    # (Backend §21) — retrieval-unit granularity + predictable costs.
    chunk_target_min_tokens: int = Field(
        default=500,
        description="Chunks are flushed once content reaches this many tokens",
    )
    chunk_target_max_tokens: int = Field(
        default=800,
        description="Chunks aim to stay under this many tokens (soft budget)",
    )
    chunk_hard_max_tokens: int = Field(
        default=1000,
        description="Absolute maximum tokens per chunk, regardless of structure",
    )
    # Overlap 10–15% between adjacent chunks WITHIN the same section only
    # (Backend §21) — 0.12 of the target band is ~78 tokens.
    chunk_overlap_ratio: float = Field(
        default=0.12,
        description="Overlap fraction of the target band applied when splitting within a section",
    )
    # Chunk persistence batching: document_chunks rows are written in batches
    # (UPSERT) and committed, so a crash resumes without duplicating work.
    chunking_batch_size: int = Field(
        default=200,
        description="Number of chunks per persisted batch during the CHUNKING stage",
    )
    # Hard cap on heading-path depth recorded in chunk metadata (JSONB is
    # size-bounded — pathological documents must not produce unbounded arrays).
    chunk_max_heading_path_depth: int = Field(
        default=10,
        description="Maximum number of ancestor titles stored in chunk metadata heading_path",
    )

    # ─── Ingestion: embedding pipeline (Phase 7) ──────────────────────────────
    # Provider selection: "openai" uses text-embedding-3-small; "stub" is for
    # tests and produces deterministic random vectors (never calls an API).
    embedding_provider: str = Field(
        default="openai",
        description="Embedding provider: 'openai' or 'stub'",
    )
    # Number of chunk texts sent to the provider in a single API call.
    # OpenAI text-embedding-3-small supports up to 2048 inputs per call;
    # 100 is conservative and matches the recommended batch size (Backend §22).
    embedding_batch_size: int = Field(
        default=100,
        description="Chunks per embedding provider call (incremental checkpoint granularity)",
    )
    # Token-bucket ceiling for the shared, cross-worker rate limiter.
    # text-embedding-3-small tier-1 limit is ~3,000 RPM; stay conservative.
    embedding_rate_limit_rpm: int = Field(
        default=2000,
        description="Max embedding requests-per-minute (Redis-backed token bucket, shared across workers)",
    )
    # Per-batch wall-clock timeout.  OpenAI embedding calls complete in <1 s
    # for 100 short texts; 15 s absorbs provider slowdowns and large batches.
    embedding_request_timeout_seconds: float = Field(
        default=15.0,
        description="Per-batch provider call timeout in seconds",
    )

    # ─── Retrieval / vector search (Phase 7) ─────────────────────────────────
    # hnsw.ef_search: higher = better recall, lower = lower latency.
    # Tune upward for the Ask-AI path (higher recall) and downward for
    # latency-sensitive features.  This is a session-level PostgreSQL setting
    # injected before every ANN query, NOT a DDL change (DB §17).
    hnsw_ef_search: int = Field(
        default=100,
        description="pgvector HNSW ef_search (recall/latency trade-off; injected per query session)",
    )
    # Default and maximum top-K for the search endpoint (Phase 7 exposes
    # semantic mode; Phase 8 extends this to hybrid + reranker candidates).
    search_top_k_default: int = Field(
        default=10,
        description="Default number of chunks returned by the search endpoint",
    )
    search_top_k_max: int = Field(
        default=50,
        description="Maximum top-K the client may request; caps the ANN scan",
    )

    # ─── Hybrid search + reranking (Phase 8) ─────────────────────────────────
    # Mode toggle for POST /search (Backend §39): "hybrid" (default) runs
    # vector + FTS branches and fuses them; "semantic" skips the FTS branch;
    # "keyword" skips the vector branch AND reranking (reranking's value is
    # specifically in refining semantically-retrieved candidates).
    search_mode_default: Literal["hybrid", "semantic", "keyword"] = Field(
        default="hybrid",
        description="Default search mode when the request does not specify one",
    )
    # Candidate counts (Backend §31 — tuned by Phase 18's evidence, never
    # hardcoded magic numbers): each branch retrieves its own top-N, the top
    # fused candidates go to the reranker, which narrows to the final top-K.
    hybrid_vector_top_k: int = Field(
        default=50,
        description="Candidates retrieved by the vector (pgvector ANN) branch",
    )
    hybrid_keyword_top_k: int = Field(
        default=50,
        description="Candidates retrieved by the full-text search (tsvector) branch",
    )
    rerank_candidate_count: int = Field(
        default=30,
        description="Maximum fused candidates sent to the reranker (bounded to keep rerank latency predictable)",
    )
    # RRF constant k (DB §18): score = Σ 1/(k + rank).  k=60 dampens the
    # influence of top ranks so a #1 hit on one branch cannot dominate a
    # chunk that both branches agree on.
    rrf_k: int = Field(
        default=60,
        description="Reciprocal Rank Fusion constant k",
    )

    # ─── Citations + source validation (Phase 10) ─────────────────────────────
    # Master switch for the claim-validation stage (Backend §36).  Disabling
    # it degrades to Phase 9 behaviour (extract + resolve only) — used by the
    # evaluation harness to measure validation's effect in isolation.
    citation_validation_enabled: bool = True
    # The whole-chunk quote shortcut: chunks at or under this many characters
    # are quoted in full (char_start=0, char_end=len) — chunks are already
    # sized to a single retrieval-relevant unit (Backend §35), so a span
    # search only pays off on longer chunks.
    citation_quoted_span_max_chars: int = 400
    # Entailment-check budget per answer (Backend §36): the verification is
    # a deliberate, BOUNDED cost — one fast LLM call per claim–citation pair,
    # capped at this many checks per answer (checks beyond the cap are
    # skipped and recorded as unverified → groundedness degrades to partial).
    citation_entailment_max_checks: int = 5
    # Regeneration loop bound (Backend §36): one retry with citation emphasis
    # when central claims are uncited/unsupported — never an unbounded loop.
    citation_regeneration_max_retries: int = 1
    # Central-claim threshold: when MORE than this fraction of factual
    # sentences are uncited (or unsupported), the answer is regenerated with
    # citation emphasis instead of stripping individual sentences.
    citation_regenerate_uncited_ratio: float = 0.5

    # ─── Conversations + streaming (Phase 11) ─────────────────────────────────
    # Keep-alive comment cadence on SSE streams (Backend §37): proxies/LBs
    # time out apparently-idle connections; a silent retrieval/entailment
    # stage emits `: keep-alive` comments at this interval.
    sse_heartbeat_seconds: float = 15.0
    # Processing-SSE correctness fallback: the relay (Redis pub/sub) wakes
    # the stream instantly, and the authoritative processing_jobs state is
    # also re-read on this slow timer so missed pub/sub messages cost at
    # most one interval (Backend §37 — Redis is disposable here).
    stream_poll_seconds: float = 2.0
    # Stop-flag TTL (Backend §37): `POST /chat/messages/{id}/stop` sets a
    # message-scoped key; it expires on its own when the target already
    # finished (or never started) — no manual cleanup, no unbounded growth.
    chat_stop_flag_ttl_seconds: int = 300

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
