"""
DocumentChunk repository — all data access for the ``document_chunks`` table.

Phase 6: UPSERT chunk persistence, list_for_version (debug API).
Phase 7: adds embedding writes (update_embeddings_batch), resumability
  helpers (list_unembedded_for_version, count_embedded_for_version), and the
  permission-enforced ANN semantic search query (semantic_search).
Phase 8: adds the full-text keyword branch (keyword_search — GIN-indexed
  ``content_tsv @@ websearch_to_tsquery``) and metadata-filter support
  (document_type/collection/department/owner) shared by BOTH branches as
  additional WHERE clauses in the same statement (Backend §30).

SECURITY (Backend §29; DB §8):
  ``semantic_search`` and ``keyword_search`` are the retrieval-level
  enforcement points:
    WHERE c.organization_id = :org_id
      AND c.document_version_id = ANY(:version_ids)
      AND [c.embedding IS NOT NULL | c.content_tsv @@ :query]
      AND d.deleted_at IS NULL
  ALL mandatory predicates are inside a SINGLE SQL statement — "search
  everything, filter after" is a security violation that is structurally
  impossible here.  Metadata filters are additional predicates in the SAME
  statement (never a post-retrieval filter, Backend §30).

  Full-text query strings are parameter-bound through PostgreSQL's own
  parsers (websearch_to_tsquery) — SQL-injection structural defense
  (Backend §52).

document_chunks carries a DENORMALIZED organization_id (every retrieval
query in Phase 7+ filters on it directly — DB §8/§16). Writes always supply
it from the owning document; the trg_chunk_org_consistency trigger makes
any drift from the parent document's org structurally impossible (DB §28).

Idempotency (Backend §49; roadmap Phase 6 step 8): chunk persistence is an
UPSERT — INSERT … ON CONFLICT (document_version_id, chunk_index) DO UPDATE —
so re-runs overwrite changed content and never duplicate. Rows beyond the
new chunk count (a re-run producing fewer chunks) are removed by
delete_for_version first, keeping the set exact across re-runs.

See Backend-Architecture-Documentation.md §9 (Repository Layer), §22, §29–31.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.domain.search import SearchFilters
from app.models.document import DocumentChunk
from app.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


# ── Result type returned by semantic_search ───────────────────────────────────

@dataclass
class ChunkSearchResult:
    """One ranked chunk returned by either search branch.

    All fields are denormalized from the JOIN at query time so the API
    layer can build the response without additional round-trips.

    ``similarity`` carries the branch's raw relevance number:
      - semantic branch: cosine similarity (1 − distance), [−1, 1]
      - keyword branch:  raw ``ts_rank`` (unbounded, BM25-like — only the
        ORDER it induces and its relative magnitude matter; the rag layer
        normalizes it for presentation, never for permission logic)
    """

    chunk_id: str
    document_id: str
    document_version_id: str
    document_name: str
    page_id: str
    page_number: int
    section_title: str | None
    chunk_index: int
    content: str
    token_count: int
    # Cosine similarity score (1 − distance); higher = more similar.
    # Stored as a raw float here; the API normalises/presents it.
    similarity: float
    embedding_model: str | None
    metadata: dict[str, Any]


# ── Repository ────────────────────────────────────────────────────────────────

class DocumentChunkRepository(BaseRepository[DocumentChunk]):
    """Repository for the per-version retrieval units."""

    model = DocumentChunk

    # ── Phase 6 reads ─────────────────────────────────────────────────────────

    async def count_for_version(self, document_version_id: str) -> int:
        """Number of chunks persisted for a version."""
        result = await self._session.execute(
            select(func.count(DocumentChunk.id)).where(
                DocumentChunk.document_version_id == document_version_id
            )
        )
        return result.scalar_one()

    async def list_for_version(
        self,
        document_version_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[DocumentChunk]:
        """Chunks in reading order (chunk_index), paginated for the API."""
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.document_version_id == document_version_id)
            .order_by(DocumentChunk.chunk_index)
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_section(
        self,
        section_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[DocumentChunk]:
        """Return chunks belonging to a specific document section (Phase 12).

        Used by the comparison worker to load section text for diffing.
        Results are returned in reading order (chunk_index ascending).
        """
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.section_id == section_id)
            .order_by(DocumentChunk.chunk_index)
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_version_text(
        self,
        document_version_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[DocumentChunk]:
        """Return all chunks for a version in reading order (Phase 12 whole-doc fallback).

        Used by ``_compare_whole_document`` when section alignment is degraded
        and the comparison falls back to a whole-document text diff.
        """
        return await self.list_for_version(
            document_version_id, offset=offset, limit=limit
        )

    # ── Phase 6 writes ────────────────────────────────────────────────────────

    async def delete_for_version(self, document_version_id: str) -> int:
        """Remove all chunks of a version (re-run reset). Does NOT commit."""
        result = await self._session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.document_version_id == document_version_id
            )
        )
        return int(result.rowcount or 0)

    async def upsert_chunks(self, rows: Sequence[dict[str, Any]]) -> int:
        """Bulk-UPSERT chunk rows on (document_version_id, chunk_index).

        Re-runs overwrite — never duplicate (roadmap Phase 6 step 8). Must
        be committed by the caller; the stage commits per batch so a crash
        resumes without duplicating work (Backend §49).
        """
        if not rows:
            return 0
        # Bind to the Table (not the mapped class) — the same reserved-name
        # hazard as insert_pages: a class-bound insert resolves values() keys
        # against class attributes, where "metadata" collides with
        # Base.metadata. The Core insert maps it straight to the column.
        insert_stmt = pg_insert(DocumentChunk.__table__).values(list(rows))
        stmt = insert_stmt.on_conflict_do_update(
            constraint="uq_document_chunks_version_index",
            set_={
                "organization_id": insert_stmt.excluded.organization_id,
                "page_id": insert_stmt.excluded.page_id,
                "end_page_id": insert_stmt.excluded.end_page_id,
                "section_id": insert_stmt.excluded.section_id,
                "content": insert_stmt.excluded.content,
                "content_hash": insert_stmt.excluded.content_hash,
                "token_count": insert_stmt.excluded.token_count,
                "metadata": insert_stmt.excluded["metadata"],
            },
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)

    # ── Phase 7 reads — resumability ──────────────────────────────────────────

    async def list_unembedded_for_version(
        self,
        document_version_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[DocumentChunk]:
        """Chunks whose ``embedding`` column is still NULL, in reading order.

        Used by the embedding stage to resume after a crash: the stage
        tracks how many batches it has written; this query re-derives the
        un-written set from authoritative DB state (Backend §49 — idempotent
        resume, never re-bill completed batches).
        """
        stmt = (
            select(DocumentChunk)
            .where(
                DocumentChunk.document_version_id == document_version_id,
                DocumentChunk.embedding.is_(None),  # type: ignore[attr-defined]
            )
            .order_by(DocumentChunk.chunk_index)
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_embedded_for_version(self, document_version_id: str) -> int:
        """Number of chunks with a non-NULL embedding — progress reporting."""
        result = await self._session.execute(
            select(func.count(DocumentChunk.id)).where(
                DocumentChunk.document_version_id == document_version_id,
                DocumentChunk.embedding.is_not(None),  # type: ignore[attr-defined]
            )
        )
        return result.scalar_one()

    # ── Phase 13 reads — conflict-scan candidate refs ─────────────────────────

    async def list_embedded_chunk_refs_for_version(
        self, document_version_id: str
    ) -> list[dict[str, Any]]:
        """Column-level projection of a version's EMBEDDED chunks (Phase 13).

        Returns chunk_id / document_version_id / page_id / page_number /
        section title+number / content — deliberately WITHOUT the embedding
        payload (candidate generation fetches one chunk's vector lazily via
        :meth:`get_chunk_embedding` only when that chunk becomes the query
        side, keeping the scan's memory bounded).  Ordered by chunk_index.
        """
        from app.models.document import DocumentPage, DocumentSection

        stmt = (
            select(
                DocumentChunk.id.label("chunk_id"),
                DocumentChunk.document_version_id.label("document_version_id"),
                DocumentChunk.page_id.label("page_id"),
                DocumentPage.page_number.label("page_number"),
                DocumentSection.title.label("section_title"),
                DocumentSection.section_number.label("section_number"),
                DocumentChunk.content.label("content"),
            )
            .join(DocumentPage, DocumentPage.id == DocumentChunk.page_id)
            .outerjoin(DocumentSection, DocumentSection.id == DocumentChunk.section_id)
            .where(
                DocumentChunk.document_version_id == document_version_id,
                DocumentChunk.embedding.is_not(None),  # type: ignore[attr-defined]
            )
            .order_by(DocumentChunk.chunk_index)
        )
        result = await self._session.execute(stmt)
        return [dict(row) for row in result.mappings().all()]

    async def get_chunk_embedding(self, chunk_id: str) -> list[float] | None:
        """Fetch one chunk's stored embedding as a float vector (or None).

        The pgvector column is read via CAST(... AS text) — the same
        driver-safe pattern the search queries use — and parsed into
        list[float] so it can be passed back as ``query_vector`` to
        ``semantic_search`` (candidate generation reuses the chunk's own
        vector; no re-embedding).
        """
        result = await self._session.execute(
            text(
                "SELECT CAST(embedding AS text) FROM document_chunks "
                "WHERE id = :chunk_id AND embedding IS NOT NULL"
            ),
            {"chunk_id": chunk_id},
        )
        row = result.first()
        if row is None or row[0] is None:
            return None
        raw = str(row[0]).strip().strip("[]")
        if not raw:
            return None
        try:
            return [float(x) for x in raw.split(",")]
        except ValueError:
            return None

    # ── Phase 7 writes — embedding updates ────────────────────────────────────

    async def update_embeddings_batch(
        self,
        updates: Sequence[dict[str, Any]],
    ) -> int:
        """Write embedding vectors for a batch of chunks.

        Each entry must have: ``chunk_id``, ``embedding`` (list[float]),
        ``embedding_model`` (str).

        Uses individual UPDATE statements per chunk in a batch — simple and
        correct.  At batch sizes of 50–100 this is fast; at much larger
        scales a VALUES(...) bulk approach should be considered but is not
        needed for V1.

        Does NOT commit — caller commits per batch to create resume checkpoints
        (Backend §49 — incremental persistence).
        """
        if not updates:
            return 0

        count = 0
        for row in updates:
            chunk_id = str(row["chunk_id"])
            embedding = row["embedding"]
            embedding_model = row["embedding_model"]

            # Use raw text to set the vector column — SQLAlchemy's type system
            # does not yet natively handle pgvector; cast via ::vector.
            await self._session.execute(
                update(DocumentChunk)
                .where(DocumentChunk.id == chunk_id)
                .values(
                    embedding=func.cast(
                        str(embedding),
                        text("vector"),
                    ),
                    embedding_model=embedding_model,
                )
            )
            count += 1

        return count

    async def update_embeddings_batch_raw(
        self,
        updates: Sequence[dict[str, Any]],
    ) -> int:
        """Write embedding vectors using raw SQL for pgvector compatibility.

        More performant alternative to update_embeddings_batch for large
        batches.  Uses individual parameterized UPDATE statements to avoid
        the SQLAlchemy type system's limitations with pgvector.
        """
        if not updates:
            return 0

        count = 0
        for row in updates:
            chunk_id = str(row["chunk_id"])
            # Convert list[float] to the PostgreSQL vector literal format
            vector_str = "[" + ",".join(str(x) for x in row["embedding"]) + "]"
            embedding_model = row["embedding_model"]

            await self._session.execute(
                text(
                    "UPDATE document_chunks "
                    "SET embedding = CAST(:vec AS vector), embedding_model = :model "
                    "WHERE id = :id"
                ),
                {"vec": vector_str, "model": embedding_model, "id": chunk_id},
            )
            count += 1

        return count

    # ── Phase 8 shared helpers — metadata filters (Backend §30) ──────────────

    @staticmethod
    def _metadata_filter_parts(
        filters: SearchFilters | None,
    ) -> tuple[list[str], dict[str, Any]]:
        """Build additional WHERE fragments + bound params for active filters.

        Only fixed SQL FRAGMENTS are conditionally appended — every value is
        a bound parameter, never interpolated into the SQL text (Backend §52).
        All fragments reference the ``d`` (documents) alias present in both
        search queries, so the same filter set applies identically to the
        vector and keyword branches (Backend §30 — one filter semantics).
        """
        if filters is None or filters.is_empty:
            return [], {}

        clauses: list[str] = []
        params: dict[str, Any] = {}

        if filters.document_types:
            clauses.append("d.document_type = ANY(CAST(:filter_document_types AS text[]))")
            params["filter_document_types"] = list(filters.document_types)

        if filters.department:
            clauses.append("d.department = :filter_department")
            params["filter_department"] = filters.department

        if filters.owner_id:
            clauses.append("d.owner_id = CAST(:filter_owner_id AS uuid)")
            params["filter_owner_id"] = filters.owner_id

        if filters.collection_ids:
            # EXISTS keeps this a pure predicate inside the same statement —
            # no JOIN fan-out (a document in multiple collections must not
            # duplicate its chunks in the result set).
            clauses.append(
                "EXISTS (SELECT 1 FROM collection_documents cd "
                "WHERE cd.document_id = d.id "
                "AND cd.collection_id = ANY(CAST(:filter_collection_ids AS uuid[])))"
            )
            params["filter_collection_ids"] = list(filters.collection_ids)

        return clauses, params

    # ── Phase 7 reads — semantic search (the retrieval-level enforcement point)

    async def semantic_search(
        self,
        *,
        organization_id: str,
        version_ids: list[str],
        query_vector: list[float],
        top_k: int = 50,
        hnsw_ef_search: int = 100,
        filters: SearchFilters | None = None,
        exclude_document_id: str | None = None,
    ) -> list[ChunkSearchResult]:
        """ANN cosine similarity search with mandatory permission predicates.

        SECURITY CONTRACT (Backend §29; roadmap Phase 7 step 7):
          ALL of these predicates are inside ONE SQL statement — they are not
          applied as a post-processing filter:
            1. c.organization_id = :org_id       — tenant isolation
            2. c.document_version_id = ANY(:ids) — scope/permission boundary
            3. c.embedding IS NOT NULL            — only indexed chunks
            4. d.deleted_at IS NULL               — exclude soft-deleted docs
          Phase 8 metadata filters (Backend §30) are additional predicates
          in the SAME statement — one query plan, no post-filtering.

        Args:
            organization_id: The requesting user's organization (from JWT,
                             never from the request body).
            version_ids:      Allowed version IDs from resolve_allowed_documents().
                             Empty list → caller must short-circuit before calling.
            query_vector:     The embedded query; must be 1536-dimensional.
            top_k:           Number of results to return (default 50; the
                             reranker in Phase 8 narrows this further).
            hnsw_ef_search:  Session-level ef_search for recall/latency tuning.
            filters:         Optional metadata filters (document_type /
                             collection / department / owner).
            exclude_document_id: Optional document ID whose chunks are excluded
                             entirely (Phase 13 candidate generation — "similar
                             chunks in a DIFFERENT document").  One additional
                             bound predicate in the same statement.

        Returns:
            Ranked list of ChunkSearchResult, highest similarity first.

        Raises:
            ValueError: If version_ids is empty (caller should short-circuit).
        """
        if not version_ids:
            raise ValueError(
                "semantic_search called with empty version_ids — "
                "the caller must short-circuit before reaching the retriever."
            )

        # Inject the ef_search session setting before the ANN query so the
        # HNSW index is used with the correct recall budget (DB §17).
        # set_config(..., is_local => true) is the parameter-bound,
        # transaction-scoped equivalent of SET LOCAL (utility statements
        # cannot take bind parameters).
        await self._session.execute(
            text("SELECT set_config('hnsw.ef_search', :value, true)"),
            {"value": str(hnsw_ef_search)},
        )

        # Convert the query vector to the pgvector literal format.
        vector_str = "[" + ",".join(str(x) for x in query_vector) + "]"

        filter_clauses, filter_params = self._metadata_filter_parts(filters)
        if exclude_document_id is not None:
            filter_clauses = [*filter_clauses, "d.id <> :exclude_document_id"]
            filter_params["exclude_document_id"] = exclude_document_id
        filter_sql = (" AND " + " AND ".join(filter_clauses)) if filter_clauses else ""

        sql = text(
            """
            SELECT
                c.id                    AS chunk_id,
                d.id                    AS document_id,
                c.document_version_id   AS document_version_id,
                d.name                  AS document_name,
                c.page_id               AS page_id,
                p.page_number           AS page_number,
                s.title                 AS section_title,
                c.chunk_index           AS chunk_index,
                c.content               AS content,
                c.token_count           AS token_count,
                c.embedding_model       AS embedding_model,
                c.metadata              AS metadata,
                1 - (c.embedding <=> CAST(:query_vec AS vector))  AS similarity
            FROM document_chunks c
            JOIN document_versions v ON v.id = c.document_version_id
            JOIN documents d         ON d.id = v.document_id
            JOIN document_pages p    ON p.id = c.page_id
            LEFT JOIN document_sections s ON s.id = c.section_id
            WHERE
                c.organization_id = :org_id
                AND c.document_version_id = ANY(CAST(:version_ids AS uuid[]))
                AND c.embedding IS NOT NULL
                AND d.deleted_at IS NULL
                {filters}
            ORDER BY c.embedding <=> CAST(:query_vec AS vector)
            LIMIT :top_k
            """.format(filters=filter_sql)
        )

        params: dict[str, Any] = {
            "query_vec": vector_str,
            "org_id": organization_id,
            "version_ids": list(version_ids),
            "top_k": top_k,
            **filter_params,
        }
        result = await self._session.execute(sql, params)
        rows = result.mappings().all()

        return [
            ChunkSearchResult(
                chunk_id=str(row["chunk_id"]),
                document_id=str(row["document_id"]),
                document_version_id=str(row["document_version_id"]),
                document_name=str(row["document_name"]),
                page_id=str(row["page_id"]),
                page_number=int(row["page_number"]),
                section_title=row["section_title"],
                chunk_index=int(row["chunk_index"]),
                content=str(row["content"]),
                token_count=int(row["token_count"]),
                similarity=float(row["similarity"]),
                embedding_model=row["embedding_model"],
                metadata=dict(row["metadata"]) if row["metadata"] else {},
            )
            for row in rows
        ]

    # ── Phase 8 reads — full-text keyword search (second retrieval branch) ────

    async def keyword_search(
        self,
        *,
        organization_id: str,
        version_ids: list[str],
        query_text: str,
        top_k: int = 50,
        filters: SearchFilters | None = None,
    ) -> list[ChunkSearchResult]:
        """Full-text keyword search over the GIN-indexed ``content_tsv``.

        The keyword branch of hybrid search (Backend §31; DB §18).  The
        query string is parsed by PostgreSQL's forgiving natural-language
        parser (``websearch_to_tsquery`` — supports quoted phrases and
        OR/-exclusion syntax and never raises on user input) and is ALWAYS
        a bound parameter — SQL-injection structural defense (Backend §52).

        SECURITY CONTRACT — identical mandatory predicates to
        ``semantic_search``, inside ONE SQL statement:
            1. c.organization_id = :org_id       — tenant isolation
            2. c.document_version_id = ANY(:ids) — scope/permission boundary
            3. c.content_tsv @@ :query           — the match itself
            4. d.deleted_at IS NULL               — exclude soft-deleted docs
          plus the same metadata-filter predicates (Backend §30) — one
          scope resolution feeds both branches with identical semantics.

        Args:
            organization_id: Requesting user's organization (from JWT).
            version_ids:     Allowed version IDs; empty → ValueError
                             (caller must short-circuit).
            query_text:      The raw query string (plain user text).
            top_k:           Number of results to return.
            filters:         Optional metadata filters.

        Returns:
            Ranked list of ChunkSearchResult, highest ``ts_rank`` first.
            ``similarity`` carries the RAW ts_rank (unbounded) — the rag
            layer normalizes for presentation only.
        """
        if not version_ids:
            raise ValueError(
                "keyword_search called with empty version_ids — "
                "the caller must short-circuit before reaching the retriever."
            )

        filter_clauses, filter_params = self._metadata_filter_parts(filters)
        filter_sql = (" AND " + " AND ".join(filter_clauses)) if filter_clauses else ""

        sql = text(
            """
            SELECT
                c.id                    AS chunk_id,
                d.id                    AS document_id,
                c.document_version_id   AS document_version_id,
                d.name                  AS document_name,
                c.page_id               AS page_id,
                p.page_number           AS page_number,
                s.title                 AS section_title,
                c.chunk_index           AS chunk_index,
                c.content               AS content,
                c.token_count           AS token_count,
                c.embedding_model       AS embedding_model,
                c.metadata              AS metadata,
                ts_rank(c.content_tsv, websearch_to_tsquery('english', :query_text)) AS rank
            FROM document_chunks c
            JOIN document_versions v ON v.id = c.document_version_id
            JOIN documents d         ON d.id = v.document_id
            JOIN document_pages p    ON p.id = c.page_id
            LEFT JOIN document_sections s ON s.id = c.section_id
            WHERE
                c.organization_id = :org_id
                AND c.document_version_id = ANY(CAST(:version_ids AS uuid[]))
                AND c.content_tsv @@ websearch_to_tsquery('english', :query_text)
                AND d.deleted_at IS NULL
                {filters}
            ORDER BY rank DESC
            LIMIT :top_k
            """.format(filters=filter_sql)
        )

        params: dict[str, Any] = {
            "query_text": query_text,
            "org_id": organization_id,
            "version_ids": list(version_ids),
            "top_k": top_k,
            **filter_params,
        }
        result = await self._session.execute(sql, params)
        rows = result.mappings().all()

        return [
            ChunkSearchResult(
                chunk_id=str(row["chunk_id"]),
                document_id=str(row["document_id"]),
                document_version_id=str(row["document_version_id"]),
                document_name=str(row["document_name"]),
                page_id=str(row["page_id"]),
                page_number=int(row["page_number"]),
                section_title=row["section_title"],
                chunk_index=int(row["chunk_index"]),
                content=str(row["content"]),
                token_count=int(row["token_count"]),
                similarity=float(row["rank"]),
                embedding_model=row["embedding_model"],
                metadata=dict(row["metadata"]) if row["metadata"] else {},
            )
            for row in rows
        ]
