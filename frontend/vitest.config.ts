/**
 * Vitest configuration (Phase 15 §9.1).
 *
 * jsdom + @testing-library/react; path aliases mirror vite.config.ts.
 * Setup file registers jest-dom matchers. Coverage excludes test/config
 * files and generated code.
 */

import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: true,
    exclude: ['node_modules/**', 'tests/e2e/**', 'dist/**'],
  },
})
