import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { BookingDetailPage } from '@/pages/BookingDetailPage'
import { bookingPath, BOOKING_DETAIL_PATTERN } from '@/router/routes'
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
import type { Booking } from '@/types/booking'

/**
 * The three booking mutations, as the UI performs them.
 *
 * Mocked at `fetch`, so the method, the URL and **the exact request body** are all
 * observable. The bodies are what most of this file is about: the whole risk of this stage
 * is a frontend that sends a price, a total, or a field the contract does not have.
 *
 * Fixtures are the shapes the live API returned during contract discovery -- decimals as
 * strings, `StayModificationResponse` as `{ booking, repricing }`, and the repricing case
 * where a stay got cheaper but nothing became refundable because nothing had been paid.
 */

/* --- fixtures --------------------------------------------------------------------------- */

const GUEST_ID = '98c235eb-5ae6-4c75-a743-836e1a2b38da'
const BOOKING_ID = 'e5b72bf8-11a1-456b-9755-5a5ac5219782'

function makeBooking(overrides: Partial<Booking> = {}): Booking {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    public_id: BOOKING_ID,
    guest_public_id: GUEST_ID,
    reference: 'MH-00162',
    check_in_date: '2026-09-17',
    check_out_date: '2026-09-19',
    status: 'confirmed',
    adults: 2,
    children: 0,
    source: 'phone',
    channel_reference: null,
    total_amount: '240.00',
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
        nights: 2,
        nightly_rates: [
          { stay_date: '2026-09-17', rate: '120.00', rate_plan_code: null, is_complimentary: false },
          { stay_date: '2026-09-18', rate: '120.00', rate_plan_code: null, is_complimentary: false },
        ],
      },
    ],
    ...overrides,
  }
}

const RECONCILIATION = {
  booking_public_id: BOOKING_ID,
  currency: 'EUR',
  accommodation_total: '240.00',
  declared_total: '240.00',
  totals_agree: true,
  charged_total: '0.00',
  refunded_total: '0.00',
  net_paid: '0.00',
  outstanding_amount: '240.00',
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

/** As the live API returns it: the booking as it now stands, and what the change cost. */
function stayResponse(booking: Booking, repricing: Record<string, unknown> = {}) {
  return {
    booking,
    repricing: {
      currency: 'EUR',
      previous_total: '240.00',
      new_total: '480.00',
      difference: '240.00',
      additional_amount_due: '240.00',
      refundable_amount: '0.00',
      outstanding_after: '480.00',
      adjustment: 'amount_due',
      ...repricing,
    },
  }
}


/** An empty payment ledger. The detail page reads one; a test that renders it must answer. */
const EMPTY_PAYMENTS = { items: [], total: 0, page: 1, page_size: 20, pages: 0 }

/* --- harness ---------------------------------------------------------------------------- */

let fetchStub: FetchStub

const envelope = (code: string, message: string) => ({ error: { code, message, details: [] } })

interface StubFailure {
  readonly status: number
  readonly body: unknown
}
const isFailure = (v: object): v is StubFailure =>
  'status' in v && typeof (v as { status: unknown }).status === 'number'

/**
 * Stub the reads a detail page performs.
 *
 * Fragment order matters: the stub matches the first handler whose fragment the URL
 * contains, so the two sub-resources must be registered before the bare `/bookings/`.
 */
function stubReads(booking: Booking = makeBooking()) {
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage([TEST_HOTEL], 1) })
  fetchStub.on('GET', '/payments', { body: EMPTY_PAYMENTS })
  fetchStub.on('GET', 'reconciliation', { body: RECONCILIATION })
  fetchStub.on('GET', '/guests/', { body: GUEST })
  fetchStub.on('GET', '/bookings/', { body: booking })
}

const mutationCalls = (method: string) =>
  fetchStub.calls.filter((c) => c.method === method && c.url.includes('/bookings/'))

const bodyOf = (call: { body: string | null }) => JSON.parse(call.body ?? '{}') as Record<string, unknown>

function mount(initial = bookingPath(BOOKING_ID)) {
  return renderWithAuth([{ path: BOOKING_DETAIL_PATTERN, element: <BookingDetailPage /> }], {
    initialEntries: [initial],
  })
}

