import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { IntelligencePage } from '@/pages/IntelligencePage'
import { ROUTES } from '@/router/routes'
import {
  clearStoredToken,
  hotelPage,
  installFetchStub,
  renderWithAuth,
  seedStoredToken,
  TEST_HOTEL,
  TEST_TOKEN,
  TEST_USER,
  type FetchStub,
} from '@/test/harness'
import type {
  AnomalyReport,
  DemandTrend,
  InsightsReport,
  OccupancyForecast,
  RevenueForecast,
} from '@/types/intelligence'

/**
 * Forecasts, demand trend, anomalies and findings.
 *
 * Mocked at `fetch`, so the exact path, method and query surface are observable. Five things
 * this file is mostly about:
 *
 * * **facts and predictions stay apart** — the on-the-books figure and the prediction are
 *   rendered as two separate values and are never summed or averaged;
 * * **nothing is invented** — a null prediction reads "No prediction", never zero, and an
 *   empty result is an honest empty state rather than a reassuring sentence;
 * * **every request is hotel-scoped** by the public identifier from the shared picker;
 * * **only the documented parameters are sent** — the backend ignores unknown ones silently,
 *   so an invented control would look like it worked;
 * * **no figure is derived in the browser** — thresholds, verdicts and severities are all
 *   the server's.
 */

/* --- fixtures --------------------------------------------------------------------------- */

const MODEL = {
  model_name: 'seasonal-naive-dow-median',
  model_version: '1.0.0',
  methodology: 'Day-of-week seasonal median over the training window.',
  generated_at: '2026-09-12T00:36:25.601169Z',
}

function occupancyForecast(overrides: Partial<OccupancyForecast> = {}): OccupancyForecast {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    model: MODEL,
    training_window: {
      date_from: '2026-06-15',
      date_to: '2026-09-12',
      days: 90,
      observations: 90,
    },
    horizon: { date_from: '2026-09-13', date_to: '2026-09-15', days: 3 },
    points: [
      {
        date: '2026-09-13',
        on_the_books_room_nights: 10,
        available_room_nights: 16,
        predicted_room_nights: '2.5000',
        predicted_occupancy_rate: '0.1563',
        interval_lower: '0.0000',
        interval_upper: '9.7647',
        confidence_level: '0.95',
        method: 'seasonal_dow_median',
        observations: 12,
        capacity_clamped: false,
      },
      {
        date: '2026-09-14',
        on_the_books_room_nights: 8,
        available_room_nights: 16,
        predicted_room_nights: null,
        predicted_occupancy_rate: null,
        interval_lower: null,
        interval_upper: null,
        confidence_level: null,
        method: 'insufficient_data',
        observations: 0,
        capacity_clamped: false,
      },
      {
        date: '2026-09-15',
        on_the_books_room_nights: 5,
        available_room_nights: 16,
        predicted_room_nights: '4.0000',
        predicted_occupancy_rate: '0.2500',
        interval_lower: '1.0000',
        interval_upper: '7.0000',
        confidence_level: '0.95',
        method: 'overall_median',
        observations: 30,
        capacity_clamped: true,
      },
    ],
    ...overrides,
  }
}

function revenueForecast(overrides: Partial<RevenueForecast> = {}): RevenueForecast {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    model: MODEL,
    training_window: { date_from: '2026-06-15', date_to: '2026-09-12', days: 90, observations: 90 },
    horizon: { date_from: '2026-09-13', date_to: '2026-09-14', days: 2 },
    currencies: [
      {
        currency: 'EUR',
        points: [
          {
            date: '2026-09-13',
            on_the_books_room_revenue: '1575.00',
            predicted_room_revenue: '337.50',
            interval_lower: '0.00',
            interval_upper: '1318.24',
            confidence_level: '0.95',
            method: 'seasonal_dow_median',
            observations: 12,
          },
          {
            date: '2026-09-14',
            on_the_books_room_revenue: '900.00',
            predicted_room_revenue: null,
            interval_lower: null,
            interval_upper: null,
            confidence_level: null,
            method: 'insufficient_data',
            observations: 0,
          },
        ],
      },
    ],
    is_multi_currency: false,
    ...overrides,
  }
}

