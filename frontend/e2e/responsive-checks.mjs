/**
 * A9: the UI-10 responsive audit's specification and its verdicts, as code.
 *
 * UI-10 (docs/ui-redesign.md §5) audited 17 routes at 375, 768 and 1440px -- 51 checks -- by
 * hand, with a script pasted into a browser. This module is that audit's definition: the routes,
 * the widths, the in-page measurement and the rule that turns a measurement into a verdict. The
 * runner, `responsive-audit.mjs`, drives a real browser against the CI stack with it; the Vitest
 * suite beside it (`responsive-checks.test.mjs`) holds the rule.
 *
 * **One check is one route at one width**, and it passes when all three of UI-10's conditions
 * hold:
 *
 *  1. no sideways scroll -- the document is exactly as wide as the viewport;
 *  2. no escaping element -- nothing inside `<main>` extends past the viewport's right edge by
 *     more than half a pixel, unless an ancestor that clips horizontally (overflow-x `auto`,
 *     `scroll`, `hidden` or `clip`, other than `<main>` itself) contains it within the viewport,
 *     which is how a scrolling table is meant to behave;
 *  3. exactly one `h1` on the page.
 *
 * Two preconditions make a check count rather than pass by accident -- the page must be the one
 * asked for (not a redirect to sign-in) and, except for the unknown route, must not be the
 * not-found page -- because a signed-out browser would otherwise see 51 sign-in pages, each
 * with one `h1` and nothing wide, and pass.
 *
 * The measurement is UI-10's, unchanged in what it measures: the same selectors, the same
 * clipping rule and the same 0.5px tolerance, after the same readiness wait (a `main h1` and no
 * `main [aria-busy="true"]`, polled every 250ms for up to 6s, then the fonts, then 300ms).
 */

/** The viewport widths, in CSS pixels. */
export const WIDTHS = Object.freeze([375, 768, 1440])

/** UI-10 measured inside a 900px-high frame. */
export const HEIGHT = 900

/**
 * The 17 routes, in UI-10's order. A `:name` segment is resolved from the seeded data: the
 * first record the list page links to.
 */
export const ROUTES = Object.freeze([
  '/',
  '/bookings',
  '/bookings/:booking',
  '/financials',
  '/reviews',
  '/guests',
  '/guests/:guest',
  '/rooms',
  '/rooms/:roomType/:roomNumber',
  '/availability',
  '/property',
  '/administration',
  '/platform',
  '/analytics',
  '/intelligence',
  '/copilot',
  '/nowhere',
])

/** The route no page owns; it must render the not-found page, with its one `h1`. */
export const NOT_FOUND_ROUTE = '/nowhere'

/** 17 routes x 3 widths. The runner refuses to report a different number. */
export const CHECK_COUNT = 51

/** UI-10's tolerance for sub-pixel layout, in CSS pixels. */
export const TOLERANCE_PX = 0.5

/** Readiness: polled every 250ms, 24 times (6s), as UI-10 did; then fonts, then 300ms. */
export const READY_POLL_MS = 250
export const READY_POLLS = 24
export const SETTLE_MS = 300

/**
 * The known defect the project-completion audit recorded after UI-10 and that the 51 checks do
 * not catch: the Analytics expenses table is wider than its box at 1440px, so its last column
 * is cut off inside its scroll container. Reported by the runner on every run, never counted,
 * never hidden; see docs/project-completion-audit.md.
 */
export const KNOWN_DEFECT = Object.freeze({
  route: '/analytics',
  width: 1440,
  caption: 'Ledger expenses',
  description: 'the Analytics expenses table is wider than its box at 1440px (last column clipped)',
})

/** Every check to run: each route at each width, its `:name` segments resolved from `ids`. */
export function plan(ids) {
  if (ROUTES.length * WIDTHS.length !== CHECK_COUNT) {
    throw new Error(`the specification is ${ROUTES.length} routes x ${WIDTHS.length} widths, not ${CHECK_COUNT} checks`)
  }
  const checks = []
  for (const width of WIDTHS) {
    for (const route of ROUTES) {
      const path = route.replace(/:([A-Za-z]+)/g, (_, name) => {
        const value = ids[name]
        if (typeof value !== 'string' || value === '') {
          throw new Error(`no value for :${name} in ${route}: the seeded data has no such record`)
        }
        return encodeURIComponent(value)
      })
      checks.push({ route, path, width })
    }
  }
  return checks
}

