/**
 * A9: the responsive audit's specification and verdicts (responsive-checks.mjs).
 *
 * The browser half -- that the 51 checks pass on the real stack -- runs in CI's "Responsive
 * audit" job. These tests hold the definition it runs: that it is UI-10's 17 routes at UI-10's
 * three widths, that each of the three conditions fails a check on its own, that the
 * measurement's clipping rule and tolerance are UI-10's, and that a short or failing run is
 * never reported as a pass. jsdom has no layout, so the measurement is driven with stubbed
 * geometry.
 *
 * The table checks (since 2026-11-27) are held here too: the Analytics breakdown tables at five
 * widths, which must fit their boxes from 768px and may scroll below only in a focusable,
 * named region that the keyboard scrolls to the last column. The first of them is the
 * recorded defect itself, a 525px expenses table in a 482px box at 1440px, which must fail.
 */

import { afterEach, describe, expect, it } from 'vitest'

import {
  BREAKDOWN_COLUMNS,
  BREAKDOWN_TABLES,
  CHECK_COUNT,
  FIT_FROM_PX,
  HEIGHT,
  NOT_FOUND_ROUTE,
  READY_POLLS,
  READY_POLL_MS,
  ROUTES,
  SETTLE_MS,
  TABLE_CHECK_COUNT,
  TABLE_ROUTE,
  TABLE_WIDTHS,
  TOLERANCE_PX,
  WIDTHS,
  evaluate,
  evaluateTables,
  measure,
  measureTables,
  plan,
  summarise,
  summariseTables,
} from './responsive-checks.mjs'

const IDS = { booking: 'b-1', guest: 'g-1', roomType: 'DBL', roomNumber: '101' }

/** A measurement that passes every condition for `check`. */
function clean(check) {
  return { path: check.path, clientWidth: check.width, docScrollWidth: check.width, escaping: [], h1Count: 1, notFound: false }
}

describe('the specification', () => {
  it('is UI-10s 17 routes at 375, 768 and 1440px: 51 checks', () => {
    expect(ROUTES).toEqual([
      '/', '/bookings', '/bookings/:booking', '/financials', '/reviews', '/guests',
      '/guests/:guest', '/rooms', '/rooms/:roomType/:roomNumber', '/availability', '/property',
      '/administration', '/platform', '/analytics', '/intelligence', '/copilot', '/nowhere',
    ])
    expect(WIDTHS).toEqual([375, 768, 1440])
    expect(HEIGHT).toBe(900)
    expect(CHECK_COUNT).toBe(51)
    expect(NOT_FOUND_ROUTE).toBe('/nowhere')
  })

  it('keeps UI-10s readiness wait and tolerance', () => {
    expect([READY_POLL_MS, READY_POLLS, SETTLE_MS, TOLERANCE_PX]).toEqual([250, 24, 300, 0.5])
  })

  it('plans every route at every width once, with the detail routes resolved', () => {
    const checks = plan(IDS)
    expect(checks).toHaveLength(51)
    expect(new Set(checks.map((c) => `${c.width} ${c.route}`)).size).toBe(51)
    expect(checks.map((c) => c.path)).toContain('/rooms/DBL/101')
    expect(checks.map((c) => c.path)).toContain('/bookings/b-1')
    expect(checks.filter((c) => c.width === 375)).toHaveLength(17)
  })

  it('refuses to plan a detail route the seeded data has no record for', () => {
    expect(() => plan({ ...IDS, guest: '' })).toThrow(/no value for :guest/)
  })
})

