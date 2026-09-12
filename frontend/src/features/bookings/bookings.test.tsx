import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { BookingDetailPage } from '@/pages/BookingDetailPage'
import { BookingsPage } from '@/pages/BookingsPage'
import { bookingPath, BOOKING_DETAIL_PATTERN, ROUTES } from '@/router/routes'
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
import type { Booking, BookingStatus } from '@/types/booking'

/**
 * The bookings operations workflow, against the real API contract.
 *
 * Mocked at `fetch`, the real boundary, so the URL, the query parameters, the
 * `Authorization` header and the error translation are all exercised. The fixtures are the
 * payloads the live backend actually returned during this stage -- decimals as strings,
 * `guest_name` null, timestamps with an offset -- not shapes invented to make assertions
 * convenient.
 *
 * **These do not re-test the backend.** Whether the sum of the nightly rates is the right
 * accommodation total is settled by the backend's own suite. What is tested here is that the
 * figure the API sent is the figure on screen, that `total_amount` is never presented as the
 * authoritative one, and that a failure never becomes a number.
 */

/* --- fixtures, transcribed from live responses ------------------------------------------ */

const GUEST_ID = '98c235eb-5ae6-4c75-a743-836e1a2b38da'

function makeBooking(overrides: Partial<Booking> = {}): Booking {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    public_id: 'e5b72bf8-11a1-456b-9755-5a5ac5219782',
    guest_public_id: GUEST_ID,
    reference: 'MH-00162',
    check_in_date: '2026-09-17',
    check_out_date: '2026-09-18',
    status: 'confirmed',
    adults: 2,
    children: 0,
    source: 'phone',
    channel_reference: null,
    total_amount: '120.00',
    currency: 'EUR',
    special_requests: null,
    cancelled_at: null,
    cancellation_reason: null,
    booked_at: '2026-09-10T00:47:24.319782+03:00',
    created_at: '2026-09-10T00:47:24.319782+03:00',
    updated_at: '2026-09-10T00:47:24.319782+03:00',
    rooms: [
      {
        room_number: '104',
        room_type_code: 'STD',
        adults: 2,
        children: 0,
        guest_name: null,
        nights: 1,
        nightly_rates: [
          {
            stay_date: '2026-09-17',
            rate: '120.00',
            rate_plan_code: null,
            is_complimentary: false,
          },
        ],
      },
    ],
    ...overrides,
  }
}

const RECONCILIATION = {
  booking_public_id: makeBooking().public_id,
  currency: 'EUR',
  accommodation_total: '120.00',
  declared_total: '120.00',
  totals_agree: true,
  charged_total: '0.00',
  refunded_total: '0.00',
  net_paid: '0.00',
  outstanding_amount: '120.00',
  payment_state: 'unpaid',
}

const GUEST = {
  hotel_public_id: TEST_HOTEL.public_id,
  public_id: GUEST_ID,
  first_name: 'Elena',
  last_name: 'Papadakis',
  email: 'elena.papadakis@example.test',
  phone: '+30 210 555 0142',
  country_code: 'GR',
  preferred_language: 'el',
  date_of_birth: '1988-04-17',
  marketing_opt_in: false,
  notes: 'Prefers a quiet room away from the lift.',
  created_at: '2026-09-01T09:00:00+03:00',
  updated_at: '2026-09-01T09:00:00+03:00',
}

function bookingsPage(items: readonly Booking[], total = items.length, page = 1, pageSize = 20) {
  return {
    items,
    total,
    page,
    page_size: pageSize,
    pages: total === 0 ? 0 : Math.ceil(total / pageSize),
  }
}


/** An empty payment ledger. The detail page reads one; a test that renders it must answer. */
const EMPTY_PAYMENTS = { items: [], total: 0, page: 1, page_size: 20, pages: 0 }

/* --- harness ---------------------------------------------------------------------------- */

let fetchStub: FetchStub

function stubSession(hotels: readonly unknown[] = [TEST_HOTEL]) {
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage(hotels, hotels.length) })
}

/**
 * A stubbed failure, told apart from a real payload by a NUMERIC `status`.
 *
 * The obvious discriminator -- `'status' in value` -- is wrong here and cost a debugging
 * round: `Booking` has a `status` field of its own ('confirmed'), so every real booking was
 * treated as a failure response and handed to `new Response(null, { status: 'confirmed' })`.
 * The type of the field, not its presence, is what separates the two.
 */
