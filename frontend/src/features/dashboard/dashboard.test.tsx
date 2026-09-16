import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { HotelContextChip } from '@/components/layout/HotelContext'
import { describeFailure } from '@/features/dashboard/failures'
import { todayInZone } from '@/features/dashboard/period'
import { DashboardPage } from '@/pages/DashboardPage'
import { ApiError } from '@/services/api/ApiError'
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
 * The dashboard's consumption of the analytics API.
 *
 * Mocked at `fetch`, the real boundary, so the URL, the query parameters, the
 * `Authorization` header and the error translation are all exercised. Stubbing the analytics
 * service instead would leave the thing most likely to be wrong -- the request that is
 * actually sent -- untested.
 *
 * **These do not re-test the backend.** Whether 9,750.00 / 65 is the right ADR is settled by
 * the backend's own suite and by PostgreSQL; what is tested here is that the number the API
 * sent is the number on screen, that a null is not rendered as a zero, and that a failure
 * does not become a figure.
 *
 * The fixtures are the real payloads captured from the live backend during this stage, not
 * invented shapes.
 */

/* --- fixtures, transcribed from live responses ------------------------------------------ */

const CURRENT_OVERVIEW = {
  hotel_public_id: TEST_HOTEL.public_id,
  range: { date_from: '2026-09-04', date_to: '2026-09-10', days: 7 },
  bookings_created: {
    total: 165,
    pending: 0,
    confirmed: 32,
    checked_in: 9,
    checked_out: 124,
    cancelled: 0,
    no_show: 0,
  },
  bookings_by_stay: {
    total: 31,
    pending: 0,
    confirmed: 4,
    checked_in: 9,
    checked_out: 18,
    cancelled: 0,
    no_show: 0,
  },
  stay_flow: { arrivals: 26, departures: 24, cancellations: 0 },
  occupancy: {
    occupied_room_nights: 65,
    room_nights_sold: 65,
    complimentary_room_nights: 0,
    available_room_nights: 112,
    occupancy_rate: '0.5804',
    available_room_nights_basis: 'current_active_rooms',
  },
  room_revenue: [{ currency: 'EUR', room_revenue: '9750.00', adr: '150.00', revpar: '87.05' }],
  other_revenue: [{ currency: 'EUR', amount: '6223.00' }],
  ledger_room_revenue: [],
  total_expenses: [{ currency: 'EUR', amount: '2220.00' }],
  net_operating_result: [{ currency: 'EUR', amount: '13753.00' }],
  is_multi_currency: false,
  reviews: { review_count: 8, published_count: 7, average_rating_normalized: '0.8750' },
}

const PREVIOUS_OVERVIEW = {
  ...CURRENT_OVERVIEW,
  range: { date_from: '2026-08-28', date_to: '2026-09-03', days: 7 },
  occupancy: { ...CURRENT_OVERVIEW.occupancy, occupancy_rate: '0.5000' },
  room_revenue: [{ currency: 'EUR', room_revenue: '8000.00', adr: '125.00', revpar: '71.43' }],
}

/** A range the backend answers with 200 and nothing in it. Confirmed against the live API. */
const EMPTY_OVERVIEW = {
  ...CURRENT_OVERVIEW,
  bookings_created: { ...CURRENT_OVERVIEW.bookings_created, total: 0, confirmed: 0, checked_in: 0, checked_out: 0 },
  bookings_by_stay: { ...CURRENT_OVERVIEW.bookings_by_stay, total: 0, confirmed: 0, checked_in: 0, checked_out: 0 },
  stay_flow: { arrivals: 0, departures: 0, cancellations: 0 },
  occupancy: {
    occupied_room_nights: 0,
    room_nights_sold: 0,
    complimentary_room_nights: 0,
    available_room_nights: 112,
    // Not null: the denominator exists, so the rate is a measured zero.
    occupancy_rate: '0.0000',
    available_room_nights_basis: 'current_active_rooms',
  },
  // Empty arrays, not zero buckets. This is what the live backend returns.
  room_revenue: [],
  other_revenue: [],
  total_expenses: [],
  net_operating_result: [],
  reviews: { review_count: 0, published_count: 0, average_rating_normalized: null },
}