function demandTrend(overrides: Partial<DemandTrend> = {}): DemandTrend {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    model: MODEL,
    window: { date_from: '2026-06-15', date_to: '2026-09-12', days: 90, observations: 90 },
    metric: 'bookings_created',
    direction: 'increasing',
    earlier_median: '2',
    recent_median: '5',
    relative_change: '1.5000',
    threshold: '0.10',
    ...overrides,
  }
}

function anomalyReport(overrides: Partial<AnomalyReport> = {}): AnomalyReport {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    model: MODEL,
    window: { date_from: '2026-06-15', date_to: '2026-09-12', days: 90, observations: 90 },
    metrics_scanned: ['bookings_created', 'occupied_room_nights', 'room_revenue[EUR]'],
    anomalies: [
      {
        metric: 'occupied_room_nights',
        date: '2026-08-14',
        value: '16',
        median: '4',
        median_absolute_deviation: '1.5',
        modified_z_score: '5.3960',
        threshold: '3.5',
        direction: 'above',
      },
    ],
    ...overrides,
  }
}

function insightsReport(overrides: Partial<InsightsReport> = {}): InsightsReport {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    model: MODEL,
    window: { date_from: '2026-06-15', date_to: '2026-09-12', days: 90, observations: 90 },
    horizon: { date_from: '2026-09-13', date_to: '2026-09-19', days: 7 },
    insights: [
      {
        type: 'demand_trend',
        severity: 'info',
        title: 'Booking demand is increasing',
        explanation: 'Median bookings taken per day moved from 2 to 5.',
        supporting_metrics: [
          { name: 'earlier_median', value: '2' },
          { name: 'threshold', value: '0.10' },
          { name: 'forecast_revenue', value: '1200.00', currency: 'EUR' },
        ],
        date_from: '2026-06-15',
        date_to: '2026-09-12',
        confidence: null,
      },
      {
        type: 'anomaly',
        severity: 'warning',
        title: 'Unusual occupancy on 14 August',
        explanation: 'The modified z-score of 5.3960 exceeds the threshold of 3.5.',
        supporting_metrics: [{ name: 'modified_z_score', value: '5.3960' }],
        date_from: '2026-08-14',
        date_to: '2026-08-14',
        confidence: '0.95',
      },
    ],
    ...overrides,
  }
}

function errorBody(code: string, message: string) {
  return { error: { code, message, details: [] } }
}

/* --- harness ---------------------------------------------------------------------------- */

let fetchStub: FetchStub

beforeEach(() => {
  /*
   * The page computes its dates from "today in the hotel's zone", so the clock is pinned --
   * otherwise every request assertion would drift with the calendar.
   *
   * Only `Date` is faked. Faking the timers as well freezes the clock that
   * `waitFor`/`findBy*` poll on, and every async assertion then times out waiting for a
   * tick that never comes.
   */
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-09-12T09:00:00Z'))

  fetchStub = installFetchStub()
  seedStoredToken()
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage([TEST_HOTEL], 1) })
  fetchStub.on('GET', '/forecast/occupancy', { body: occupancyForecast() })
  fetchStub.on('GET', '/forecast/revenue', { body: revenueForecast() })
  fetchStub.on('GET', '/demand-trend', { body: demandTrend() })
  fetchStub.on('GET', '/intelligence/anomalies', { body: anomalyReport() })
  fetchStub.on('GET', '/intelligence/insights', { body: insightsReport() })
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function renderPage() {
  return renderWithAuth([{ path: ROUTES.intelligence, element: <IntelligencePage /> }], {
    initialEntries: [ROUTES.intelligence],
  })
}

function requestsFor(fragment: string) {
  return fetchStub.calls.filter((call) => call.url.includes(fragment))
}

