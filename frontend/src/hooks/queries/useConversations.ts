/**
 * React Query hooks for conversations (Phase 11 — FE §6.6).
 *
 * Query keys per FE §9.1: ['chat', 'conversations'] for the recency list
 * and ['chat', 'conversations', id] for a loaded transcript.  The in-flight
 * streaming turn is optimistic client state (Zustand-free local state in
 * AskPage); the detail query is invalidated when the turn completes so the
 * server-persisted truth replaces the local optimistic rendering.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  deleteConversationApi,
  getConversationApi,
  listConversationsApi,
  rateMessageApi,
  stopMessageApi,
  type ConversationDetailResponse,
  type ConversationListResponse,
} from '@/lib/api/chat'
import { useAuthStore } from '@/store/authStore'

export const conversationKeys = {
  all: () => ['chat', 'conversations'] as const,
  list: (limit: number) => ['chat', 'conversations', { limit }] as const,
  detail: (id: string) => ['chat', 'conversations', id] as const,
}

export function useConversations(limit = 30) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<ConversationListResponse>({
    queryKey: conversationKeys.list(limit),
    enabled: Boolean(accessToken),
    queryFn: () => listConversationsApi(limit),
    staleTime: 5_000,
  })
}

export function useConversation(conversationId: string | null) {
  const accessToken = useAuthStore((s) => s.accessToken)
  return useQuery<ConversationDetailResponse>({
    queryKey: conversationKeys.detail(conversationId ?? 'none'),
    enabled: Boolean(conversationId) && Boolean(accessToken),
    queryFn: () => getConversationApi(conversationId as string),
    staleTime: 30_000,
  })
}

export function useDeleteConversation() {
  const queryClient = useQueryClient()
  return useMutation<void, Error, string>({
    mutationFn: (conversationId: string) => deleteConversationApi(conversationId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: conversationKeys.all() })
    },
  })
}

export function useStopMessage() {
  return useMutation<{ message_id: string; stopped: boolean }, Error, string>({
    mutationFn: (messageId: string) => stopMessageApi(messageId),
  })
}

export function useRateMessage() {
  return useMutation<
    { message_id: string; rating: number; comment: string | null },
    Error,
    { messageId: string; rating: -1 | 1; comment?: string | null }
  >({
    mutationFn: ({ messageId, rating, comment }) =>
      rateMessageApi(messageId, rating, comment),
  })
}
