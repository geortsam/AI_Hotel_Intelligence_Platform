import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { ForecastPerformanceSection } from '@/features/forecastPerformance/ForecastPerformanceSection'
import {
  clearStoredToken,
  hotelPage,
  installFetchStub,
  renderInAuth,
  seedStoredToken,
  TEST_HOTEL,
  TEST_USER,
  type FetchStub,
} from '@/test/harness'
import type { Hotel } from '@/types/hotel'

/**
 * Stage 7.4: the trained model's measured performance, on screen.
 *
 * Mocked at `fetch`, so the exact path, method and query surface are observable. What this
 * file is mostly about:
 *
 * * **a viewer's 403 is an answer, not a fault** — the accuracy panel does not render, the
 *   rest of the section does, and nothing collapses;
 * * **actual and predicted stay apart** — two named series, two table columns, never summed;
 * * **the two calibration segments are never merged** — no combined metric appears anywhere;
 * * **nothing is computed here** — every figure on screen arrived in a response;
 * * **absence is stated, not zeroed** — a null metric, a missing prediction and a null
 *   comparison each render as themselves;
 * * **no confidence band**, because the protocol produces none;
 * * hotel-scoped by the public UUID, with no numeric identifier anywhere.
 */

/* --- fixtures ----------------------------------------------------------------------------- */

const HOTEL = TEST_HOTEL as unknown as Hotel

const STATEMENT =
  'These figures measure predictions this hotel was served, under a protocol fixed and ' +
  'checksummed before any number was computed. They establish no production accuracy, ' +
  'evaluate no threshold, compare against no baseline model, detect nothing and rank nothing. ' +
  'The served artifact remains an offline research candidate.'

const MEASUREMENT = {
  protocol_version: 'accuracy_v1',
  protocol_checksum: 'a1b2c3d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef',
  establishes_production_accuracy: false,
  statement: STATEMENT,
}

function day(date: string, occupied: number) {
  return {
    date,
    occupied_room_nights: occupied,
    room_nights_sold: occupied,
    available_room_nights: 48,
    occupancy_rate: '0.5000',
    room_revenue: [],
    other_revenue: [],
    total_expenses: [],
    arrivals: 2,
    departures: 1,
    bookings_created: 3,
    cancellations: 0,
  }
}

function dailySeries(days: readonly ReturnType<typeof day>[]) {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    range: { date_from: '2026-06-15', date_to: '2026-09-12', days: 90 },
    days,
  }
}

function stored(targetDate: string, predicted: number, publicId = targetDate) {
  return {
    public_id: `pred-${publicId}`,
    target_date: targetDate,
    forecast_horizon_days: 7,
    prediction_cutoff: '2026-06-08T00:00:00Z',
    predicted_room_nights: predicted,
    model_name: 'demand_baseline',
    model_version: 'demand_baseline_v1',
    feature_version: 'v1',
    dataset_version: 'v1',
    generated_at: '2026-06-01T12:00:00Z',
  }
}

function predictionPage(items: readonly unknown[], total = items.length) {
  return { items, total, page: 1, page_size: 100, pages: total === 0 ? 0 : 1 }
}

function segmentAccuracy(segment: string, observations: number, mae: number | null) {
  return {
    segment,
    metrics: {
      observations,
      skipped: 1,
      mae,
      rmse: mae === null ? null : 4.25,
      smape: mae === null ? null : 12.5,
    },
  }
}

function accuracyResponse(overrides: Record<string, unknown> = {}) {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    as_of_date: '2026-09-12',
    window_from: '2026-06-15',
    window_to: '2026-09-12',
    scored_from: '2026-06-15',
    scored_to: '2026-08-15',
    settlement_lag_days: 28,
    candidates: 9,
    ineligible_by_settlement: 3,
    out_of_scope_model_digest: 0,
    unsettled_allocations: 0,
    settled: true,
    by_model_version: [
      {
        model_version: 'demand_baseline_v1',
        below_calibration: segmentAccuracy('below_calibration', 0, null),
        within_calibration: segmentAccuracy('within_calibration', 5, 3.5),
      },
    ],
    measurement: MEASUREMENT,
    ...overrides,
  }
}

