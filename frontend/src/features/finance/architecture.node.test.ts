import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * The calculations this feature must NOT contain, asserted against its own source.
 *
 * ## Why a source test rather than a rendering test
 *
 * A rendering test can prove that a particular total is absent from a particular screen. It
 * cannot prove that no total is ever computed -- the wrong figure might appear only for a
 * currency the fixture lacks, or only once a second page is loaded. The property this stage
 * is actually asked to hold is architectural: **the browser performs no financial
 * arithmetic**, ever, for any input. That is a property of the code, so it is checked in the
 * code.
 *
 * The behavioural half is in `finance.test.tsx`, which asserts that the figures on screen are
 * the server's own strings and that no combination of them appears. The two together are the
 * claim: nothing computes a total, and nothing displays one.
 *
 * ## What each pattern would mean if it appeared
 *
 * * `parseFloat` / `parseInt` / `Number(` on a money string puts a 64-bit binary float in the
 *   middle of a `NUMERIC(14,2)` figure, where `0.1 + 0.2` is not `0.3`.
 * * `reduce` over ledger rows is a client-side aggregation -- a partial sum over one page,
 *   presented as a total.
 * * `+=`, or `+` between two amounts, is the same thing written out.
 * * `toFixed` is rounding a figure this application does not own.
 * * `dangerouslySetInnerHTML`, `innerHTML`, `eval`, `new Function` are the injection surface.
 * * `console.` in shipped code puts request data in a place nobody audits.
 * * `Bearer` anywhere but the API client means a second place assembles credentials.
 *
 * ## The one deliberate exception
 *
 * `formatMoney` in `@/lib/format` calls `Number()` at the very edge, to hand a value to
 * `Intl.NumberFormat` for display. It is documented there, the result is never read back, and
 * it is the only conversion in the application. This test covers the finance feature, its
 * service and its types -- none of which may do the same.
 */

/*
 * Resolved from the runner's working directory, which vitest sets to the package root.
 * `import.meta.url` is not a `file:` URL under the jsdom environment, so `fileURLToPath`
 * cannot be used here.
 */
const ROOT = process.cwd()

const FINANCE_DIRECTORIES = [
  join(ROOT, 'src', 'features', 'finance'),
  join(ROOT, 'src', 'services', 'finance'),
]

const PAGE_FILES = [join(ROOT, 'src', 'pages', 'FinancialsPage.tsx')]

/** Every `.ts`/`.tsx` file this stage added, minus its own tests. */
function financeSources(): { name: string; text: string }[] {
  const files: { name: string; text: string }[] = []
  for (const directory of FINANCE_DIRECTORIES) {
    for (const entry of readdirSync(directory)) {
      if (!/\.tsx?$/.test(entry) || entry.includes('.test.')) {
        continue
      }
      files.push({ name: entry, text: readFileSync(join(directory, entry), 'utf8') })
    }
  }
  for (const path of PAGE_FILES) {
    files.push({ name: 'FinancialsPage.tsx', text: readFileSync(path, 'utf8') })
  }
  return files
}

/**
 * Strip comments before matching.
 *
 * These files explain at length *why* they do not call `parseFloat` -- so a naive search for
 * the word finds the prose that promises it is absent and fails the test that proves it. Only
 * executable text is searched.
 */
function code(text: string): string {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1')
}

const sources = financeSources()

/**
 * The detector, checked against known positives before it is trusted on negatives.
 *
 * A source scan is only as good as its stripper. If `code()` were too aggressive -- and a
 * greedy block-comment match over a file that opens with a docstring very nearly is -- it
 * would return almost nothing and every "contains no parseFloat" assertion would pass on an
 * empty string. These three tests fail if that ever happens.
 */