interface StubFailure {
  readonly status: number
  readonly body: unknown
}

function isFailure(value: object): value is StubFailure {
  return 'status' in value && typeof (value as { status: unknown }).status === 'number'
}

/**
 * Detail routes, registered in this order on purpose.
 *
 * The stub matches the FIRST handler whose fragment the URL contains, so `reconciliation`
 * and `/guests/` must be registered before the bare `/bookings/`, which would otherwise
 * swallow the reconciliation URL.
 */
function stubDetail(
  booking: Booking | StubFailure,
  reconciliation: object = RECONCILIATION,
  guest: object = GUEST,
) {
  fetchStub.on('GET', '/payments', { body: EMPTY_PAYMENTS })
  fetchStub.on(
    'GET',
    'reconciliation',
    isFailure(reconciliation) ? reconciliation : { body: reconciliation },
  )
  fetchStub.on('GET', '/guests/', isFailure(guest) ? guest : { body: guest })
  fetchStub.on('GET', '/bookings/', isFailure(booking) ? booking : { body: booking })
}

function mountList() {
  return renderWithAuth([{ path: ROUTES.bookings, element: <BookingsPage /> }], {
    initialEntries: [ROUTES.bookings],
  })
}

/** Mounts the list and the detail page together, so a real link click can be followed. */
function mountRouted(initial: string) {
  return renderWithAuth(
    [
      { path: ROUTES.bookings, element: <BookingsPage /> },
      { path: BOOKING_DETAIL_PATTERN, element: <BookingDetailPage /> },
    ],
    { initialEntries: [initial] },
  )
}

const bookingCalls = () => fetchStub.calls.filter((c) => c.url.includes('/bookings'))
const listCalls = () => fetchStub.calls.filter((c) => c.url.includes('/bookings?'))
const envelope = (code: string, message: string) => ({ error: { code, message, details: [] } })

beforeEach(() => {
  fetchStub = installFetchStub()
  seedStoredToken()
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
  vi.restoreAllMocks()
})

/* --- the request the list actually sends -------------------------------------------------- */

describe('the bookings list request', () => {
  beforeEach(() => {
    stubSession()
    fetchStub.on('GET', '/bookings?', { body: bookingsPage([makeBooking()], 1) })
  })

  it('addresses the hotel by public id and asks for page 1', async () => {
    mountList()
    await screen.findByText('MH-00162')

    const call = listCalls()[0]!
    expect(call.url).toContain(`/hotels/${TEST_HOTEL.public_id}/bookings`)
    const params = new URL(call.url, 'http://localhost').searchParams
    expect(params.get('page')).toBe('1')
    expect(params.get('page_size')).toBe('20')
  })

  it('sends only the two parameters the endpoint accepts', async () => {
    mountList()
    await screen.findByText('MH-00162')

    // The backend ignores unrecognised query parameters silently -- `?status=confirmed`
    // returns everything with a 200 -- so an invented one would look like it worked. This is
    // the assertion that stops one being added.
    const params = new URL(listCalls()[0]!.url, 'http://localhost').searchParams
    expect([...params.keys()].sort()).toEqual(['page', 'page_size'])
  })

  it('carries the session token', async () => {
    mountList()
    await screen.findByText('MH-00162')

    expect(listCalls()[0]!.authorization).toMatch(/^Bearer /)
  })

  it('issues exactly one request per view, and none per row', async () => {
    fetchStub.on('GET', '/bookings?', {
      body: bookingsPage(
        Array.from({ length: 20 }, (_, i) =>
          makeBooking({ public_id: `id-${i}`, reference: `MH-${i}` }),
        ),
        20,
      ),
    })
    mountList()
    await screen.findByText('MH-0')

    // Twenty rows, one request. The N+1 this would be is "fetch the guest for each booking",
    // which is the obvious thing to reach for because the list carries only guest ids.
    expect(listCalls()).toHaveLength(1)
    expect(fetchStub.calls.filter((c) => c.url.includes('/guests/'))).toHaveLength(0)
  })

  it('does not duplicate the request', async () => {
    mountList()
    await screen.findByText('MH-00162')

    expect(new Set(listCalls().map((c) => c.url)).size).toBe(listCalls().length)
  })
})