function fieldSummary(field: string) {
  return {
    field,
    count: 5,
    minimum: 10,
    maximum: 90,
    mean: 50,
    median: 48,
    quantiles: [
      { label: 'p05', value: 11 },
      { label: 'p25', value: 30 },
      { label: 'p50', value: 48 },
      { label: 'p75', value: 70 },
      { label: 'p95', value: 88 },
    ],
  }
}

function windowSummary() {
  return {
    window_from: '2026-06-15',
    window_to: '2026-09-12',
    candidates: 5,
    out_of_scope_model_digest: 0,
    by_model_version: [
      {
        model_version: 'demand_baseline_v1',
        below_calibration: {
          segment: 'below_calibration',
          observations: 0,
          fields: [fieldSummary('demand_lag_7')],
        },
        within_calibration: {
          segment: 'within_calibration',
          observations: 5,
          fields: [fieldSummary('demand_lag_7'), fieldSummary('predicted_room_nights')],
        },
      },
    ],
  }
}

function distributionResponse(overrides: Record<string, unknown> = {}) {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    observed: windowSummary(),
    comparison: null,
    measurement: { ...MEASUREMENT, protocol_version: 'distribution_v1' },
    ...overrides,
  }
}

/* --- harness ------------------------------------------------------------------------------ */

let fetchStub: FetchStub

interface StubOptions {
  readonly daily?: unknown
  readonly dailyStatus?: number
  readonly predictions?: unknown
  readonly predictionsStatus?: number
  readonly distribution?: unknown
  readonly distributionStatus?: number
  readonly accuracy?: unknown
  readonly accuracyStatus?: number
}

function stubAll(options: StubOptions = {}) {
  fetchStub.on('GET', '/auth/me', { status: 200, body: TEST_USER })
  fetchStub.on('GET', '/hotels?', { status: 200, body: hotelPage() })
  fetchStub.on('GET', '/analytics/daily', {
    status: options.dailyStatus ?? 200,
    body: options.daily ?? dailySeries([day('2026-06-15', 40), day('2026-06-16', 44)]),
  })
  fetchStub.on('GET', '/ml/demand-predictions', {
    status: options.predictionsStatus ?? 200,
    body:
      options.predictions ??
      predictionPage([stored('2026-06-15', 38.5), stored('2026-06-16', 45.25)]),
  })
  fetchStub.on('GET', '/ml/prediction-distribution', {
    status: options.distributionStatus ?? 200,
    body: options.distribution ?? distributionResponse(),
  })
  fetchStub.on('GET', '/ml/forecast-accuracy', {
    status: options.accuracyStatus ?? 200,
    body: options.accuracy ?? accuracyResponse(),
  })
}

/** A 403, shaped exactly as the API's `ErrorResponse`. */
const FORBIDDEN_BODY = {
  error: {
    code: 'FORBIDDEN',
    message: 'This action requires the manager role at this hotel.',
  },
}

function renderSection() {
  return renderInAuth(
    <ForecastPerformanceSection hotel={HOTEL} period="last90" onPeriodChange={() => {}} />,
  )
}

/** Every request the section issued, as URL strings. */
function requestedUrls(): string[] {
  return fetchStub.calls.map((call) => call.url)
}

beforeEach(() => {
  fetchStub = installFetchStub()
  seedStoredToken()
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
})

/* --- rendering ---------------------------------------------------------------------------- */

describe('the section renders what the server measured', () => {
  it('shows a loading state before anything has arrived', () => {
    stubAll()
    renderSection()

    expect(screen.getByText(/loading measured forecast performance/i)).toBeInTheDocument()
  })

  it('renders all three panels for a manager', async () => {
    stubAll()
    renderSection()

    expect(
      await screen.findByRole('region', { name: /forecast against what actually happened/i }),
    ).toBeInTheDocument()
    expect(screen.getByRole('region', { name: /measured forecast error/i })).toBeInTheDocument()
    expect(
      screen.getByRole('region', { name: /distribution of stored predictions/i }),
    ).toBeInTheDocument()
  })

  it('renders the measured error the server reported, and nothing it did not', async () => {
    stubAll()
    renderSection()

    const panel = await screen.findByRole('region', { name: /measured forecast error/i })

    // 3.5 -> MAE of the within-calibration segment, straight off the response.
    expect(within(panel).getByText('3.5')).toBeInTheDocument()
    expect(within(panel).getByText('4.25')).toBeInTheDocument()
    expect(within(panel).getByText('12.5%')).toBeInTheDocument()
  })
})

