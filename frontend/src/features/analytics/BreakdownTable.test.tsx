import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { BreakdownTable, type BreakdownRow } from '@/features/analytics/BreakdownTable'
import { expectNoAxeViolations } from '@/test/axe'

/**
 * The breakdown table's box, and the fix for the A9 audit's recorded defect (2026-11-27).
 *
 * The layout half (the table fits its card from 768px, and the cards share a line only when
 * both tables fit) needs a real browser, and the CI responsive audit holds it: five table
 * checks at 375, 768, 1024, 1280 and 1440px (frontend/e2e/responsive-checks.mjs). jsdom has
 * no layout, so these tests hold the other half with stubbed geometry:
 *
 * * every column and every figure is in the table, the category's tag included;
 * * while the table is wider than its box, the box is a focusable region named by the
 *   table's caption, so a keyboard can reach it and scroll it;
 * * while the table fits, the box is a plain container with no tab stop;
 * * a resize moves the box from one state to the other.
 */

const ROWS: BreakdownRow[] = [
  { category_code: 'MAINTENANCE', currency: 'EUR', amount: '1441.96', tax_amount: '346.07', entry_count: 2, flag: false },
  { category_code: 'PAYROLL', currency: 'EUR', amount: '49000.00', tax_amount: '0.00', entry_count: 5, flag: true },
]

const CAPTION = 'Ledger expenses, 30 Aug – 27 Nov 2026'

/** The geometry jsdom lacks: every element is `scrollWidth` wide inside `clientWidth`. */
let geometry = { scrollWidth: 0, clientWidth: 0 }
/** The observer callbacks the component registered, to replay a resize. */
let observers: Array<() => void> = []

beforeEach(() => {
  geometry = { scrollWidth: 0, clientWidth: 0 }
  observers = []
  vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockImplementation(() => geometry.scrollWidth)
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => geometry.clientWidth)
  vi.stubGlobal(
    'ResizeObserver',
    class {
      private readonly callback: () => void
      constructor(callback: () => void) {
        this.callback = callback
        observers.push(callback)
      }
      observe() {}
      disconnect() {
        observers = observers.filter((c) => c !== this.callback)
      }
    },
  )
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function renderTable() {
  return render(
    <BreakdownTable caption={CAPTION} rows={ROWS} flagLabel="Fixed cost" emptyMessage="None." />,
  )
}

describe('the breakdown table', () => {
  it('keeps every column and every figure, the tag beside its code', () => {
    renderTable()
    const table = screen.getByRole('table', { name: CAPTION })
    const headers = within(table)
      .getAllByRole('columnheader')
      .map((th) => th.textContent)
    expect(headers).toEqual(['Category', 'Currency', 'Amount', 'Tax', 'Entries'])
    const payroll = within(table).getByRole('rowheader', { name: /PAYROLL/ })
    expect(within(payroll).getByText('Fixed cost')).toBeInTheDocument()
    for (const figure of ['EUR 49000.00', '0.00', '5', 'EUR 1441.96', '346.07', '2']) {
      expect(within(table).getByText(figure)).toBeInTheDocument()
    }
  })

  it('is a plain container with no tab stop while the table fits its box', () => {
    geometry = { scrollWidth: 482, clientWidth: 482 }
    renderTable()
    const box = screen.getByRole('table', { name: CAPTION }).parentElement as HTMLElement
    expect(box).not.toHaveAttribute('tabindex')
    expect(box).not.toHaveAttribute('role')
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
  })

  it('is a focusable region named by its caption while the table is wider than its box', async () => {
    geometry = { scrollWidth: 462, clientWidth: 261 }
    const { container } = renderTable()
    const region = screen.getByRole('region', { name: CAPTION })
    expect(region).toHaveAttribute('tabindex', '0')
    expect(within(region).getByRole('table', { name: CAPTION })).toBeInTheDocument()
    await userEvent.tab()
    expect(region).toHaveFocus()
    await expectNoAxeViolations(container)
  })

  it('follows a resize from scrolling to fitting and back', () => {
    geometry = { scrollWidth: 462, clientWidth: 261 }
    renderTable()
    expect(screen.getByRole('region', { name: CAPTION })).toBeInTheDocument()
    geometry = { scrollWidth: 482, clientWidth: 482 }
    act(() => observers.forEach((callback) => callback()))
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
    geometry = { scrollWidth: 533, clientWidth: 482 }
    act(() => observers.forEach((callback) => callback()))
    expect(screen.getByRole('region', { name: CAPTION })).toBeInTheDocument()
  })

  it('renders its message, and no box, when there are no rows', () => {
    render(<BreakdownTable caption={CAPTION} rows={[]} flagLabel="Fixed cost" emptyMessage="None." />)
    expect(screen.getByRole('status')).toHaveTextContent('None.')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })
})