/* --- rendering ---------------------------------------------------------------------------- */

describe('the bookings table', () => {
  beforeEach(() => {
    stubSession()
    fetchStub.on('GET', '/bookings?', { body: bookingsPage([makeBooking()], 1) })
  })

  it('is a semantic table with column headers', async () => {
    mountList()
    await screen.findByText('MH-00162')

    const table = screen.getByRole('table')
    for (const header of ['Reference', 'Occupant', 'Stay', 'Rooms', 'Status', 'Booked']) {
      expect(within(table).getByRole('columnheader', { name: header })).toBeInTheDocument()
    }
  })

  it('gives each row a row header rather than a plain cell', async () => {
    mountList()
    await screen.findByText('MH-00162')

    expect(screen.getByRole('rowheader')).toBeInTheDocument()
  })

  it('makes the reference a real link, not a clickable row', async () => {
    mountList()
    await screen.findByText('MH-00162')

    const link = screen.getByRole('link', { name: /Booking MH-00162/ })
    expect(link).toHaveAttribute('href', bookingPath(makeBooking().public_id))
    // A `<tr onClick>` would be invisible here: no role, no name, no keyboard reach.
    expect(screen.getAllByRole('link').length).toBeGreaterThan(0)
  })

  it('names the link by reference and stay, so forty rows are distinguishable', async () => {
    mountList()
    await screen.findByText('MH-00162')

    expect(
      screen.getByRole('link', { name: 'Booking MH-00162, 17 Sept – 18 Sept 2026' }),
    ).toBeInTheDocument()
  })

  it('renders the money exactly as the API sent it, with the currency code', async () => {
    mountList()
    await screen.findByText('MH-00162')

    expect(screen.getByText('EUR 120.00')).toBeInTheDocument()
  })

  it('labels the amount as CONTRACTED, never as what the stay is worth', async () => {
    mountList()
    await screen.findByText('MH-00162')

    // `total_amount` is the client's figure and is explicitly not the sum of the nightly
    // rates. A column headed "Total" would assert something only reconciliation can.
    expect(
      screen.getByRole('columnheader', { name: 'Contracted total' }),
    ).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Total' })).not.toBeInTheDocument()
  })

  it('reports nights from the allocation rather than recomputing them', async () => {
    mountList()
    await screen.findByText('MH-00162')

    // 17th to 18th is ONE night: the departure day is not a room night.
    expect(screen.getByText('1 night')).toBeInTheDocument()
  })

  it('shows the occupant column as a dash when the allocation names nobody', async () => {
    mountList()
    await screen.findByText('MH-00162')

    // `guest_name` is null on this fixture, as it is on most real bookings. The column must
    // not silently show the booker instead -- the list endpoint does not carry them.
    expect(screen.getByRole('columnheader', { name: 'Occupant' })).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })
})

describe('status presentation', () => {
  const statuses: readonly [BookingStatus, string][] = [
    ['pending', 'Pending'],
    ['confirmed', 'Confirmed'],
    ['checked_in', 'Checked in'],
    ['checked_out', 'Checked out'],
    ['cancelled', 'Cancelled'],
    ['no_show', 'No show'],
  ]

  it('renders every status the backend can send, as text', async () => {
    stubSession()
    fetchStub.on('GET', '/bookings?', {
      body: bookingsPage(
        statuses.map(([status], i) =>
          makeBooking({ public_id: `id-${i}`, reference: `MH-${i}`, status }),
        ),
        6,
      ),
    })
    mountList()
    await screen.findByText('MH-0')

    for (const [, label] of statuses) {
      // getAllBy: the badge's own text node and its enclosing cell both match, which is an
      // accident of the DOM rather than a duplicate label.
      expect(screen.getAllByText(new RegExp(`^${label}`)).length).toBeGreaterThan(0)
    }
  })

  it('conveys status without relying on colour', async () => {
    stubSession()
    fetchStub.on('GET', '/bookings?', {
      body: bookingsPage([makeBooking({ status: 'checked_in' })], 1),
    })
    mountList()
    await screen.findByText('MH-00162')

    // The word is present, and so is a longer description for a screen reader. Removing the
    // colour changes nothing about what can be read.
    expect(screen.getAllByText(/Checked in/).length).toBeGreaterThan(0)
    expect(screen.getByText(/the guest is in the property now/)).toBeInTheDocument()
  })
})