/* --- the claim boundary ------------------------------------------------------------------- */

describe('the claim boundary is on screen', () => {
  it('states that predictive accuracy has not been established', async () => {
    stubAll()
    renderSection()

    const caveat = await screen.findByTestId('accuracy-caveat')

    expect(caveat).toHaveTextContent(/predictive accuracy has not been established/i)
  })

  it('renders the server statement verbatim rather than paraphrasing it', async () => {
    stubAll()
    renderSection()

    const caveat = await screen.findByTestId('accuracy-caveat')

    expect(caveat).toHaveTextContent('establish no production accuracy')
    expect(caveat).toHaveTextContent('offline research candidate')
  })

  it('shows the settlement lag in the panel body, not only in a title attribute', async () => {
    stubAll()
    renderSection()

    const lag = await screen.findByTestId('settlement-lag')

    expect(lag).toHaveTextContent('28 days')
    // Visible text, so it survives a reader who never hovers anything.
    expect(lag.textContent).toMatch(/28 days/)
  })

  it('marks a measurement provisional when allocations can still change', async () => {
    stubAll({ accuracy: accuracyResponse({ settled: false, unsettled_allocations: 4 }) })
    renderSection()

    expect(await screen.findByText(/provisional/i)).toBeInTheDocument()
  })

  it.each([
    /\bis accurate\b/i,
    /\breliable\b/i,
    /\bproduction[- ]ready\b/i,
    /\bvalidated\b/i,
    /\bgeneralis/i,
    /\bdrift\b/i,
  ])('makes no claim the API does not support: %s', async (pattern) => {
    stubAll()
    renderSection()
    await screen.findByRole('region', { name: /measured forecast error/i })

    expect(document.body.textContent ?? '').not.toMatch(pattern)
  })
})

/* --- authorization ------------------------------------------------------------------------ */

describe('viewer and manager see different things', () => {
  it('hides the accuracy figures when the API refuses with 403', async () => {
    stubAll({ accuracyStatus: 403, accuracy: FORBIDDEN_BODY })
    renderSection()

    expect(await screen.findByText(/measured error is restricted/i)).toBeInTheDocument()
    expect(screen.queryByTestId('accuracy-caveat')).not.toBeInTheDocument()
    expect(screen.queryByText('3.5')).not.toBeInTheDocument()
  })

  it('still renders the distribution for a viewer whose accuracy request was refused', async () => {
    stubAll({ accuracyStatus: 403, accuracy: FORBIDDEN_BODY })
    renderSection()

    const panel = await screen.findByRole('region', {
      name: /distribution of stored predictions/i,
    })

    expect(
      within(panel).getByRole('heading', { name: /^observed window$/i }),
    ).toBeInTheDocument()
  })

  it('still renders the forecast-vs-actual chart for a refused viewer', async () => {
    stubAll({ accuracyStatus: 403, accuracy: FORBIDDEN_BODY })
    renderSection()

    const panel = await screen.findByRole('region', {
      name: /forecast against what actually happened/i,
    })

    expect(within(panel).getByRole('img')).toBeInTheDocument()
  })

  it('tells a refused reader that the rest of the section is still theirs', async () => {
    stubAll({ accuracyStatus: 403, accuracy: FORBIDDEN_BODY })
    renderSection()

    expect(
      await screen.findByText(/everything else in this section is available to you/i),
    ).toBeInTheDocument()
  })

  it('announces the refusal as a status rather than an alert', async () => {
    stubAll({ accuracyStatus: 403, accuracy: FORBIDDEN_BODY })
    renderSection()

    const message = await screen.findByText(/measured error is restricted/i)

    // A refusal a reader is expected to encounter is not an emergency.
    expect(message.closest('[role="status"]')).not.toBeNull()
  })
})

/* --- actual and forecast stay apart ------------------------------------------------------- */