async function openBooking(booking: Booking = makeBooking()) {
  stubReads(booking)
  const rendered = mount()
  await screen.findByText('Booking MH-00162')
  return rendered
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

/* --- which actions are offered ---------------------------------------------------------- */

describe('the actions offered', () => {
  it('offers exactly the lifecycle moves a confirmed booking has', async () => {
    await openBooking(makeBooking({ status: 'confirmed' }))

    expect(screen.getByRole('button', { name: /Mark checked in/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Mark cancelled/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Mark no show/i })).toBeInTheDocument()
    // Not reachable from `confirmed`, so not offered.
    expect(screen.queryByRole('button', { name: /Mark checked out/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Mark pending/i })).not.toBeInTheDocument()
  })

  it('offers nothing to a terminal booking, and says why', async () => {
    await openBooking(makeBooking({ status: 'checked_out' }))

    expect(screen.queryByRole('button', { name: /^Mark /i })).not.toBeInTheDocument()
    expect(screen.getByText(/lifecycle is closed/i)).toBeInTheDocument()
  })

  it('offers the stay editor to a confirmed booking and no extension', async () => {
    await openBooking(makeBooking({ status: 'confirmed' }))

    expect(screen.getByRole('button', { name: /Change stay dates/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Extend stay/i })).not.toBeInTheDocument()
  })

  it('offers the extension to a checked-in booking and no stay editor', async () => {
    await openBooking(makeBooking({ status: 'checked_in' }))

    expect(screen.getByRole('button', { name: /Extend stay/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Change stay dates/i })).not.toBeInTheDocument()
  })

  it('offers neither stay operation once the booking is terminal', async () => {
    await openBooking(makeBooking({ status: 'cancelled' }))

    expect(screen.queryByRole('button', { name: /Change stay dates/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Extend stay/i })).not.toBeInTheDocument()
  })
})

/* --- status transitions ------------------------------------------------------------------ */

describe('changing status', () => {
  it('sends only the status, to the right URL, with the right method', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    fetchStub.on('PATCH', '/bookings/', { body: makeBooking({ status: 'checked_in' }) })

    await user.click(screen.getByRole('button', { name: /Mark checked in/i }))

    await waitFor(() => {
      expect(mutationCalls('PATCH')).toHaveLength(1)
    })
    const call = mutationCalls('PATCH')[0]!
    expect(call.url).toContain(`/hotels/${TEST_HOTEL.public_id}/bookings/${BOOKING_ID}`)
    expect(call.url).not.toContain('/stay')
    // The one field this application is allowed to send for a status change.
    expect(bodyOf(call)).toEqual({ status: 'checked_in' })
  })

  it('never sends total_amount, even though the schema accepts it', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    fetchStub.on('PATCH', '/bookings/', { body: makeBooking({ status: 'checked_in' }) })

    await user.click(screen.getByRole('button', { name: /Mark checked in/i }))
    await waitFor(() => {
      expect(mutationCalls('PATCH')).toHaveLength(1)
    })

    // `BookingUpdate` accepts `total_amount` -- the CONTRACTED total. An operations screen
    // rewriting it would silently change what the booking claims to be worth.
    expect(bodyOf(mutationCalls('PATCH')[0]!)).not.toHaveProperty('total_amount')
  })

  it('adopts the booking the SERVER returned, not the one that was asked for', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    // The server answers with `checked_in` AND a changed reference: if the page were
    // patching its local copy with the request, the reference would not move.
    fetchStub.on('PATCH', '/bookings/', {
      body: makeBooking({ status: 'checked_in', reference: 'MH-SERVER-SAYS' }),
    })

    await user.click(screen.getByRole('button', { name: /Mark checked in/i }))

    expect(await screen.findByText('Booking status updated.')).toBeInTheDocument()
    expect(screen.getByText('MH-SERVER-SAYS')).toBeInTheDocument()
    expect(screen.getAllByText(/Checked in/).length).toBeGreaterThan(0)
  })

  it('shows nothing changed until the server answers', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    fetchStub.on('PATCH', '/bookings/', {
      status: 409,
      body: envelope('CONFLICT', 'A booking cannot move from confirmed to pending.'),
    })

    await user.click(screen.getByRole('button', { name: /Mark checked in/i }))
    await screen.findByRole('alert')

    // The refusal must leave the booking exactly as it was. An optimistic update would have
    // shown "Checked in" for the duration of the round trip and then taken it back.
    expect(screen.getAllByText(/Confirmed/).length).toBeGreaterThan(0)
    expect(screen.queryByText('Booking status updated.')).not.toBeInTheDocument()
  })

  it('does not fire twice when the control is pressed twice in one tick', async () => {
    await openBooking(makeBooking({ status: 'confirmed' }))
    fetchStub.on('PATCH', '/bookings/', { body: makeBooking({ status: 'checked_in' }) })

    const button = screen.getByRole('button', { name: /Mark checked in/i })
    // fireEvent, not userEvent: two clicks in the SAME tick is the case a state-based guard
    // misses, because both reads see the same stale `false`.
    fireEvent.click(button)
    fireEvent.click(button)

    await waitFor(() => {
      expect(screen.queryByText('Booking status updated.')).toBeInTheDocument()
    })
    expect(mutationCalls('PATCH')).toHaveLength(1)
  })
})

