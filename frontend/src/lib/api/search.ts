/**
 * Typed API client for hybrid search (Phase 15 §6.6).
 *
 * Matches backend/app/schemas/search.py EXACTLY (the endpoint is
 * POST /search — the Phase 15 plan's GET /search shorthand does not exist).
 *
 * Field-name mapping from the plan's sketch → the REAL backend contract:
 *   version_id            → document_version_id
 *   content               → snippet (first 500 chars of the chunk)
 *   score                 → relevance (normalized presentation score [0,1])
 *   highlighted_content   → absent (client highlights from the query)
 */

import { post } from './client'

// ─── Types ────────────────────────────────────────────────────────────────────

export type SearchMode = 'hybrid' | 'semantic' | 'keyword'

/** Scope narrowing — one of document_ids / collection_ids, or neither. */
export interface SearchScope {
  document_ids?: string[]
  collection_ids?: string[]
}

/** Metadata filters (ANDed inside the retrieval SQL). */
export interface SearchFilters {
  document_types?: string[]
  collection_ids?: string[]
  department?: string
  owner_id?: string
}

export interface SearchRequestBody {
  query: string
  mode?: SearchMode
  scope?: SearchScope | null
  filters?: SearchFilters | null
  top_k?: number
}

export interface SearchResultItem {
  chunk_id: string
  document_id: string
  document_version_id: string
  document_name: string
  page_number: number
  section_title: string | null
  chunk_index: number
  snippet: string
  relevance: number
  metadata: Record<string, unknown>
}

export interface SearchResponse {
  results: SearchResultItem[]
  total: number
  query: string
  scope_kind: 'all' | 'documents' | 'collections'
  mode: SearchMode
  used_reranker: boolean
  message: string | null
}

// ─── Endpoint ─────────────────────────────────────────────────────────────────

export async function searchDocuments(body: SearchRequestBody): Promise<SearchResponse> {
  return post<SearchResponse>('/search', body)
}
