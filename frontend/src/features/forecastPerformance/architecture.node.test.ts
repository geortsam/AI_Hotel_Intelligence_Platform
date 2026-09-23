import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * What the forecast-performance feature must not contain, asserted against its own source.
 *
 * ## Why a source test rather than a rendering test
 *
 * `forecastPerformance.test.tsx` proves that particular figures on a particular screen came
 * from the server. It cannot prove that *no* figure is ever computed here — a derived metric
 * might appear only for a model version the fixture lacks, or only once a second segment has
 * observations. The property Stage 7.4 is required to hold is architectural: **the browser
 * computes no accuracy**, for any input, ever. That is a property of the code, so it is
 * checked in the code. This mirrors the analytics and finance features, written for the same
 * reason.
 *
 * ## What each pattern would mean if it appeared
 *
 * * `Math.` — the only reason to reach for it here is to derive a metric. MAE, RMSE, sMAPE and
 *   every quantile are computed offline under a checksummed protocol; a second definition in a
 *   browser would disagree with the first the moment either moved.
 * * `.reduce(` — a client-side aggregation, presented as a figure the server never sent. The
 *   accuracy panel deliberately shows no combined number.
 * * `+=`, or `-` between two measured values — the same thing written out. A difference
 *   between an actual and a prediction *is* an error metric, and computing one here would be
 *   this stage silently reimplementing Stage 6.9.
 * * `parseFloat` / `parseInt` on a server value puts a binary float where a measured quantity
 *   was.
 * * `toFixed` is rounding a figure this application does not own.
 * * `dangerouslySetInnerHTML`, `innerHTML`, `eval`, `new Function` are the injection surface.
 * * `console.` in shipped code puts request data somewhere nobody audits.
 * * `Bearer` anywhere but the API client means a second place assembles credentials.
 */

const ROOT = process.cwd()

const FEATURE_DIRECTORY = join(ROOT, 'src', 'features', 'forecastPerformance')
const SERVICE_FILE = join(ROOT, 'src', 'services', 'ml', 'mlService.ts')

/** Every `.ts`/`.tsx` file this stage added to the feature, minus its own tests. */
function featureSources(): { name: string; text: string }[] {
  const files = readdirSync(FEATURE_DIRECTORY)
    .filter((entry) => /\.tsx?$/.test(entry) && !entry.includes('.test.'))
    .map((entry) => ({ name: entry, text: readFileSync(join(FEATURE_DIRECTORY, entry), 'utf8') }))
  files.push({ name: 'mlService.ts', text: readFileSync(SERVICE_FILE, 'utf8') })
  return files
}

/** Comments explain the rules; they must not trip them. */
function withoutComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

const SOURCES = featureSources()