describe('the source scan itself works', () => {
  it('strips prose but keeps the code', () => {
    const service = sources.find((file) => file.name === 'financeService.ts')!
    const stripped = code(service.text)
    // The docstring promises append-only semantics; the code issues the calls.
    expect(service.text).toMatch(/append-only/)
    expect(stripped).not.toMatch(/append-only/)
    expect(stripped).toMatch(/api\.get<Page<RevenueEntry>>/)
    expect(stripped).toMatch(/analytics\/revenue-by-category/)
  })

  it('leaves most of every file behind', () => {
    for (const file of sources) {
      // Comments are dense here, but a file reduced to under a fifth of itself means the
      // stripper ate code rather than prose.
      expect(code(file.text).length, `${file.name} was over-stripped`).toBeGreaterThan(
        file.text.length / 5,
      )
    }
  })

  it('detects the one Number() the application is allowed', () => {
    // `formatMoney` converts at the very edge, for `Intl.NumberFormat`. It is the positive
    // control: if this pattern stops matching there, it would stop matching a real breach.
    const format = readFileSync(join(ROOT, 'src', 'lib', 'format.ts'), 'utf8')
    expect(code(format)).toMatch(/\bNumber\s*\(/)
  })
})

describe('the finance feature performs no financial arithmetic', () => {
  it('covers every source file this stage added', () => {
    const names = sources.map((file) => file.name).sort()
    expect(names).toEqual([
      'CategoryTotals.tsx',
      'ExpenseForm.tsx',
      'FinancialsPage.tsx',
      'LedgerCards.tsx',
      'LedgerFilters.tsx',
      'LedgerSection.tsx',
      'LedgerTable.tsx',
      'RevenueForm.tsx',
      'financeService.ts',
      'useLedger.ts',
      'vocabulary.ts',
    ])
  })

  it.each([
    ['parseFloat', /\bparseFloat\s*\(/],
    ['parseInt', /\bparseInt\s*\(/],
    ['Number()', /\bNumber\s*\(/],
    ['unary +', /[^\w)\]]\+\s*(?:amount|tax|value|entry\.)/],
    ['reduce', /\.reduce\s*\(/],
    ['compound addition', /\+=/],
    ['toFixed', /\.toFixed\s*\(/],
    ['Math on money', /\bMath\.(?:round|abs|floor|ceil)\s*\(/],
  ])('contains no %s', (_label, pattern) => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not use ${_label}`).not.toMatch(pattern)
    }
  })

  it('never sums two amounts', () => {
    // Any `a + b` where either side names a monetary field. String concatenation of labels is
    // fine and common; adding `amount` to anything is not.
    const summing = /\b(?:amount|tax_amount|total_amount|net_paid|outstanding)\w*\s*\+(?!\+)/
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not sum amounts`).not.toMatch(summing)
    }
  })

  it('names no figure the API does not report', () => {
    // profit, net revenue, gross margin, running balance: none is an endpoint's field, so
    // none may be a label. `balance` is included because a ledger screen showing one implies
    // it was derived from the rows above it.
    const invented = /\b(?:profit|gross margin|net revenue|running balance|grand total)\b/i
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not name an invented figure`).not.toMatch(invented)
    }
  })
})

describe('the finance feature has no unsafe or leaky surface', () => {
  it.each([
    ['dangerouslySetInnerHTML', /dangerouslySetInnerHTML/],
    ['innerHTML', /\.innerHTML\b/],
    ['eval', /\beval\s*\(/],
    ['new Function', /new\s+Function\s*\(/],
    ['console', /\bconsole\.\w+\s*\(/],
    ['a hand-built Authorization header', /Bearer/],
    ['localStorage', /\blocalStorage\b/],
    ['a raw fetch', /\bfetch\s*\(/],
  ])('contains no %s', (_label, pattern) => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not contain ${_label}`).not.toMatch(pattern)
    }
  })

  it('routes every request through the shared API client', () => {
    // Only the service module may import it, and it is the only place a URL is built.
    const importers = sources.filter((file) => /@\/services\/api\/client/.test(file.text))
    expect(importers.map((file) => file.name)).toEqual(['financeService.ts'])
  })
})

describe('the finance feature offers no mutation the API lacks', () => {
  it('issues no PATCH, PUT or DELETE', () => {
    // Both journals answer 405 to all three. A wrapper for one would be a control that could
    // only ever fail.
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not issue a mutation`).not.toMatch(
        /\bapi\.(?:patch|put|delete)\b/,
      )
    }
  })

  it('builds no single-entry URL', () => {
    // Neither table has a `public_id`, so `/revenue/{something}` cannot address a row.
    for (const file of sources) {
      expect(code(file.text)).not.toMatch(/\/revenue\/\$\{/)
      expect(code(file.text)).not.toMatch(/\/expenses\/\$\{/)
    }
  })
})