/* --- filtering ------------------------------------------------------------------------- */

describe('filtering', () => {
  beforeEach(() => {
    stubSession()
    fetchStub.on('GET', '/bookings?', {
      body: bookingsPage(
        [
          makeBooking({ public_id: 'a', reference: 'MH-00001', status: 'confirmed' }),
          makeBooking({ public_id: 'b', reference: 'MH-00002', status: 'cancelled' }),
          makeBooking({ public_id: 'c', reference: 'MH-00003', status: 'checked_in' }),
        ],
        3,
      ),
    })
  })

  it('narrows by status without issuing a request', async () => {
    const user = userEvent.setup()
    mountList()
    await screen.findByText('MH-00001')
    const before = listCalls().length

    await user.selectOptions(screen.getByLabelText('Status'), 'cancelled')

    expect(screen.getByText('MH-00002')).toBeInTheDocument()
    expect(screen.queryByText('MH-00001')).not.toBeInTheDocument()
    // No request, because there is no server-side filter to ask for.
    expect(listCalls()).toHaveLength(before)
  })

  it('narrows by free text', async () => {
    const user = userEvent.setup()
    mountList()
    await screen.findByText('MH-00001')

    await user.type(screen.getByLabelText('Search this page'), 'MH-00003')

    expect(screen.getByText('MH-00003')).toBeInTheDocument()
    expect(screen.queryByText('MH-00001')).not.toBeInTheDocument()
  })

  it('states that the filter reaches this page only', async () => {
    mountList()
    await screen.findByText('MH-00001')

    // The scope has to be visible, every time. An operator who searches a reference held on
    // page 3 and is told "no bookings" has been misled by the interface.
    expect(screen.getByText(/Filters apply to this page only/)).toBeInTheDocument()
    expect(screen.getByText(/no server-side search/)).toBeInTheDocument()
  })

  it('distinguishes "nothing matched" from "no bookings"', async () => {
    const user = userEvent.setup()
    mountList()
    await screen.findByText('MH-00001')

    await user.type(screen.getByLabelText('Search this page'), 'nothing-matches-this')

    expect(screen.getByText('No bookings on this page match')).toBeInTheDocument()
    expect(screen.queryByText('No bookings yet')).not.toBeInTheDocument()
    expect(screen.getByText(/Filters apply to the current page only/)).toBeInTheDocument()
  })

  it('offers a way back once a filter is active', async () => {
    const user = userEvent.setup()
    mountList()
    await screen.findByText('MH-00001')

    await user.selectOptions(screen.getByLabelText('Status'), 'cancelled')
    await user.click(screen.getByRole('button', { name: /Clear/ }))

    expect(screen.getByText('MH-00001')).toBeInTheDocument()
  })

  it('preserves the backend ordering rather than re-sorting', async () => {
    mountList()
    await screen.findByText('MH-00001')

    // Sorting is not something the API offers, and a client-side sort of one page would
    // claim an ordering the other pages do not share.
    const refs = screen.getAllByRole('rowheader').map((cell) => cell.textContent)
    expect(refs[0]).toContain('MH-00001')
    expect(refs[2]).toContain('MH-00003')
  })
})

/* --- pagination -------------------------------------------------------------------------- */

