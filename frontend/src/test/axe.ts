import axe from 'axe-core'
import { expect } from 'vitest'

/**
 * Automated accessibility checks (A4), with axe-core used directly -- no wrapper, no matcher
 * library, nothing between the test and the engine.
 *
 * ## What axe cannot see here, and therefore does not check
 *
 * jsdom builds a DOM but lays nothing out: every element measures 0 x 0, nothing has a rendered
 * colour, and `css: false` (vitest.config.ts) means no stylesheet is applied at all. So:
 *
 * - **`color-contrast` is off.** It needs the colours and the overlap a real browser computes;
 *   in jsdom it could only pass or fail on nothing. Contrast is measured in a real browser
 *   instead and recorded beside each token in `styles/tokens.css`.
 *
 * The other rules axe runs on structure -- names, labels, roles, ARIA states, headings, table
 * semantics, alternative text, list markup, duplicate ids -- and those are what these checks
 * hold every page to.
 *
 * ## Rules that belong to the document, not to a rendered page
 *
 * A page under test is mounted into jsdom's bare `document`, not into `index.html`, and without
 * the application shell that owns the landmarks. Three rules judge that document rather than
 * the page, so on a page they would report the test's scaffolding:
 *
 * - **`html-has-lang`** and **`document-title`**: `index.html` sets both, and
 *   `index.node.test.ts` asserts it does.
 * - **`region`** (all content inside landmarks) and **`landmark-one-main`**: the shell renders
 *   `<header>`, `<nav>` and `<main>` around every page. `AppShell.test.tsx` runs axe on the
 *   shell itself with both rules on.
 *
 * Nothing else is disabled, and nothing is disabled to make a page pass: a rule that fired on a
 * real defect was fixed in the component, not switched off here.
 */

/** Rules that need real layout or rendering: never meaningful under jsdom. */
const NEEDS_A_BROWSER = ['color-contrast'] as const

/** Rules about the document a page is mounted in, checked elsewhere (see above). */
const DOCUMENT_ONLY = ['html-has-lang', 'document-title', 'region', 'landmark-one-main'] as const

export interface AxeOptions {
  /** Keep the landmark rules on: for the application shell, which owns the landmarks. */
  readonly withLandmarks?: boolean
}

function rulesFor({ withLandmarks = false }: AxeOptions): Record<string, { enabled: false }> {
  const off = [
    ...NEEDS_A_BROWSER,
    ...DOCUMENT_ONLY.filter(
      (rule) => !withLandmarks || (rule !== 'region' && rule !== 'landmark-one-main'),
    ),
  ]
  return Object.fromEntries(off.map((rule) => [rule, { enabled: false }]))
}

/** One line per failing node, so a failure names the rule, the element and the fix. */
function describeViolations(violations: readonly axe.Result[]): string {
  return violations
    .flatMap((violation) =>
      violation.nodes.map(
        (node) =>
          `${violation.id} (${violation.impact ?? 'n/a'}): ${violation.help}\n` +
          `  at ${node.target.join(' ')}\n  ${node.failureSummary ?? ''}`,
      ),
    )
    .join('\n')
}

/** The axe violations in *root* (the whole rendered document by default). */
export async function axeViolations(
  root: Element = document.body,
  options: AxeOptions = {},
): Promise<axe.Result[]> {
  const results = await axe.run(root, {
    rules: rulesFor(options),
    resultTypes: ['violations'],
  })
  return results.violations
}

/**
 * One project rule axe has no rule for: every data table is named.
 *
 * axe checks a table's header cells but not that the table says what it is, and a screen
 * reader's table list then reads "table, 7 columns" for each of them. Every table here carries a
 * caption (usually visually hidden, `DataTable.module.css`) or an `aria-label`, so a table
 * without one is a regression, not a style choice.
 */
function unnamedTables(root: Element): string[] {
  return [...root.querySelectorAll('table')]
    .filter(
      (table) =>
        !(table.querySelector('caption')?.textContent ?? '').trim() &&
        !(table.getAttribute('aria-label') ?? '').trim() &&
        !table.getAttribute('aria-labelledby'),
    )
    .map((table) => `table-has-name (project rule): a <table> has no caption or aria-label\n  ${table.outerHTML.slice(0, 120)}`)
}

/** Fail with every violation spelled out, or pass when there are none. */
export async function expectNoAxeViolations(
  root: Element = document.body,
  options: AxeOptions = {},
): Promise<void> {
  const violations = await axeViolations(root, options)
  const problems = [describeViolations(violations), ...unnamedTables(root)].filter(Boolean)
  // The summary, not the result objects: a failure then reads as rule, element and fix.
  expect(problems.join('\n')).toBe('')
}
