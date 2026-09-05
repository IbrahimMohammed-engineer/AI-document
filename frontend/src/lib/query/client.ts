/**
 * React Query client configuration.
 *
 * - Caches data for 5 minutes (staleTime)
 * - Retries only non-4xx errors (client errors are not transient)
 * - Uses the ApiError class for error handling
 */
import { QueryClient } from '@tanstack/react-query'
import { ApiError } from '@/lib/api/client'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000 * 60 * 5, // 5 minutes
      gcTime: 1000 * 60 * 10,   // 10 minutes
      retry: (failureCount, error) => {
        // Don't retry client errors (4xx)
        if (error instanceof ApiError && error.httpStatus >= 400 && error.httpStatus < 500) {
          return false
        }
        return failureCount < 2
      },
      refetchOnWindowFocus: false,
    },
    mutations: {
      retry: false,
    },
  },
})