describe('pagination', () => {
  beforeEach(() => {
    stubSession()
    fetchStub.on('GET', '/bookings?', ({ url }) => {
      const params = new URL(url, 'http://localhost').searchParams
      const page = Number(params.get('page'))
      const size = Number(params.get('page_size'))
      return {
        body: bookingsPage(
          [makeBooking({ public_id: `p${page}`, reference: `MH-PAGE-${page}` })],
          166,
          page,
          size,
        ),
      }
    })
  })

  it('reports the backend’s own total and page count', async () => {
    mountList()
    await screen.findByText('MH-PAGE-1')

    expect(screen.getByText(/1–20 of 166/)).toBeInTheDocument()
    expect(screen.getByText('Page 1 of 9')).toBeInTheDocument()
  })

  it('requests the next page', async () => {
    const user = userEvent.setup()
    mountList()
    await screen.findByText('MH-PAGE-1')

    await user.click(screen.getByRole('button', { name: /Next/ }))

    await waitFor(() => {
      expect(screen.getByText('MH-PAGE-2')).toBeInTheDocument()
    })
    expect(new URL(listCalls().at(-1)!.url, 'http://localhost').searchParams.get('page')).toBe('2')
  })

  it('disables Previous on the first page rather than hiding it', async () => {
    mountList()
    await screen.findByText('MH-PAGE-1')

    // Hiding it would change the control's shape as you page and move the tab order.
    expect(screen.getByRole('button', { name: /Previous/ })).toBeDisabled()
  })

  it('returns to page 1 when the page size changes', async () => {
    const user = userEvent.setup()
    mountList()
    await screen.findByText('MH-PAGE-1')
    await user.click(screen.getByRole('button', { name: /Next/ }))
    await screen.findByText('MH-PAGE-2')

    await user.selectOptions(screen.getByLabelText('Per page'), '100')

    await waitFor(() => {
      const params = new URL(listCalls().at(-1)!.url, 'http://localhost').searchParams
      // Page 5 of 20-row pages is not page 5 of 100-row pages; keeping the offset would land
      // the operator somewhere they did not choose.
      expect(params.get('page')).toBe('1')
      expect(params.get('page_size')).toBe('100')
    })
  })

  it('never offers a page size the API would reject', async () => {
    mountList()
    await screen.findByText('MH-PAGE-1')

    const options = within(screen.getByLabelText('Per page')).getAllByRole('option')
    for (const option of options) {
      const size = Number(option.getAttribute('value'))
      expect(size).toBeGreaterThanOrEqual(1)
      expect(size).toBeLessThanOrEqual(100)
    }
  })

  it('aborts a superseded page request', async () => {
    const user = userEvent.setup()
    mountList()
    await screen.findByText('MH-PAGE-1')

    await user.click(screen.getByRole('button', { name: /Next/ }))
    await screen.findByText('MH-PAGE-2')

    // The stale response for page 1 must never overwrite page 2's. The hook aborts on
    // change; this asserts the outcome that matters.
    expect(screen.queryByText('MH-PAGE-1')).not.toBeInTheDocument()
  })
})

/* --- empty and failure states -------------------------------------------------------- */

describe('empty and failure', () => {
  it('says a hotel has no bookings, distinctly from a failure', async () => {
    stubSession()
    fetchStub.on('GET', '/bookings?', { body: bookingsPage([], 0) })
    mountList()

    expect(await screen.findByText('No bookings yet')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('shows no fabricated rows when the request fails', async () => {
    stubSession()
    fetchStub.on('GET', '/bookings?', {
      status: 500,
      body: envelope('INTERNAL_ERROR', 'An internal error occurred.'),
    })
    mountList()

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByText(/EUR/)).not.toBeInTheDocument()
    expect(screen.queryByText('No bookings yet')).not.toBeInTheDocument()
  })

  it('never lets a backend internal detail reach the DOM', async () => {
    stubSession()
    fetchStub.on('GET', '/bookings?', {
      status: 500,
      body: envelope(
        'INTERNAL_ERROR',
        'psycopg.errors.UndefinedTable: relation "booking_room_nights" does not exist',
      ),
    })
    mountList()
    await screen.findByRole('alert')

    for (const leak of [
      'psycopg',
      'UndefinedTable',
      'booking_room_nights',
      'relation',
      'SQLSTATE',
      'sqlalchemy',
      'Traceback',
    ]) {
      expect(document.body.innerHTML).not.toContain(leak)
    }
  })

  it('treats a 200 whose body is not a page as a failure, not as an empty list', async () => {
    stubSession()
    // A proxy or a moved endpoint answers 200 with something else entirely. Reaching for
    // `.items.map` on it would take the page down from a render.
    fetchStub.on('GET', '/bookings?', { body: { detail: 'not a page' } })
    mountList()

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByText('No bookings yet')).not.toBeInTheDocument()
  })

  it('reports a 404 for the hotel without claiming which reason', async () => {
    stubSession()
    fetchStub.on('GET', '/bookings?', { status: 404, body: envelope('NOT_FOUND', 'Hotel not found.') })
    mountList()

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/Hotel not available/)).toBeInTheDocument()
  })

  it('shows the server’s own wait on a 429', async () => {
    stubSession()
    fetchStub.on('GET', '/bookings?', {
      status: 429,
      body: envelope('RATE_LIMITED', 'Too many requests.'),
      headers: { 'Retry-After': '37' },
    })
    mountList()

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/try again in 37 seconds/i)).toBeInTheDocument()
  })

  it('distinguishes an unreachable server from a refusal', async () => {
    stubSession()
    fetchStub.on('GET', '/bookings?', { networkError: true })
    mountList()

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/Could not reach the server/i)).toBeInTheDocument()
  })

  it('ends the session on a 401 and leaves no booking data behind', async () => {
    stubSession()
    fetchStub.on('GET', '/bookings?', {
      status: 401,
      body: envelope('INVALID_TOKEN', 'Not authenticated.'),
    })
    mountList()

    await waitFor(() => {
      expect(window.sessionStorage.getItem('ahip.access_token')).toBeNull()
    })
    expect(screen.queryByText('MH-00162')).not.toBeInTheDocument()
  })
})