describe('actual and forecast are never blended', () => {
  it('names both series separately in the legend', async () => {
    stubAll()
    renderSection()
    await screen.findByRole('region', { name: /forecast against what actually happened/i })

    /* `getAllByText`: "on the books" appears in the legend AND as a table column header, which
       is the point -- the two series are named in both places. What matters is that each name
       exists and that they are different names, not that either occurs exactly once. */
    expect(screen.getAllByText(/on the books/i).length).toBeGreaterThan(0)
    expect(screen.getByText(/predicted by the model/i)).toBeInTheDocument()
  })

  it('lists them in separate table columns', async () => {
    stubAll()
    renderSection()
    await screen.findByRole('region', { name: /forecast against what actually happened/i })

    expect(screen.getByRole('columnheader', { name: /on the books/i })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: /^predicted$/i })).toBeInTheDocument()
  })

  it('distinguishes the two series by shape, not by colour alone', async () => {
    stubAll()
    renderSection()
    await screen.findByRole('region', { name: /forecast against what actually happened/i })

    const chart = screen.getAllByRole('img')[0]!
    // Circles for actual, rotated squares for predicted: two different elements.
    expect(chart.querySelectorAll('circle').length).toBeGreaterThan(0)
    expect(chart.querySelectorAll('rect').length).toBeGreaterThan(0)
  })

  it('gives the chart an accessible name and description', async () => {
    stubAll()
    renderSection()
    await screen.findByRole('region', { name: /forecast against what actually happened/i })

    const chart = screen.getAllByRole('img')[0]!
    expect(chart).toHaveAccessibleName(/occupied room nights forecast/i)
    expect(chart).toHaveAccessibleDescription(/never combined/i)
  })

  it('draws no confidence band, because the protocol produces none', async () => {
    stubAll()
    renderSection()
    await screen.findByRole('region', { name: /forecast against what actually happened/i })

    const chart = screen.getAllByRole('img')[0]!

    // The band is a polygon. None is drawn, and no legend entry names one.
    expect(chart.querySelectorAll('polygon')).toHaveLength(0)
    expect(screen.queryByText(/prediction interval$/i)).not.toBeInTheDocument()

    /*
     * The chart is REQUIRED to mention the absence -- "the response carried no prediction
     * interval" is the honest disclosure, and an assertion that the phrase never appears would
     * push the UI into silence about exactly the thing it must disclose. What is banned is a
     * claim that an interval exists.
     */
    expect(chart).toHaveAccessibleDescription(/carried no prediction interval/i)
    expect(document.body.textContent ?? '').not.toMatch(/\d+\s*%\s*(confidence|prediction)/i)
    expect(document.body.textContent ?? '').not.toMatch(/\bconfidence band\b/i)
  })
})

/* --- absence is stated, never zeroed ------------------------------------------------------ */

describe('absence is rendered as absence', () => {
  it('renders a null metric as unavailable rather than zero', async () => {
    stubAll()
    renderSection()

    const panel = await screen.findByRole('region', { name: /measured forecast error/i })
    const segments = within(panel).getAllByRole('table')
    // The below-calibration segment scored nothing: its MAE is null.
    expect(within(segments[0]!).getAllByText('—').length).toBeGreaterThan(0)
  })

  it('says so when no date in the window cleared the settlement lag', async () => {
    stubAll({ accuracy: accuracyResponse({ scored_from: null, scored_to: null }) })
    renderSection()

    expect(
      await screen.findByText(/no date in this window had cleared the settlement lag/i),
    ).toBeInTheDocument()
  })

  it('reports a day with no stored prediction as a gap, not a zero', async () => {
    stubAll({
      predictions: predictionPage([stored('2026-06-15', 38.5)]),
    })
    renderSection()
    await screen.findByRole('region', { name: /forecast against what actually happened/i })

    expect(screen.getByText(/1 day\(s\) in this window carry no stored prediction/i)).toBeInTheDocument()
    expect(screen.getByText(/drawn as a gap rather than as zero/i)).toBeInTheDocument()
  })

  it('declines to choose when one date holds more than one prediction', async () => {
    stubAll({
      predictions: predictionPage([
        stored('2026-06-15', 38.5, 'a'),
        stored('2026-06-15', 41.0, 'b'),
        stored('2026-06-16', 45.25),
      ]),
    })
    renderSection()
    await screen.findByRole('region', { name: /forecast against what actually happened/i })

    expect(screen.getByText(/hold more than one stored prediction/i)).toBeInTheDocument()
    expect(screen.getByText(/no predicted point is drawn for those days/i)).toBeInTheDocument()
  })

  it('renders the documented empty state when comparison is null', async () => {
    stubAll()
    renderSection()

    const empty = await screen.findByTestId('no-comparison')

    expect(empty).toHaveTextContent(/no baseline window was requested/i)
    // The distinction that matters: absent is not "nothing changed".
    expect(empty).toHaveTextContent(/not the same as finding no change/i)
  })

  it('renders a comparison as signed differences when one is present', async () => {
    stubAll({
      distribution: distributionResponse({
        comparison: {
          baseline: windowSummary(),
          by_model_version: [
            {
              model_version: 'demand_baseline_v1',
              below_calibration: {
                segment: 'below_calibration',
                observations: -2,
                fields: [
                  {
                    field: 'demand_lag_7',
                    count: 3,
                    minimum: 1.5,
                    maximum: null,
                    mean: -2,
                    median: 0,
                    quantiles: [],
                  },
                ],
              },
              within_calibration: {
                segment: 'within_calibration',
                observations: 0,
                fields: [],
              },
            },
          ],
        },
      }),
    })
    renderSection()

    const panel = await screen.findByRole('region', {
      name: /distribution of stored predictions/i,
    })

    expect(screen.queryByTestId('no-comparison')).not.toBeInTheDocument()
    expect(within(panel).getByText('+1.5')).toBeInTheDocument()
    expect(within(panel).getByText('-2')).toBeInTheDocument()
  })

  it('says when no predictions were stored in the window at all', async () => {
    stubAll({
      distribution: distributionResponse({
        observed: { ...windowSummary(), candidates: 0, by_model_version: [] },
      }),
    })
    renderSection()

    expect(
      await screen.findByText(/no stored predictions in this window, so there is nothing to summarise/i),
    ).toBeInTheDocument()
  })
})