const MULTI_CURRENCY_OVERVIEW = {
  ...CURRENT_OVERVIEW,
  room_revenue: [
    { currency: 'EUR', room_revenue: '9750.00', adr: '150.00', revpar: '87.05' },
    { currency: 'USD', room_revenue: '4200.00', adr: '210.00', revpar: '37.50' },
  ],
  other_revenue: [
    { currency: 'EUR', amount: '6223.00' },
    { currency: 'USD', amount: '900.00' },
  ],
  total_expenses: [{ currency: 'EUR', amount: '2220.00' }],
  net_operating_result: [
    { currency: 'EUR', amount: '13753.00' },
    { currency: 'USD', amount: '5100.00' },
  ],
  is_multi_currency: true,
}

function dailyRow(date: string, occupied: number, revenue: string | null) {
  return {
    date,
    occupied_room_nights: occupied,
    room_nights_sold: occupied,
    available_room_nights: 16,
    occupancy_rate: (occupied / 16).toFixed(4),
    room_revenue:
      revenue === null
        ? []
        : [{ currency: 'EUR', room_revenue: revenue, adr: '150.00', revpar: '86.25' }],
    other_revenue: [],
    total_expenses: [],
    arrivals: 2,
    departures: 1,
    bookings_created: 0,
    cancellations: 0,
  }
}

const DAILY = {
  hotel_public_id: TEST_HOTEL.public_id,
  range: { date_from: '2026-09-04', date_to: '2026-09-10', days: 7 },
  days: [
    dailyRow('2026-09-04', 9, '1380.00'),
    dailyRow('2026-09-05', 11, '1710.00'),
    dailyRow('2026-09-06', 8, '1140.00'),
    dailyRow('2026-09-07', 10, '1455.00'),
    dailyRow('2026-09-08', 7, '1005.00'),
    dailyRow('2026-09-09', 12, '1830.00'),
    dailyRow('2026-09-10', 8, '1230.00'),
  ],
}

/* --- harness ---------------------------------------------------------------------------- */

let fetchStub: FetchStub

function stubSession(hotels: readonly unknown[] = [TEST_HOTEL], total?: number) {
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage(hotels, total ?? hotels.length) })
}

/**
 * Routes the current window to one payload and the comparison window to another.
 *
 * Keyed on `date_to`, because that is what actually separates them for every period: the
 * selected window always ends on the hotel's today, and the comparison window always ends
 * before it. An earlier version keyed on `date_from` against a fixed date, which silently
 * mislabelled every period except the seven-day default -- the 30-day window starts in
 * August and was served the comparison fixture.
 */
function stubOverview(current: unknown, previous: unknown = PREVIOUS_OVERVIEW) {
  const today = todayInZone(TEST_HOTEL.timezone)
  fetchStub.on('GET', 'analytics/overview', ({ url }) => {
    const to = new URL(url, 'http://localhost').searchParams.get('date_to')!
    return { body: to === today ? current : previous }
  })
}

function mount() {
  return renderWithAuth([{ path: '/', element: <DashboardPage /> }], { initialEntries: ['/'] })
}

/** Every analytics request that was issued, for counting and for checking query strings. */
function analyticsCalls() {
  return fetchStub.calls.filter((call) => call.url.includes('/analytics/'))
}

function overviewCalls() {
  return fetchStub.calls.filter((call) => call.url.includes('analytics/overview'))
}

beforeEach(() => {
  fetchStub = installFetchStub()
  seedStoredToken()
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
  vi.restoreAllMocks()
})

/* --- successful rendering --------------------------------------------------------------- */

