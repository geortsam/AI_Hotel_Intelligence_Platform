import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * The statistics this feature must NOT compute, and the HTML it must NOT render, asserted
 * against its own source.
 *
 * ## Why a source test rather than a rendering test
 *
 * A rendering test can prove that a particular average is absent from a particular screen. It
 * cannot prove that no average is ever computed -- the wrong figure might appear only for a
 * source the fixture lacks, or only once a second page is loaded. Two properties this stage
 * is asked to hold are properties of the code, so they are checked in the code:
 *
 * 1. **The browser derives no review statistic.** Counts, averages and the rating
 *    distribution come from `analytics/reviews`; nothing here reduces, sums or averages a
 *    page of rows. That matters more on reviews than on most screens, because two rating
 *    scales share one list: averaging a five-point score with a ten-point one produces a
 *    number that is not a rating at all.
 * 2. **Review text never becomes markup.** `body`, `title` and `reviewer_name` are written by
 *    the public on platforms this application does not control, so a single
 *    `dangerouslySetInnerHTML` anywhere in this feature would be a stored-XSS path from a
 *    Booking.com review form into this console.
 *
 * The behavioural half is in `reviews.test.tsx`.
 *
 * ## The one deliberate calculation
 *
 * `ReviewSummary` scales each distribution bar to the largest bucket, which is what a chart's
 * y-axis does. It is an axis, not a figure: the count beside every bar is the number the
 * server sent, and the bar is `aria-hidden`. `the axis calculation is confined to the bar` below
 * pins it to that one component and that one expression, so it cannot spread into a statistic.
 */

/*
 * Resolved from the runner's working directory, which vitest sets to the package root.
 * `import.meta.url` is not a `file:` URL under the jsdom environment.
 */
const ROOT = process.cwd()

const REVIEW_DIRECTORIES = [
  join(ROOT, 'src', 'features', 'reviews'),
  join(ROOT, 'src', 'services', 'reviews'),
]

const PAGE_FILES = [join(ROOT, 'src', 'pages', 'ReviewsPage.tsx')]

/** Every `.ts`/`.tsx` file this stage added, minus its own tests. */
function reviewSources(): { name: string; text: string }[] {
  const files: { name: string; text: string }[] = []
  for (const directory of REVIEW_DIRECTORIES) {
    for (const entry of readdirSync(directory)) {
      if (!/\.tsx?$/.test(entry) || entry.includes('.test.')) {
        continue
      }
      files.push({ name: entry, text: readFileSync(join(directory, entry), 'utf8') })
    }
  }
  for (const path of PAGE_FILES) {
    files.push({ name: 'ReviewsPage.tsx', text: readFileSync(path, 'utf8') })
  }
  return files
}

/**
 * Strip comments before matching.
 *
 * These files explain at length *why* they do not average a page of ratings -- so a naive
 * search finds the prose promising the absence and fails the test proving it. Only executable
 * text is searched.
 */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1')
}

const sources = reviewSources()

/**
 * The detector, checked against known positives before it is trusted on negatives.
 *
 * A source scan is only as good as its stripper: if `code()` were too aggressive it would
 * return almost nothing and every assertion below would pass on an empty string.
 */