describe('the verdict', () => {
  const check = { route: '/bookings', path: '/bookings', width: 375 }

  it('passes a measurement that meets all three conditions', () => {
    expect(evaluate(check, clean(check))).toEqual([])
  })

  it('fails a page that scrolls sideways', () => {
    expect(evaluate(check, { ...clean(check), docScrollWidth: 380 })).toEqual([
      'sideways scroll: the document is 380px wide in a 375px viewport',
    ])
  })

  it('fails a page with an escaping element', () => {
    const problems = evaluate(check, { ...clean(check), escaping: ['div.kpiGrid right=410'] })
    expect(problems).toEqual(['escapes the viewport: div.kpiGrid right=410'])
  })

  it('fails a page with no h1', () => {
    expect(evaluate(check, { ...clean(check), h1Count: 0 })).toEqual(['0 h1 elements, not exactly one'])
  })

  it('fails a page with two h1 elements', () => {
    expect(evaluate(check, { ...clean(check), h1Count: 2 })).toEqual(['2 h1 elements, not exactly one'])
  })

  it('does not count a redirect to sign-in as the page', () => {
    expect(evaluate(check, { ...clean(check), path: '/login' })).toEqual([
      'not the page asked for: the browser is at /login',
    ])
  })

  it('does not count the not-found page except on the unknown route', () => {
    expect(evaluate(check, { ...clean(check), notFound: true })).toEqual(['rendered the not-found page'])
    const unknown = { route: '/nowhere', path: '/nowhere', width: 768 }
    expect(evaluate(unknown, { ...clean(unknown), notFound: true })).toEqual([])
  })

  it('passes a run only when all 51 checks ran and passed', () => {
    const passing = plan(IDS).map((c) => ({ ...c, problems: [] }))
    expect(summarise(passing).ok).toBe(true)
    expect(summarise(passing.slice(1)).ok).toBe(false)
    const oneFails = passing.map((r, i) => (i === 7 ? { ...r, problems: ['x'] } : r))
    const verdict = summarise(oneFails)
    expect(verdict.ok).toBe(false)
    expect(verdict.failed).toBe(1)
    expect(verdict.lines.at(-1)).toBe('50 of 51 checks passed')
  })
})

describe('the in-page measurement', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  /** A viewport `width` wide, and `main > box > table` with the given right edges. */
  function layout(width, { tableRight, boxRight, boxOverflow, mainOverflow }) {
    Object.defineProperty(document.documentElement, 'clientWidth', { configurable: true, value: width })
    Object.defineProperty(document.documentElement, 'scrollWidth', { configurable: true, value: width })
    document.body.innerHTML = '<main><h1>Title</h1><div class="_box_x"><table class="_wide_y"></table></div></main>'
    const main = document.querySelector('main')
    const box = document.querySelector('div')
    const table = document.querySelector('table')
    main.style.overflowX = mainOverflow
    if (boxOverflow) box.style.overflowX = boxOverflow
    const rect = (right) => () => ({ left: 0, right, width: right, top: 0, bottom: 10, height: 10 })
    main.getBoundingClientRect = rect(width)
    box.getBoundingClientRect = rect(boxRight)
    table.getBoundingClientRect = rect(tableRight)
  }

  it('reports an element past the viewport that nothing clips', () => {
    layout(375, { tableRight: 376, boxRight: 375, mainOverflow: 'visible' })
    const m = measure(TOLERANCE_PX)
    expect(m.escaping).toEqual(['table.wide_y right=376'])
    expect(m.h1Count).toBe(1)
    expect(m.clientWidth).toBe(375)
  })

  it('allows a wide table inside a scrolling box that stays within the viewport', () => {
    layout(375, { tableRight: 900, boxRight: 375, boxOverflow: 'auto' })
    expect(measure(TOLERANCE_PX).escaping).toEqual([])
  })

  it('counts content that only main clips as escaping', () => {
    // `hidden` here stands for main's real `clip`: the rule treats every clipping value alike.
    layout(375, { tableRight: 900, boxRight: 375, mainOverflow: 'hidden' })
    expect(measure(TOLERANCE_PX).escaping).toEqual(['table.wide_y right=900'])
  })

  it('ignores sub-pixel overhang within half a pixel', () => {
    layout(375, { tableRight: 375.4, boxRight: 375, mainOverflow: 'visible' })
    expect(measure(TOLERANCE_PX).escaping).toEqual([])
    layout(375, { tableRight: 375.6, boxRight: 375, mainOverflow: 'visible' })
    expect(measure(TOLERANCE_PX).escaping).toHaveLength(1)
  })
})

/** One breakdown table as the in-page measurement reports it, fitting a 482px box. */
function table(name, overrides = {}) {
  return {
    caption: `${name}, 30 Aug – 27 Nov 2026`,
    columns: [...BREAKDOWN_COLUMNS],
    rows: 3,
    emptyCells: 0,
    tableWidth: 482,
    boxWidth: 482,
    boxTabIndex: -1,
    boxRole: null,
    boxLabel: '',
    ...overrides,
  }
}

