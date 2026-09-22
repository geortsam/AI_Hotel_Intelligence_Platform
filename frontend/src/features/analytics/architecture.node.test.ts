import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * What the analytics feature must not contain, asserted against its own source.
 *
 * ## Why a source test rather than a rendering test
 *
 * `analytics.test.tsx` proves that particular figures on a particular screen came from the
 * server. It cannot prove that *no* figure is ever computed here — a wrong total might appear
 * only for a currency the fixture lacks, or only once a second page of categories loads. The
 * property Stage 7.2 is actually required to hold is architectural: **the browser performs no
 * business arithmetic**, for any input, ever. That is a property of the code, so it is checked
 * in the code. This mirrors the finance feature's test, which was written for the same reason.
 *
 * ## What each pattern would mean if it appeared
 *
 * * `parseFloat` / `parseInt` on a server decimal puts a 64-bit binary float in the middle of a
 *   `NUMERIC(14,2)`, where `0.1 + 0.2` is not `0.3` and a cent goes missing.
 * * `reduce` over category rows is a client-side aggregation — a partial sum over one response,
 *   presented as a total the server never sent. The screen deliberately shows no total row.
 * * `+=`, or `+` between two amounts, is the same thing written out.
 * * `toFixed` is rounding a figure this application does not own.
 * * `dangerouslySetInnerHTML`, `innerHTML`, `eval`, `new Function` are the injection surface.
 * * `console.` in shipped code puts request data somewhere nobody audits.
 * * `Bearer` anywhere but the API client means a second place assembles credentials.
 *
 * ## The one permitted conversion, and where it is allowed to live
 *
 * `series.ts` calls `Number()` once, on `occupancy_rate`, to place a point on a chart axis.
 * That file documents why a ratio is not money and why the value is never written back. The
 * test below pins it: `Number(` may appear in `series.ts` and **nowhere else** in the feature,
 * so a second conversion cannot be added quietly — least of all to a money field.
 */

const ROOT = process.cwd()

const FEATURE_DIRECTORY = join(ROOT, 'src', 'features', 'analytics')
const SERVICE_DIRECTORY = join(ROOT, 'src', 'services', 'ml')
const PAGE_FILE = join(ROOT, 'src', 'pages', 'AnalyticsPage.tsx')

/** Every `.ts`/`.tsx` file this stage added, minus its own tests. */
function analyticsSources(): { name: string; text: string }[] {
  const files: { name: string; text: string }[] = []
  for (const directory of [FEATURE_DIRECTORY, SERVICE_DIRECTORY]) {
    for (const entry of readdirSync(directory)) {
      if (!/\.tsx?$/.test(entry) || entry.includes('.test.')) {
        continue
      }
      files.push({ name: entry, text: readFileSync(join(directory, entry), 'utf8') })
    }
  }
  files.push({ name: 'AnalyticsPage.tsx', text: readFileSync(PAGE_FILE, 'utf8') })
  return files
}

/** Comments explain the rules; they must not trip them. */
function withoutComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

const SOURCES = analyticsSources()

