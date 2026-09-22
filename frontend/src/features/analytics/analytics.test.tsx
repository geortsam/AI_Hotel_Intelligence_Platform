import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AnalyticsPage } from '@/pages/AnalyticsPage'
import { ROUTES } from '@/router/routes'
import {
  clearStoredToken,
  hotelPage,
  installFetchStub,
  renderWithAuth,
  seedStoredToken,
  TEST_HOTEL,
  TEST_USER,
  type FetchStub,
} from '@/test/harness'

/**
 * Period reporting, and the first screen to ask the trained model anything.
 *
 * Mocked at `fetch`, so the exact path, method and query surface are observable. What this
 * file is mostly about:
 *
 * * **every figure is the server's** — the KPIs, the category tables and the charts render
 *   values that arrived in a response, and no total, percentage or comparison is computed here;
 * * **recorded and modelled stay apart** — the forecast has its own panel, says so in words,
 *   and carries the model's `production_ready: false` rather than hiding it;
 * * **a refusal is an answer** — `422 INSUFFICIENT_HISTORY` renders as an explanation, never
 *   as zero, and never takes the rest of the page down with it;
 * * **a failure is never a zero** — when the report's requests fail the page says so;
 * * **only documented parameters are sent**, hotel-scoped by the public identifier.
 */

/* --- fixtures ----------------------------------------------------------------------------- */

const RANGE = { date_from: '2026-06-15', date_to: '2026-09-12', days: 90 }

function overview(overrides: Record<string, unknown> = {}) {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    range: RANGE,
    bookings_created: {
      total: 41,
      pending: 1,
      confirmed: 33,
      checked_in: 2,
      checked_out: 3,
      cancelled: 1,
      no_show: 1,
    },
    bookings_by_stay: {
      total: 38,
      pending: 0,
      confirmed: 30,
      checked_in: 2,
      checked_out: 5,
      cancelled: 1,
      no_show: 0,
    },
    stay_flow: { arrivals: 12, departures: 11, cancellations: 1 },
    occupancy: {
      occupied_room_nights: 413,
      room_nights_sold: 413,
      complimentary_room_nights: 0,
      available_room_nights: 1440,
      occupancy_rate: '0.2868',
      available_room_nights_basis: 'current_active_rooms',
    },
    room_revenue: [
      { currency: 'EUR', room_revenue: '61485.00', adr: '148.87', revpar: '42.70' },
    ],
    other_revenue: [{ currency: 'EUR', amount: '34808.00' }],
    ledger_room_revenue: [{ currency: 'EUR', amount: '61485.00' }],
    total_expenses: [{ currency: 'EUR', amount: '47328.00' }],
    net_operating_result: [{ currency: 'EUR', amount: '48965.00' }],
    is_multi_currency: false,
    reviews: { review_count: 55, published_count: 46, average_rating_normalized: '0.78' },
    ...overrides,
  }
}

function dailyRow(date: string, overrides: Record<string, unknown> = {}) {
  return {
    date,
    occupied_room_nights: 5,
    room_nights_sold: 5,
    available_room_nights: 16,
    occupancy_rate: '0.3125',
    room_revenue: [{ currency: 'EUR', amount: '600.00' }],
    other_revenue: [],
    total_expenses: [],
    arrivals: 2,
    departures: 1,
    bookings_created: 3,
    cancellations: 0,
    ...overrides,
  }
}

function dailySeries(days = [dailyRow('2026-09-10'), dailyRow('2026-09-11')]) {
  return { hotel_public_id: TEST_HOTEL.public_id, range: RANGE, days }
}

function revenueBreakdown(categories = [
  {
    category_code: 'FNB',
    is_room_revenue: false,
    currency: 'EUR',
    amount: '22701.00',
    tax_amount: '2951.13',
    entry_count: 39,
  },
  {
    category_code: 'SPA',
    is_room_revenue: false,
    currency: 'EUR',
    amount: '9030.00',
    tax_amount: '1173.90',
    entry_count: 39,
  },
]) {
  return { hotel_public_id: TEST_HOTEL.public_id, range: RANGE, categories }
}

function expenseBreakdown(categories = [
  {
    category_code: 'PAYROLL',
    is_fixed_cost: true,
    currency: 'EUR',
    amount: '31000.00',
    tax_amount: '0.00',
    entry_count: 3,
  },
]) {
  return { hotel_public_id: TEST_HOTEL.public_id, range: RANGE, categories }
}