function urlOf(fragment: string) {
  const matches = requestsFor(fragment)
  return new URL(matches[matches.length - 1]!.url, 'http://localhost')
}

/* --- contract ----------------------------------------------------------------------------- */

describe('the request contract', () => {
  it('asks each endpoint at its documented path, scoped to the selected hotel', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    const base = `/api/v1/hotels/${TEST_HOTEL.public_id}/intelligence`
    expect(urlOf('/forecast/occupancy').pathname).toBe(`${base}/forecast/occupancy`)
    expect(urlOf('/demand-trend').pathname).toBe(`${base}/demand-trend`)
    expect(urlOf('/intelligence/anomalies').pathname).toBe(`${base}/anomalies`)
    expect(urlOf('/intelligence/insights').pathname).toBe(`${base}/insights`)
  })

  it('uses GET and sends no body anywhere', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    for (const call of fetchStub.calls.filter((c) => c.url.includes('/intelligence'))) {
      expect(call.method).toBe('GET')
      expect(call.body).toBeNull()
    }
  })

  it('sends exactly the documented parameters and no invented ones', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    expect([...urlOf('/forecast/occupancy').searchParams.keys()].sort()).toEqual([
      'date_from',
      'date_to',
      'training_days',
    ])
    expect([...urlOf('/demand-trend').searchParams.keys()].sort()).toEqual([
      'date_from',
      'date_to',
    ])
    expect([...urlOf('/intelligence/anomalies').searchParams.keys()].sort()).toEqual([
      'date_from',
      'date_to',
    ])
    expect([...urlOf('/intelligence/insights').searchParams.keys()].sort()).toEqual([
      'date_from',
      'date_to',
      'horizon_days',
    ])
  })

  it('starts the horizon the day after the observation window ends', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    // Today is 2026-09-12 in Europe/Athens. The window ends today; the horizon starts
    // tomorrow -- no gap, no overlap, which is what makes the window exactly the training
    // data.
    const windowParams = urlOf('/demand-trend').searchParams
    const horizonParams = urlOf('/forecast/occupancy').searchParams
    expect(windowParams.get('date_to')).toBe('2026-09-12')
    expect(horizonParams.get('date_from')).toBe('2026-09-13')
    // 90-day window, both bounds counted.
    expect(windowParams.get('date_from')).toBe('2026-06-15')
    // Default 7-day horizon.
    expect(horizonParams.get('date_to')).toBe('2026-09-19')
  })

  it('never asks for a span the routers would refuse', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Booking demand is increasing')

    for (const [label, value] of [
      ['Observation window', '180'],
      ['Forecast horizon', '60'],
      ['Training history', '365'],
    ] as const) {
      await user.selectOptions(screen.getByLabelText(label), value)
    }

    await waitFor(() => {
      expect(urlOf('/forecast/occupancy').searchParams.get('training_days')).toBe('365')
    })
    for (const call of fetchStub.calls.filter((c) => c.url.includes('/intelligence'))) {
      const params = new URL(call.url, 'http://localhost').searchParams
      const training = params.get('training_days')
      const horizon = params.get('horizon_days')
      if (training !== null) {
        expect(Number(training)).toBeGreaterThanOrEqual(14)
        expect(Number(training)).toBeLessThanOrEqual(365)
      }
      if (horizon !== null) {
        expect(Number(horizon)).toBeLessThanOrEqual(90)
      }
    }
  })

  it('fetches the revenue forecast only once its tab is opened', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Booking demand is increasing')

    expect(requestsFor('/forecast/revenue')).toHaveLength(0)

    await user.click(screen.getByRole('tab', { name: 'Revenue' }))
    await waitFor(() => {
      expect(requestsFor('/forecast/revenue')).toHaveLength(1)
    })
  })

  it('issues one request per endpoint, and none per point or row', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    expect(requestsFor('/forecast/occupancy')).toHaveLength(1)
    expect(requestsFor('/demand-trend')).toHaveLength(1)
    expect(requestsFor('/intelligence/anomalies')).toHaveLength(1)
    expect(requestsFor('/intelligence/insights')).toHaveLength(1)
    // auth/me, hotels, and the four reads for the default tab.
    expect(fetchStub.calls).toHaveLength(6)
  })
})