const FORBIDDEN: readonly (readonly [string, RegExp])[] = [
  ['parseFloat', /\bparseFloat\s*\(/],
  ['parseInt', /\bparseInt\s*\(/],
  ['toFixed', /\.toFixed\s*\(/],
  ['reduce', /\.reduce\s*\(/],
  ['compound addition', /\+=/],
  ['dangerouslySetInnerHTML', /dangerouslySetInnerHTML/],
  ['innerHTML', /\binnerHTML\b/],
  ['eval', /\beval\s*\(/],
  ['new Function', /new\s+Function\s*\(/],
  ['console', /\bconsole\./],
  ['Bearer', /Bearer/],
]

describe('the analytics feature performs no business arithmetic', () => {
  it('reads the files it means to read', () => {
    // Guards every assertion below: an empty scan would otherwise pass silently.
    expect(SOURCES.length).toBeGreaterThanOrEqual(5)
    expect(SOURCES.map((file) => file.name)).toContain('AnalyticsPage.tsx')
    expect(SOURCES.map((file) => file.name)).toContain('series.ts')
    expect(SOURCES.map((file) => file.name)).toContain('mlService.ts')
  })

  it.each(FORBIDDEN)('contains no %s', (_label, pattern) => {
    for (const file of SOURCES) {
      expect(withoutComments(file.text)).not.toMatch(pattern)
    }
  })

  it('converts a server value in exactly one documented place', () => {
    const converting = SOURCES.filter((file) => /\bNumber\s*\(/.test(withoutComments(file.text)))
    expect(converting.map((file) => file.name)).toEqual(['series.ts'])
  })

  it('never converts a money field', () => {
    for (const file of SOURCES) {
      const text = withoutComments(file.text)
      for (const field of ['amount', 'tax_amount', 'room_revenue', 'adr', 'revpar']) {
        expect(text).not.toMatch(new RegExp(`Number\\s*\\(\\s*[\\w.]*${field}`))
      }
    }
  })

  it('renders no total the server did not send', () => {
    const table = readFileSync(join(FEATURE_DIRECTORY, 'BreakdownTable.tsx'), 'utf8')
    // No arithmetic operator between two values, and no "total" column.
    expect(withoutComments(table)).not.toMatch(/\.reduce\s*\(/)
    expect(withoutComments(table)).not.toMatch(/>\s*Total\s*</)
  })

  it('reaches the backend only through the shared API client', () => {
    for (const file of SOURCES) {
      const text = withoutComments(file.text)
      expect(text).not.toMatch(/\bfetch\s*\(/)
      expect(text).not.toMatch(/XMLHttpRequest/)
      if (/\bapi\./.test(text)) {
        expect(text).toMatch(/from '@\/services\/api\/client'/)
      }
    }
  })

  it('never builds a hotel-scoped path from anything but a public identifier', () => {
    const service = readFileSync(join(SERVICE_DIRECTORY, 'mlService.ts'), 'utf8')
    // The path takes `hotelPublicId`; an internal numeric id has no way in.
    expect(service).toMatch(/\/hotels\/\$\{hotelPublicId\}\//)
    expect(service).not.toMatch(/hotel_id/)
  })
})

describe('the demand estimate makes no claim the model does not support', () => {
  const panel = readFileSync(join(FEATURE_DIRECTORY, 'DemandForecastPanel.tsx'), 'utf8')
  const rendered = withoutComments(panel)

  /* Words that would mean a range the model does not produce. A point forecaster has no
     distribution, so any of these on screen would be invented. */
  it.each(['confidence', 'interval', 'guaranteed', '±'])(
    'renders no uncertainty the model does not produce: %s',
    (word) => {
      expect(rendered.toLowerCase()).not.toContain(word.toLowerCase())
    },
  )

  /*
   * "accuracy" is not banned outright -- the panel is REQUIRED to say accuracy has not been
   * established, and a test forbidding the word would push the UI into silence about exactly
   * the thing it must disclose. What is banned is a positive claim.
   */
  it.each([
    /is accurate/i,
    /accuracy of\s*\d/i,
    /\d\s*%\s*accurate/i,
    /* Word boundaries matter: `provenance` contains `proven`, and the panel renders a
       provenance list. An unbounded alternation here failed on the component's own markup. */
    /(proven|reliable|trustworthy)/i,
  ])(
    'makes no positive accuracy claim: %s',
    (pattern) => {
      expect(rendered).not.toMatch(pattern)
    },
  )

  it('states that accuracy has not been established', () => {
    expect(rendered).toMatch(/has not been established/i)
  })

  it('surfaces the model version and its production-ready flag rather than hiding them', () => {
    expect(rendered).toContain('model_version')
    expect(rendered).toContain('production_ready')
  })

  it('labels the number as modelled', () => {
    expect(rendered).toContain('Modelled estimate')
  })
})
