/**
 * A9: the responsive audit's specification and verdicts (responsive-checks.mjs).
 *
 * The browser half -- that the 51 checks pass on the real stack -- runs in CI's "Responsive
 * audit" job. These tests hold the definition it runs: that it is UI-10's 17 routes at UI-10's
 * three widths, that each of the three conditions fails a check on its own, that the
 * measurement's clipping rule and tolerance are UI-10's, and that a short or failing run is
 * never reported as a pass. jsdom has no layout, so the measurement is driven with stubbed
 * geometry.
 */

import { afterEach, describe, expect, it } from 'vitest'

import {
  CHECK_COUNT,
  HEIGHT,
  KNOWN_DEFECT,
  NOT_FOUND_ROUTE,
  READY_POLLS,
  READY_POLL_MS,
  ROUTES,
  SETTLE_MS,
  TOLERANCE_PX,
  WIDTHS,
  evaluate,
  knownDefect,
  measure,
  plan,
  summarise,
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

describe('the known defect', () => {
  it('is reported as reproducing while the expenses table is wider than its box', () => {
    expect(KNOWN_DEFECT.route).toBe('/analytics')
    expect(KNOWN_DEFECT.width).toBe(1440)
    const wide = knownDefect([{ caption: 'Ledger expenses, 25 Aug – 22 Nov 2026', tableWidth: 542, boxWidth: 475 }])
    expect(wide).toMatchObject({ found: true, reproduces: true })
    const fits = knownDefect([{ caption: 'Ledger expenses, x', tableWidth: 400, boxWidth: 475 }])
    expect(fits).toMatchObject({ found: true, reproduces: false })
    expect(knownDefect([])).toMatchObject({ found: false })
  })
})