/* --- destructive confirmation ------------------------------------------------------------ */

describe('destructive transitions', () => {
  it('asks before cancelling, and sends nothing until confirmed', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))

    await user.click(screen.getByRole('button', { name: /Mark cancelled/i }))

    expect(screen.getByRole('group', { name: /Confirm Cancelled/i })).toBeInTheDocument()
    expect(screen.getByText(/terminal state/i)).toBeInTheDocument()
    expect(mutationCalls('PATCH')).toHaveLength(0)
  })

  it('moves focus to the confirming action', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))

    await user.click(screen.getByRole('button', { name: /Mark cancelled/i }))

    expect(screen.getByRole('button', { name: /Yes, mark cancelled/i })).toHaveFocus()
  })

  it('lets the operator back out without sending anything', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))

    await user.click(screen.getByRole('button', { name: /Mark cancelled/i }))
    await user.click(screen.getByRole('button', { name: 'Keep booking' }))

    expect(screen.getByRole('button', { name: /Mark cancelled/i })).toBeInTheDocument()
    expect(mutationCalls('PATCH')).toHaveLength(0)
  })

  it('sends the optional reason when one is typed', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    fetchStub.on('PATCH', '/bookings/', { body: makeBooking({ status: 'cancelled' }) })

    await user.click(screen.getByRole('button', { name: /Mark cancelled/i }))
    await user.type(screen.getByLabelText(/Reason/i), 'Guest cancelled by telephone')
    await user.click(screen.getByRole('button', { name: /Yes, mark cancelled/i }))

    await waitFor(() => {
      expect(mutationCalls('PATCH')).toHaveLength(1)
    })
    expect(bodyOf(mutationCalls('PATCH')[0]!)).toEqual({
      status: 'cancelled',
      cancellation_reason: 'Guest cancelled by telephone',
    })
  })

  it('omits the reason entirely when none is given', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    fetchStub.on('PATCH', '/bookings/', { body: makeBooking({ status: 'no_show' }) })

    await user.click(screen.getByRole('button', { name: /Mark no show/i }))
    await user.click(screen.getByRole('button', { name: /Yes, mark no show/i }))

    await waitFor(() => {
      expect(mutationCalls('PATCH')).toHaveLength(1)
    })
    // An empty string is not a reason, and the field means nothing outside a cancellation.
    expect(bodyOf(mutationCalls('PATCH')[0]!)).toEqual({ status: 'no_show' })
  })

  it('does not offer a reason field for a no-show', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))

    await user.click(screen.getByRole('button', { name: /Mark no show/i }))

    expect(screen.queryByLabelText(/Reason/i)).not.toBeInTheDocument()
  })
})

/* --- modify stay -------------------------------------------------------------------------- */