/* --- the two segments ---------------------------------------------------------------------- */

describe('the calibration segments are never merged', () => {
  it('renders each segment with its own table and denominator', async () => {
    stubAll()
    renderSection()

    const panel = await screen.findByRole('region', { name: /measured forecast error/i })

    // Scoped to headings: each label also appears in its table's caption, which is deliberate.
    expect(
      within(panel).getByRole('heading', { name: /^at or below 40 room nights$/i }),
    ).toBeInTheDocument()
    expect(
      within(panel).getByRole('heading', { name: /^above 40 room nights$/i }),
    ).toBeInTheDocument()

    // Exactly two: one per declared segment, and no third holding an aggregate of them.
    const tables = within(panel).getAllByRole('table')
    expect(tables).toHaveLength(2)
    expect(within(tables[0]!).getByText('0')).toBeInTheDocument()
    expect(within(tables[1]!).getByText('5')).toBeInTheDocument()
  })

  it('offers no combined, overall or total accuracy figure', async () => {
    stubAll()
    renderSection()

    const panel = await screen.findByRole('region', { name: /measured forecast error/i })

    /*
     * Asserted over the LABELS rather than the panel's whole text. The prose legitimately
     * contains the word "combined" -- each caption says the segment "is never combined with
     * the other", which is the claim this test exists to protect. Banning the word would
     * forbid the disclosure and permit the thing.
     */
    const labels = within(panel)
      .getAllByRole('rowheader')
      .map((cell) => cell.textContent ?? '')
    const headings = within(panel)
      .getAllByRole('heading')
      .map((node) => node.textContent ?? '')

    for (const text of [...labels, ...headings]) {
      expect(text).not.toMatch(/\b(combined|overall|aggregate|total)\b/i)
    }
  })

  it('names the model version the figures belong to', async () => {
    stubAll()
    renderSection()

    const panel = await screen.findByRole('region', { name: /measured forecast error/i })

    expect(
      within(panel).getByRole('region', { name: /model version demand_baseline_v1/i }),
    ).toBeInTheDocument()
  })
})

/* --- failures ------------------------------------------------------------------------------ */

