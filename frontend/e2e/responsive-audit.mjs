/**
 * A9: run UI-10's 51-check responsive audit in a real browser against a running stack.
 *
 *     AUDIT_BASE_URL=https://127.0.0.1:28443 AUDIT_EMAIL=... AUDIT_PASSWORD=... \
 *       AUDIT_REPORT=/tmp/responsive-audit.json node e2e/responsive-audit.mjs
 *
 * (optionally AUDIT_CHROMIUM=/path/to/chrome for a local run outside the Playwright image)
 *
 * CI runs it in the pinned Playwright image (its Chromium is the one playwright-core expects),
 * against the Compose stack, seeded with scripts/seed_demo.py's synthetic data. The checks and
 * the verdicts are `responsive-checks.mjs`'s; this file only drives the browser:
 *
 *  1. signs in through the sign-in page, as a person would;
 *  2. reads the first booking, guest and room the list pages link to, for the three detail
 *     routes;
 *  3. visits each of the 17 routes at each of the 3 widths in a fresh page of one signed-in
 *     context, waits as UI-10 did, measures, and evaluates;
 *  4. runs the five table checks: the Analytics breakdown tables at 375, 768, 1024, 1280 and
 *     1440px, each fitting its box or, on a phone, scrolling in a box the keyboard reaches and
 *     scrolls to the last column (the 1440px clipping these replace was fixed on 2026-11-27);
 *  5. writes the JSON report and exits 1 unless all 51 checks and all 5 table checks ran and
 *     passed.
 *
 * The browser is headless Chromium at a device scale factor of 1, so the result does not depend
 * on anyone's display: Playwright hides scrollbars, so the viewport's client width is the width
 * asked for. (UI-10 ran in a desktop browser with classic scrollbars, whose frames were ~15px
 * narrower than their nominal width on a long page; the widths here are the nominal ones.)
 * The password comes from the environment and is never printed.
 */

import { writeFileSync } from 'node:fs'
import { chromium } from 'playwright-core'

import {
  BREAKDOWN_TABLES,
  HEIGHT,
  READY_POLL_MS,
  READY_POLLS,
  SETTLE_MS,
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

function required(name) {
  const value = process.env[name]
  if (!value) {
    console.error(`::error::${name} is not set`)
    process.exit(2)
  }
  return value
}

const base = required('AUDIT_BASE_URL').replace(/\/$/, '')
const email = required('AUDIT_EMAIL')
const password = required('AUDIT_PASSWORD')
const reportPath = required('AUDIT_REPORT')

/** UI-10's readiness wait: a `main h1` and nothing busy, polled; then fonts; then a settle. */
async function settle(page) {
  try {
    await page.waitForFunction(
      () => document.querySelector('main h1') && !document.querySelector('main [aria-busy="true"]'),
      undefined,
      { polling: READY_POLL_MS, timeout: READY_POLL_MS * READY_POLLS },
    )
  } catch {
    // UI-10 measured after the last poll whether or not the page was ready; the h1 count and
    // the preconditions then report what was wrong. Kept, so a slow page fails as a finding.
  }
  await page.evaluate(() => document.fonts.ready.then(() => undefined))
  await page.waitForTimeout(SETTLE_MS)
}

async function open(context, path, width) {
  const page = await context.newPage()
  await page.setViewportSize({ width, height: HEIGHT })
  await page.goto(`${base}${path}`, { waitUntil: 'load' })
  await settle(page)
  return page
}

/** The first detail link a list page offers, split into its path segments after the prefix. */
async function firstLink(context, listPath, segments) {
  const page = await open(context, listPath, Math.max(...WIDTHS))
  const hrefs = await page.$$eval('main a[href]', (links) => links.map((a) => a.getAttribute('href')))
  await page.close()
  for (const href of hrefs) {
    const parts = (href ?? '').split('?')[0].split('/').filter(Boolean)
    if (parts.length === segments + 1 && `/${parts[0]}` === listPath) {
      return parts.slice(1).map(decodeURIComponent)
    }
  }
  throw new Error(`${listPath} links to no record (${hrefs.length} links): the seeded data is missing`)
}

/** Key presses allowed to reach a table's box from the top of the page, and to scroll it. */
const TAB_LIMIT = 200
const ARROW_LIMIT = 60

/**
 * Scroll one breakdown table by keyboard alone, as a person without a pointer would: Tab from
 * the top of the page until focus is on the table's box, then ArrowRight until it stops
 * moving. Reports whether focus got there, whether the box scrolled, and whether the last
 * column header then lies inside the box.
 */
async function scrollByKeyboard(page, name) {
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
    window.scrollTo(0, 0)
  })
  const onTable = (n) => {
    const table = document.activeElement?.querySelector(':scope > table')
    return Boolean(table && (table.querySelector('caption')?.textContent ?? '').trim().startsWith(n))
  }
  let focused = false
  for (let i = 0; i < TAB_LIMIT && !focused; i += 1) {
    await page.keyboard.press('Tab')
    focused = await page.evaluate(onTable, name)
  }
  if (!focused) return { focused, scrolled: false, lastColumnVisible: false }
  const position = () => document.activeElement.scrollLeft
  for (let i = 0; i < ARROW_LIMIT; i += 1) {
    const before = await page.evaluate(position)
    await page.keyboard.press('ArrowRight')
    await page.waitForTimeout(60)
    if ((await page.evaluate(position)) === before) break
  }
  return page.evaluate((tolerance) => {
    const box = document.activeElement
    const last = [...box.querySelectorAll('thead th')].at(-1).getBoundingClientRect()
    const edge = box.getBoundingClientRect()
    return {
      focused: true,
      scrolled: box.scrollLeft > 0,
      lastColumnVisible: last.left >= edge.left - tolerance && last.right <= edge.right + tolerance,
    }
  }, TOLERANCE_PX)
}