describe('changing the stay', () => {
  async function openEditor() {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    await user.click(screen.getByRole('button', { name: /Change stay dates/i }))
    return user
  }

  it('sends the whole stay, with one night per date of the new span', async () => {
    const user = await openEditor()
    fetchStub.on('PATCH', 'stay', {
      body: stayResponse(makeBooking({ check_out_date: '2026-09-21' })),
    })

    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2026-09-21')
    await user.click(screen.getByRole('button', { name: 'Save stay' }))

    await waitFor(() => {
      expect(mutationCalls('PATCH')).toHaveLength(1)
    })
    const body = bodyOf(mutationCalls('PATCH')[0]!)
    expect(body['check_in_date']).toBe('2026-09-17')
    expect(body['check_out_date']).toBe('2026-09-21')
    // 17th to 21st is FOUR nights: the departure day is not a room night. The server rejects
    // any other set with a 422 naming the missing and unexpected dates.
    const rooms = body['rooms'] as { room_number: string; nights: { stay_date: string }[] }[]
    expect(rooms).toHaveLength(1)
    expect(rooms[0]!.room_number).toBe('104')
    expect(rooms[0]!.nights.map((n) => n.stay_date)).toEqual([
      '2026-09-17',
      '2026-09-18',
      '2026-09-19',
      '2026-09-20',
    ])
  })

  it('sends no rate, no total and no amount of any kind', async () => {
    const user = await openEditor()
    fetchStub.on('PATCH', 'stay', {
      body: stayResponse(makeBooking({ check_out_date: '2026-09-21' })),
    })

    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2026-09-21')
    await user.click(screen.getByRole('button', { name: 'Save stay' }))
    await waitFor(() => {
      expect(mutationCalls('PATCH')).toHaveLength(1)
    })

    // The single most important assertion in this file. Stage 4.5.23 moved pricing to the
    // server; a rate reaching the wire from here would be the frontend proposing a price.
    const raw = mutationCalls('PATCH')[0]!.body ?? ''
    for (const forbidden of ['rate', 'total_amount', 'amount', 'price', 'currency']) {
      expect(raw).not.toContain(`"${forbidden}"`)
    }
  })

  it('sends the URL and method the stay endpoint defines', async () => {
    const user = await openEditor()
    fetchStub.on('PATCH', 'stay', { body: stayResponse(makeBooking()) })

    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2026-09-21')
    await user.click(screen.getByRole('button', { name: 'Save stay' }))

    await waitFor(() => {
      expect(mutationCalls('PATCH')[0]!.url).toContain(
        `/hotels/${TEST_HOTEL.public_id}/bookings/${BOOKING_ID}/stay`,
      )
    })
  })

  it('refuses an impossible span before sending it', async () => {
    const user = await openEditor()

    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2026-09-17')
    await user.click(screen.getByRole('button', { name: 'Save stay' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/at least one night/i)
    expect(mutationCalls('PATCH')).toHaveLength(0)
  })

  it('shows the repricing the server reported, and computes none of it', async () => {
    const user = await openEditor()
    fetchStub.on('PATCH', 'stay', {
      body: stayResponse(makeBooking({ check_out_date: '2026-09-21' })),
    })

    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2026-09-21')
    await user.click(screen.getByRole('button', { name: 'Save stay' }))

    expect(await screen.findByText('Stay updated.')).toBeInTheDocument()
    expect(screen.getByText('This change increased what is owed')).toBeInTheDocument()
    expect(screen.getAllByText('EUR 480.00').length).toBeGreaterThan(0)
    expect(screen.getByText(/Nothing has been charged and nothing has been refunded/)).toBeInTheDocument()
  })

  it('does not turn a cheaper stay into a refund', async () => {
    const user = await openEditor()
    // The live API's own answer for a reduction against a booking that had paid nothing.
    fetchStub.on('PATCH', 'stay', {
      body: stayResponse(makeBooking({ check_out_date: '2026-09-18' }), {
        previous_total: '480.00',
        new_total: '120.00',
        difference: '-360.00',
        additional_amount_due: '0.00',
        refundable_amount: '0.00',
        outstanding_after: '120.00',
        adjustment: 'none',
      }),
    })

    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2026-09-18')
    await user.click(screen.getByRole('button', { name: 'Save stay' }))

    await screen.findByText('Stay updated.')
    // The heading follows the server's `adjustment`, not the sign of the difference.
    expect(screen.getByText('This change altered no balance')).toBeInTheDocument()
    expect(screen.queryByText(/refundable/i)).toBeInTheDocument() // the label, showing 0.00
    expect(screen.queryByRole('button', { name: /refund/i })).not.toBeInTheDocument()
  })

  it('re-reads the financial summary, because the nightly rates were rewritten', async () => {
    const user = await openEditor()
    fetchStub.on('PATCH', 'stay', {
      body: stayResponse(makeBooking({ check_out_date: '2026-09-21' })),
    })
    const before = fetchStub.calls.filter((c) => c.url.includes('reconciliation')).length

    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2026-09-21')
    await user.click(screen.getByRole('button', { name: 'Save stay' }))
    await screen.findByText('Stay updated.')

    await waitFor(() => {
      expect(
        fetchStub.calls.filter((c) => c.url.includes('reconciliation')).length,
      ).toBeGreaterThan(before)
    })
  })

  it('renders an inventory conflict safely', async () => {
    const user = await openEditor()
    fetchStub.on('PATCH', 'stay', {
      status: 409,
      body: envelope(
        'CONFLICT',
        'One of the requested rooms is already booked for overlapping dates.',
      ),
    })

    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2026-09-21')
    await user.click(screen.getByRole('button', { name: 'Save stay' }))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/changed before the action completed/i)).toBeInTheDocument()
    expect(within(alert).getByText(/a room may have been taken/i)).toBeInTheDocument()
    expect(screen.queryByText('Stay updated.')).not.toBeInTheDocument()
  })
})