/* --- forecast ------------------------------------------------------------------------------ */

describe('the forecast', () => {
  it('shows the actual and the prediction as separate values, never combined', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Booking demand is increasing')

    await user.click(screen.getByText('Show occupied room nights values'))
    const table = screen.getByRole('table', { name: /Occupied room nights by day/ })

    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((h) => h.textContent),
    ).toEqual(['Date', 'On the books', 'Predicted', 'Method'])

    const row = within(table).getByRole('rowheader', { name: '13 Sept' }).closest('tr')!
    expect(within(row).getByText('10')).toBeInTheDocument()
    expect(within(row).getByText('2.5000')).toBeInTheDocument()
    // 10 + 2.5 and (10 + 2.5) / 2 are both absent: a confirmed reservation is never blended
    // with an estimate.
    expect(table.textContent).not.toContain('12.5')
    expect(table.textContent).not.toContain('6.25')
  })

  it('renders a null prediction as no prediction, not as zero', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Booking demand is increasing')

    await user.click(screen.getByText('Show occupied room nights values'))
    const table = screen.getByRole('table', { name: /Occupied room nights by day/ })
    const row = within(table).getByRole('rowheader', { name: '14 Sept' }).closest('tr')!

    expect(within(row).getByText('No prediction')).toBeInTheDocument()
    expect(within(row).getByText('insufficient_data')).toBeInTheDocument()
    // The on-the-books figure is still a fact and is still shown.
    expect(within(row).getByText('8')).toBeInTheDocument()
  })

  it('keeps the server’s own decimal scale in the table', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Booking demand is increasing')

    await user.click(screen.getByText('Show occupied room nights values'))
    // `2.5000`, not `2.5` -- the string is printed, not round-tripped through a float.
    expect(screen.getByText('2.5000')).toBeInTheDocument()
  })

  it('names the interval by the confidence the server stated', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    const chart = await screen.findByRole('img', { name: 'Occupied room nights forecast' })
    expect(chart).toHaveAccessibleDescription(/0\.95 confidence prediction interval/)
    expect(chart).toHaveAccessibleDescription(/never combined/)
    expect(chart).toHaveAccessibleDescription(/1 of 3 days have no prediction/)
  })

  it('reports the training window that produced it', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    expect(await screen.findByText(/Trained on 90 days of history/)).toBeInTheDocument()
    expect(screen.getAllByText(/seasonal-naive-dow-median v1\.0\.0/).length).toBeGreaterThan(0)
  })

  it('keeps currencies apart and never converts one into another', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/forecast/revenue', {
      body: revenueForecast({
        is_multi_currency: true,
        currencies: [
          { currency: 'EUR', points: revenueForecast().currencies[0]!.points },
          {
            currency: 'GBP',
            points: [
              {
                date: '2026-09-13',
                on_the_books_room_revenue: '400.00',
                predicted_room_revenue: '120.00',
                interval_lower: '0.00',
                interval_upper: '300.00',
                confidence_level: '0.95',
                method: 'seasonal_dow_median',
                observations: 20,
              },
            ],
          },
        ],
      }),
    })
    renderPage()
    await screen.findByText('Booking demand is increasing')
    await user.click(screen.getByRole('tab', { name: 'Revenue' }))

    expect(await screen.findByRole('heading', { level: 3, name: 'EUR' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'GBP' })).toBeInTheDocument()
    expect(screen.getByText(/never converted or combined/)).toBeInTheDocument()
    // Two charts, one per currency -- not one chart of a summed series.
    expect(screen.getByRole('img', { name: 'Room revenue (EUR) forecast' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Room revenue (GBP) forecast' })).toBeInTheDocument()
  })

  it('says so honestly when there is no revenue history to forecast', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/forecast/revenue', { body: revenueForecast({ currencies: [] }) })
    renderPage()
    await screen.findByText('Booking demand is increasing')
    await user.click(screen.getByRole('tab', { name: 'Revenue' }))

    expect(await screen.findByText('No revenue history to forecast from')).toBeInTheDocument()
    expect(screen.getByText(/absent rather than predicted at zero/)).toBeInTheDocument()
  })
})

