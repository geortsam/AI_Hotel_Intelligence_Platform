import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * What this feature must NOT do, asserted against its own source.
 *
 * `intelligence.test.tsx` proves that a particular screen sends a particular request and
 * renders a particular response. It cannot prove that **no code path anywhere** computes a
 * forecast, invents a severity, or reaches the network without the shared client. Five
 * properties this stage is asked to hold are properties of the code, so they are checked in
 * the code:
 *
 * 1. **No intelligence is computed in the browser.** No forecast, no trend classification, no
 *    anomaly detection, no percentage change, no severity. The server decides; this renders.
 * 2. **Facts and predictions are never blended.** Nothing adds an on-the-books figure to a
 *    prediction, and nothing averages them.
 * 3. **No currency is converted or combined.** There is no FX rate in this codebase, and a
 *    cross-currency total would invent one.
 * 4. **Every request goes through the shared client**, hotel-scoped, with no invented
 *    parameter.
 * 5. **No fabricated intelligence of any kind** -- no placeholder insight, no demo forecast,
 *    no random confidence, no hard-coded anomaly.
 *
 * `Number()` is the one conversion allowed, and only where a value becomes a **chart
 * coordinate** -- the same boundary Stage 5.4's dashboard chart uses. It is bounded by name
 * below rather than banned, because a pixel is a float whatever the value was.
 */

/*
 * Resolved from the runner's working directory, which vitest sets to the package root.
 * `import.meta.url` is not a `file:` URL under the jsdom environment.
 */
const ROOT = process.cwd()

const DIRECTORIES = [
  join(ROOT, 'src', 'features', 'intelligence'),
  join(ROOT, 'src', 'services', 'intelligence'),
]

const EXTRA_FILES = [
  join(ROOT, 'src', 'pages', 'IntelligencePage.tsx'),
  join(ROOT, 'src', 'types', 'intelligence.ts'),
]

/** Every `.ts`/`.tsx` file this stage added, minus its own tests. */
function sourcesOf(): { name: string; text: string }[] {
  const files: { name: string; text: string }[] = []
  for (const directory of DIRECTORIES) {
    for (const entry of readdirSync(directory)) {
      if (!/\.tsx?$/.test(entry) || entry.includes('.test.')) {
        continue
      }
      files.push({ name: entry, text: readFileSync(join(directory, entry), 'utf8') })
    }
  }
  for (const path of EXTRA_FILES) {
    files.push({ name: path.split(/[\\/]/).pop()!, text: readFileSync(path, 'utf8') })
  }
  return files
}

/**
 * Strip comments before matching.
 *
 * These files explain at length *why* they compute no forecast -- so a naive search finds the
 * prose promising the absence and fails the test proving it. Only executable text is searched.
 */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1')
}

const sources = sourcesOf()

function named(name: string): string {
  return code(sources.find((file) => file.name === name)!.text)
}

/**
 * The detector, checked against known positives before it is trusted on negatives.
 *
 * A source scan is only as good as its stripper: if `code()` were too aggressive it would
 * return almost nothing and every assertion below would pass on an empty string.
 */
