/**
 * UI store (Zustand) — cross-cutting shell preferences (Phase 15).
 *
 * Persisted to localStorage via zustand/middleware persist:
 *  - sidebarCollapsed   — AppShell sidebar collapse (§6.1.1)
 *  - documentViewMode   — Documents library grid/table toggle (§6.3)
 * Transient (never persisted):
 *  - commandPaletteOpen — ⌘K palette open state (§6.10)
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type DocumentViewMode = 'grid' | 'table'

interface UiState {
  sidebarCollapsed: boolean
  commandPaletteOpen: boolean
  documentViewMode: DocumentViewMode

  toggleSidebar: () => void
  setSidebarCollapsed: (collapsed: boolean) => void
  openCommandPalette: () => void
  closeCommandPalette: () => void
  setDocumentViewMode: (mode: DocumentViewMode) => void
}

export const useUiStore = create<UiState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      commandPaletteOpen: false,
      documentViewMode: 'grid',

      toggleSidebar: () =>
        set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
      setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),
      openCommandPalette: () => set({ commandPaletteOpen: true }),
      closeCommandPalette: () => set({ commandPaletteOpen: false }),
      setDocumentViewMode: (mode) => set({ documentViewMode: mode }),
    }),
    {
      name: 'aidoc-ui',
      // Only the shell preferences persist — the palette is ephemeral
      partialize: (state) => ({
        sidebarCollapsed: state.sidebarCollapsed,
        documentViewMode: state.documentViewMode,
      }),
    },
  ),
)