/* --- trend, anomalies, findings ------------------------------------------------------------- */

describe('the demand trend', () => {
  it('shows the server’s verdict with the medians it was computed from', async () => {
    renderPage()

    const verdict = await screen.findByText('Increasing')
    const summary = verdict.closest('div')!
    expect(within(summary).getByText('bookings_created')).toBeInTheDocument()
    expect(within(summary).getByText('1.5000')).toBeInTheDocument()
    // The medians the verdict was computed from, so it can be checked rather than trusted.
    expect(within(summary).getByText('2')).toBeInTheDocument()
    expect(within(summary).getByText('5')).toBeInTheDocument()
    expect(within(summary).getByText('0.10')).toBeInTheDocument()
  })

  it('explains an undefined relative change rather than printing a dash', async () => {
    fetchStub.on('GET', '/demand-trend', {
      body: demandTrend({
        direction: 'stable',
        earlier_median: '0',
        recent_median: '0',
        relative_change: null,
      }),
    })
    renderPage()

    expect(await screen.findByText('Stable')).toBeInTheDocument()
    expect(screen.getByText(/the earlier half’s median was zero/)).toBeInTheDocument()
  })

  it('renders insufficient data as its own state, not as stable', async () => {
    fetchStub.on('GET', '/demand-trend', {
      body: demandTrend({
        direction: 'insufficient_data',
        earlier_median: null,
        recent_median: null,
        relative_change: null,
      }),
    })
    renderPage()

    expect(await screen.findByText('Not enough data')).toBeInTheDocument()
    expect(screen.queryByText('Stable')).not.toBeInTheDocument()
  })
})

describe('anomalies', () => {
  it('shows the statistical basis for every flag', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    const table = screen.getByRole('table', { name: /flagged as unusual/ })
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((h) => h.textContent),
    ).toEqual([
      'Date',
      'Metric',
      'Direction',
      'Value',
      'Window median',
      'MAD',
      'Modified z-score',
      'Threshold',
    ])
    expect(within(table).getByText('5.3960')).toBeInTheDocument()
    expect(within(table).getByText('3.5')).toBeInTheDocument()
  })

  it('states direction in words, not by colour alone', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    expect(screen.getByText('Above normal')).toBeInTheDocument()
  })

  it('invents no severity, because the contract has none', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    const table = screen.getByRole('table', { name: /flagged as unusual/ })
    expect(table.textContent).not.toMatch(/critical|severe|high risk/i)
  })

  it('says what was scanned when nothing was unusual', async () => {
    fetchStub.on('GET', '/intelligence/anomalies', { body: anomalyReport({ anomalies: [] }) })
    renderPage()

    expect(await screen.findByText('Nothing unusual in this window')).toBeInTheDocument()
    // The empty case is unambiguous: nothing was unusual, not nothing was looked at.
    expect(screen.getByText(/3 metrics were scanned/)).toBeInTheDocument()
    expect(screen.getByText(/room_revenue\[EUR\]/)).toBeInTheDocument()
  })
})