/* --- extension ---------------------------------------------------------------------------- */

describe('extending an in-house stay', () => {
  async function openExtension() {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'checked_in' }))
    await user.click(screen.getByRole('button', { name: /Extend stay/i }))
    return user
  }

  it('sends one field, by POST, to the extension route', async () => {
    const user = await openExtension()
    fetchStub.on('POST', 'stay/extension', {
      body: stayResponse(makeBooking({ status: 'checked_in', check_out_date: '2026-09-22' })),
    })

    await user.clear(screen.getByLabelText('New check-out'))
    await user.type(screen.getByLabelText('New check-out'), '2026-09-22')
    await user.click(screen.getByRole('button', { name: 'Extend stay' }))

    await waitFor(() => {
      expect(mutationCalls('POST')).toHaveLength(1)
    })
    const call = mutationCalls('POST')[0]!
    expect(call.url).toContain(`/bookings/${BOOKING_ID}/stay/extension`)
    // The fields it lacks are the contract: no check_in_date, no rooms, no rate, no total.
    expect(bodyOf(call)).toEqual({ check_out_date: '2026-09-22' })
  })

  it('defaults to the earliest date the server would accept', async () => {
    await openExtension()

    // Current check-out is the 19th; an equal date is a 409, so the earliest offer is the 20th.
    const input = screen.getByLabelText('New check-out')
    expect(input).toHaveValue('2026-09-20')
    expect(input).toHaveAttribute('min', '2026-09-20')
  })

  it('refuses a date that would not move the departure later', async () => {
    const user = await openExtension()

    await user.clear(screen.getByLabelText('New check-out'))
    await user.type(screen.getByLabelText('New check-out'), '2026-09-19')
    await user.click(screen.getByRole('button', { name: 'Extend stay' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/cannot shorten a stay in progress/i)
    expect(mutationCalls('POST')).toHaveLength(0)
  })

  it('refuses more nights than the backend permits', async () => {
    const user = await openExtension()

    await user.clear(screen.getByLabelText('New check-out'))
    await user.type(screen.getByLabelText('New check-out'), '2028-09-19')
    await user.click(screen.getByRole('button', { name: 'Extend stay' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/at most 366 nights/i)
    expect(mutationCalls('POST')).toHaveLength(0)
  })

  it('reports a repeated extension as the conflict it is', async () => {
    const user = await openExtension()
    fetchStub.on('POST', 'stay/extension', {
      status: 409,
      body: envelope('CONFLICT', 'An extension must move check-out later than 2026-09-22.'),
    })

    await user.clear(screen.getByLabelText('New check-out'))
    await user.type(screen.getByLabelText('New check-out'), '2026-09-22')
    await user.click(screen.getByRole('button', { name: 'Extend stay' }))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/changed before the action completed/i)).toBeInTheDocument()
    // Never retried: the endpoint is deliberately not idempotent.
    expect(mutationCalls('POST')).toHaveLength(1)
    expect(screen.queryByText('Stay extended.')).not.toBeInTheDocument()
  })

  it('creates no payment or refund control on success', async () => {
    const user = await openExtension()
    fetchStub.on('POST', 'stay/extension', {
      body: stayResponse(makeBooking({ status: 'checked_in', check_out_date: '2026-09-22' })),
    })

    await user.clear(screen.getByLabelText('New check-out'))
    await user.type(screen.getByLabelText('New check-out'), '2026-09-22')
    await user.click(screen.getByRole('button', { name: 'Extend stay' }))

    const success = await screen.findByText('Stay extended.')
    /*
     * The repricing panel reports what the change did to the money and must offer no way to
     * settle it: `additional_amount_due` has not been charged and `refundable_amount` has not
     * been refunded.
     *
     * Scoped to the Operations card. Stage 5.7 added a payment ledger to this page with its
     * own "Record a charge" control, which is a separate, deliberate feature -- the rule here
     * was always that an EXTENSION must not produce a settle action, not that the page may
     * never contain one.
     */
    const operations = success.closest('[class*="_card_"]') as HTMLElement
    for (const label of [/take payment/i, /refund/i, /charge/i, /settle/i]) {
      expect(within(operations).queryByRole('button', { name: label })).not.toBeInTheDocument()
    }
  })
})

