import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TrendChart } from '@/features/dashboard/TrendChart'

const POINTS = [
  { date: '2026-11-01', value: 10 },
  { date: '2026-11-02', value: 20 },
  { date: '2026-11-03', value: 15 },
]

function renderChart() {
  return render(
    <TrendChart
      metric="Occupancy"
      points={POINTS}
      formatValue={(value) => `${value}%`}
      unit="%"
      emptyMessage="No data"
    />,
  )
}

describe('a chart draws at the width of its own box', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('falls back to 720 units where nothing can be measured, as in jsdom', () => {
    renderChart()
    expect(screen.getByRole('img', { name: 'Occupancy trend' })).toHaveAttribute('viewBox', '0 0 720 220')
  })

  it('uses the measured width, so one unit is one pixel and the labels keep their size', () => {
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(312)
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        disconnect() {}
      },
    )
    renderChart()
    expect(screen.getByRole('img', { name: 'Occupancy trend' })).toHaveAttribute('viewBox', '0 0 312 220')
  })

  it('never draws narrower than the stylesheet minimum, below which the card scrolls', () => {
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(150)
    renderChart()
    expect(screen.getByRole('img', { name: 'Occupancy trend' })).toHaveAttribute('viewBox', '0 0 280 220')
  })
})