function demandForecast(overrides: Record<string, unknown> = {}) {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    target_date: '2026-09-13',
    forecast_horizon_days: 7,
    cutoff_date: '2026-09-06',
    prediction_cutoff: '2026-09-07T00:00:00Z',
    predicted_room_nights: 122.91338862512222,
    model: {
      model_name: 'demand_baseline',
      model_version: 'demand_baseline_v1',
      feature_version: 'v1',
      dataset_version: 'v1',
      status: 'offline_research_candidate',
      production_ready: false,
      methodology: 'Gradient-boosted regression trees fitted offline.',
    },
    features_used: ['day_of_week', 'demand_lag_7', 'demand_lag_14', 'demand_lag_28'],
    ...overrides,
  }
}

/* --- setup -------------------------------------------------------------------------------- */

let fetchStub: FetchStub

beforeEach(() => {
  /* The page derives its range and its forecast target from "today in the hotel's zone", so
     the clock is pinned. Only `Date` is faked: faking timers as well freezes the clock that
     `waitFor` polls on. */
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-09-12T09:00:00Z'))

  fetchStub = installFetchStub()
  seedStoredToken()
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage([TEST_HOTEL], 1) })
  fetchStub.on('GET', '/analytics/overview', { body: overview() })
  fetchStub.on('GET', '/analytics/daily', { body: dailySeries() })
  fetchStub.on('GET', '/analytics/revenue-by-category', { body: revenueBreakdown() })
  fetchStub.on('GET', '/analytics/expenses-by-category', { body: expenseBreakdown() })
  fetchStub.on('GET', '/ml/demand-forecast', { body: demandForecast() })
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function renderPage() {
  return renderWithAuth([{ path: ROUTES.analytics, element: <AnalyticsPage /> }], {
    initialEntries: [ROUTES.analytics],
  })
}

function requestsFor(fragment: string) {
  return fetchStub.calls.filter((call) => call.url.includes(fragment))
}

function urlOf(fragment: string) {
  const matches = requestsFor(fragment)
  return new URL(matches[matches.length - 1]!.url, 'http://localhost')
}

/* --- the request contract ------------------------------------------------------------------ */

describe('the request contract', () => {
  it('asks each endpoint at its documented path, scoped to the selected hotel', async () => {
    renderPage()
    await screen.findByText('28.7%')

    for (const fragment of [
      'analytics/overview',
      'analytics/daily',
      'analytics/revenue-by-category',
      'analytics/expenses-by-category',
      'ml/demand-forecast',
    ]) {
      const url = urlOf(fragment)
      expect(url.pathname).toContain(`/hotels/${TEST_HOTEL.public_id}/${fragment}`)
    }
  })

  it('sends an explicit, bounded date range and nothing undocumented', async () => {
    renderPage()
    await screen.findByText('28.7%')

    for (const fragment of [
      'analytics/overview',
      'analytics/daily',
      'analytics/revenue-by-category',
      'analytics/expenses-by-category',
    ]) {
      const url = urlOf(fragment)
      expect(url.searchParams.get('date_from')).toBe('2026-06-15')
      expect(url.searchParams.get('date_to')).toBe('2026-09-12')
      expect([...url.searchParams.keys()].sort()).toEqual(['date_from', 'date_to'])
    }
  })

  it('asks the model for one explicit day and does not send a horizon', async () => {
    renderPage()
    await screen.findByText('28.7%')

    const url = urlOf('ml/demand-forecast')
    // Tomorrow in the hotel's zone. The endpoint has no implicit "next day".
    expect(url.searchParams.get('target_date')).toBe('2026-09-13')
    expect(url.searchParams.has('horizon_days')).toBe(false)
  })

  it('issues the five requests concurrently rather than in a waterfall', async () => {
    renderPage()
    await screen.findByText('28.7%')

    // All five are in flight before any of them resolves: the report's requests appear
    // consecutively in the call log, uninterrupted by a later round.
    const reportCalls = fetchStub.calls.filter(
      (call) => call.url.includes('/analytics/') || call.url.includes('/ml/'),
    )
    expect(reportCalls).toHaveLength(5)
  })

  it('re-queries the backend when the period changes instead of filtering locally', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    renderPage()
    await screen.findByText('28.7%')

    const before = requestsFor('analytics/overview').length
    await user.click(screen.getByRole('radio', { name: /last 30 days/i }))

    await waitFor(() => {
      expect(requestsFor('analytics/overview').length).toBeGreaterThan(before)
    })
    expect(urlOf('analytics/overview').searchParams.get('date_from')).toBe('2026-08-14')
  })
})