describe('findings', () => {
  it('renders the server’s explanation and supporting numbers', async () => {
    renderPage()

    expect(await screen.findByText('Booking demand is increasing')).toBeInTheDocument()
    expect(
      screen.getByText('Median bookings taken per day moved from 2 to 5.'),
    ).toBeInTheDocument()
    expect(screen.getByText('Warning')).toBeInTheDocument()
    expect(screen.getByText('Model confidence 0.95')).toBeInTheDocument()
  })

  it('formats a monetary supporting metric in its own currency', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    // `currencyDisplay: 'code'`, and Intl's separator is a non-breaking space.
    expect(screen.getByText(/EUR\s1,200\.00/)).toBeInTheDocument()
  })

  it('does not present the findings as generated text', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    expect(screen.getByText(/the same data always produces the same sentence/i)).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/AI[- ]generated|AI predicts|powered by AI/i)
  })

  it('renders an empty result as a result, not a failure', async () => {
    fetchStub.on('GET', '/intelligence/insights', { body: insightsReport({ insights: [] }) })
    renderPage()

    expect(await screen.findByText('No findings for this window')).toBeInTheDocument()
    expect(screen.getByText(/This is a result, not a failure to analyse/)).toBeInTheDocument()
  })
})

/* --- failures ------------------------------------------------------------------------------- */

describe('failures', () => {
  it.each([
    [404, 'NOT_FOUND', 'Intelligence is not available for this property'],
    [500, 'INTERNAL_ERROR', 'Intelligence is temporarily unavailable'],
    [422, 'VALIDATION_ERROR', 'That request could not be processed'],
    [429, 'RATE_LIMITED', 'Too many requests'],
  ])('renders a %s distinctly', async (status, code, title) => {
    fetchStub.on('GET', '/intelligence/anomalies', {
      status,
      body: errorBody(code, 'Backend detail that must not be shown.'),
    })
    renderPage()

    expect(await screen.findByText(title)).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('Backend detail that must not be shown.')
  })

  it('renders a network failure distinctly', async () => {
    fetchStub.on('GET', '/intelligence/anomalies', { networkError: true })
    renderPage()

    expect(await screen.findByText('Could not reach the server')).toBeInTheDocument()
  })

  it('treats a malformed 200 as a failure, not as an empty result', async () => {
    fetchStub.on('GET', '/intelligence/anomalies', { body: { anomalies: 'nope' } })
    renderPage()

    expect(await screen.findByText('Unexpected response')).toBeInTheDocument()
    expect(screen.queryByText('Nothing unusual in this window')).not.toBeInTheDocument()
  })

  it('lets one endpoint fail without blanking the others', async () => {
    fetchStub.on('GET', '/intelligence/anomalies', { networkError: true })
    renderPage()

    expect(await screen.findByText('Could not reach the server')).toBeInTheDocument()
    // The trend and the findings arrived and are still shown.
    expect(screen.getByText('Increasing')).toBeInTheDocument()
    expect(screen.getByText('Booking demand is increasing')).toBeInTheDocument()
  })

  it('never leaks a backend internal on a 500', async () => {
    fetchStub.on('GET', '/intelligence/insights', {
      status: 500,
      body: errorBody(
        'INTERNAL',
        'psycopg.errors.UndefinedTable: relation "daily_hotel_metrics" does not exist',
      ),
    })
    renderPage()
    await screen.findByText('Increasing')

    for (const leak of ['psycopg', 'UndefinedTable', 'daily_hotel_metrics', 'relation']) {
      expect(document.body.textContent ?? '').not.toContain(leak)
    }
  })
})

/* --- safety and accessibility ---------------------------------------------------------------- */