describe('the source scan itself works', () => {
  it('strips prose but keeps the code', () => {
    const service = sources.find((file) => file.name === 'intelligenceService.ts')!
    const stripped = code(service.text)
    expect(service.text).toMatch(/observes and explains/)
    expect(stripped).not.toMatch(/observes and explains/)
    expect(stripped).toMatch(/api\.get<OccupancyForecast>/)
    expect(stripped).toMatch(/intelligence\/forecast\/revenue/)
  })

  it('leaves most of every file behind', () => {
    for (const file of sources) {
      expect(code(file.text).length, `${file.name} was over-stripped`).toBeGreaterThan(
        file.text.length / 5,
      )
    }
  })

  it.each([
    ['a raw fetch', 'const r = await fetch("/api/v1/x")', /\bfetch\s*\(/],
    ['a hand-built Bearer', "headers: { Authorization: `Bearer ${t}` }", /Bearer/],
    ['dangerous HTML', '<div dangerouslySetInnerHTML={{ __html: x }} />', /dangerouslySetInnerHTML/],
    ['a client-side mean', 'const mean = total / points.length', /\/\s*\w+\.length/],
  ])('detects %s where one really exists', (_label, control, pattern) => {
    // Positive controls. If any of these stops matching, the matching negative assertion
    // below would stop catching a real breach.
    expect(code(control)).toMatch(pattern)
  })

  it('covers every source file this stage added', () => {
    expect(sources.map((file) => file.name).sort()).toEqual([
      'AnomalyList.tsx',
      'ForecastChart.tsx',
      'InsightList.tsx',
      'IntelligencePage.tsx',
      'TrendSummary.tsx',
      'intelligence.ts',
      'intelligenceService.ts',
      'useIntelligence.ts',
    ])
  })
})

describe('no intelligence is computed in the browser', () => {
  it.each([
    ['parseFloat', /\bparseFloat\s*\(/],
    ['toFixed', /\.toFixed\s*\(/],
    ['reduce', /\.reduce\s*\(/],
    ['a division by a length', /\/\s*\w+\.length/],
  ])('contains no %s', (label, pattern) => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not use ${label}`).not.toMatch(pattern)
    }
  })

  it('confines compound addition to the axis tick loop', () => {
    // `value += step` walks the y axis. It is an axis, not a figure: it never touches a
    // response value, and it is the only `+=` this feature is allowed.
    const users = sources.filter((file) => /\+=/.test(code(file.text)))
    expect(users.map((file) => file.name)).toEqual(['ForecastChart.tsx'])

    const chart = code(users[0]!.text)
    expect(chart.match(/\+=/g)).toHaveLength(1)
    expect(chart).toMatch(/for \(let value = 0; value <= upper \+ step \/ 2; value \+= step\)/)
  })

  it('classifies no trend and detects no anomaly', () => {
    const deriving = [
      /\bz[ _]?score\s*=/i,
      /medianAbsoluteDeviation\s*=|\bcomputeMedian\b|\bmedianOf\b/,
      /direction\s*=\s*['"](?:increasing|decreasing|stable|above|below)['"]/,
      /severity\s*=\s*['"](?:info|warning|critical)['"]/,
      /\bisAnomal|\bdetectAnomal|\bclassifyTrend|\bpredict\w*\s*\(/i,
      /\bthreshold\s*=\s*[\d.]/,
    ]
    for (const file of sources) {
      const stripped = code(file.text)
      for (const pattern of deriving) {
        expect(stripped, `${file.name} must not derive a verdict`).not.toMatch(pattern)
      }
    }
  })

  it('reads the verdict, the threshold and the severity straight off the response', () => {
    expect(named('TrendSummary.tsx')).toMatch(/PRESENTATION\[trend\.direction\]/)
    expect(named('TrendSummary.tsx')).toMatch(/trend\.threshold/)
    expect(named('InsightList.tsx')).toMatch(/SEVERITY\[insight\.severity\]/)
    expect(named('AnomalyList.tsx')).toMatch(/anomaly\.modified_z_score/)
    expect(named('AnomalyList.tsx')).toMatch(/anomaly\.threshold/)
  })

  it('confines Number() to chart coordinates and select values', () => {
    for (const file of sources) {
      const uses = code(file.text).match(/Number\s*\(\s*([^)]*)/g) ?? []
      for (const use of uses) {
        expect(
          use,
          `${file.name} may convert only a plotted value or a control's value: ${use}`,
        ).toMatch(
          /Number\s*\(\s*(?:point\.|event\.target\.value|bucket|forecast)/,
        )
      }
    }
  })

  it('computes only date arithmetic, and only to decide what to ask for', () => {
    const hook = named('useIntelligence.ts')
    // `shiftDate` is the Stage 5.4 helper; the only sums here are day offsets.
    expect(hook).toMatch(/shiftDate\(today, -\(params\.observationDays - 1\)\)/)
    expect(hook).toMatch(/shiftDate\(today, 1\)/)
    // It may NAME the resources it fetches -- `anomalies`, `trend` -- but must not derive
    // one. No verdict, median or prediction is ever assigned here.
    expect(hook).not.toMatch(/(?:direction|median|predicted|severity|score)\s*=[^=]/i)
  })
})

describe('facts and predictions are never blended', () => {
  it('adds no on-the-books figure to a prediction anywhere', () => {
    const blending =
      /on_the_books\w*\s*[+*/]|[+*/]\s*\w*on_the_books|actual\s*\+\s*predicted|predicted\s*\+\s*actual/
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not blend a fact with an estimate`).not.toMatch(
        blending,
      )
    }
  })

  it('carries them as two separate fields all the way to the chart', () => {
    const chart = named('ForecastChart.tsx')
    expect(chart).toMatch(/readonly actual: number/)
    expect(chart).toMatch(/readonly predicted: number \| null/)
    // Two draws, never one combined series.
    expect(chart).toMatch(/actualSegments/)
    expect(chart).toMatch(/predictedSegments/)
  })

  it('draws no prediction where the server sent none', () => {
    const chart = named('ForecastChart.tsx')
    // A null prediction breaks the line rather than being drawn at zero.
    expect(chart).toMatch(/point\.predicted === null \? null :/)
    expect(chart).not.toMatch(/predicted\s*\?\?\s*0|predicted \|\| 0/)
  })
})

describe('no currency is converted or combined', () => {
  it('names no exchange rate anywhere', () => {
    const fx = /\bfx\b|exchangeRate|convertCurrency|toBaseCurrency|\brate\s*\*/i
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not convert a currency`).not.toMatch(fx)
    }
  })

  it('renders one chart per currency bucket', () => {
    const page = named('IntelligencePage.tsx')
    expect(page).toMatch(/forecast\.currencies\.map\(\(bucket\) =>/)
    expect(page).toMatch(/formatMoney\([^,]+, bucket\.currency\)/)
  })
})