/** A table-check measurement at `width` in which both tables fit. */
function fitting(width, expenses = {}) {
  return {
    path: TABLE_ROUTE,
    clientWidth: width,
    docScrollWidth: width,
    tables: [table('Ledger revenue'), table('Ledger expenses', expenses)],
  }
}

/** A phone-width expenses table that scrolls in a focusable region named after it. */
const SCROLLING = {
  tableWidth: 462,
  boxWidth: 261,
  boxTabIndex: 0,
  boxRole: 'region',
  boxLabel: 'Ledger expenses, 30 Aug – 27 Nov 2026',
}
const SCROLLED = { 'Ledger expenses': { focused: true, scrolled: true, lastColumnVisible: true } }

describe('the table checks', () => {
  it('are the Analytics breakdown tables at five widths: 5 checks', () => {
    expect(TABLE_ROUTE).toBe('/analytics')
    expect(TABLE_WIDTHS).toEqual([375, 768, 1024, 1280, 1440])
    expect(TABLE_CHECK_COUNT).toBe(5)
    expect(BREAKDOWN_TABLES).toEqual(['Ledger revenue', 'Ledger expenses'])
    expect(BREAKDOWN_COLUMNS).toEqual(['Category', 'Currency', 'Amount', 'Tax', 'Entries'])
    expect(FIT_FROM_PX).toBe(768)
  })

  it('pass when both tables fit their boxes', () => {
    for (const width of TABLE_WIDTHS) {
      expect(evaluateTables(width, fitting(width))).toEqual([])
    }
  })

  it('fail the recorded defect: a 525px expenses table in a 482px box at 1440px', () => {
    const m = fitting(1440, { tableWidth: 525, boxWidth: 482 })
    expect(evaluateTables(1440, m)).toEqual([
      '"Ledger expenses" is clipped at 1440px: table 525px in a 482px box',
    ])
  })

  it('fail a clipped table at every width from 768px, even in a focusable region', () => {
    for (const width of [768, 1024, 1280]) {
      const m = fitting(width, { ...SCROLLING, tableWidth: 533, boxWidth: 402 })
      expect(evaluateTables(width, m, SCROLLED)).toEqual([
        `"Ledger expenses" is clipped at ${width}px: table 533px in a 402px box`,
      ])
    }
  })

  it('let a phone-width table scroll in a named, focusable region the keyboard scrolls', () => {
    expect(evaluateTables(375, fitting(375, SCROLLING), SCROLLED)).toEqual([])
  })

  it('fail a phone-width scroll box the keyboard cannot focus', () => {
    const m = fitting(375, { ...SCROLLING, boxTabIndex: -1 })
    expect(evaluateTables(375, m, SCROLLED)).toEqual([
      '"Ledger expenses" scrolls (table 462px in a 261px box) in a box that is not a focusable region named after it',
    ])
  })

  it('fail a phone-width scroll box that is not a region named after its table', () => {
    for (const overrides of [{ boxRole: null }, { boxLabel: '' }, { boxLabel: 'Ledger revenue, x' }]) {
      const m = fitting(375, { ...SCROLLING, ...overrides })
      expect(evaluateTables(375, m, SCROLLED)).toHaveLength(1)
    }
  })

  it('fail a phone-width table the keyboard never reached or could not scroll to its end', () => {
    const m = fitting(375, SCROLLING)
    expect(evaluateTables(375, m, {})).toEqual([
      '"Ledger expenses" scrolls (table 462px in a 261px box) but the keyboard never reached its box',
    ])
    const stuck = { 'Ledger expenses': { focused: true, scrolled: false, lastColumnVisible: false } }
    expect(evaluateTables(375, m, stuck)).toEqual([
      '"Ledger expenses" scrolls (table 462px in a 261px box) but the arrow keys did not bring its last column into view',
    ])
    const short = { 'Ledger expenses': { focused: true, scrolled: true, lastColumnVisible: false } }
    expect(evaluateTables(375, m, short)).toHaveLength(1)
  })

  it('fail a missing table, a missing column, an empty cell, no rows and a sideways page', () => {
    const missing = { ...fitting(1440), tables: [table('Ledger revenue')] }
    expect(evaluateTables(1440, missing)).toEqual(['no table captioned "Ledger expenses…"'])
    const columns = fitting(1440, { columns: ['Category', 'Currency', 'Amount', 'Tax'] })
    expect(evaluateTables(1440, columns)).toEqual([
      '"Ledger expenses" has the columns Category, Currency, Amount, Tax',
    ])
    expect(evaluateTables(1440, fitting(1440, { emptyCells: 2 }))).toEqual([
      '"Ledger expenses" has 2 empty cells',
    ])
    expect(evaluateTables(1440, fitting(1440, { rows: 0 }))).toEqual([
      '"Ledger expenses" has no rows to measure',
    ])
    expect(evaluateTables(1440, { ...fitting(1440), docScrollWidth: 1460 })).toEqual([
      'sideways scroll: the document is 1460px wide in a 1440px viewport',
    ])
    expect(evaluateTables(1440, { ...fitting(1440), path: '/login' })).toEqual([
      'not the page asked for: the browser is at /login',
    ])
  })

  it('pass a run only when all 5 table checks ran and passed', () => {
    const passing = TABLE_WIDTHS.map((width) => ({ width, problems: [] }))
    expect(summariseTables(passing).ok).toBe(true)
    expect(summariseTables(passing.slice(1)).ok).toBe(false)
    const verdict = summariseTables(passing.map((r, i) => (i === 4 ? { ...r, problems: ['x'] } : r)))
    expect(verdict.ok).toBe(false)
    expect(verdict.lines.at(-1)).toBe('4 of 5 table checks passed')
  })
})

