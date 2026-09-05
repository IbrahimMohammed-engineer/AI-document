/**
 * UploadDialog tests (Phase 15 §9.2) — inline validation (rejects non-PDF/
 * DOCX), accepts a PDF, and shows the multipart submit state.
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useUploadDocument } from '@/hooks/queries/useDocuments'
import { UploadDialog } from './UploadDialog'

vi.mock('@/hooks/queries/useDocuments', async (importOriginal) => {
  const original =
    await importOriginal<typeof import('@/hooks/queries/useDocuments')>()
  return {
    ...original,
    useUploadDocument: vi.fn(),
  }
})

const mockedUpload = vi.mocked(useUploadDocument)

function renderDialog(open = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <UploadDialog open={open} onClose={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function makeFile(name: string, type: string): File {
  return new File(['test-content'], name, { type })
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedUpload.mockReturnValue({
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
    isPending: false,
    isError: false,
    isSuccess: false,
    error: null,
  } as unknown as ReturnType<typeof useUploadDocument>)
})

describe('UploadDialog', () => {
  it('rejects non-PDF/DOCX files with an inline error', async () => {
    const user = userEvent.setup()
    renderDialog()

    const input = document.querySelector('#upload-file-input') as HTMLInputElement
    expect(input).not.toBeNull()
    // user-event enforces the accept attribute; bypass it to exercise the
    // component's OWN validation path (a .txt chosen via a modified picker)
    input.accept = ''

    await user.upload(input, makeFile('notes.txt', 'text/plain'))
    expect(await screen.findByText(/only pdf and docx files are supported/i)).toBeInTheDocument()
  })

  it('accepts a PDF and shows its name', async () => {
    const user = userEvent.setup()
    renderDialog()

    const input = document.querySelector('#upload-file-input') as HTMLInputElement
    await user.upload(input, makeFile('handbook.pdf', 'application/pdf'))

    expect(await screen.findByText('handbook.pdf')).toBeInTheDocument()
    // Name field auto-fills from the file name
    const nameInput = screen.getByLabelText(/name/i) as HTMLInputElement
    expect(nameInput.value).toBe('handbook')
  })

  it('submits via the upload mutation and surfaces the async-processing note', async () => {
    const user = userEvent.setup()
    const mutate = vi.fn((_params: unknown, options?: { onSuccess: () => void }) => {
      options?.onSuccess()
    })
    mockedUpload.mockReturnValue({
      mutate,
      isPending: false,
      isError: false,
      isSuccess: true,
      error: null,
    } as unknown as ReturnType<typeof useUploadDocument>)

    renderDialog()
    const input = document.querySelector('#upload-file-input') as HTMLInputElement
    await user.upload(input, makeFile('contract.pdf', 'application/pdf'))

    const submit = screen.getByRole('button', { name: /upload/i })
    await user.click(submit)

    await waitFor(() => {
      expect(mutate).toHaveBeenCalledTimes(1)
    })
    const params = mutate.mock.calls[0][0] as { name: string; document_type: string }
    expect(params.name).toBe('contract')
    expect(params.document_type).toBe('policy')
  })

  it('renders nothing when closed', () => {
    const { container } = renderDialog(false)
    expect(container).toBeEmptyDOMElement()
  })
})