/**
 * The in-page measurement, run in the browser (Playwright serialises the function, so it must
 * be self-contained). UI-10's expressions, returned as data rather than as a string.
 */
export function measure(tolerance) {
  const d = document
  const cw = d.documentElement.clientWidth
  const clipper = (e) => {
    for (let p = e.parentElement; p && p !== d.body; p = p.parentElement) {
      const o = window.getComputedStyle(p).overflowX
      if (o === 'auto' || o === 'scroll' || o === 'hidden' || o === 'clip') return p
    }
    return null
  }
  const escaping = [...d.querySelectorAll('main *')].filter((e) => {
    const b = e.getBoundingClientRect()
    if (!(b.width > 0 && b.right > cw + tolerance)) return false
    const c = clipper(e)
    return !c || c.tagName === 'MAIN' || c.getBoundingClientRect().right > cw + tolerance
  })
  const top = escaping
    .filter((e) => !escaping.includes(e.parentElement))
    .map((e) => {
      const name = String(e.className).split(' ')[0].replace(/^_/, '').slice(0, 24)
      return `${e.tagName.toLowerCase()}.${name} right=${Math.round(e.getBoundingClientRect().right)}`
    })
  return {
    path: window.location.pathname,
    clientWidth: cw,
    docScrollWidth: d.documentElement.scrollWidth,
    escaping: top,
    h1Count: d.querySelectorAll('h1').length,
    notFound: /Page not found/.test(d.querySelector('main')?.innerText ?? ''),
  }
}

/** The known defect's in-page probe: every table on the page, its width against its box. */
export function measureTables() {
  return [...document.querySelectorAll('main table')].map((table) => ({
    caption: (table.querySelector('caption')?.textContent ?? '').trim(),
    tableWidth: table.scrollWidth,
    boxWidth: table.parentElement ? table.parentElement.clientWidth : 0,
  }))
}

/** What is wrong with one check's measurement: an empty list when it passes. */
export function evaluate(check, m) {
  const problems = []
  if (m.path !== check.path) {
    problems.push(`not the page asked for: the browser is at ${m.path}`)
  }
  if (check.route !== NOT_FOUND_ROUTE && m.notFound) {
    problems.push('rendered the not-found page')
  }
  if (m.docScrollWidth !== m.clientWidth) {
    problems.push(`sideways scroll: the document is ${m.docScrollWidth}px wide in a ${m.clientWidth}px viewport`)
  }
  if (m.escaping.length > 0) {
    problems.push(`escapes the viewport: ${m.escaping.slice(0, 5).join(', ')}`)
  }
  if (m.h1Count !== 1) {
    problems.push(`${m.h1Count} h1 elements, not exactly one`)
  }
  return problems
}

/** The known defect's state, from the probe's tables. */
export function knownDefect(tables) {
  const table = tables.find((t) => t.caption.startsWith(KNOWN_DEFECT.caption))
  if (!table) {
    return { found: false, reproduces: false, detail: `no table captioned "${KNOWN_DEFECT.caption}…" on ${KNOWN_DEFECT.route}` }
  }
  const reproduces = table.tableWidth > table.boxWidth
  return {
    found: true,
    reproduces,
    detail: `"${table.caption}": table ${table.tableWidth}px in a ${table.boxWidth}px box`,
  }
}

/** The run's verdict: every planned check reported, every one passing. */
export function summarise(results) {
  const failed = results.filter((r) => r.problems.length > 0)
  const ok = results.length === CHECK_COUNT && failed.length === 0
  const lines = results.map(
    (r) => `${r.problems.length ? 'FAIL' : 'PASS'}  ${String(r.width).padStart(4)}px  ${r.route}${r.problems.length ? `  -- ${r.problems.join('; ')}` : ''}`,
  )
  if (results.length !== CHECK_COUNT) {
    lines.push(`FAIL  ${results.length} checks ran, the specification has ${CHECK_COUNT}`)
  }
  lines.push(`${results.length - failed.length} of ${CHECK_COUNT} checks passed`)
  return { ok, passed: results.length - failed.length, failed: failed.length, lines }
}