/* --- failures across every mutation ------------------------------------------------------- */

describe('refused mutations', () => {
  async function attemptStatus(failure: StubFailure | { networkError: true }) {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    fetchStub.on('PATCH', '/bookings/', isFailure(failure) ? failure : { networkError: true })
    await user.click(screen.getByRole('button', { name: /Mark checked in/i }))
    return screen.findByRole('alert')
  }

  it('renders a 403 without describing the permission model', async () => {
    const alert = await attemptStatus({
      status: 403,
      body: envelope('FORBIDDEN', 'This operation requires the staff role at this hotel.'),
    })

    expect(within(alert).getByText(/do not have access/i)).toBeInTheDocument()
    // The backend's message names the required role. That is a fact about the permission
    // model and is not repeated to somebody who has just been refused.
    expect(document.body.innerHTML).not.toContain('staff role')
  })

  it('renders a 404 with the established safe wording', async () => {
    const alert = await attemptStatus({
      status: 404,
      body: envelope('NOT_FOUND', 'Booking not found for this hotel.'),
    })

    expect(within(alert).getByText('Booking not found')).toBeInTheDocument()
  })

  it('renders a 422 without field noise from the server', async () => {
    const alert = await attemptStatus({
      status: 422,
      body: {
        error: {
          code: 'VALIDATION_ERROR',
          message: 'The request payload is invalid.',
          details: [{ location: ['body', 'status'], message: 'Field required', type: 'missing' }],
        },
      },
    })

    expect(within(alert).getByText(/could not be processed/i)).toBeInTheDocument()
  })

  it('renders a 429 with the server’s own wait', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    fetchStub.on('PATCH', '/bookings/', {
      status: 429,
      body: envelope('RATE_LIMITED', 'Too many requests.'),
      headers: { 'Retry-After': '37' },
    })
    await user.click(screen.getByRole('button', { name: /Mark checked in/i }))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/try again in 37 seconds/i)).toBeInTheDocument()
  })

  it('renders a network failure as retryable copy', async () => {
    const alert = await attemptStatus({ networkError: true })

    expect(within(alert).getByText(/Could not reach the server/i)).toBeInTheDocument()
  })

  it('treats a malformed 200 as a failure rather than a success', async () => {
    const alert = await attemptStatus({ status: 200, body: { ok: true } })

    expect(within(alert).getByText(/Unexpected response/i)).toBeInTheDocument()
    expect(screen.queryByText('Booking status updated.')).not.toBeInTheDocument()
    // And the booking on screen is untouched.
    expect(screen.getAllByText(/Confirmed/).length).toBeGreaterThan(0)
  })

  it('never lets a backend internal reach the DOM', async () => {
    await attemptStatus({
      status: 500,
      body: envelope(
        'INTERNAL_ERROR',
        'psycopg.errors.ExclusionViolation: conflicting key value violates exclusion constraint "ex_booking_rooms_no_overlap"',
      ),
    })

    for (const leak of [
      'psycopg',
      'ExclusionViolation',
      'ex_booking_rooms_no_overlap',
      'booking_rooms',
      'SQLSTATE',
      'sqlalchemy',
      'Traceback',
    ]) {
      expect(document.body.innerHTML).not.toContain(leak)
    }
  })
})

/* --- security ----------------------------------------------------------------------------- */

