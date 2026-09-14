import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { RouteLoading } from '@/components/layout/RouteLoading'

/**
 * The fallback shown while a route's chunk is in flight.
 *
 * Two properties matter and neither is visual. It must announce the wait ONCE, because it is
 * built from several skeleton bars and a screen reader that read each of them would turn one
 * page load into six interruptions. And it must state nothing about the property, because at
 * this moment the application has not fetched anything -- any figure here would be invented.
 */

describe('RouteLoading', () => {
  it('announces the wait once, not once per placeholder', () => {
    render(<RouteLoading />)

    expect(screen.getAllByRole('status')).toHaveLength(1)
    expect(screen.getByRole('status')).toHaveAccessibleName('Loading the page')
  })

  it('reports no figures while there is nothing to report', () => {
    const { container } = render(<RouteLoading />)

    // No digits at all: a placeholder number here is indistinguishable from a real one.
    expect(container.textContent ?? '').not.toMatch(/\d/)
  })
})