/* --- loading ------------------------------------------------------------------------- */

describe('loading', () => {
  it('shows placeholders, never plausible booking data', () => {
    stubSession()
    fetchStub.on('GET', '/bookings?', { body: bookingsPage([makeBooking()], 1) })
    const { container } = mountList()

    expect(screen.getByText('Loading bookings…')).toBeInTheDocument()
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
    // Nothing that could be mistaken for a real booking is on screen yet.
    expect(screen.queryByText('MH-00162')).not.toBeInTheDocument()
    expect(screen.queryByText(/EUR/)).not.toBeInTheDocument()
  })
})

/* --- the detail page ------------------------------------------------------------------- */

describe('the booking detail request', () => {
  beforeEach(() => {
    stubSession()
    stubDetail(makeBooking())
  })

  it('fetches the booking, its reconciliation and its guest -- three requests, no more', async () => {
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    // Four, and each is named: the booking, its reconciliation, its payment ledger
    // (Stage 5.7) and its guest. Fixed regardless of how many rooms, nights or postings the
    // booking has -- that is what makes it a detail fetch rather than an N+1.
    const detailReads = (fragment: string) =>
      fetchStub.calls.filter((c) => c.method === 'GET' && c.url.includes(fragment))
    expect(detailReads('reconciliation')).toHaveLength(1)
    expect(detailReads('/payments')).toHaveLength(1)
    expect(detailReads('/guests/')).toHaveLength(1)
    expect(
      fetchStub.calls.filter(
        (c) =>
          c.method === 'GET' &&
          c.url.includes(`/bookings/${makeBooking().public_id}`) &&
          !c.url.includes('reconciliation') &&
          !c.url.includes('/payments'),
      ),
    ).toHaveLength(1)
  })

  it('addresses the booking by hotel and public id', async () => {
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    const call = bookingCalls().find((c) => !c.url.includes('reconciliation'))!
    expect(call.url).toContain(
      `/hotels/${TEST_HOTEL.public_id}/bookings/${makeBooking().public_id}`,
    )
  })

  it('carries the token on every one of them', async () => {
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    for (const call of fetchStub.calls.filter((c) => c.url.includes('/api/v1/hotels/'))) {
      expect(call.authorization).toMatch(/^Bearer /)
    }
  })
})