describe('safety', () => {
  const PAYLOAD = '<img src=x onerror="document.title=\'pwned\'">'

  it('renders markup from every server-supplied string as text', async () => {
    fetchStub.on('GET', '/intelligence/anomalies', {
      body: anomalyReport({
        metrics_scanned: [PAYLOAD],
        anomalies: [
          {
            metric: PAYLOAD,
            date: '2026-08-14',
            value: '16',
            median: '4',
            median_absolute_deviation: '1.5',
            modified_z_score: '5.3960',
            threshold: '3.5',
            direction: 'above',
          },
        ],
      }),
    })
    fetchStub.on('GET', '/intelligence/insights', {
      body: insightsReport({
        insights: [
          {
            type: PAYLOAD,
            severity: 'info',
            title: PAYLOAD,
            explanation: PAYLOAD,
            supporting_metrics: [{ name: PAYLOAD, value: PAYLOAD }],
            date_from: '2026-08-14',
            date_to: '2026-08-14',
            confidence: null,
          },
        ],
      }),
    })
    renderPage()

    expect(await screen.findAllByText(PAYLOAD, { exact: false })).not.toHaveLength(0)
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.title).not.toBe('pwned')
    expect(document.body.innerHTML).toContain('&lt;img')
  })

  it('renders markup in model metadata as text', async () => {
    fetchStub.on('GET', '/demand-trend', {
      body: demandTrend({
        model: { ...MODEL, methodology: PAYLOAD, model_name: PAYLOAD },
        metric: PAYLOAD,
      }),
    })
    renderPage()

    expect(await screen.findAllByText(PAYLOAD, { exact: false })).not.toHaveLength(0)
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.title).not.toBe('pwned')
  })

  it('puts no credential in the page and none in a URL', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    expect(document.body.innerHTML).not.toContain(TEST_TOKEN)
    for (const call of fetchStub.calls) {
      expect(call.url).not.toContain(TEST_TOKEN)
      expect(call.url).not.toContain('token')
    }
  })

  it('sends the public hotel identifier and never an internal one', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    for (const call of requestsFor('/intelligence')) {
      const path = new URL(call.url, 'http://localhost').pathname
      expect(path).toContain(TEST_HOTEL.public_id)
      // A bare integer between the hotel segment and intelligence would be an internal key.
      expect(path).not.toMatch(/\/hotels\/\d+\//)
    }
  })
})

describe('accessibility', () => {
  it('gives every chart an accessible name and a described alternative', async () => {
    renderPage()

    const chart = await screen.findByRole('img', { name: 'Occupied room nights forecast' })
    expect(chart).toHaveAccessibleDescription()
    expect(screen.getByText('Show occupied room nights values')).toBeInTheDocument()
  })

  it('labels every control', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    for (const label of ['Observation window', 'Forecast horizon', 'Training history']) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }
    expect(
      document.querySelectorAll(
        'select:not([id]), input:not([id]):not([type=hidden]), textarea:not([id])',
      ),
    ).toHaveLength(0)
  })

  it('exposes the forecast metric selector as a real tab list', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Booking demand is increasing')

    expect(screen.getByRole('tab', { name: 'Occupancy' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    await user.click(screen.getByRole('tab', { name: 'Revenue' }))
    expect(screen.getByRole('tab', { name: 'Revenue' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel')).toBeInTheDocument()
  })

  it('keeps a sane heading hierarchy', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    expect(screen.getByRole('heading', { level: 1, name: 'Intelligence' })).toBeInTheDocument()
    expect(
      screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent),
    ).toEqual(['Forecast', 'Booking demand', 'Anomalies', 'Findings'])
  })

  it('uses no positive tabindex', async () => {
    renderPage()
    await screen.findByText('Booking demand is increasing')

    expect(
      document.querySelectorAll('[tabindex]:not([tabindex="-1"]):not([tabindex="0"])'),
    ).toHaveLength(0)
  })
})

describe('the compact layout', () => {
  it('renders anomaly cards instead of a table at phone width, losing no figure', async () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    )
    renderPage()
    await screen.findByText('Booking demand is increasing')

    expect(
      screen.queryByRole('table', { name: /flagged as unusual/ }),
    ).not.toBeInTheDocument()
    const card = screen.getByText('Above normal').closest('li')!
    // The statistical basis survives the narrow layout.
    expect(within(card).getByText('5.3960')).toBeInTheDocument()
    expect(within(card).getByText('3.5')).toBeInTheDocument()
    expect(within(card).getByText('1.5')).toBeInTheDocument()
    expect(within(card).getByText('16')).toBeInTheDocument()
  })
})