describe('rendering real analytics', () => {
  beforeEach(() => {
    stubSession()
    stubOverview(CURRENT_OVERVIEW)
    fetchStub.on('GET', 'analytics/daily', { body: DAILY })
  })

  it('shows the figures the backend sent', async () => {
    mount()

    // occupancy_rate "0.5804" is a fraction: 58.0%, not 0.6% and not 5804%.
    expect(await screen.findByText('58.0%')).toBeInTheDocument()
  })

  it('labels every metric it shows', async () => {
    const { container } = mount()
    await screen.findByText('58.0%')

    // Read the <dt> elements directly. `getByText('Occupancy')` matches the chart's own
    // table header too, and asserting on whichever came first would be asserting on an
    // accident of document order rather than on the KPI tiles.
    const terms = Array.from(container.querySelectorAll('dt')).map((el) => el.textContent)
    expect(terms).toEqual(
      expect.arrayContaining(['Occupancy', 'ADR', 'RevPAR', 'Room revenue']),
    )
  })

  it('renders ADR exactly as the API reported it, with its currency', async () => {
    mount()
    await screen.findByText('58.0%')

    // "150.00" in EUR. The code, not a symbol -- see formatMoney.
    expect(screen.getByText(/EUR\s*150\.00/)).toBeInTheDocument()
  })

  it('renders RevPAR exactly as the API reported it', async () => {
    mount()
    await screen.findByText('58.0%')

    expect(screen.getByText(/EUR\s*87\.05/)).toBeInTheDocument()
  })

  it('renders room revenue without rounding it away', async () => {
    mount()
    await screen.findByText('58.0%')

    expect(screen.getByText(/EUR\s*9,750\.00/)).toBeInTheDocument()
  })

  it('renders ledger revenue, expenses and the net result from their own fields', async () => {
    mount()
    await screen.findByText('58.0%')

    expect(screen.getByText(/EUR\s*6,223\.00/)).toBeInTheDocument()
    expect(screen.getByText(/EUR\s*2,220\.00/)).toBeInTheDocument()
    expect(screen.getByText(/EUR\s*13,753\.00/)).toBeInTheDocument()
  })

  it('states the capacity basis the backend declared, rather than hiding it', async () => {
    mount()
    await screen.findByText('58.0%')

    expect(screen.getByText(/current active room count/)).toBeInTheDocument()
  })

  it('reports the period being shown, with both dates included', async () => {
    mount()
    await screen.findByText('58.0%')

    expect(screen.getByText(/both dates included/)).toBeInTheDocument()
  })

  it('names the hotel and the calendar the dates belong to', async () => {
    mount()
    await screen.findByText('58.0%')

    expect(screen.getByText(/Meridian Harbour Hotel/)).toBeInTheDocument()
    expect(screen.getByText(/Europe\/Athens/)).toBeInTheDocument()
  })
})

/* --- comparison figures ----------------------------------------------------------------- */

describe('period-over-period comparison', () => {
  it('derives the change from two authoritative windows', async () => {
    stubSession()
    stubOverview(CURRENT_OVERVIEW)
    fetchStub.on('GET', 'analytics/daily', { body: DAILY })
    mount()
    await screen.findByText('58.0%')

    // ADR 150.00 against 125.00 is +20.0%. Occupancy 0.5804 against 0.5000 is +16.1%.
    expect(screen.getByText('+20.0%')).toBeInTheDocument()
    expect(screen.getByText('+16.1%')).toBeInTheDocument()
    expect(screen.getAllByText('vs previous 7 days').length).toBeGreaterThan(0)
  })

  it('shows no comparison at all when the previous window could not be fetched', async () => {
    stubSession()
    // Keyed on `date_to` for the same reason stubOverview is: the selected window always ends
    // on the hotel's today and the comparison window always ends before it, so that is what
    // separates them on every day this suite runs. Keying on `date_from` against the fixture's
    // own literal made this test a calendar bomb -- it passed only while the comparison window
    // began before 2026-09-04, and started failing on 2026-09-17, when `today - 13` reached it.
    const today = todayInZone(TEST_HOTEL.timezone)
    fetchStub.on('GET', 'analytics/overview', ({ url }) => {
      const to = new URL(url, 'http://localhost').searchParams.get('date_to')!
      return to === today
        ? { body: CURRENT_OVERVIEW }
        : { status: 500, body: { error: { code: 'INTERNAL_ERROR', message: 'x', details: [] } } }
    })
    fetchStub.on('GET', 'analytics/daily', { body: DAILY })
    mount()
    await screen.findByText('58.0%')

    // The figures are still shown -- a missing comparison must not blank the dashboard --
    // but no invented change appears beside them.
    expect(screen.queryByText(/vs previous/)).not.toBeInTheDocument()
    expect(screen.queryByText(/%$/, { selector: 'p' })).not.toBeInTheDocument()
  })
})

