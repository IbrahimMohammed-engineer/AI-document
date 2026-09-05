/**
 * PermissionDenied component test (Phase 15 §9.2) — renders with/without a
 * custom message and exposes the alert + back link.
 */

import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { PermissionDenied } from './PermissionDenied'

function renderWithRouter(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>)
}

describe('PermissionDenied', () => {
  it('renders the default message', () => {
    renderWithRouter(<PermissionDenied />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/don't have permission to view this/i)).toBeInTheDocument()
  })

  it('renders a custom message', () => {
    renderWithRouter(<PermissionDenied message="Admins only." />)
    expect(screen.getByText('Admins only.')).toBeInTheDocument()
  })

  it('links back to the dashboard', () => {
    renderWithRouter(<PermissionDenied />)
    const link = screen.getByRole('link', { name: /back to dashboard/i })
    expect(link).toHaveAttribute('href', '/app/dashboard')
  })
})