describe('the source scan itself works', () => {
  it('strips prose but keeps the code', () => {
    const service = sources.find((file) => file.name === 'reviewService.ts')!
    const stripped = code(service.text)
    expect(service.text).toMatch(/no DELETE/)
    expect(stripped).not.toMatch(/no DELETE/)
    expect(stripped).toMatch(/api\.get<Page<Review>>/)
    expect(stripped).toMatch(/analytics\/reviews/)
  })

  it('leaves most of every file behind', () => {
    for (const file of sources) {
      expect(code(file.text).length, `${file.name} was over-stripped`).toBeGreaterThan(
        file.text.length / 5,
      )
    }
  })

  it('detects a reduce where one really exists', () => {
    // The positive control: a file known to contain the pattern. If this stops matching, the
    // negative assertions below would stop catching a real breach.
    const chart = readFileSync(join(ROOT, 'src', 'features', 'dashboard', 'TrendChart.tsx'), 'utf8')
    expect(code(chart)).toMatch(/\.map\s*\(/)
  })

  it('covers every source file this stage added', () => {
    expect(sources.map((file) => file.name).sort()).toEqual([
      'ModerationControls.tsx',
      'ReviewCard.tsx',
      'ReviewFilters.tsx',
      'ReviewForm.tsx',
      'ReviewSummary.tsx',
      'ReviewsPage.tsx',
      'reviewService.ts',
      'useReviews.ts',
      'vocabulary.ts',
    ])
  })
})

describe('the reviews feature computes no review statistic', () => {
  it.each([
    ['reduce', /\.reduce\s*\(/],
    ['compound addition', /\+=/],
    ['parseFloat', /\bparseFloat\s*\(/],
    ['parseInt', /\bparseInt\s*\(/],
    ['Number()', /\bNumber\s*\(/],
    ['toFixed', /\.toFixed\s*\(/],
    ['rounding', /\bMath\.(?:round|floor|ceil|abs)\s*\(/],
  ])('contains no %s', (_label, pattern) => {
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not use ${_label}`).not.toMatch(pattern)
    }
  })

  it('never sums or averages a rating or a count', () => {
    const aggregating =
      /\b(?:rating|rating_normalized|review_count|published_count|count)\w*\s*\+(?!\+)/
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not aggregate`).not.toMatch(aggregating)
    }
  })

  it('names no statistic the API does not report', () => {
    // sentiment, satisfaction score, response rate, moderation score: none is a field of any
    // endpoint, so none may be a label on this page.
    const invented =
      /\b(?:sentiment|satisfaction score|response rate|moderation score|nps)\b/i
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not name an invented statistic`).not.toMatch(
        invented,
      )
    }
  })

  it('confines the axis calculation to the distribution bar', () => {
    // `Math.max` scales the bars to the tallest bucket, exactly as a chart axis does. It is
    // allowed in one file, once, and only in the expression that produces a CSS width.
    const users = sources.filter((file) => /Math\.max/.test(code(file.text)))
    expect(users.map((file) => file.name)).toEqual(['ReviewSummary.tsx'])

    const summary = code(users[0]!.text)
    expect(summary.match(/Math\.max/g)).toHaveLength(1)
    // The bar width is the one place a review figure is divided by anything. Counting every
    // `/` would count JSX closing tags and import paths, so this looks for the division of a
    // countable field specifically.
    const dividing = summary.match(/\b(?:count|review_count|published_count|length)\s*\//g) ?? []
    expect(dividing).toHaveLength(1)
    expect(summary).toMatch(/width: `\$\{\(bucket\.count \/ axisMax\) \* 100\}%`/)
    // And it produces a CSS width, never text: the bar is hidden from assistive technology
    // because the count printed beside it already states the number.
    expect(summary).toMatch(/className=\{styles\.bar\}[\s\S]{0,140}aria-hidden="true"/)
  })

  it('reads every displayed statistic off the analytics response', () => {
    const summary = code(sources.find((file) => file.name === 'ReviewSummary.tsx')!.text)
    for (const field of [
      'totals.review_count',
      'totals.published_count',
      'totals.average_rating_normalized',
    ]) {
      expect(summary, `${field} must come from the server`).toContain(field)
    }
  })
})

describe('the reviews feature renders no markup from review text', () => {
  it.each([
    ['dangerouslySetInnerHTML', /dangerouslySetInnerHTML/],
    ['innerHTML', /\.innerHTML\b/],
    ['eval', /\beval\s*\(/],
    ['new Function', /new\s+Function\s*\(/],
    // Matched as an import specifier rather than as a word: "marked answered" is ordinary
    // copy on this page, and a bare word search would flag the button that writes
    // `responded_at`.
    [
      'a markdown or sanitiser import',
      /from\s+['"](?:marked|remark|markdown-it|showdown|dompurify|sanitize-html)/i,
    ],
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
    const importers = sources.filter((file) => /@\/services\/api\/client/.test(file.text))
    expect(importers.map((file) => file.name)).toEqual(['reviewService.ts'])
  })
})

describe('the reviews feature offers no operation the API lacks', () => {
  it('issues no DELETE and no PUT', () => {
    // Both review URLs answer 405 to both. `is_published: false` is the withdrawal.
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not delete`).not.toMatch(
        /\bapi\.(?:delete|put)\b/,
      )
    }
  })

  it('sends no field PATCH would refuse', () => {
    // `ReviewModerationUpdate` accepts `is_published` and `responded_at`. Sending `rating` or
    // `body` is a 422, so a moderation payload naming either would be a control that could
    // only fail.
    const controls = code(sources.find((file) => file.name === 'ModerationControls.tsx')!.text)
    for (const field of ['rating:', 'body:', 'title:', 'reviewer_name:', 'source:']) {
      expect(controls, `moderation must not send ${field}`).not.toContain(field)
    }
    expect(controls).toContain('is_published:')
    expect(controls).toContain('responded_at:')
  })

  it('names no moderation verb the schema cannot store', () => {
    // The table has one boolean and one timestamp. approve/reject/flag would be a workflow
    // with states the database has nowhere to put.
    const verbs = /\b(?:approve|reject|flag|escalate|quarantine)\w*\b/i
    for (const file of sources) {
      expect(code(file.text), `${file.name} must not invent a moderation verb`).not.toMatch(verbs)
    }
  })
})