describe('the table measurement', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('reports each table, its columns and cells, its width against its box, and its box', () => {
    Object.defineProperty(document.documentElement, 'clientWidth', { configurable: true, value: 375 })
    Object.defineProperty(document.documentElement, 'scrollWidth', { configurable: true, value: 375 })
    document.body.innerHTML = `<main><h1>Analytics</h1>
      <div tabindex="0" role="region" aria-labelledby="c1"><table>
        <caption id="c1">Ledger expenses, x</caption>
        <thead><tr><th>Category</th><th>Currency</th><th>Amount</th><th>Tax</th><th>Entries</th></tr></thead>
        <tbody><tr><th>PAYROLL</th><td>EUR</td><td>EUR 49000.00</td><td>0.00</td><td></td></tr></tbody>
      </table></div>
      <div><table>
        <caption>Ledger revenue, x</caption>
        <thead><tr><th>Category</th></tr></thead>
        <tbody><tr><th>FNB</th></tr><tr><th>SPA</th></tr></tbody>
      </table></div></main>`
    const [table, plain] = document.querySelectorAll('table')
    Object.defineProperty(table, 'scrollWidth', { configurable: true, value: 462 })
    Object.defineProperty(table.parentElement, 'clientWidth', { configurable: true, value: 261 })
    Object.defineProperty(plain, 'scrollWidth', { configurable: true, value: 300 })
    Object.defineProperty(plain.parentElement, 'clientWidth', { configurable: true, value: 300 })
    const m = measureTables()
    expect(m).toMatchObject({ clientWidth: 375, docScrollWidth: 375 })
    // The second box is a plain container: no tab stop, no role, no label.
    expect(m.tables[1]).toEqual({
      caption: 'Ledger revenue, x',
      columns: ['Category'],
      rows: 2,
      emptyCells: 0,
      tableWidth: 300,
      boxWidth: 300,
      boxTabIndex: -1,
      boxRole: null,
      boxLabel: '',
    })
    expect(m.tables.slice(0, 1)).toEqual([
      {
        caption: 'Ledger expenses, x',
        columns: ['Category', 'Currency', 'Amount', 'Tax', 'Entries'],
        rows: 1,
        emptyCells: 1,
        tableWidth: 462,
        boxWidth: 261,
        boxTabIndex: 0,
        boxRole: 'region',
        boxLabel: 'Ledger expenses, x',
      },
    ])
  })
})
