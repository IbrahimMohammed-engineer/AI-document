/**
 * Research Workspace panel store (Zustand, Phase 15 §6.5).
 *
 * Persisted: panel widths + collapsed flags (resizable three-panel layout).
 * Transient: activeCitation — the citation whose source snippet the right-hand
 * EvidencePanel shows. Clicking a citation badge in the transcript sets this
 * INSTEAD of navigating away (Journey 5 — citation without navigation).
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'

import type { AskCitation } from '@/lib/api/ask'

export const PANEL_LIMITS = {
  left: { min: 200, max: 420 },
  right: { min: 280, max: 560 },
} as const

interface PanelState {
  leftWidth: number
  rightWidth: number
  leftCollapsed: boolean
  rightCollapsed: boolean
  activeCitation: AskCitation | null

  setActiveCitation: (citation: AskCitation | null) => void
  setPanelWidth: (panel: 'left' | 'right', width: number) => void
  togglePanel: (panel: 'left' | 'right') => void
}

export const usePanelStore = create<PanelState>()(
  persist(
    (set) => ({
      leftWidth: 280,
      rightWidth: 360,
      leftCollapsed: false,
      rightCollapsed: false,
      activeCitation: null,

      setActiveCitation: (citation) => set({ activeCitation: citation }),
      setPanelWidth: (panel, width) =>
        set({
          [panel === 'left' ? 'leftWidth' : 'rightWidth']: Math.min(
            Math.max(width, PANEL_LIMITS[panel].min),
            PANEL_LIMITS[panel].max,
          ),
        }),
      togglePanel: (panel) =>
        set((state) => ({
          [panel === 'left' ? 'leftCollapsed' : 'rightCollapsed']:
            !state[panel === 'left' ? 'leftCollapsed' : 'rightCollapsed'],
        })),
    }),
    {
      name: 'aidoc-research-panels',
      partialize: (state) => ({
        leftWidth: state.leftWidth,
        rightWidth: state.rightWidth,
        leftCollapsed: state.leftCollapsed,
        rightCollapsed: state.rightCollapsed,
      }),
    },
  ),
)
