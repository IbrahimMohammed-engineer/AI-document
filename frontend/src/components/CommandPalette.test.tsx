/**
 * CommandPalette tests (Phase 15 §9.2) — open/close, keyboard navigation,
 * Enter activation, and the ARIA result-count live region.
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { CommandPalette } from './CommandPalette'
import { useUiStore } from '@/store/uiStore'

// Mock data hooks — the palette filters client-side over cached lists
vi.mock('@/hooks/queries/useDocuments', () => ({
  useDocumentList: vi.fn(() => ({
    data: {
      items: [
        {
          id: 'doc-1',
          name: 'Policy 2026',
          document_type: 'policy',
          department: null,
          status: 'active',
          access_level: 'organization',
          owner_id: 'u1',
          current_version_id: null,
          created_at: '',
          updated_at: '',
          tags: [],
          processing_status: 'READY',
          page_count: 3,
        },
      ],
      total: 1,
    },
    isLoading: false,
    isError: false,
  })),
}))

vi.mock('@/hooks/queries/useConversations', () => ({
  useConversations: vi.fn(() => ({
    data: {
      items: [
        {
          id: 'conv-1',
          title: 'Approval process question',
          updated_at: '2026-01-01T00:00:00Z',
        },
      ],
      total: 1,
    },
    isLoading: false,
    isError: false,
  })),
}))

function renderPalette() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CommandPalette />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  useUiStore.setState({ commandPaletteOpen: false })
})

describe('CommandPalette', () => {
  it('renders nothing when closed', () => {
    const { container } = renderPalette()
    expect(container).toBeEmptyDOMElement()
  })

  it('opens, shows sections, and closes on Escape', async () => {
    const user = userEvent.setup()
    const { container } = renderPalette()

    useUiStore.getState().openCommandPalette()
    expect(await screen.findByRole('dialog', { name: /command palette/i })).toBeInTheDocument()
    expect(screen.getByText('Documents')).toBeInTheDocument()
    expect(screen.getByText('Quick Actions')).toBeInTheDocument()

    await user.keyboard('{Escape}')
    await waitFor(() => {
      expect(container).toBeEmptyDOMElement()
    })
  })

  it('filters documents client-side by query', async () => {
    const user = userEvent.setup()
    renderPalette()
    useUiStore.getState().openCommandPalette()

    const input = await screen.findByRole('combobox')
    await user.type(input, 'policy')
    expect(await screen.findByText('Policy 2026')).toBeInTheDocument()

    await user.clear(input)
    await user.type(input, 'zzzz-not-found')
    expect(await screen.findByText(/no matching results/i)).toBeInTheDocument()
  })

  it('navigates with ArrowDown and activates with Enter', async () => {
    const user = userEvent.setup()
    renderPalette()
    useUiStore.getState().openCommandPalette()

    await screen.findByRole('dialog')

    // First item is "Policy 2026" (documents section comes first)
    const firstOption = await screen.findByRole('option', { name: /policy 2026/i })
    expect(firstOption).toHaveAttribute('aria-selected', 'true')

    await user.keyboard('{ArrowDown}')
    const conversationOption = screen.getByRole('option', {
      name: /approval process question/i,
    })
    expect(conversationOption).toHaveAttribute('aria-selected', 'true')

    await user.keyboard('{Enter}')
    // Activation closes the palette
    await waitFor(() => {
      expect(useUiStore.getState().commandPaletteOpen).toBe(false)
    })
  })

  it('announces the result count to screen readers', async () => {
    renderPalette()
    useUiStore.getState().openCommandPalette()
    const live = await screen.findByText(/result/)
    expect(live).toHaveClass('sr-only')
    expect(live.closest('[aria-live="polite"]')).toBeInTheDocument()
  })
})