describe('each request fails on its own', () => {
  it('reports a failed distribution without taking the accuracy panel down', async () => {
    stubAll({
      distributionStatus: 503,
      distribution: { error: { code: 'SERVICE_UNAVAILABLE', message: 'nope' } },
    })
    renderSection()

    expect(await screen.findByTestId('accuracy-caveat')).toBeInTheDocument()
    expect(screen.getByText(/temporarily unavailable/i)).toBeInTheDocument()
  })

  it('reports a failed chart without taking the other panels down', async () => {
    stubAll({
      dailyStatus: 500,
      daily: { error: { code: 'INTERNAL_ERROR', message: 'nope' } },
    })
    renderSection()

    expect(await screen.findByTestId('accuracy-caveat')).toBeInTheDocument()
    expect(
      screen.getByRole('region', { name: /distribution of stored predictions/i }),
    ).toBeInTheDocument()
  })

  it('renders a 404 as the shared not-found copy', async () => {
    stubAll({
      accuracyStatus: 404,
      accuracy: { error: { code: 'NOT_FOUND', message: 'Hotel not found.' } },
    })
    renderSection()

    expect(await screen.findByText(/not available for this property/i)).toBeInTheDocument()
  })

  it('renders a 422 without inventing a figure', async () => {
    stubAll({
      accuracyStatus: 422,
      accuracy: { error: { code: 'VALIDATION_ERROR', message: 'window too long' } },
    })
    renderSection()

    await waitFor(() => {
      expect(screen.queryByTestId('accuracy-caveat')).not.toBeInTheDocument()
    })
    expect(screen.queryByText('3.5')).not.toBeInTheDocument()
  })

  it('never renders a zero for a request that failed', async () => {
    stubAll({
      accuracyStatus: 503,
      accuracy: { error: { code: 'SERVICE_UNAVAILABLE', message: 'nope' } },
      distributionStatus: 503,
      distribution: { error: { code: 'SERVICE_UNAVAILABLE', message: 'nope' } },
    })
    renderSection()
    await screen.findAllByText(/temporarily unavailable/i)

    expect(screen.queryByTestId('accuracy-caveat')).not.toBeInTheDocument()
    expect(screen.queryByTestId('no-comparison')).not.toBeInTheDocument()
  })
})

/* --- the request surface ------------------------------------------------------------------ */

describe('tenant isolation and the request surface', () => {
  it('addresses every request by the public hotel UUID', async () => {
    stubAll()
    renderSection()
    await screen.findByRole('region', { name: /measured forecast error/i })

    const scoped = requestedUrls().filter((url) => url.includes('/ml/') || url.includes('/analytics/'))
    expect(scoped.length).toBeGreaterThanOrEqual(4)
    for (const url of scoped) {
      expect(url).toContain(`/hotels/${TEST_HOTEL.public_id}/`)
    }
  })

  it('sends no hotel identifier as a query parameter', async () => {
    stubAll()
    renderSection()
    await screen.findByRole('region', { name: /measured forecast error/i })

    for (const url of requestedUrls()) {
      const query = url.split('?')[1] ?? ''
      expect(query).not.toMatch(/hotel_id/)
      expect(query).not.toMatch(/hotel_public_id/)
    }
  })

  it('sends only the documented parameters on the accuracy request', async () => {
    stubAll()
    renderSection()
    await screen.findByRole('region', { name: /measured forecast error/i })

    const url = requestedUrls().find((candidate) => candidate.includes('/ml/forecast-accuracy'))!
    const query = new URLSearchParams(url.split('?')[1] ?? '')

    expect([...query.keys()].sort()).toEqual(['as_of_date', 'window_from', 'window_to'])
  })

  it('sends no half baseline pair on the distribution request', async () => {
    stubAll()
    renderSection()
    await screen.findByRole('region', { name: /measured forecast error/i })

    const url = requestedUrls().find((candidate) =>
      candidate.includes('/ml/prediction-distribution'),
    )!
    const query = new URLSearchParams(url.split('?')[1] ?? '')

    expect([...query.keys()].sort()).toEqual(['window_from', 'window_to'])
  })

  it('issues exactly four data requests, regardless of how many days there are', async () => {
    stubAll({
      daily: dailySeries(
        Array.from({ length: 40 }, (_, index) =>
          day(`2026-07-${String((index % 28) + 1).padStart(2, '0')}`, 40 + index),
        ),
      ),
    })
    renderSection()
    await screen.findByRole('region', { name: /measured forecast error/i })

    const data = requestedUrls().filter(
      (url) => url.includes('/ml/') || url.includes('/analytics/'),
    )
    expect(data).toHaveLength(4)
  })

  it('discloses a truncated page rather than presenting it as the whole history', async () => {
    stubAll({
      predictions: predictionPage([stored('2026-06-15', 38.5), stored('2026-06-16', 45.25)], 250),
    })
    renderSection()
    await screen.findByRole('region', { name: /forecast against what actually happened/i })

    expect(screen.getByText(/showing 2 of 250 stored predictions/i)).toBeInTheDocument()
  })
})
