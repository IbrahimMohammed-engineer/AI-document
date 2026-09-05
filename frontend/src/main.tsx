/**
 * Application entry point.
 *
 * Wraps the app in:
 *  - BrowserRouter (moved here in Phase 2 so App.tsx can use router hooks)
 *  - QueryClientProvider (React Query)
 *  - ReactQueryDevtools (dev only)
 */
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { ReactQueryDevtools } from '@tanstack/react-query-devtools'
import { queryClient } from '@/lib/query/client'
import { App } from './App'
import './index.css'

// NOTE: the PDF.js GlobalWorkerOptions.workerSrc assignment lives in
// PdfViewer.tsx (lazy route chunk) — configuring it here would pull the
// entire pdfjs-dist library into the initial bundle (§6.14 performance).

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
        {import.meta.env.DEV && <ReactQueryDevtools initialIsOpen={false} />}
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
