# Phase 10 — Citations and Source Validation

## Overview

Phase 10 makes every grounded answer traceable to exact source material — vertical slice **M5**: *answer → citation → source page*.  The backend makes explainability **enforced rather than prompted** (Backend §35–36):

- **Citation extraction + resolution** (`rag/citations.py`) — inline `[1]`/`[1][2]`/`[SOURCE 1]` markers are pattern-matched in the completed answer and resolved through the **backend-owned** `SOURCE N → chunk_id` map built during context assembly (never model memory — the architectural linchpin, Backend §35).  Every citation field (document/version/page/section) round-trips from the `SourceBlock`; `page_id` was threaded through the whole retrieval chain (`document_chunk_repository` → `SearchResult` → `SourceBlock`) to anchor navigation.
- **quoted_text from REAL source text** — chunks ≤ 400 chars are quoted in full; longer chunks yield the sentence with the highest lexical+coverage overlap with the claim, with exact `char_start`/`char_end` offsets into the chunk content plus `context_before`/`after` for the preview popover (Backend §35).  `quoted_text` is never model-paraphrased text mislabeled as a quote.
- **Invalid references are stripped, never persisted** — `[3]` with 1 source is removed from the text and logged as a generation-quality signal (Backend §36).
- **Citation validation** (`rag/citation_validator.py`) — sentence-level claim segmentation (transitional/meta sentences exempt); reference check (every factual sentence needs ≥1 resolvable citation); evidence verification via a **bounded fast-LLM entailment call** per claim–citation pair (`yes/no/partial`, ≤ `citation_entailment_max_checks`=5 per answer, one constrained-schema retry); central-failure detection drives ONE citation-emphasis regeneration (non-streaming, `citation_regeneration_max_retries`=1) — if the retry still fails, claims are dropped rather than shipped uncited.
- **Outcomes** — `grounded` (all claims cited + supported) / `partial` (some stripped, or entailment failed/budget-exhausted → claims kept but never silently passed) / `ungrounded` (everything stripped ⇒ the explicit insufficient-evidence response; zero retrieved chunks upstream behaves as before).
- **Versioned prompts** (`rag/prompts.py`) — `ENTAILMENT_SYSTEM_PROMPT` + template (v1) and `CITATION_EMPHASIS_INSTRUCTION` (v1), appended to the user message by the generator's centralized prompt assembly (never a second system prompt).
- **Atomic persistence** (`repositories/message_repository.py`) — the assistant `messages` row + all `citations` rows flush and commit in ONE transaction (Backend §50): a message with citation markers but no rows (or vice versa) is structurally impossible.  Persisted BEFORE the terminal SSE event so the done payload carries the real `message_id`.
- **Migration 010** — `messages` (DB §21: role CHECK, assistant-only nullable metric columns + CHECK, `groundedness` CHECK; `conversation_id` nullable with the FK arriving in Phase 11's conversations migration) and `citations` (DB §22: denormalized read-path columns, `citation_index`, `quoted_text`, char offsets, `relevance_score`; FK policy **RESTRICT** to document/version/chunk/page — cited evidence cannot be silently deleted — and CASCADE only to the message; indexes on `message_id`, `chunk_id` (reverse lookup), `document_id`).
- **Citation-enriched payloads** (`schemas/ask.py`, `api/ask.py`) — the `done` event now carries `answer` (post-validation text — render THIS, not the raw token stream), `citations[]` (FE §12 Citation contract: index/document/version/page/section/quoted text/offsets/context/effective date/relevance), `message_id`, `regenerated`, `stripped_claims`, `entailment_checks`.  Citations are emitted only AFTER generation + validation — never mid-stream (Backend §37).
- **`GET /documents/{id}/content?version=&page=`** — one page's extracted text + geometry for the citation source panel (org-verified permission path identical to the pages listing).
- **Citation UX** (FE §6.7/§12, the product's most reused pattern) — `CitationBadge` (numbered, focusable, aria-labeled), `SourcePreview` popover (exact span in context, no network), `CitationList` ("Sources" block; multi-citation claims stay independent rows), `CitedAnswer` (inline markers → badges, canonical contract reused everywhere), muted unresolved/source-deleted state; click → Document Workspace `?page=&q=` deep link with the persistent `SourceHighlight` overlay (auto-expand + scroll + `<mark>`).

## Proposed Changes

### Database

#### [NEW] [`010_messages_citations.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/alembic/versions/010_messages_citations.py)

`messages` + `citations` per DB §21–22; RESTRICT FK policy on citations (CASCADE only to the message); CHECK constraints for role/groundedness/assistant-only metrics/citation bounds; indexes `ix_messages_conversation_created`, `ix_citations_message_id`, `ix_citations_chunk_id`, `ix_citations_document_id`.

### Models

#### [NEW] [`models/message.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/models/message.py)

`Message` (assistant metrics + groundedness) and `Citation` (denormalized, FK-anchored provenance chain).

#### [MODIFY] [`alembic/env.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/alembic/env.py)

Registers the new models for autogenerate.

### RAG Layer

#### [NEW] [`rag/citations.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/rag/citations.py)

Pure module: `_CITATION_RE` extraction (`extract_references`), `strip_unresolvable_references` (invalid markers removed + reported), `split_sentences` (offset-preserving; post-marker boundaries are claim boundaries), `find_quoted_span` (full-chunk shortcut ≤ 400 chars; best-overlap sentence with exact offsets + bounded head-span fallback), `resolve_citations` (markers → backend map → `ResolvedCitation`s; claim text = containing sentence).

#### [NEW] [`rag/citation_validator.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/rag/citation_validator.py)

`extract_claims` (transitional sentences exempt), `find_central_regenerate_reason` (>50% factual sentences uncited ⇒ regenerate once), `check_entailment` (fast structured call, ≤5 s timeout, one retry, defensive JSON parse), `validate_answer` (reference check → entailment with budget → strip/keep decisions → `ValidationOutcome` with groundedness + per-claim statuses; `allow_regenerate=False` on the post-retry pass).  Entailment failure ⇒ claim kept as **unverified**, groundedness degrades to partial — never silently passed.

#### [MODIFY] [`rag/prompts.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/rag/prompts.py)

`ENTAILMENT_PROMPT_VERSION`/`ENTAILMENT_SYSTEM_PROMPT`/`ENTAILMENT_USER_TEMPLATE` (v1) and `CITATION_EMPHASIS_INSTRUCTION` (v1).

#### [MODIFY] [`rag/generator.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/rag/generator.py)

`extra_instruction` parameter on `build_generation_messages`/`generate_answer` (the regeneration path is non-streaming by design — its output replaces the streamed draft only if it passes re-validation).

#### [MODIFY] [`rag/context_builder.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/rag/context_builder.py), [`rag/retriever.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/rag/retriever.py), [`rag/hybrid_search.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/rag/hybrid_search.py), [`repositories/document_chunk_repository.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/repositories/document_chunk_repository.py)

`page_id` threaded through both search SQLs and every mapping layer — the citation page anchor.

### Repositories

#### [NEW] [`repositories/message_repository.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/repositories/message_repository.py)

`save_assistant_message_with_citations` — message + citations in ONE transaction (Backend §50); citation column mapping lives in exactly one place.

### Services

#### [MODIFY] [`services/ask_service.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/ask_service.py)

Stages 6–8 of the pipeline: extraction → validation → bounded regeneration (`RegenerationResult`, regeneration tokens folded into the answer's cost budget) → version enrichment (one query: version_number + effective_date) → **atomic persistence before the terminal event** (ungrounded path persists the message too, zero citations).  Failure of persistence never corrupts the stream — the write rolls back whole and is logged.

### API Layer

#### [MODIFY] [`schemas/ask.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/schemas/ask.py)

`AskCitationItem` (the FE §12 contract), `AskSourceItem.page_id`, `AskDonePayload.answer/message_id/citations/regenerated/stripped_claims/entailment_checks` + `citations` latency stage.

#### [MODIFY] [`api/ask.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/api/ask.py)

Done event emits the citation-enriched payload (`model_dump(mode="json")` for the date column).

#### [MODIFY] [`api/documents.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/api/documents.py), [`services/document_service.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/services/document_service.py), [`schemas/document.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/schemas/document.py), [`repositories/document_page_repository.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/repositories/document_page_repository.py)

`GET /documents/{id}/content?version=&page=` → `DocumentPageContentResponse` (404 when the page has no extracted content yet).

### Config

#### [MODIFY] [`config.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/app/core/config.py), [`.env.example`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/.env.example)

`citation_validation_enabled=true`, `citation_quoted_span_max_chars=400`, `citation_entailment_max_checks=5`, `citation_regeneration_max_retries=1`, `citation_regenerate_uncited_ratio=0.5`.

### Frontend

#### [MODIFY] [`lib/api/ask.ts`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/lib/api/ask.ts)

`AskCitation` type; done event carries `answer`/`citations`/`message_id`/validation counters.

#### [NEW] [`features/ask/CitationBadge.tsx`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/features/ask/CitationBadge.tsx), [`SourcePreview.tsx`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/features/ask/SourcePreview.tsx), [`CitationList.tsx`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/features/ask/CitationList.tsx), [`CitedAnswer.tsx`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/features/ask/CitedAnswer.tsx)

The canonical citation interaction contract: badge → popover → navigation; unresolved muted state; independent multi-citation rows.

#### [MODIFY] [`features/ask/AskPage.tsx`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/features/ask/AskPage.tsx), [`ask.css`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/features/ask/ask.css), [`index.ts`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/features/ask/index.ts)

Validated answer replaces the token stream on done; `CitedAnswer` + `CitationList` rendering; citation styles.

#### [MODIFY] [`features/documents/DocumentWorkspace.tsx`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/frontend/src/features/documents/DocumentWorkspace.tsx)

`?page=&q=` deep link: cited page auto-expands + scrolls into view with the persistent `SourceHighlight` overlay on the exact quoted span.

### Tests

- **Unit** [`test_citation_extraction.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/unit/test_citation_extraction.py) (19): marker regex, adjacency/repeats, `[SOURCE N]`, stripping, sentence offsets, quoted-span policies + offset round-trips, resolution through the backend map (the never-fabricate property).
- **Unit** [`test_citation_validator.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/unit/test_citation_validator.py) (19): decision matrix with mocked entailment — supported/partial/unsupported/uncited/unverified; central-regeneration signal; bounded retries; budget enforcement; provider-failure degradation; validation-disabled passthrough; emptied ⇒ ungrounded.
- **Integration** [`test_citations_persistence.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/integration/test_citations_persistence.py) (5): atomic message+citations round-trip; injected FK failure leaves NEITHER row; RESTRICT blocks deleting cited chunks/pages; message delete cascades citations.
- **API** [`test_ask.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/api/test_ask.py) (+5): done payload citations verifiably match the seeded corpus + persisted rows; `[3]` never persisted; unsupported claim → honest ungrounded response; unparseable entailment → partial; central-uncited → exactly one regeneration with the emphasis prompt.
- **Migrations** [`test_migrations.py`](file:///d:/Projects/AI-full-stack-projects/AI-Document-Intelligence/backend/tests/integration/test_migrations.py) (+2): 010 tables/constraints/FK delete rules; `downgrade -1` → `upgrade head` cycle.
- Cleanup order in `conftest.py` and the three pipeline test files puts `citations`/`messages` first (RESTRICT makes cited chunks undeletable while citations exist).

## Verification

- `pytest tests` — **476 passed, 1 skipped** (testcontainers; includes the new suite).
- `ruff check app tests alembic` — no new errors vs baseline (11 pre-existing).
- `mypy app` — 43 errors, identical to baseline.
- `npm run build` (tsc -b + vite) and `npm run lint` — clean.

## Business rules enforced (roadmap Phase 10)

1. Citations are never fabricated — resolution via the backend's own context record; invalid references stripped, never persisted.
2. `quoted_text` is real source text with exact offsets — never model output.
3. Claims without evidentially-supporting citations are stripped or regenerated once — never shipped uncited; fully-stripped answers become the explicit (success-shaped) insufficient-evidence response.
4. Message + citations persist atomically; persisted message carries the groundedness outcome.
5. Cited evidence is undeletable (RESTRICT) until a Phase 16 purge path exists.