describe('every request goes through the shared client', () => {
  it.each([
    ['a raw fetch', /\bfetch\s*\(/],
    ['a hand-built Authorization header', /Bearer/],
    ['localStorage', /\blocalStorage\b/],
    ['sessionStorage', /\bsessionStorage\b/],
    ['console', /\bconsole\.\w+\s*\(/],
    ['dangerouslySetInnerHTML', /dangerouslySetInnerHTML/],
    ['innerHTML', /\.innerHTML\b/],
    ['eval', /\beval\s*\(/],
    ['new Function', /new\s+Function\s*\(/],
  ])('contains no %s', (label, pattern) => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not contain ${label}`).not.toMatch(pattern)
    }
  })

  it('imports the client in exactly one module', () => {
    const importers = sources.filter((file) => /@\/services\/api\/client/.test(file.text))
    expect(importers.map((file) => file.name)).toEqual(['intelligenceService.ts'])
  })

  it('scopes every path to the hotel’s public identifier', () => {
    const service = named('intelligenceService.ts')
    const paths = service.match(/`\/hotels\/[^`]*`/g) ?? []
    expect(paths).toHaveLength(5)
    for (const path of paths) {
      expect(path).toMatch(/^`\/hotels\/\$\{hotelPublicId\}\/intelligence\//)
    }
    // No flat portfolio-wide route exists, and none is called.
    expect(service).not.toMatch(/api\.get<[^>]*>\('\/intelligence/)
  })

  it('sends only the parameters the routers declare', () => {
    const service = named('intelligenceService.ts')

    /*
     * Every `query: { ... }` block, parsed for the keys inside it regardless of how it is
     * laid out. An earlier version of this matched keys only at one indentation, and a
     * mutation that added `metric: 'occupancy'` inline slipped straight past it -- which is
     * exactly the breach this assertion exists to catch, since the backend ignores an
     * unknown query parameter silently and an invented control would look like it worked.
     */
    const blocks: string[] = []
    for (const start of service.matchAll(/query:\s*\{/g)) {
      // Brace matching, not a regex: a `query: { ... }` can be written on one line or on
      // six, and a lazy pattern gets the wrong end of at least one of those.
      let depth = 0
      let index = start.index! + start[0].length - 1
      const from = index
      do {
        if (service[index] === '{') depth += 1
        if (service[index] === '}') depth -= 1
        index += 1
      } while (depth > 0 && index < service.length)
      blocks.push(service.slice(from, index))
    }

    /*
     * Ten `query: {` blocks exist — five are the method signatures' camelCase parameter
     * types, five are the snake_case objects actually put on the wire. Only the latter are
     * the contract, and `date_from` is what distinguishes them.
     */
    const sent = blocks.filter((block) => block.includes('date_from'))
    expect(sent).toHaveLength(5)

    const keys = new Set<string>()
    for (const block of sent) {
      for (const match of block.matchAll(/(?:^|[{,]|\s)([a-z_][a-z0-9_]*)\s*:/gi)) {
        if (match[1] !== 'query') {
          keys.add(match[1]!)
        }
      }
    }
    expect([...keys].sort()).toEqual(['date_from', 'date_to', 'horizon_days', 'training_days'])
  })

  it('issues no write of any kind', () => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not write`).not.toMatch(
        /api\.(post|patch|put|delete)\b/,
      )
    }
  })
})

describe('nothing is fabricated', () => {
  it('contains no random source', () => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not be random`).not.toMatch(
        /Math\.random|faker|mockForecast|sampleData|DEMO_|PLACEHOLDER_/i,
      )
    }
  })

  it('hard-codes no forecast, confidence or anomaly count', () => {
    const page = named('IntelligencePage.tsx')
    // Confidence comes from the point that carries it, never from a literal.
    expect(page).toMatch(/confidence_level/)
    expect(page).not.toMatch(/confidence\s*=\s*['"0-9]/)
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not assert an AI claim`).not.toMatch(
        /AI[- ]generated|AI predicts|powered by AI|our AI\b/i,
      )
    }
  })

  it('states in the page that the findings are templates, not generated text', () => {
    const page = named('IntelligencePage.tsx')
    expect(page).toMatch(/the same data always\s*\n?\s*produces the same sentence/)
    expect(page).toMatch(/Nothing here is generated text/)
  })

  it('uses no internal identifier', () => {
    // Database keys — not the local `option.id` a tab list uses to name its own panels,
    // which is a UI label and never leaves the browser.
    const internal =
      /\b(?:hotel_id|booking_id|room_id|user_id|amenity_id)\b|\b(?:hotel|booking|point|row|record|anomaly|insight)\.id\b/
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not use an internal id`).not.toMatch(internal)
    }
    // And the identifier that IS sent is the public one.
    expect(named('intelligenceService.ts')).toMatch(/hotelPublicId/)
  })
})

describe('the locked dashboard chart is reused, not reopened', () => {
  it('imports the Stage 5.4 date helpers rather than copying them', () => {
    expect(named('useIntelligence.ts')).toMatch(
      /import \{ shiftDate, todayInZone \} from '@\/features\/dashboard\/period'/,
    )
  })

  it('does not import or wrap TrendChart, which cannot express two series', () => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not reuse the single-series chart`).not.toMatch(
        /TrendChart/,
      )
    }
  })
})
