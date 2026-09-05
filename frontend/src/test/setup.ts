/**
 * Vitest setup (Phase 15 §9.1) — jest-dom matchers + browser-API stubs that
 * jsdom lacks (matchMedia, scrollIntoView, URL.createObjectURL).
 */

import '@testing-library/jest-dom/vitest'

// jsdom has no IntersectionObserver/scroll APIs used by components
Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => {})

if (!('createObjectURL' in URL)) {
  Object.assign(URL, {
    createObjectURL: () => 'blob:mock',
    revokeObjectURL: () => {},
  })
}