const FORBIDDEN: readonly (readonly [string, RegExp])[] = [
  ['Math', /\bMath\./],
  ['parseFloat', /\bparseFloat\s*\(/],
  ['parseInt', /\bparseInt\s*\(/],
  ['toFixed', /\.toFixed\s*\(/],
  ['reduce', /\.reduce\s*\(/],
  ['compound addition', /\+=/],
  ['compound subtraction', /-=/],
  ['dangerouslySetInnerHTML', /dangerouslySetInnerHTML/],
  ['innerHTML', /\binnerHTML\b/],
  ['eval', /\beval\s*\(/],
  ['new Function', /new\s+Function\s*\(/],
  ['console', /\bconsole\./],
  ['Bearer', /Bearer/],
]

describe('the forecast-performance feature computes no metric', () => {
  it('reads the files it means to read', () => {
    // Guards every assertion below: an empty scan would otherwise pass silently.
    expect(SOURCES.length).toBeGreaterThanOrEqual(5)
    const names = SOURCES.map((file) => file.name)
    expect(names).toContain('useForecastPerformance.ts')
    expect(names).toContain('AccuracyPanel.tsx')
    expect(names).toContain('DistributionPanel.tsx')
    expect(names).toContain('pairing.ts')
    expect(names).toContain('mlService.ts')
  })

  it.each(FORBIDDEN)('contains no %s', (_label, pattern) => {
    for (const file of SOURCES) {
      expect(withoutComments(file.text)).not.toMatch(pattern)
    }
  })

  it('never converts a server value with Number()', () => {
    /*
     * Unlike the analytics feature, this one needs no conversion at all: every value it
     * renders arrives as a JSON number already, and every one it renders as text goes through
     * the shared `formatCount`. So the permitted count here is zero rather than one.
     */
    const converting = SOURCES.filter((file) => /\bNumber\s*\(/.test(withoutComments(file.text)))
    expect(converting.map((file) => file.name)).toEqual([])
  })

  it('never subtracts one measured value from another', () => {
    /*
     * The one arithmetic operator a forecast-vs-actual screen is genuinely tempted by:
     * `actual - predicted` is an error, and error is Stage 6.9's to define. The API sends
     * differences pre-computed where it sends them at all.
     */
    for (const file of SOURCES) {
      const text = withoutComments(file.text)
      expect(text).not.toMatch(/actual\s*-\s*predict/i)
      expect(text).not.toMatch(/predicted\s*-\s*actual/i)
      expect(text).not.toMatch(/\bobserved\s*-\s*baseline\b/i)
    }
  })

  it('names no metric it would have to compute to name', () => {
    // MAE/RMSE/sMAPE may be *displayed* by label, but never assembled from parts here.
    for (const file of SOURCES) {
      const text = withoutComments(file.text)
      expect(text).not.toMatch(/\bsqrt\b/i)
      expect(text).not.toMatch(/\bmeanOf\b|\bcomputeMae\b|\bcalculateRmse\b|\bquantileOf\b/i)
    }
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
})

describe('tenant isolation is by public identifier only', () => {
  const service = readFileSync(SERVICE_FILE, 'utf8')

  it.each([
    '/ml/forecast-accuracy',
    '/ml/prediction-distribution',
    '/ml/demand-predictions',
  ])('builds %s under the hotel segment', (path) => {
    expect(service).toContain(`/hotels/\${hotelPublicId}${path}`)
  })

  it('never names an internal hotel identifier', () => {
    for (const file of SOURCES) {
      // `hotelPublicId` is the only identifier on this path; nothing numeric may appear.
      expect(withoutComments(file.text).replace(/hotelPublicId/g, '')).not.toMatch(/hotel_?[Ii]d/)
    }
  })

  it('sends no hotel identifier as a query parameter', () => {
    const queries = service.match(/query:\s*\{[\s\S]*?\}/g) ?? []
    expect(queries.length).toBeGreaterThanOrEqual(4)
    for (const query of queries) {
      expect(query).not.toMatch(/hotel/i)
    }
  })
})

describe('the measured figures make no claim the API does not', () => {
  const panels = SOURCES.filter((file) => file.name.endsWith('Panel.tsx'))
  const section = SOURCES.find((file) => file.name === 'ForecastPerformanceSection.tsx')!

  it('found the components it means to check', () => {
    expect(panels.map((file) => file.name).sort()).toEqual([
      'AccuracyPanel.tsx',
      'DistributionPanel.tsx',
    ])
  })

  /* Words that would assert something the protocols explicitly refuse to establish. Word
     boundaries matter: `provenance` contains `proven`, and both panels render a provenance
     line. An unbounded alternation here would fail on the components' own markup. */
  it.each([
    /\bis accurate\b/i,
    /\baccuracy of\s*\d/i,
    /\d\s*%\s*accurate/i,
    /\b(proven|reliable|trustworthy|validated)\b/i,
    /\bproduction[- ]ready\b/i,
    /\bgeneralis[ez]/i,
  ])('makes no positive accuracy claim: %s', (pattern) => {
    for (const file of [...panels, section]) {
      expect(withoutComments(file.text)).not.toMatch(pattern)
    }
  })

  it('states that accuracy has not been established', () => {
    const accuracy = panels.find((file) => file.name === 'AccuracyPanel.tsx')!
    expect(accuracy.text).toMatch(/has not been established/i)
  })

  it('renders the server statement rather than a paraphrase of it', () => {
    for (const panel of panels) {
      expect(panel.text).toContain('measurement.statement')
    }
  })

  it('surfaces the protocol that produced the figures', () => {
    for (const panel of panels) {
      expect(panel.text).toContain('protocol_version')
      expect(panel.text).toContain('protocol_checksum')
    }
  })

  it('shows the settlement lag outside a tooltip', () => {
    const accuracy = panels.find((file) => file.name === 'AccuracyPanel.tsx')!
    expect(accuracy.text).toContain('settlement_lag_days')
    // A `title` attribute is a tooltip: the requirement is that the lag is on screen.
    expect(accuracy.text).not.toMatch(/title=\{[^}]*settlement/i)
  })
})

describe('the distribution reaches no verdict', () => {
  const distribution = SOURCES.find((file) => file.name === 'DistributionPanel.tsx')!
  const rendered = withoutComments(distribution.text)

  /* The protocol contains no threshold, verdict, alert or ranking, so the component that
     renders it must not introduce one. `drift` is the headline: the word would assert a
     finding the platform explicitly does not compute. */
  it.each(['drift', 'anomaly', 'threshold', 'verdict', 'alert', 'significant', 'degraded'])(
    'renders no %s',
    (word) => {
      expect(rendered.toLowerCase()).not.toContain(word.toLowerCase())
    },
  )

  it('distinguishes an absent comparison from an unchanged one', () => {
    expect(distribution.text).toMatch(/not the same as finding no change/i)
  })
})

describe('no confidence band is drawn', () => {
  const section = SOURCES.find((file) => file.name === 'ForecastPerformanceSection.tsx')!

  it('passes a null confidence label to the chart', () => {
    expect(section.text).toMatch(/confidenceLabel=\{null\}/)
  })

  it('supplies no interval bounds anywhere in the feature', () => {
    const pairing = SOURCES.find((file) => file.name === 'pairing.ts')!
    // `ForecastChart` draws the band only where both bounds are present. They never are.
    /*
     * Asserted positively, on the captured value. A negative pattern does not work here:
     * `\s*` can match zero characters, so both `/lower:\s*[^n]/` and `/lower:\s*(?!null)/`
     * backtrack to a zero-width match and are satisfied by the space in `lower: null` itself.
     * Collecting what each key is actually assigned says what is meant and cannot pass
     * vacuously.
     */
    const assigned = (key: string): string[] =>
      [...withoutComments(pairing.text).matchAll(new RegExp(`${key}:\\s*([A-Za-z0-9_.]+)`, 'g'))]
        .map((match) => match[1] ?? '')

    expect(assigned('lower')).toEqual(['null'])
    expect(assigned('upper')).toEqual(['null'])
  })
})

describe('the accuracy request is the one that can be refused', () => {
  const hook = SOURCES.find((file) => file.name === 'useForecastPerformance.ts')!

  it('settles every request independently', () => {
    // `Promise.all` would make one 403 take the whole section down.
    expect(hook.text).toContain('Promise.allSettled')
    expect(withoutComments(hook.text)).not.toMatch(/Promise\.all\s*\(/)
  })

  it('carries each outcome separately rather than collapsing them into one error', () => {
    for (const field of ['daily', 'predictions', 'distribution', 'accuracy']) {
      expect(hook.text).toMatch(new RegExp(`${field}: Outcome<`))
    }
  })

  it('makes no authorization decision of its own', () => {
    /*
     * The frontend has no role information: `HotelResponse` carries none and `/auth/me`
     * deliberately carries none. A client-side role check would therefore be invented, and a
     * second authorization decision in a place that cannot enforce one. The server's 403 is
     * the answer.
     */
    for (const file of SOURCES) {
      const text = withoutComments(file.text)
      expect(text).not.toMatch(/\bHotelRole\b/)
      expect(text).not.toMatch(/\bisManager\b|\bhasRole\b|\bcanViewAccuracy\b/)
    }
  })
})