describe('the booking detail page', () => {
  it('shows the stay, the rooms and the nightly rates the API returned', async () => {
    stubSession()
    stubDetail(makeBooking())
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    expect(screen.getAllByText('17 Sept 2026').length).toBeGreaterThan(0)
    expect(screen.getAllByText('18 Sept 2026').length).toBeGreaterThan(0)
    expect(screen.getByText(/Room 104/)).toBeInTheDocument()
    expect(screen.getByText('STD')).toBeInTheDocument()
  })

  it('presents accommodation_total as the authoritative figure', async () => {
    stubSession()
    stubDetail(makeBooking())
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    expect(screen.getByText('Accommodation total')).toBeInTheDocument()
    expect(screen.getByText(/Server-authoritative/)).toBeInTheDocument()
  })

  it('presents total_amount as contracted and NOT authoritative', async () => {
    stubSession()
    stubDetail(makeBooking())
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    expect(screen.getByText('Contracted total')).toBeInTheDocument()
    expect(screen.getByText(/Not authoritative/)).toBeInTheDocument()
  })

  it('reports a divergence between the two totals rather than smoothing it over', async () => {
    stubSession()
    stubDetail(makeBooking(), {
      ...RECONCILIATION,
      accommodation_total: '120.00',
      declared_total: '99.00',
      totals_agree: false,
    })
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    expect(screen.getByText(/disagree/)).toBeInTheDocument()
    // And says the server cannot explain it, which is the honest position.
    expect(screen.getByText(/cannot explain it/)).toBeInTheDocument()
  })

  it('never sums the nightly rates itself', async () => {
    stubSession()
    // Three nights at 120 is 360, but the server says the accommodation total is 300 -- a
    // discount the schema has nowhere to record. The page must show 300.
    stubDetail(
      makeBooking({
        rooms: [
          {
            room_number: '104',
            room_type_code: 'STD',
            adults: 2,
            children: 0,
            guest_name: null,
            nights: 3,
            nightly_rates: [
              { stay_date: '2026-09-17', rate: '120.00', rate_plan_code: null, is_complimentary: false },
              { stay_date: '2026-09-18', rate: '120.00', rate_plan_code: null, is_complimentary: false },
              { stay_date: '2026-09-19', rate: '120.00', rate_plan_code: null, is_complimentary: false },
            ],
          },
        ],
      }),
      { ...RECONCILIATION, accommodation_total: '300.00', declared_total: '300.00' },
    )
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    expect(screen.getAllByText('EUR 300.00').length).toBeGreaterThan(0)
    // 3 x 120.00 = 360.00. If the page ever adds the nightly rates up itself, this is the
    // assertion that catches it.
    expect(screen.queryByText('EUR 360.00')).not.toBeInTheDocument()
    expect(document.body.innerHTML).not.toContain('360.00')
  })

  it('flags a complimentary night rather than hiding a zero', async () => {
    stubSession()
    stubDetail(
      makeBooking({
        rooms: [
          {
            room_number: '104',
            room_type_code: 'STD',
            adults: 1,
            children: 0,
            guest_name: null,
            nights: 1,
            nightly_rates: [
              { stay_date: '2026-09-17', rate: '0.00', rate_plan_code: null, is_complimentary: true },
            ],
          },
        ],
      }),
    )
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    expect(screen.getByText('Complimentary')).toBeInTheDocument()
  })

  it('shows the cancellation panel only for a cancelled booking', async () => {
    stubSession()
    stubDetail(makeBooking())
    const { unmount } = mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')
    expect(screen.queryByText('Cancellation')).not.toBeInTheDocument()
    unmount()

    fetchStub.restore()
    fetchStub = installFetchStub()
    seedStoredToken()
    stubSession()
    stubDetail(
      makeBooking({
        status: 'cancelled',
        cancelled_at: '2026-09-11T10:15:00+03:00',
        cancellation_reason: 'Guest cancelled by telephone',
      }),
    )
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    expect(screen.getByText('Cancellation')).toBeInTheDocument()
    expect(screen.getByText('Guest cancelled by telephone')).toBeInTheDocument()
  })

  it('reports a missing booking without revealing whether it exists elsewhere', async () => {
    stubSession()
    stubDetail({ status: 404, body: envelope('NOT_FOUND', 'Booking not found for this hotel.') })
    mountRouted(bookingPath('00000000-0000-4000-8000-000000000000'))

    const alert = await screen.findByRole('alert')
    // The backend answers a non-member's booking and a nonexistent one identically. The copy
    // must cover both without claiming to know which.
    expect(within(alert).getByText('Booking not found')).toBeInTheDocument()
    expect(within(alert).getByText(/or your access to it has been removed/)).toBeInTheDocument()
  })

  it('treats a malformed 200 as a failure', async () => {
    stubSession()
    stubDetail({ status: 200, body: { public_id: 'x' } })
    mountRouted(bookingPath(makeBooking().public_id))

    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })

  it('keeps the page when only the financial summary fails', async () => {
    stubSession()
    stubDetail(makeBooking(), { status: 500, body: envelope('INTERNAL_ERROR', 'boom') })
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    // The booking still renders; the section says it is unavailable rather than showing a
    // zero, which would be indistinguishable from a stay worth nothing.
    expect(screen.getByText(/financial summary could not be loaded/i)).toBeInTheDocument()
    expect(screen.getByText('Financial summary')).toBeInTheDocument()
    expect(screen.queryByText('Accommodation total')).not.toBeInTheDocument()
  })

  it('keeps the page when only the guest fails', async () => {
    stubSession()
    stubDetail(makeBooking(), RECONCILIATION, { status: 404, body: envelope('NOT_FOUND', 'x') })
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    expect(screen.getByText(/guest record for this booking could not be loaded/i)).toBeInTheDocument()
    expect(screen.getByText('Accommodation total')).toBeInTheDocument()
  })
})