/* --- request discipline ------------------------------------------------------------------ */

describe('request count', () => {
  beforeEach(() => {
    stubSession()
    stubOverview(CURRENT_OVERVIEW)
    fetchStub.on('GET', 'analytics/daily', { body: DAILY })
  })

  it('issues exactly three analytics requests for one hotel and period', async () => {
    mount()
    await screen.findByText('58.0%')

    // Two overviews (current and comparison) and one daily series. Fixed, regardless of how
    // many days, rooms, bookings or currencies the response holds -- this is the assertion
    // that would fail if a per-day or per-currency fetch ever crept in.
    expect(analyticsCalls()).toHaveLength(3)
  })

  it('does not duplicate any of them', async () => {
    mount()
    await screen.findByText('58.0%')

    const urls = analyticsCalls().map((call) => call.url)
    expect(new Set(urls).size).toBe(urls.length)
  })

  it('sends the session token on every analytics request', async () => {
    mount()
    await screen.findByText('58.0%')

    for (const call of analyticsCalls()) {
      expect(call.authorization).toMatch(/^Bearer /)
    }
  })

  it('asks the API for the hotel by its public id and nothing else', async () => {
    mount()
    await screen.findByText('58.0%')

    for (const call of analyticsCalls()) {
      expect(call.url).toContain(`/hotels/${TEST_HOTEL.public_id}/analytics/`)
    }
  })
})

/* --- the period control ------------------------------------------------------------------ */

describe('changing the period', () => {
  beforeEach(() => {
    stubSession()
    stubOverview(CURRENT_OVERVIEW)
    fetchStub.on('GET', 'analytics/daily', { body: DAILY })
  })

  it('offers the periods as a labelled radio group', async () => {
    mount()
    await screen.findByText('58.0%')

    const group = screen.getByRole('group', { name: 'Reporting period' })
    expect(within(group).getByRole('radio', { name: 'Today' })).toBeInTheDocument()
    expect(within(group).getByRole('radio', { name: 'Last 7 days' })).toBeChecked()
    expect(within(group).getByRole('radio', { name: 'Last 30 days' })).toBeInTheDocument()
  })

  it('issues a new request spanning exactly the days it advertises', async () => {
    const user = userEvent.setup()
    mount()
    await screen.findByText('58.0%')
    const before = overviewCalls().length

    await user.click(screen.getByRole('radio', { name: 'Last 30 days' }))
    await waitFor(() => {
      expect(overviewCalls().length).toBeGreaterThan(before)
    })

    const params = new URL(overviewCalls().at(-2)!.url, 'http://localhost').searchParams
    const from = Date.parse(`${params.get('date_from')}T00:00:00Z`)
    const to = Date.parse(`${params.get('date_to')}T00:00:00Z`)
    // Inclusive of both ends: 30 days is 29 days of difference.
    expect((to - from) / 86_400_000).toBe(29)
  })

  it('requests the comparison window immediately before the selected one', async () => {
    mount()
    await screen.findByText('58.0%')

    const ranges = overviewCalls().map((call) => {
      const params = new URL(call.url, 'http://localhost').searchParams
      return { from: params.get('date_from')!, to: params.get('date_to')! }
    })
    const [current, previous] = ranges[0]!.from > ranges[1]!.from
      ? [ranges[0]!, ranges[1]!]
      : [ranges[1]!, ranges[0]!]

    // Adjacent and non-overlapping: previous ends the day before current begins.
    const dayAfterPrevious = new Date(Date.parse(`${previous.to}T00:00:00Z`) + 86_400_000)
    expect(dayAfterPrevious.toISOString().slice(0, 10)).toBe(current.from)
  })

  it('labels the range from the response, so the caption can never describe other figures', async () => {
    const user = userEvent.setup()
    mount()
    await screen.findByText('58.0%')

    // Every response is stubbed with the same echoed range (4–10 Sept), so if the caption
    // followed the SELECTION rather than the data it would change here and if it follows the
    // data it will not. Found in the browser: for one frame after clicking, a 30-day caption
    // sat above seven days of figures.
    await user.click(screen.getByRole('radio', { name: 'Last 30 days' }))
    await screen.findByText('58.0%')

    expect(screen.getByText(/4 Sept – 10 Sept 2026/)).toBeInTheDocument()
  })

  it('uses the backend query parameter names', async () => {
    mount()
    await screen.findByText('58.0%')

    const params = new URL(overviewCalls()[0]!.url, 'http://localhost').searchParams
    expect(params.get('date_from')).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    expect(params.get('date_to')).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })
})