describe('security', () => {
  it('addresses every mutation by public id, with no internal key anywhere', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    fetchStub.on('PATCH', '/bookings/', { body: makeBooking({ status: 'checked_in' }) })

    await user.click(screen.getByRole('button', { name: /Mark checked in/i }))
    await waitFor(() => {
      expect(mutationCalls('PATCH')).toHaveLength(1)
    })

    const hotelScoped = fetchStub.calls.filter((c) => c.url.includes('/hotels/'))
    expect(hotelScoped.length).toBeGreaterThan(0)
    for (const call of hotelScoped) {
      // Every hotel-scoped URL is keyed by UUIDs. The backend never sends an internal
      // BIGINT, so there is none available to leak even by accident.
      expect(call.url).toMatch(/\/hotels\/[0-9a-f-]{36}\//)
      expect(call.url).not.toMatch(/\/bookings\/\d+(\?|\/|$)/)
    }
  })

  it('puts no token in the DOM or the URL while mutating', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    fetchStub.on('PATCH', '/bookings/', { body: makeBooking({ status: 'checked_in' }) })

    await user.click(screen.getByRole('button', { name: /Mark checked in/i }))
    await screen.findByText('Booking status updated.')

    const token = window.sessionStorage.getItem('ahip.access_token')!
    expect(document.documentElement.outerHTML).not.toContain(token)
    expect(document.documentElement.outerHTML).not.toMatch(/Bearer\s/)
    expect(window.location.href).not.toContain(token)
  })

  it('carries the token on the mutation itself', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    fetchStub.on('PATCH', '/bookings/', { body: makeBooking({ status: 'checked_in' }) })

    await user.click(screen.getByRole('button', { name: /Mark checked in/i }))
    await waitFor(() => {
      expect(mutationCalls('PATCH')).toHaveLength(1)
    })

    expect(mutationCalls('PATCH')[0]!.authorization).toMatch(/^Bearer /)
  })

  it('leaks no guest contact detail through a mutation body', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    fetchStub.on('PATCH', '/bookings/', { body: makeBooking({ status: 'cancelled' }) })

    await user.click(screen.getByRole('button', { name: /Mark cancelled/i }))
    await user.click(screen.getByRole('button', { name: /Yes, mark cancelled/i }))
    await waitFor(() => {
      expect(mutationCalls('PATCH')).toHaveLength(1)
    })

    for (const call of fetchStub.calls) {
      expect(call.body ?? '').not.toContain('example.test')
      expect(call.url).not.toContain('elena')
    }
  })
})

/* --- accessibility -------------------------------------------------------------------------- */

describe('accessibility of the operations panel', () => {
  it('labels both date fields', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    await user.click(screen.getByRole('button', { name: /Change stay dates/i }))

    expect(screen.getByLabelText('Check-in')).toHaveAttribute('type', 'date')
    expect(screen.getByLabelText('Check-out')).toHaveAttribute('type', 'date')
  })

  it('associates a validation message with the field it is about', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    await user.click(screen.getByRole('button', { name: /Change stay dates/i }))

    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2026-09-17')
    await user.click(screen.getByRole('button', { name: 'Save stay' }))

    const field = screen.getByLabelText('Check-out')
    expect(field).toHaveAttribute('aria-invalid', 'true')
    expect(field).toHaveAccessibleDescription(/at least one night/i)
  })

  it('announces a completed action politely and a refusal assertively', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    fetchStub.on('PATCH', '/bookings/', { body: makeBooking({ status: 'checked_in' }) })

    await user.click(screen.getByRole('button', { name: /Mark checked in/i }))
    await screen.findByText('Booking status updated.')

    // A success is `status`; the failure cases above all assert `alert`. `getAllBy` because
    // the payments section (Stage 5.7) announces its own empty ledger politely too -- which
    // is correct, and is why this asserts the operations message is among them rather than
    // that it is the only one.
    const announcements = screen.getAllByRole('status').map((el) => el.textContent)
    expect(announcements.some((text) => text?.includes('Booking status updated.'))).toBe(true)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('says a destructive action is terminal in words, not by colour', async () => {
    await openBooking(makeBooking({ status: 'confirmed' }))

    expect(
      screen.getByRole('button', { name: /Mark cancelled \(terminal — asks to confirm\)/i }),
    ).toBeInTheDocument()
  })

  it('disables every control while a mutation is in flight', async () => {
    await openBooking(makeBooking({ status: 'confirmed' }))
    let release: (() => void) | undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    // A handler that blocks, so the in-flight window is observable at all.
    const original = globalThis.fetch
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET') === 'PATCH') {
        await gate
        return new Response(JSON.stringify(makeBooking({ status: 'checked_in' })), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return original(input, init)
    }) as unknown as typeof fetch

    fireEvent.click(screen.getByRole('button', { name: /Mark checked in/i }))

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Working/i })).toBeDisabled()
    })
    expect(screen.getByRole('button', { name: /Mark cancelled/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /Change stay dates/i })).toBeDisabled()

    release?.()
    await screen.findByText('Booking status updated.')
    globalThis.fetch = original
  })
})