async function signIn(context) {
  const page = await context.newPage()
  await page.setViewportSize({ width: Math.max(...WIDTHS), height: HEIGHT })
  await page.goto(`${base}/login`, { waitUntil: 'load' })
  await page.locator('input[name="email"]').fill(email)
  await page.locator('input[type="password"]').fill(password)
  await page.locator('button[type="submit"]').click()
  await page.waitForURL((url) => url.pathname !== '/login', { timeout: 20000 })
  await page.close()
}

// CI uses the Chromium the pinned Playwright image ships. AUDIT_CHROMIUM points a local run at
// an installed Chromium-family browser instead; CI never sets it.
const browser = await chromium.launch({ executablePath: process.env.AUDIT_CHROMIUM || undefined })
const exitCode = await (async () => {
  try {
    const context = await browser.newContext({
      deviceScaleFactor: 1,
      // The stack's certificate is the run's own self-signed one, for 127.0.0.1.
      ignoreHTTPSErrors: true,
      locale: 'en-GB',
      timezoneId: 'UTC',
    })
    await signIn(context)
    console.log('  OK   signed in through the sign-in page')

    const [booking] = await firstLink(context, '/bookings', 1)
    const [guest] = await firstLink(context, '/guests', 1)
    const [roomType, roomNumber] = await firstLink(context, '/rooms', 2)
    console.log(`  OK   detail routes from the seeded data: booking, guest, room ${roomType}/${roomNumber}`)

    const results = []
    for (const check of plan({ booking, guest, roomType, roomNumber })) {
      const page = await open(context, check.path, check.width)
      const m = await page.evaluate(measure, TOLERANCE_PX)
      await page.close()
      results.push({ ...check, measurement: m, problems: evaluate(check, m) })
    }

    const tableResults = []
    for (const width of TABLE_WIDTHS) {
      const page = await open(context, TABLE_ROUTE, width)
      const m = await page.evaluate(measureTables)
      const keyboard = {}
      for (const name of BREAKDOWN_TABLES) {
        const table = m.tables.find((t) => t.caption.startsWith(name))
        if (table && table.tableWidth > table.boxWidth) {
          keyboard[name] = await scrollByKeyboard(page, name)
        }
      }
      await page.close()
      tableResults.push({ width, measurement: m, keyboard, problems: evaluateTables(width, m, keyboard) })
    }

    const verdict = summarise(results)
    for (const line of verdict.lines) console.log(line)
    const tableVerdict = summariseTables(tableResults)
    for (const line of tableVerdict.lines) console.log(line)
    writeFileSync(
      reportPath,
      `${JSON.stringify({ base: 'the CI stack', widths: WIDTHS, height: HEIGHT, results, passed: verdict.passed, failed: verdict.failed, tableWidths: TABLE_WIDTHS, tableResults, tablesPassed: tableVerdict.passed, tablesFailed: tableVerdict.failed, ok: verdict.ok && tableVerdict.ok }, null, 2)}\n`,
    )
    if (!verdict.ok || !tableVerdict.ok) {
      for (const r of results.filter((x) => x.problems.length)) {
        console.log(`::error::responsive audit: ${r.route} at ${r.width}px -- ${r.problems.join('; ')}`)
      }
      for (const r of tableResults.filter((x) => x.problems.length)) {
        console.log(`::error::responsive audit: ${TABLE_ROUTE} tables at ${r.width}px -- ${r.problems.join('; ')}`)
      }
      return 1
    }
    return 0
  } catch (error) {
    console.error(`::error::the responsive audit could not run: ${error instanceof Error ? error.message : error}`)
    return 2
  } finally {
    await browser.close()
  }
})()
process.exit(exitCode)