/* --- loading -------------------------------------------------------------------------- */

describe('loading', () => {
  it('announces the wait instead of showing placeholder numbers', () => {
    stubSession()
    stubOverview(CURRENT_OVERVIEW)
    fetchStub.on('GET', 'analytics/daily', { body: DAILY })
    // Asserted synchronously, on the first render: `findBy*` would poll, and by its first
    // retry the data has arrived, so the loading state it was written to catch is gone.
    const { container } = mount()

    expect(screen.getByText('Loading analytics…')).toBeInTheDocument()
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
    // Nothing that could be mistaken for a measurement is on screen yet.
    expect(screen.queryByText('58.0%')).not.toBeInTheDocument()
    expect(screen.queryByText(/EUR/)).not.toBeInTheDocument()
  })

  it('keeps the application shell and the period control usable while data loads', async () => {
    stubSession()
    stubOverview(CURRENT_OVERVIEW)
    fetchStub.on('GET', 'analytics/daily', { body: DAILY })
    mount()

    expect(await screen.findByRole('heading', { name: 'Overview' })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Reporting period' })).toBeInTheDocument()
  })
})

/* --- empty data ------------------------------------------------------------------------ */

describe('an empty period', () => {
  beforeEach(() => {
    stubSession()
    stubOverview(EMPTY_OVERVIEW, EMPTY_OVERVIEW)
    fetchStub.on('GET', 'analytics/daily', {
      body: { ...DAILY, days: DAILY.days.map((day) => dailyRow(day.date, 0, null)) },
    })
  })

  it('says the period was empty rather than looking broken', async () => {
    mount()

    expect(await screen.findByText(/No activity in the last 7 days/i)).toBeInTheDocument()
  })

  it('announces it politely, not as an alert', async () => {
    mount()
    await screen.findByText(/No activity in the last 7 days/i)

    // An empty week is an ordinary answer. Announcing it as an alert would make a quiet
    // period sound like a fault.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getAllByRole('status').length).toBeGreaterThan(0)
  })

  it('shows a measured zero occupancy, because the denominator exists', async () => {
    mount()
    await screen.findByText(/No activity in the last 7 days/i)

    expect(screen.getByText('0.0%')).toBeInTheDocument()
  })

  it('shows a dash for ADR, because an undefined rate is not zero', async () => {
    mount()
    await screen.findByText(/No activity in the last 7 days/i)

    expect(screen.getByText(/No room nights were sold in this period/)).toBeInTheDocument()
    // The single most important negative assertion in this file.
    expect(screen.queryByText(/EUR\s*0\.00/)).not.toBeInTheDocument()
  })

  it('offers no retry, because nothing failed', async () => {
    mount()
    await screen.findByText(/No activity in the last 7 days/i)

    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  })
})

/* --- failures --------------------------------------------------------------------------- */

function stubOverviewFailure(status: number, body: unknown, headers?: Record<string, string>) {
  fetchStub.on('GET', 'analytics/overview', {
    status,
    body,
    ...(headers ? { headers } : {}),
  })
  fetchStub.on('GET', 'analytics/daily', { status, body })
}

const envelope = (code: string, message: string) => ({ error: { code, message, details: [] } })

describe('failures', () => {
  beforeEach(() => {
    stubSession()
  })

  it('does not invent figures when the request fails', async () => {
    stubOverviewFailure(500, envelope('INTERNAL_ERROR', 'An internal error occurred.'))
    mount()

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    // No zeroes, no dashes pretending to be data, no currency.
    expect(screen.queryByText('0.0%')).not.toBeInTheDocument()
    expect(screen.queryByText(/EUR/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Loading key figures')).not.toBeInTheDocument()
  })

  it('never lets a backend internal detail reach the DOM', async () => {
    stubOverviewFailure(
      500,
      envelope(
        'INTERNAL_ERROR',
        'psycopg.errors.UndefinedTable: relation "booking_room_nights" does not exist',
      ),
    )
    mount()
    await screen.findByRole('alert')

    const markup = document.body.innerHTML
    for (const leak of ['psycopg', 'UndefinedTable', 'booking_room_nights', 'relation']) {
      expect(markup).not.toContain(leak)
    }
  })

  it('offers a retry for a server fault, and repeating it re-requests', async () => {
    const user = userEvent.setup()
    stubOverviewFailure(503, envelope('DATABASE_UNAVAILABLE', 'Unavailable.'))
    mount()
    await screen.findByRole('alert')
    const before = analyticsCalls().length

    await user.click(screen.getByRole('button', { name: 'Try again' }))

    await waitFor(() => {
      expect(analyticsCalls().length).toBeGreaterThan(before)
    })
  })

  it('explains a 403 without describing the authorization model', async () => {
    stubOverviewFailure(
      403,
      envelope('FORBIDDEN', 'This operation requires the manager role at this hotel.'),
    )
    mount()

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/do not have access/i)).toBeInTheDocument()
    // The backend's own message names a role. That is a fact about the permission model and
    // is not repeated to someone who has just been refused.
    expect(document.body.innerHTML).not.toContain('manager role')
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  })

  it('treats a 404 as "not available to you", which is what it means here', async () => {
    stubOverviewFailure(404, envelope('NOT_FOUND', 'Hotel not found.'))
    mount()

    const alert = await screen.findByRole('alert')
    // The backend answers a non-member and a nonexistent hotel identically, on purpose. The
    // copy therefore covers both without claiming to know which.
    expect(within(alert).getByText(/Hotel not available/i)).toBeInTheDocument()
  })

  it('shows the server’s own wait on a 429, not one of its own', async () => {
    stubOverviewFailure(429, envelope('RATE_LIMITED', 'Too many requests.'), {
      'Retry-After': '37',
    })
    mount()

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/try again in 37 seconds/i)).toBeInTheDocument()
  })

  it('shows no countdown on a 429 that carried no Retry-After', async () => {
    stubOverviewFailure(429, envelope('RATE_LIMITED', 'Too many requests.'))
    mount()

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/wait a moment/i)).toBeInTheDocument()
    expect(within(alert).queryByText(/\d+ seconds/)).not.toBeInTheDocument()
  })

  it('distinguishes an unreachable server from a refusal', async () => {
    fetchStub.on('GET', 'analytics/overview', { networkError: true })
    fetchStub.on('GET', 'analytics/daily', { networkError: true })
    mount()

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/Could not reach the server/i)).toBeInTheDocument()
  })

  it('handles a body that is not the documented envelope', async () => {
    fetchStub.on('GET', 'analytics/overview', { status: 502, body: '<html>Bad Gateway</html>' })
    fetchStub.on('GET', 'analytics/daily', { status: 502, body: '<html>Bad Gateway</html>' })
    mount()

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/Unexpected response|temporarily unavailable/i)).toBeInTheDocument()
    expect(document.body.innerHTML).not.toContain('Bad Gateway')
  })

  it('ends the session on a 401 and leaves no figures behind', async () => {
    stubOverviewFailure(401, envelope('INVALID_TOKEN', 'Not authenticated.'))
    mount()

    // The API client's bridge clears the session, so nothing is left in storage for the
    // next person at this machine. In the running application the route guard then
    // redirects to sign-in; this test renders the page without that guard, so what it can
    // assert is the part that matters for security.
    await waitFor(() => {
      expect(window.sessionStorage.getItem('ahip.access_token')).toBeNull()
    })
    expect(screen.queryByText('58.0%')).not.toBeInTheDocument()
    expect(screen.queryByText(/EUR/)).not.toBeInTheDocument()
  })

  it('drops the hotel list when the session ends, so the next user sees no property names', async () => {
    stubOverviewFailure(401, envelope('INVALID_TOKEN', 'Not authenticated.'))
    mount()
    await waitFor(() => {
      expect(window.sessionStorage.getItem('ahip.access_token')).toBeNull()
    })

    expect(screen.queryByText('Meridian Harbour Hotel')).not.toBeInTheDocument()
  })
})