/* --- the stay-modification response cannot be adopted ------------------------------------- */

describe('the incomplete stay-modification response', () => {
  /**
   * A backend defect, characterised and worked around rather than assumed away.
   *
   * `PATCH .../stay` answers with `booking.rooms: []` even when the modification succeeded
   * and the booking still has its room. Measured against the live API on 2026-09-10: the
   * response carried an empty allocation list while a GET of the same booking a moment later
   * returned one room with three priced nights. `POST .../stay/extension` does not have the
   * problem. Reported, not patched -- the backend is out of scope for this stage.
   *
   * These tests pin the frontend's response to it. If the backend is fixed, the re-read
   * becomes redundant rather than wrong, and the first test here still passes.
   */
  it('re-reads the booking instead of adopting a response with no rooms', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    await user.click(screen.getByRole('button', { name: /Change stay dates/i }))

    // Exactly what the live API returns: the modification worked, the rooms are missing.
    fetchStub.on('PATCH', 'stay', {
      body: stayResponse({ ...makeBooking({ check_out_date: '2026-09-21' }), rooms: [] }),
    })
    const readsBefore = fetchStub.calls.filter(
      (c) => c.method === 'GET' && c.url.includes(`/bookings/${BOOKING_ID}`) && !c.url.includes('reconciliation'),
    ).length

    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2026-09-21')
    await user.click(screen.getByRole('button', { name: 'Save stay' }))
    await screen.findByText('Stay updated.')

    await waitFor(() => {
      expect(
        fetchStub.calls.filter(
          (c) => c.method === 'GET' && c.url.includes(`/bookings/${BOOKING_ID}`) && !c.url.includes('reconciliation'),
        ).length,
      ).toBeGreaterThan(readsBefore)
    })
  })

  it('never renders "Rooms (0)" for a booking that has one', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    await user.click(screen.getByRole('button', { name: /Change stay dates/i }))

    fetchStub.on('PATCH', 'stay', {
      body: stayResponse({ ...makeBooking({ check_out_date: '2026-09-21' }), rooms: [] }),
    })

    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2026-09-21')
    await user.click(screen.getByRole('button', { name: 'Save stay' }))
    await screen.findByText('Stay updated.')

    // The GET stub still returns a booking with its room, so the re-read restores the truth.
    await waitFor(() => {
      expect(screen.getByText(/Rooms \(1\)/)).toBeInTheDocument()
    })
    expect(screen.queryByText(/Rooms \(0\)/)).not.toBeInTheDocument()
    expect(screen.queryByText(/no room allocations/i)).not.toBeInTheDocument()
  })

  it('still shows the repricing from that same response', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'confirmed' }))
    await user.click(screen.getByRole('button', { name: /Change stay dates/i }))

    fetchStub.on('PATCH', 'stay', {
      body: stayResponse({ ...makeBooking({ check_out_date: '2026-09-21' }), rooms: [] }),
    })

    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2026-09-21')
    await user.click(screen.getByRole('button', { name: 'Save stay' }))

    // `repricing` is complete even though `booking` is not, so it is used rather than
    // discarded along with it.
    expect(await screen.findByText('This change increased what is owed')).toBeInTheDocument()
  })

  it('adopts the extension response directly, because that one is complete', async () => {
    const user = userEvent.setup()
    await openBooking(makeBooking({ status: 'checked_in' }))
    await user.click(screen.getByRole('button', { name: /Extend stay/i }))
    fetchStub.on('POST', 'stay/extension', {
      body: stayResponse(makeBooking({ status: 'checked_in', check_out_date: '2026-09-22' })),
    })
    const readsBefore = fetchStub.calls.filter(
      (c) => c.method === 'GET' && c.url.includes(`/bookings/${BOOKING_ID}`) && !c.url.includes('reconciliation'),
    ).length

    await user.clear(screen.getByLabelText('New check-out'))
    await user.type(screen.getByLabelText('New check-out'), '2026-09-22')
    await user.click(screen.getByRole('button', { name: 'Extend stay' }))
    await screen.findByText('Stay extended.')

    // No second GET of the booking: the response was usable, so re-reading would be a
    // request that cannot return anything the page does not already have.
    expect(
      fetchStub.calls.filter(
        (c) => c.method === 'GET' && c.url.includes(`/bookings/${BOOKING_ID}`) && !c.url.includes('reconciliation'),
      ).length,
    ).toBe(readsBefore)
  })
})