/* --- the figures --------------------------------------------------------------------------- */

describe('the figures on screen', () => {
  it('renders occupancy, ADR, RevPAR and room revenue from the overview response', async () => {
    renderPage()

    expect(await screen.findByText('28.7%')).toBeInTheDocument()
    expect(screen.getByText('EUR 148.87')).toBeInTheDocument()
    expect(screen.getByText('EUR 42.70')).toBeInTheDocument()
    expect(screen.getByText('EUR 61,485.00')).toBeInTheDocument()
  })

  it('shows a dash rather than zero when the backend reports a metric as undefined', async () => {
    fetchStub.on('GET', '/analytics/overview', {
      body: overview({
        occupancy: {
          occupied_room_nights: 0,
          room_nights_sold: 0,
          complimentary_room_nights: 0,
          available_room_nights: 0,
          occupancy_rate: null,
          available_room_nights_basis: 'current_active_rooms',
        },
        room_revenue: [{ currency: 'EUR', room_revenue: '0.00', adr: null, revpar: null }],
      }),
    })
    renderPage()

    await screen.findByText(/no room nights were sold in this period/i)

    /* ADR and RevPAR arrived as null and must read as a dash. Room revenue arrived as a real
       "0.00" and correctly shows EUR 0.00 -- a recorded zero is not the same as an undefined
       metric, and conflating them is exactly what this card exists to avoid. */
    const adr = screen.getByText('ADR').closest('div')!
    expect(within(adr).getByText('—')).toBeInTheDocument()
    const revpar = screen.getByText('RevPAR').closest('div')!
    expect(within(revpar).getByText('—')).toBeInTheDocument()
    expect(within(adr).queryByText(/EUR/)).not.toBeInTheDocument()
  })

  it('prints the category tables exactly as the server grouped them, with no total row', async () => {
    renderPage()
    await screen.findByText('28.7%')

    const revenueTable = screen.getByRole('table', { name: /ledger revenue/i })
    expect(within(revenueTable).getByText('FNB')).toBeInTheDocument()
    expect(within(revenueTable).getByText('EUR 22701.00')).toBeInTheDocument()
    expect(within(revenueTable).getByText('EUR 9030.00')).toBeInTheDocument()
    // 22701.00 + 9030.00 = 31731.00. Nothing must add them up.
    expect(within(revenueTable).queryByText(/31731/)).not.toBeInTheDocument()
    expect(within(revenueTable).queryByText(/total/i)).not.toBeInTheDocument()

    const expenseTable = screen.getByRole('table', { name: /ledger expenses/i })
    expect(within(expenseTable).getByText('PAYROLL')).toBeInTheDocument()
    expect(within(expenseTable).getByText('Fixed cost')).toBeInTheDocument()
  })

  it('says so when a period mixes currencies rather than choosing one silently', async () => {
    fetchStub.on('GET', '/analytics/overview', {
      body: overview({
        is_multi_currency: true,
        room_revenue: [
          { currency: 'EUR', room_revenue: '61485.00', adr: '148.87', revpar: '42.70' },
          { currency: 'USD', room_revenue: '880.00', adr: '110.00', revpar: '30.00' },
        ],
      }),
    })
    renderPage()

    expect(await screen.findByText(/nothing is converted/i)).toBeInTheDocument()
  })

  it('renders the activity charts with accessible names and no interpolation', async () => {
    renderPage()
    await screen.findByText('28.7%')

    expect(screen.getByRole('heading', { name: 'Occupancy', level: 3 })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Arrivals', level: 3 })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Bookings created', level: 3 })).toBeInTheDocument()
  })
})

/* --- the forecast, and its boundary ---------------------------------------------------------- */