/* --- the copy a failure produces, independently of the page --------------------------- */

describe('failure copy', () => {
  it('explains an expired session without inviting a pointless retry', () => {
    const notice = describeFailure(new ApiError(401, 'INVALID_TOKEN', 'Not authenticated.'))

    expect(notice.title).toMatch(/session has ended/i)
    expect(notice.canRetry).toBe(false)
  })

  it('never passes the backend’s own message through', () => {
    const leak = 'relation "bookings" does not exist'
    const notice = describeFailure(new ApiError(500, 'INTERNAL_ERROR', leak))

    expect(notice.title).not.toContain(leak)
    expect(notice.detail).not.toContain(leak)
    expect(notice.canRetry).toBe(true)
  })

  it('quotes the server’s own Retry-After rather than guessing one', () => {
    expect(
      describeFailure(new ApiError(429, 'RATE_LIMITED', 'Too many.', [], 1)).detail,
    ).toContain('1 second.')
    expect(
      describeFailure(new ApiError(429, 'RATE_LIMITED', 'Too many.', [], 37)).detail,
    ).toContain('37 seconds')
  })
})

/* --- hotel context ---------------------------------------------------------------------- */

describe('hotel context', () => {
  it('asks the membership-filtered endpoint, not for all hotels', async () => {
    stubSession()
    stubOverview(CURRENT_OVERVIEW)
    fetchStub.on('GET', 'analytics/daily', { body: DAILY })
    mount()
    await screen.findByText('58.0%')

    const call = fetchStub.calls.find((c) => c.url.includes('/hotels?page='))!
    expect(call.authorization).toMatch(/^Bearer /)
  })

  it('says an account has no property, instead of showing an empty dashboard', async () => {
    stubSession([])
    mount()

    expect(await screen.findByText(/No hotel is linked to your account/i)).toBeInTheDocument()
    // And asks for no analytics at all -- there is no hotel to ask about.
    expect(analyticsCalls()).toHaveLength(0)
  })

  it('separates "you have no hotels" from "we could not ask"', async () => {
    fetchStub.on('GET', '/auth/me', { body: TEST_USER })
    fetchStub.on('GET', '/hotels?page=', { networkError: true })
    mount()

    expect(await screen.findByText(/Could not load your hotels/i)).toBeInTheDocument()
    expect(screen.queryByText(/No hotel is linked/i)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('offers a selector only when the account holds more than one membership', async () => {
    stubSession()
    stubOverview(CURRENT_OVERVIEW)
    fetchStub.on('GET', 'analytics/daily', { body: DAILY })
    // The chip lives in the header, so it is rendered explicitly here rather than through
    // the whole shell.
    renderWithAuth([{ path: '/', element: <HotelContextChip /> }])

    expect(await screen.findByText('Meridian Harbour Hotel')).toBeInTheDocument()
    // One property is not a choice. A single-option dropdown would offer a decision that
    // does not exist.
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('re-requests analytics for the hotel that was chosen', async () => {
    const user = userEvent.setup()
    const second = {
      ...TEST_HOTEL,
      public_id: 'b0b0b0b0-0000-4000-8000-000000000001',
      name: 'Second Property',
    }
    stubSession([TEST_HOTEL, second])
    stubOverview(CURRENT_OVERVIEW)
    fetchStub.on('GET', 'analytics/daily', { body: DAILY })
    renderWithAuth([
      {
        path: '/',
        element: (
          <>
            <HotelContextChip />
            <DashboardPage />
          </>
        ),
      },
    ])
    await screen.findByText('58.0%')

    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Selected hotel' }),
      second.public_id,
    )

    await waitFor(() => {
      expect(analyticsCalls().some((call) => call.url.includes(second.public_id))).toBe(true)
    })
  })

  it('ignores a hotel id the server did not return', async () => {
    stubSession()
    stubOverview(CURRENT_OVERVIEW)
    fetchStub.on('GET', 'analytics/daily', { body: DAILY })
    renderWithAuth([{ path: '/', element: <DashboardPage /> }])
    await screen.findByText('58.0%')

    // Selection is not authorization -- the backend re-decides on every request -- but the
    // UI must not enter a state its own data does not describe either.
    expect(
      analyticsCalls().every((call) => call.url.includes(TEST_HOTEL.public_id)),
    ).toBe(true)
  })
})

/* --- currency --------------------------------------------------------------------------- */

describe('multiple currencies', () => {
  beforeEach(() => {
    stubSession()
    stubOverview(MULTI_CURRENCY_OVERVIEW, MULTI_CURRENCY_OVERVIEW)
    fetchStub.on('GET', 'analytics/daily', { body: DAILY })
  })

  it('says so, rather than quietly showing one of them', async () => {
    mount()

    expect(await screen.findByText('Multiple currencies')).toBeInTheDocument()
    expect(screen.getByText(/Nothing is converted/)).toBeInTheDocument()
  })

  it('lists each currency separately and never adds them together', async () => {
    mount()
    await screen.findByText('Multiple currencies')

    const table = screen.getByRole('table', { name: /reported on its own/i })
    expect(within(table).getByRole('rowheader', { name: 'EUR' })).toBeInTheDocument()
    expect(within(table).getByRole('rowheader', { name: 'USD' })).toBeInTheDocument()

    // 9750.00 + 4200.00 = 13950.00. That sum is not money and must appear nowhere.
    expect(document.body.innerHTML).not.toContain('13,950.00')
  })
})

/* --- charts ----------------------------------------------------------------------------- */

describe('the trend charts', () => {
  beforeEach(() => {
    stubSession()
    stubOverview(CURRENT_OVERVIEW)
    fetchStub.on('GET', 'analytics/daily', { body: DAILY })
  })

  it('describes itself to a screen reader rather than being an unlabelled picture', async () => {
    mount()
    await screen.findByText('58.0%')

    const chart = screen.getByRole('img', { name: /Occupancy trend/ })
    expect(chart).toHaveAccessibleDescription(/Lowest .* highest .* latest/)
  })

  it('offers every plotted value as a real table', async () => {
    const user = userEvent.setup()
    mount()
    await screen.findByText('58.0%')

    // Clicked by its visible text: `<details>` does not map to a queryable role in jsdom's
    // accessibility mapping, and the disclosure is what a user actually clicks anyway.
    await user.click(screen.getByText(/Show occupancy values/i))
    const table = screen.getByRole('table', { name: /Occupancy by day/i })
    // Seven days in, seven rows out -- the series is gap-free by the API's guarantee.
    expect(within(table).getAllByRole('row')).toHaveLength(DAILY.days.length + 1)
  })

  it('states the units it is measured in', async () => {
    mount()
    await screen.findByText('58.0%')

    expect(screen.getByRole('img', { name: /Room revenue trend/ })).toHaveAccessibleDescription(
      /Measured in EUR/,
    )
  })

  it('says a series is empty instead of drawing an empty axis', async () => {
    fetchStub.on('GET', 'analytics/daily', {
      body: {
        ...DAILY,
        days: DAILY.days.map((day) => ({ ...dailyRow(day.date, 0, null), occupancy_rate: null })),
      },
    })
    mount()
    await screen.findByText('58.0%')

    expect(screen.getByText(/Occupancy could not be calculated for any day/i)).toBeInTheDocument()
  })
})
