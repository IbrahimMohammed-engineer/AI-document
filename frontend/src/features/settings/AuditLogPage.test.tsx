/**
 * AuditLogPage tests (Phase 15 §9.2) — filter changes drive new query
 * params; loading skeleton; empty state; 403 gate for non-admins.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useAuditLogs } from '@/hooks/queries/useSettings'
import { useAuthStore } from '@/store/authStore'
import { AuditLogPage } from './AuditLogPage'

vi.mock('@/hooks/queries/useSettings', async (importOriginal) => {
  const original =
    await importOriginal<typeof import('@/hooks/queries/useSettings')>()
  return {
    ...original,
    useAuditLogs: vi.fn(),
  }
})

const mockedUseAuditLogs = vi.mocked(useAuditLogs)

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AuditLogPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function setUser(permissions: string[]) {
  useAuthStore.setState({
    currentUser: {
      id: 'u1',
      email: 'a@b.c',
      full_name: 'Ada',
      organization: { id: 'o1', name: 'Acme', slug: 'acme' },
      permissions,
    },
    accessToken: 'token',
  })
}

beforeEach(() => {
  setUser(['settings:manage'])
})

describe('AuditLogPage', () => {
  it('passes the action filter to the query hook', async () => {
    const user = userEvent.setup()
    mockedUseAuditLogs.mockReturnValue({
      isLoading: false,
      isError: false,
      data: { items: [], total: 0, limit: 50, offset: 0 },
      error: null,
      refetch: vi.fn(),
    } as never)
    renderPage()

    const actionSelect = screen.getByLabelText(/action/i)
    await user.selectOptions(actionSelect, 'DOCUMENT_UPLOADED')

    expect(mockedUseAuditLogs).toHaveBeenLastCalledWith(
      expect.objectContaining({ action: 'DOCUMENT_UPLOADED' }),
    )
  })

  it('shows the loading skeleton while fetching', () => {
    mockedUseAuditLogs.mockReturnValue({
      isLoading: true,
      isError: false,
      data: undefined,
      error: null,
      refetch: vi.fn(),
    } as never)
    renderPage()
    expect(screen.getByLabelText(/audit log filters/i)).toBeInTheDocument()
    expect(document.querySelector('[aria-busy="true"]')).not.toBeNull()
  })

  it('shows the empty state when there are no events', () => {
    mockedUseAuditLogs.mockReturnValue({
      isLoading: false,
      isError: false,
      data: { items: [], total: 0, limit: 50, offset: 0 },
      error: null,
      refetch: vi.fn(),
    } as never)
    renderPage()
    expect(screen.getByText(/no matching events/i)).toBeInTheDocument()
  })

  it('renders events newest-first data as a table with pagination', () => {
    mockedUseAuditLogs.mockReturnValue({
      isLoading: false,
      isError: false,
      data: {
        items: [
          {
            id: 'a1',
            organization_id: 'o1',
            user_id: null,
            user_email: 'a@b.c',
            action: 'DOCUMENT_UPLOADED',
            resource_type: 'document',
            resource_id: null,
            metadata: {},
            ip_address: '10.0.0.1',
            created_at: '2026-01-01T00:00:00Z',
          },
        ],
        total: 60,
        limit: 50,
        offset: 0,
      },
      error: null,
      refetch: vi.fn(),
    } as never)
    renderPage()

    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByText('a@b.c')).toBeInTheDocument()
    expect(screen.getByText(/page 1 of 2/i)).toBeInTheDocument()
  })

  it('blocks non-admin users with PermissionDenied', () => {
    setUser(['document:read'])
    renderPage()
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })
})