/* --- guest privacy ---------------------------------------------------------------------- */

describe('guest data', () => {
  beforeEach(() => {
    stubSession()
    stubDetail(makeBooking())
  })

  it('masks the email rather than printing it', async () => {
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    expect(document.body.innerHTML).not.toContain('elena.papadakis@example.test')
    expect(screen.getByText(/@example\.test/)).toBeInTheDocument()
  })

  it('masks the phone number', async () => {
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    expect(document.body.innerHTML).not.toContain('+30 210 555 0142')
    expect(screen.getByText(/0142/)).toBeInTheDocument()
  })

  it('does not render the date of birth or the free-text notes', async () => {
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    // Both are returned by the endpoint and neither has an operational use on this screen.
    expect(document.body.innerHTML).not.toContain('1988-04-17')
    expect(document.body.innerHTML).not.toContain('Prefers a quiet room')
  })

  it('never puts guest data in a URL', async () => {
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    for (const call of fetchStub.calls) {
      expect(call.url).not.toContain('elena')
      expect(call.url).not.toContain('@example.test')
    }
    expect(window.location.search).toBe('')
  })
})

/* --- security --------------------------------------------------------------------------- */

describe('security', () => {
  beforeEach(() => {
    stubSession()
    stubDetail(makeBooking())
  })

  it('puts no access token in the DOM or the URL', async () => {
    mountRouted(bookingPath(makeBooking().public_id))
    await screen.findByText('Booking MH-00162')

    const token = window.sessionStorage.getItem('ahip.access_token')!
    expect(document.documentElement.outerHTML).not.toContain(token)
    expect(window.location.href).not.toContain(token)
    expect(document.documentElement.outerHTML).not.toMatch(/Bearer\s/)
  })

  it('keys the booking route by public id, with no internal key anywhere', async () => {
    fetchStub.on('GET', '/bookings?', { body: bookingsPage([makeBooking()], 1) })
    mountList()
    await screen.findByText('MH-00162')

    // The backend never sends an internal BIGINT -- BookingResponse's own docstring says so --
    // so the assertion is that the generated URL is the UUID and that nothing numeric stands
    // in for it. `window.location` is not consulted: a memory router keeps its own.
    const href = screen.getByRole('link', { name: /Booking MH-00162/ }).getAttribute('href')!
    expect(href).toBe(`/bookings/${makeBooking().public_id}`)
    expect(href).not.toMatch(/\/bookings\/\d+$/)

    for (const call of fetchStub.calls) {
      expect(call.url).not.toMatch(/\/bookings\/\d+(\?|$|\/)/)
    }
  })
})

/* --- responsive ------------------------------------------------------------------------- */

describe('the compact layout', () => {
  it('renders a list of cards instead of a table at phone width', async () => {
    stubSession()
    fetchStub.on('GET', '/bookings?', { body: bookingsPage([makeBooking()], 1) })

    // jsdom does not evaluate media queries, so the breakpoint is stubbed. The point of the
    // assertion is the STRUCTURE: a seven-column table is not made readable at 375px by
    // narrowing it, so the page renders a different one.
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    )
    mountList()
    await screen.findByText('MH-00162')

    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.getByRole('list')).toBeInTheDocument()
    // Still one accessible link per booking, not a card-sized anchor.
    expect(screen.getByRole('link', { name: /Booking MH-00162/ })).toBeInTheDocument()
  })
})