describe('the demand estimate', () => {
  it('renders the model-s number, labelled as modelled rather than recorded', async () => {
    renderPage()

    expect(await screen.findByText('122.9')).toBeInTheDocument()
    expect(screen.getByText(/modelled estimate/i)).toBeInTheDocument()
    expect(screen.getByText('demand_baseline_v1')).toBeInTheDocument()
  })

  it('states that the model is not production ready and claims no accuracy', async () => {
    renderPage()
    await screen.findByText('122.9')

    expect(screen.getByText(/not production ready/i)).toBeInTheDocument()
    expect(screen.getByText(/has not been established/i)).toBeInTheDocument()
  })

  it('draws no confidence interval and invents no range', async () => {
    renderPage()
    await screen.findByText('122.9')

    expect(screen.queryByText(/confidence/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/interval/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/±/)).not.toBeInTheDocument()
  })

  it('shows no up or down indicator against the recorded period', async () => {
    renderPage()
    await screen.findByText('122.9')

    const panel = screen.getByText(/modelled estimate/i).closest('div')!
    expect(within(panel).queryByText(/vs previous/i)).not.toBeInTheDocument()
  })

  it('explains a refusal instead of showing zero, and keeps the rest of the report', async () => {
    fetchStub.on('GET', '/ml/demand-forecast', {
      status: 422,
      body: {
        error: {
          code: 'INSUFFICIENT_HISTORY',
          message: 'Not enough recorded occupancy to forecast this date.',
        },
      },
    })
    renderPage()

    // The report still rendered.
    expect(await screen.findByText('28.7%')).toBeInTheDocument()
    // And the model's refusal is an explanation, not a number.
    expect(screen.getByText(/no estimate for tomorrow/i)).toBeInTheDocument()
    expect(screen.getByText(/declines rather than guessing/i)).toBeInTheDocument()
    expect(screen.queryByText('0.0')).not.toBeInTheDocument()
  })

  it('survives the model being unavailable in this runtime', async () => {
    fetchStub.on('GET', '/ml/demand-forecast', {
      status: 503,
      body: {
        error: { code: 'MODEL_UNAVAILABLE', message: 'The demand model is not available.' },
      },
    })
    renderPage()

    expect(await screen.findByText('28.7%')).toBeInTheDocument()
    expect(screen.getByText(/no estimate for tomorrow/i)).toBeInTheDocument()
  })
})

/* --- states ---------------------------------------------------------------------------------- */

describe('loading, empty and failure', () => {
  it('shows a pending state rather than empty figures while loading', async () => {
    renderPage()

    /* Queried synchronously: the first render has no hotel and no data yet, so the pending
       state is on screen before any request resolves. `findBy*` would poll after it had
       already been replaced. */
    expect(screen.getByText(/loading this period/i)).toBeInTheDocument()
    expect(screen.queryByText('28.7%')).not.toBeInTheDocument()

    // And it is replaced by the real figures rather than persisting.
    expect(await screen.findByText('28.7%')).toBeInTheDocument()
    expect(screen.queryByText(/loading this period/i)).not.toBeInTheDocument()
  })

  it('renders an honest empty state for a period with no ledger entries', async () => {
    fetchStub.on('GET', '/analytics/revenue-by-category', { body: revenueBreakdown([]) })
    fetchStub.on('GET', '/analytics/expenses-by-category', { body: expenseBreakdown([]) })
    renderPage()

    expect(await screen.findByText(/no revenue was posted in this period/i)).toBeInTheDocument()
    expect(screen.getByText(/no expenses were posted in this period/i)).toBeInTheDocument()
  })

  it('never shows zero when a report request failed', async () => {
    fetchStub.on('GET', '/analytics/overview', {
      status: 500,
      body: { error: { code: 'INTERNAL_ERROR', message: 'Something went wrong.' } },
    })
    renderPage()

    await waitFor(() => {
      expect(screen.queryByText('28.7%')).not.toBeInTheDocument()
    })
    expect(screen.queryByText('0.0%')).not.toBeInTheDocument()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })

  it('reports an authorization failure without inventing figures', async () => {
    fetchStub.on('GET', '/analytics/overview', {
      status: 404,
      body: { error: { code: 'HOTEL_NOT_FOUND', message: 'Hotel not found.' } },
    })
    renderPage()

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByText('28.7%')).not.toBeInTheDocument()
  })

  it('explains that no hotel is linked rather than rendering an empty report', async () => {
    fetchStub.on('GET', '/hotels?page=', { body: hotelPage([], 0) })
    renderPage()

    expect(await screen.findByText(/no hotel is linked to your account/i)).toBeInTheDocument()
  })
})

/* --- identifiers ------------------------------------------------------------------------------ */

describe('identifiers', () => {
  it('addresses the hotel by its public UUID and shows no internal identifier', async () => {
    renderPage()
    await screen.findByText('28.7%')

    for (const call of fetchStub.calls) {
      expect(call.url).not.toMatch(/\/hotels\/\d+(\/|$)/)
    }
    expect(screen.queryByText(TEST_HOTEL.public_id)).not.toBeInTheDocument()
  })
})
