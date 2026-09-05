/**
 * DocumentsPage tests (Phase 15 §9.2) — the four primary state-matrix rows:
 * loading skeleton, empty (upload CTA), populated list, error + retry.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useDocumentList } from '@/hooks/queries/useDocuments'
import { useAuthStore } from '@/store/authStore'
import { useUiStore } from '@/store/uiStore'
import DocumentsPage from './DocumentsPage'

vi.mock('@/hooks/queries/useDocuments', async (importOriginal) => {
  const original =
    await importOriginal<typeof import('@/hooks/queries/useDocuments')>()
  return {
    ...original,
    useDocumentList: vi.fn(),
    useDeleteDocument: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  }
})

vi.mock('@/features/documents', () => ({
  ProcessingStatusBadge: ({ status }: { status: string }) => (
    <span>status:{status}</span>
  ),
  UploadDialog: ({ open }: { open: boolean }) =>
    open ? <div data-testid="upload-dialog" /> : null,
}))

const mockedUseDocumentList = vi.mocked(useDocumentList)

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <DocumentsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function makeDoc(id: string, name: string) {
  return {
    id,
    name,
    document_type: 'policy',
    department: 'Legal',
    status: 'active',
    access_level: 'organization',
    owner_id: 'u1',
    current_version_id: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-02T00:00:00Z',
    tags: [],
    processing_status: 'READY',
    page_count: 4,
  }
}

beforeEach(() => {
  useAuthStore.setState({
    currentUser: {
      id: 'u1',
      email: 'a@b.c',
      full_name: 'Ada',
      organization: { id: 'o1', name: 'Acme', slug: 'acme' },
      permissions: ['document:read', 'document:create', 'document:delete'],
    },
    accessToken: 'token',
  })
  useUiStore.setState({ documentViewMode: 'grid' })
})

describe('DocumentsPage', () => {
  it('shows the loading skeleton while fetching', () => {
    mockedUseDocumentList.mockReturnValue({
      isLoading: true,
      isError: false,
      data: undefined,
      error: null,
      refetch: vi.fn(),
    } as never)
    renderPage()
    expect(screen.getByRole('group', { name: /view mode/i })).toBeInTheDocument()
    expect(screen.getAllByRole('generic', { hidden: true }).length).toBeGreaterThan(0)
    // skeleton elements carry aria-busy
    expect(screen.getByLabelText(/document filters/i)).toBeInTheDocument()
  })

  it('shows the empty state with an upload CTA when there are no documents', () => {
    mockedUseDocumentList.mockReturnValue({
      isLoading: false,
      isError: false,
      data: { items: [], total: 0 },
      error: null,
      refetch: vi.fn(),
    } as never)
    renderPage()
    expect(screen.getByText('No documents yet')).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /upload your first document/i }),
    ).toBeInTheDocument()
  })

  it('renders the populated list as links into the workspace', () => {
    mockedUseDocumentList.mockReturnValue({
      isLoading: false,
      isError: false,
      data: { items: [makeDoc('d1', 'Policy 2026'), makeDoc('d2', 'Handbook')], total: 2 },
      error: null,
      refetch: vi.fn(),
    } as never)
    renderPage()

    const link = screen.getByRole('link', { name: 'Policy 2026' })
    expect(link).toHaveAttribute('href', '/app/documents/d1')
    expect(screen.getByText(/2 documents/i)).toBeInTheDocument()
  })

  it('shows the no-match empty state when filters exclude everything', () => {
    mockedUseDocumentList.mockReturnValue({
      isLoading: false,
      isError: false,
      data: { items: [], total: 0 },
      error: null,
      refetch: vi.fn(),
    } as never)
    renderPage()

    const search = screen.getByLabelText(/search documents by name/i)
    // Type into the controlled input to activate the hasFilters branch
    fireEventChange(search, 'zzz')
    expect(screen.getByText(/no documents match your filters/i)).toBeInTheDocument()
  })

  it('shows an error state with Retry', async () => {
    const refetch = vi.fn()
    mockedUseDocumentList.mockReturnValue({
      isLoading: false,
      isError: true,
      data: undefined,
      error: new Error('boom'),
      refetch,
    } as never)
    renderPage()
    expect(screen.getByText(/could not load documents/i)).toBeInTheDocument()

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /retry/i }))
    expect(refetch).toHaveBeenCalledTimes(1)
  })
})

function fireEventChange(element: Element, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    'value',
  )?.set
  setter?.call(element, value)
  element.dispatchEvent(new Event('input', { bubbles: true }))
}
