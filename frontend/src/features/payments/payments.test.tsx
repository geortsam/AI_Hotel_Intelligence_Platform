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
import type { Payment } from '@/types/payment'

/**
 * The payment ledger and the two postings, as the UI performs them.
 *
 * Mocked at `fetch`, so the method, the URL and **the exact request body** are observable.
 * The bodies are most of what this file is about: on a financial screen the risk is not a
 * misdrawn table, it is a figure that reaches the database having been through a float, or a
 * total this application invented.
 *
 * Fixtures are the shapes the live API returned during contract discovery: `amount` a string
 * and positive on both kinds, `refunds_public_id` null on a charge, decimals as strings.
 */

/* --- fixtures --------------------------------------------------------------------------- */

const BOOKING_ID = 'e5b72bf8-11a1-456b-9755-5a5ac5219782'
const GUEST_ID = '98c235eb-5ae6-4c75-a743-836e1a2b38da'
const CHARGE_ID = 'f02536fc-b6bd-446b-afd2-6cfe37a77dad'

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

function makePayment(overrides: Partial<Payment> = {}): Payment {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    booking_public_id: BOOKING_ID,
    public_id: CHARGE_ID,
    kind: 'charge',
    amount: '200.00',
    currency: 'EUR',
    method: 'card',
    status: 'captured',
    paid_at: '2027-06-01T10:00:00+03:00',
    provider: 'stripe-like',
    transaction_reference: 'TXN-0001',
    refunds_public_id: null,
    failure_reason: null,
    card_last_four: '4242',
    created_at: '2027-06-01T10:00:00+03:00',
    updated_at: '2027-06-01T10:00:00+03:00',
    ...overrides,
  }
}

const RECONCILIATION = {
  booking_public_id: BOOKING_ID,
  currency: 'EUR',
  accommodation_total: '240.00',
  declared_total: '240.00',
  totals_agree: true,
  charged_total: '200.00',
  refunded_total: '0.00',
  net_paid: '200.00',
  outstanding_amount: '40.00',
  payment_state: 'partially_paid',
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

function ledger(items: readonly Payment[]) {
  return { items, total: items.length, page: 1, page_size: 20, pages: items.length === 0 ? 0 : 1 }
}

/* --- harness ---------------------------------------------------------------------------- */

let fetchStub: FetchStub

const envelope = (code: string, message: string) => ({ error: { code, message, details: [] } })

interface StubFailure {
  readonly status: number
  readonly body: unknown
}

/**
 * Stub every read the detail page performs.
 *
 * Fragment order matters: `/payments` and `reconciliation` must precede the bare
 * `/bookings/`, which would otherwise swallow both sub-resources.
 */
function stubReads(
  payload: readonly Payment[] | StubFailure = [],
  reconciliation: object = RECONCILIATION,
) {
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage([TEST_HOTEL], 1) })
  fetchStub.on(
    'GET',
    '/payments',
    Array.isArray(payload) ? { body: ledger(payload) } : (payload as StubFailure),
  )
  fetchStub.on(
    'GET',
    'reconciliation',
    'status' in reconciliation ? (reconciliation as StubFailure) : { body: reconciliation },
  )
  fetchStub.on('GET', '/guests/', { body: GUEST })
  fetchStub.on('GET', '/bookings/', { body: makeBooking() })
}

const posts = () => fetchStub.calls.filter((c) => c.method === 'POST')
const bodyOf = (call: { body: string | null }) =>
  JSON.parse(call.body ?? '{}') as Record<string, unknown>

function mount() {
  return renderWithAuth([{ path: BOOKING_DETAIL_PATTERN, element: <BookingDetailPage /> }], {
    initialEntries: [bookingPath(BOOKING_ID)],
  })
}

async function open(payload: readonly Payment[] | StubFailure = [], reconciliation?: object) {
  stubReads(payload, reconciliation)
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

/* --- reading the ledger ------------------------------------------------------------------ */

describe('reading the ledger', () => {
  it('asks the booking-scoped payments route', async () => {
    await open([makePayment()])

    const call = fetchStub.calls.find((c) => c.url.includes('/payments'))!
    expect(call.url).toContain(
      `/hotels/${TEST_HOTEL.public_id}/bookings/${BOOKING_ID}/payments`,
    )
    expect(call.method).toBe('GET')
    expect(call.authorization).toMatch(/^Bearer /)
  })

  it('shows each posting with its own currency, and totals nothing', async () => {
    await open([
      makePayment({ amount: '200.00', currency: 'EUR' }),
      makePayment({ public_id: 'p2', kind: 'refund', amount: '50.00', currency: 'EUR', refunds_public_id: CHARGE_ID, status: 'pending' }),
    ])

    const ledgerTable = screen.getByRole('table', { name: /Charges and refunds/i })
    expect(within(ledgerTable).getByText('EUR 200.00')).toBeInTheDocument()
    expect(within(ledgerTable).getByText('EUR 50.00')).toBeInTheDocument()
    // 200 - 50 = 150. The ledger must state no net figure and no sum; both belong to
    // reconciliation, which has its own panel and its own endpoint.
    expect(within(ledgerTable).queryByText(/150\.00/)).not.toBeInTheDocument()
    expect(within(ledgerTable).queryByText(/250\.00/)).not.toBeInTheDocument()
    // And the table has no footer in which one could appear.
    expect(ledgerTable.querySelector('tfoot')).toBeNull()
  })

  it('shows direction as a word, never as a negative amount', async () => {
    await open([
      makePayment({ public_id: 'p2', kind: 'refund', amount: '50.00', refunds_public_id: CHARGE_ID }),
    ])

    // `amount` is positive for both kinds; direction lives in `kind`.
    expect(screen.getByText('Refund')).toBeInTheDocument()
    expect(screen.getByText('EUR 50.00')).toBeInTheDocument()
    expect(screen.queryByText(/-50\.00/)).not.toBeInTheDocument()
    expect(screen.queryByText(/EUR -/)).not.toBeInTheDocument()
  })

  it('preserves the decimal exactly as the server sent it', async () => {
    await open([makePayment({ amount: '1234.05' })])

    expect(screen.getByText('EUR 1,234.05')).toBeInTheDocument()
  })

  it('renders a semantic table with column headers', async () => {
    await open([makePayment()])

    const table = screen.getByRole('table', { name: /Charges and refunds/i })
    for (const header of ['Posting', 'Amount', 'Status', 'Method', 'Reference', 'Recorded']) {
      expect(within(table).getByRole('columnheader', { name: header })).toBeInTheDocument()
    }
  })

  it('states an empty ledger politely, and not as a failure', async () => {
    await open([])

    const empty = screen.getByText(/No payments have been recorded/i)
    expect(empty).toBeInTheDocument()
    expect(empty).toHaveAttribute('role', 'status')
    expect(screen.queryByRole('table', { name: /Charges and refunds/i })).not.toBeInTheDocument()
  })

  it('never shows a failed read as an empty ledger', async () => {
    await open({ status: 500, body: envelope('INTERNAL_ERROR', 'boom') })

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/could not be loaded/i)).toBeInTheDocument()
    // The distinction that matters: an operator who reads "no payments" for a booking that
    // has them may take money twice.
    expect(screen.queryByText(/No payments have been recorded/i)).not.toBeInTheDocument()
    expect(within(alert).getByText(/not the same as having no payments/i)).toBeInTheDocument()
  })

  it('treats a 200 that is not a page as a failure', async () => {
    await open({ status: 200, body: { detail: 'nope' } })

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByText(/No payments have been recorded/i)).not.toBeInTheDocument()
  })

  it('marks a voided posting and withholds its refund control', async () => {
    await open([
      makePayment({ status: 'failed' }),
      makePayment({ public_id: 'p2', status: 'cancelled', transaction_reference: 'TXN-2' }),
    ])

    expect(screen.getAllByText('Moved no money')).toHaveLength(2)
    // The server refuses a refund against either; offering one would be a button whose only
    // outcome is a 409.
    expect(screen.queryByRole('button', { name: /Refund the/i })).not.toBeInTheDocument()
  })

  it('withholds the refund control on a refund', async () => {
    await open([
      makePayment({ public_id: 'p2', kind: 'refund', refunds_public_id: CHARGE_ID }),
    ])

    // "A refund cannot be issued against another refund" -- verified live.
    expect(screen.queryByRole('button', { name: /Refund the/i })).not.toBeInTheDocument()
  })
})

/* --- recording a charge ------------------------------------------------------------------- */

describe('recording a charge', () => {
  async function openForm() {
    const user = userEvent.setup()
    await open([])
    await user.click(screen.getByRole('button', { name: /Record a charge/i }))
    return user
  }

  it('sends the exact body, to the right route, as strings', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/payments', { status: 201, body: makePayment() })

    await user.type(screen.getByLabelText(/Amount/), '200.00')
    await user.selectOptions(screen.getByLabelText('Method'), 'card')
    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    await user.click(screen.getByRole('button', { name: 'Record charge' }))

    await waitFor(() => {
      expect(posts()).toHaveLength(1)
    })
    const call = posts()[0]!
    expect(call.url).toContain(`/bookings/${BOOKING_ID}/payments`)
    expect(call.url).not.toContain('/refunds')
    const body = bodyOf(call)
    // The amount is a STRING, exactly as typed. Never a number.
    expect(body['amount']).toBe('200.00')
    expect(typeof body['amount']).toBe('string')
    expect(body['currency']).toBe('EUR')
    expect(body['method']).toBe('card')
    expect(body['status']).toBe('pending')
  })

  it('never sends kind, which the endpoint determines', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/payments', { status: 201, body: makePayment() })

    await user.type(screen.getByLabelText(/Amount/), '10')
    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    await user.click(screen.getByRole('button', { name: 'Record charge' }))

    await waitFor(() => {
      expect(posts()).toHaveLength(1)
    })
    // Sending `kind` is a 422 -- verified live -- because a charge must not name a parent.
    expect(bodyOf(posts()[0]!)).not.toHaveProperty('kind')
    expect(bodyOf(posts()[0]!)).not.toHaveProperty('refunds_public_id')
  })

  it('sends the booking currency and offers no way to change it', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/payments', { status: 201, body: makePayment() })

    // A foreign-currency charge makes the booking permanently unreconcilable -- verified
    // live -- so the field is stated, not offered.
    expect(screen.queryByLabelText(/Currency/i)).not.toBeInTheDocument()
    expect(screen.getByText(/Amount \(EUR\)/)).toBeInTheDocument()

    await user.type(screen.getByLabelText(/Amount/), '10')
    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    await user.click(screen.getByRole('button', { name: 'Record charge' }))
    await waitFor(() => {
      expect(posts()).toHaveLength(1)
    })
    expect(bodyOf(posts()[0]!)['currency']).toBe('EUR')
  })

  it('offers no field for a card number', async () => {
    await openForm()

    // The schema stores the last four and nothing else; `additionalProperties: false` makes
    // sending a PAN a 422. There must be nowhere to type one.
    expect(screen.getByLabelText(/Card last four/i)).toHaveAttribute('maxlength', '4')
    for (const label of [/card number/i, /^pan$/i, /cvv/i, /expiry/i, /cardholder/i]) {
      expect(screen.queryByLabelText(label)).not.toBeInTheDocument()
    }
  })

  it('requires a settlement time for a captured payment', async () => {
    const user = await openForm()

    // ck_payments_captured_has_paid_at. The field appears when the status needs it.
    await user.selectOptions(screen.getByLabelText('Status'), 'captured')
    expect(screen.getByLabelText(/Taken at/i)).toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    expect(screen.queryByLabelText(/Taken at/i)).not.toBeInTheDocument()
  })

  it('sends the transaction reference exactly as typed', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/payments', { status: 201, body: makePayment() })

    await user.type(screen.getByLabelText(/Amount/), '10')
    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    await user.type(screen.getByLabelText(/Transaction reference/i), 'ch_1AbC-xyz_09')
    await user.click(screen.getByRole('button', { name: 'Record charge' }))

    await waitFor(() => {
      expect(posts()).toHaveLength(1)
    })
    // Not upper-cased, not normalised. It is the processor's identifier.
    expect(bodyOf(posts()[0]!)['transaction_reference']).toBe('ch_1AbC-xyz_09')
  })

  it('omits optional fields rather than sending empty strings', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/payments', { status: 201, body: makePayment() })

    await user.type(screen.getByLabelText(/Amount/), '10')
    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    await user.click(screen.getByRole('button', { name: 'Record charge' }))

    await waitFor(() => {
      expect(posts()).toHaveLength(1)
    })
    const body = bodyOf(posts()[0]!)
    expect(body).not.toHaveProperty('provider')
    expect(body).not.toHaveProperty('transaction_reference')
    expect(body).not.toHaveProperty('card_last_four')
    expect(body).not.toHaveProperty('paid_at')
  })

  it('refuses a non-positive or over-precise amount before sending it', async () => {
    const user = await openForm()

    for (const bad of ['0', '-5', '1.555', 'abc']) {
      await user.clear(screen.getByLabelText(/Amount/))
      await user.type(screen.getByLabelText(/Amount/), bad)
      await user.click(screen.getByRole('button', { name: 'Record charge' }))
      expect(await screen.findByRole('alert')).toHaveTextContent(/greater than zero/i)
    }
    expect(posts()).toHaveLength(0)
  })

  it('re-reads the ledger and the financial summary after a success', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/payments', { status: 201, body: makePayment() })
    const readsBefore = fetchStub.calls.filter((c) => c.method === 'GET' && c.url.includes('/payments')).length
    const reconBefore = fetchStub.calls.filter((c) => c.url.includes('reconciliation')).length

    await user.type(screen.getByLabelText(/Amount/), '200.00')
    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    await user.click(screen.getByRole('button', { name: 'Record charge' }))

    expect(await screen.findByText(/Charge recorded\./)).toBeInTheDocument()
    await waitFor(() => {
      expect(
        fetchStub.calls.filter((c) => c.method === 'GET' && c.url.includes('/payments')).length,
      ).toBeGreaterThan(readsBefore)
      expect(fetchStub.calls.filter((c) => c.url.includes('reconciliation')).length).toBeGreaterThan(
        reconBefore,
      )
    })
  })

  it('shows no row and no success until the server answers', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/payments', {
      status: 409,
      body: envelope('CONFLICT', 'A payment with this provider transaction reference has already been recorded.'),
    })

    await user.type(screen.getByLabelText(/Amount/), '200.00')
    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    await user.click(screen.getByRole('button', { name: 'Record charge' }))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/nothing was recorded/i)).toBeInTheDocument()
    expect(screen.queryByText(/Charge recorded\./)).not.toBeInTheDocument()
    // No optimistic row: the ledger is still empty.
    expect(screen.getByText(/No payments have been recorded/i)).toBeInTheDocument()
  })

  it('does not post twice when the button is pressed twice in one tick', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/payments', { status: 201, body: makePayment() })

    await user.type(screen.getByLabelText(/Amount/), '200.00')
    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    const submit = screen.getByRole('button', { name: 'Record charge' })
    // On money, a double-fire is not cosmetic. The guard is a ref, not state.
    fireEvent.click(submit)
    fireEvent.click(submit)

    await screen.findByText(/Charge recorded\./)
    expect(posts()).toHaveLength(1)
  })
})

/* --- recording a refund --------------------------------------------------------------------- */

describe('recording a refund', () => {
  async function openRefund() {
    const user = userEvent.setup()
    await open([makePayment()])
    await user.click(screen.getByRole('button', { name: /Refund the/i }))
    return user
  }

  it('names the parent by public id and takes its currency', async () => {
    const user = await openRefund()
    fetchStub.on('POST', '/refunds', {
      status: 201,
      body: makePayment({ public_id: 'r1', kind: 'refund', amount: '50.00', refunds_public_id: CHARGE_ID }),
    })

    await user.type(screen.getByLabelText(/Refund amount/i), '50.00')
    await user.click(screen.getByRole('button', { name: 'Review refund' }))
    await user.click(screen.getByRole('button', { name: /Yes, record refund/i }))

    await waitFor(() => {
      expect(posts()).toHaveLength(1)
    })
    const call = posts()[0]!
    expect(call.url).toContain(`/bookings/${BOOKING_ID}/payments/refunds`)
    expect(bodyOf(call)).toEqual({
      amount: '50.00',
      currency: 'EUR',
      refunds_public_id: CHARGE_ID,
      method: 'card',
    })
  })

  it('offers no currency field, because the server requires the parent’s', async () => {
    await openRefund()

    expect(screen.queryByLabelText(/^Currency/i)).not.toBeInTheDocument()
    expect(screen.getByLabelText(/Refund amount \(EUR\)/i)).toBeInTheDocument()
  })

  it('shows no remaining-refundable figure anywhere', async () => {
    await openRefund()

    /*
     * The single most important negative assertion in this file. The server computes
     * `parent.amount - already_refunded` under a row lock and exposes it through no endpoint.
     * A number here would be a snapshot taken outside the lock, and on a partly-refunded
     * charge it would be wrong.
     */
    const form = screen.getByLabelText(/Refund amount/i).closest('form') as HTMLElement
    /*
     * The assertion is about a FIGURE, not a phrase: the hint deliberately uses the words
     * "still refundable" to say the number is not shown. So the form is scanned for any
     * money-shaped value other than the parent's own amount, which is a real field.
     */
    const figures = (form.textContent ?? '').match(/\d[\d,]*\.\d{2}/g) ?? []
    expect(figures).toEqual(['200.00'])
    expect(within(form).getByText(/decided by the server when the refund is posted/i)).toBeInTheDocument()
  })

  it('requires a confirmation step, and posts nothing before it', async () => {
    const user = await openRefund()

    await user.type(screen.getByLabelText(/Refund amount/i), '50.00')
    await user.click(screen.getByRole('button', { name: 'Review refund' }))

    expect(screen.getByRole('group', { name: 'Confirm refund' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Yes, record refund/i })).toHaveFocus()
    expect(posts()).toHaveLength(0)
  })

  it('lets the operator go back without posting', async () => {
    const user = await openRefund()

    await user.type(screen.getByLabelText(/Refund amount/i), '50.00')
    await user.click(screen.getByRole('button', { name: 'Review refund' }))
    await user.click(screen.getByRole('button', { name: 'Back' }))

    expect(screen.getByLabelText(/Refund amount/i)).toBeInTheDocument()
    expect(posts()).toHaveLength(0)
  })

  it('refuses locally only what is unarguable: more than the charge itself', async () => {
    const user = await openRefund()

    await user.type(screen.getByLabelText(/Refund amount/i), '500.00')
    await user.click(screen.getByRole('button', { name: 'Review refund' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/cannot exceed the charge/i)
    expect(posts()).toHaveLength(0)
  })

  it('reports a concurrent refusal without claiming money moved', async () => {
    const user = await openRefund()
    fetchStub.on('POST', '/refunds', {
      status: 409,
      body: envelope(
        'CONFLICT',
        'This refund exceeds the amount still refundable on the payment (20.00 EUR of 100.00 remaining).',
      ),
    })

    await user.type(screen.getByLabelText(/Refund amount/i), '80.00')
    await user.click(screen.getByRole('button', { name: 'Review refund' }))
    await user.click(screen.getByRole('button', { name: /Yes, record refund/i }))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(/nothing was recorded/i)).toBeInTheDocument()
    expect(screen.queryByText(/Refund recorded\./)).not.toBeInTheDocument()
    // Never retried: the endpoint is not idempotent without a transaction reference.
    expect(posts()).toHaveLength(1)
  })

  it('does not post twice when confirmed twice in one tick', async () => {
    const user = await openRefund()
    fetchStub.on('POST', '/refunds', {
      status: 201,
      body: makePayment({ public_id: 'r1', kind: 'refund', amount: '50.00', refunds_public_id: CHARGE_ID }),
    })

    await user.type(screen.getByLabelText(/Refund amount/i), '50.00')
    await user.click(screen.getByRole('button', { name: 'Review refund' }))
    const confirm = screen.getByRole('button', { name: /Yes, record refund/i })
    fireEvent.click(confirm)
    fireEvent.click(confirm)

    await screen.findByText(/Refund recorded\./)
    expect(posts()).toHaveLength(1)
  })

  it('re-reads the financial summary after a success', async () => {
    const user = await openRefund()
    fetchStub.on('POST', '/refunds', {
      status: 201,
      body: makePayment({ public_id: 'r1', kind: 'refund', amount: '50.00', refunds_public_id: CHARGE_ID }),
    })
    const before = fetchStub.calls.filter((c) => c.url.includes('reconciliation')).length

    await user.type(screen.getByLabelText(/Refund amount/i), '50.00')
    await user.click(screen.getByRole('button', { name: 'Review refund' }))
    await user.click(screen.getByRole('button', { name: /Yes, record refund/i }))
    await screen.findByText(/Refund recorded\./)

    await waitFor(() => {
      expect(fetchStub.calls.filter((c) => c.url.includes('reconciliation')).length).toBeGreaterThan(before)
    })
  })
})

/* --- failures shared by both postings -------------------------------------------------------- */

describe('refused postings', () => {
  async function attempt(failure: StubFailure | { networkError: true }) {
    const user = userEvent.setup()
    await open([])
    await user.click(screen.getByRole('button', { name: /Record a charge/i }))
    fetchStub.on('POST', '/payments', failure as never)
    await user.type(screen.getByLabelText(/Amount/), '10')
    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    await user.click(screen.getByRole('button', { name: 'Record charge' }))
    return screen.findByRole('alert')
  }

  it('renders a 403 without describing the permission model', async () => {
    const alert = await attempt({
      status: 403,
      body: envelope('FORBIDDEN', 'This operation requires the staff role at this hotel.'),
    })

    expect(within(alert).getByText(/do not have access/i)).toBeInTheDocument()
    expect(document.body.innerHTML).not.toContain('staff role')
  })

  it('renders a 404 safely', async () => {
    const alert = await attempt({ status: 404, body: envelope('NOT_FOUND', 'Booking not found for this hotel.') })

    expect(within(alert).getByText('Not found')).toBeInTheDocument()
  })

  it('renders a 422 without field noise', async () => {
    const alert = await attempt({
      status: 422,
      body: {
        error: {
          code: 'VALIDATION_ERROR',
          message: 'The request payload is invalid.',
          details: [{ location: ['body', 'amount'], message: 'Input should be greater than 0', type: 'greater_than' }],
        },
      },
    })

    expect(within(alert).getByText(/could not be processed/i)).toBeInTheDocument()
  })

  it('renders a network failure as a failure to record', async () => {
    const alert = await attempt({ networkError: true })

    expect(within(alert).getByText(/Could not reach the server/i)).toBeInTheDocument()
    expect(screen.queryByText(/Charge recorded\./)).not.toBeInTheDocument()
  })

  it('treats a malformed 201 as a failure, not a success', async () => {
    const alert = await attempt({ status: 201, body: { ok: true } })

    expect(within(alert).getByText(/Unexpected response/i)).toBeInTheDocument()
    expect(screen.queryByText(/Charge recorded\./)).not.toBeInTheDocument()
  })

  it('never lets a backend internal reach the DOM', async () => {
    await attempt({
      status: 500,
      body: envelope(
        'INTERNAL_ERROR',
        'psycopg.errors.UniqueViolation: duplicate key value violates unique constraint "uq_payments_provider_transaction_reference"',
      ),
    })

    for (const leak of [
      'psycopg',
      'UniqueViolation',
      'uq_payments_provider_transaction_reference',
      'SQLSTATE',
      'sqlalchemy',
      'Traceback',
    ]) {
      expect(document.body.innerHTML).not.toContain(leak)
    }
  })
})

/* --- the mixed-currency reconciliation state --------------------------------------------------- */

describe('a booking whose payments are in more than one currency', () => {
  it('explains the 409 rather than calling the summary temporarily unavailable', async () => {
    await open(
      [
        makePayment({ amount: '200.00', currency: 'EUR' }),
        makePayment({ public_id: 'p2', amount: '10.00', currency: 'USD', transaction_reference: 'TXN-2' }),
      ],
      {
        status: 409,
        body: envelope(
          'CONFLICT',
          'This booking cannot be reconciled: it has payments in USD but the booking is in EUR.',
        ),
      },
    )

    // A permanent, explicable state -- not a fault that will clear.
    expect(screen.getByText(/cannot be reconciled/i)).toBeInTheDocument()
    expect(screen.getByText(/more than one currency/i)).toBeInTheDocument()
    // The two postings still show, each in its own currency, and nothing merges them.
    expect(screen.getByText('EUR 200.00')).toBeInTheDocument()
    expect(screen.getByText('USD 10.00')).toBeInTheDocument()
    expect(screen.queryByText(/EUR 210\.00/)).not.toBeInTheDocument()
  })
})

/* --- security ------------------------------------------------------------------------------- */

describe('security', () => {
  it('puts no token in the DOM or the URL while posting', async () => {
    const user = userEvent.setup()
    await open([])
    await user.click(screen.getByRole('button', { name: /Record a charge/i }))
    fetchStub.on('POST', '/payments', { status: 201, body: makePayment() })

    await user.type(screen.getByLabelText(/Amount/), '10')
    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    await user.click(screen.getByRole('button', { name: 'Record charge' }))
    await screen.findByText(/Charge recorded\./)

    const token = window.sessionStorage.getItem('ahip.access_token')!
    expect(document.documentElement.outerHTML).not.toContain(token)
    expect(document.documentElement.outerHTML).not.toMatch(/Bearer\s/)
    expect(window.location.href).not.toContain(token)
  })

  it('keys every payment URL by UUID, with no internal id', async () => {
    await open([makePayment()])

    for (const call of fetchStub.calls.filter((c) => c.url.includes('/payments'))) {
      expect(call.url).toMatch(/\/hotels\/[0-9a-f-]{36}\/bookings\/[0-9a-f-]{36}\/payments/)
      expect(call.url).not.toMatch(/\/payments\/\d+(\?|\/|$)/)
    }
  })

  it('shows only the card tail, never a fuller number', async () => {
    await open([makePayment({ card_last_four: '4242' })])

    expect(screen.getByText(/4242/)).toBeInTheDocument()
    // There is no field for one and nothing to render, but assert the shape anyway.
    expect(document.body.innerHTML).not.toMatch(/\b\d{13,19}\b/)
  })

  it('leaks no guest contact detail through a posting body', async () => {
    const user = userEvent.setup()
    await open([])
    await user.click(screen.getByRole('button', { name: /Record a charge/i }))
    fetchStub.on('POST', '/payments', { status: 201, body: makePayment() })

    await user.type(screen.getByLabelText(/Amount/), '10')
    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    await user.click(screen.getByRole('button', { name: 'Record charge' }))
    await waitFor(() => {
      expect(posts()).toHaveLength(1)
    })

    for (const call of fetchStub.calls) {
      expect(call.body ?? '').not.toContain('example.test')
    }
  })
})

/* --- accessibility ---------------------------------------------------------------------------- */

describe('accessibility of the payments section', () => {
  it('labels every field of the charge form', async () => {
    const user = userEvent.setup()
    await open([])
    await user.click(screen.getByRole('button', { name: /Record a charge/i }))

    for (const label of [/Amount/, 'Method', 'Status', /Provider/, /Transaction reference/, /Card last four/]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }
  })

  it('associates a validation message with the field it is about', async () => {
    const user = userEvent.setup()
    await open([])
    await user.click(screen.getByRole('button', { name: /Record a charge/i }))

    await user.type(screen.getByLabelText(/Amount/), '-5')
    await user.click(screen.getByRole('button', { name: 'Record charge' }))

    const field = screen.getByLabelText(/Amount/)
    expect(field).toHaveAttribute('aria-invalid', 'true')
    expect(field).toHaveAccessibleDescription(/greater than zero/i)
  })

  it('announces status without relying on colour', async () => {
    await open([makePayment({ status: 'captured' })])

    // getAllBy: the visible label and the longer description a screen reader hears both
    // carry the word, which is the point -- the meaning survives without the colour.
    expect(screen.getAllByText(/Captured/).length).toBeGreaterThan(0)
    expect(screen.getByText(/the money has been taken/i)).toBeInTheDocument()
  })

  it('disables the submit while a posting is in flight', async () => {
    const user = userEvent.setup()
    await open([])
    await user.click(screen.getByRole('button', { name: /Record a charge/i }))

    let release: (() => void) | undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const original = globalThis.fetch
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET') === 'POST') {
        await gate
        return new Response(JSON.stringify(makePayment()), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return original(input, init)
    }) as unknown as typeof fetch

    await user.type(screen.getByLabelText(/Amount/), '10')
    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    fireEvent.click(screen.getByRole('button', { name: 'Record charge' }))

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Recording/i })).toBeDisabled()
    })

    release?.()
    await screen.findByText(/Charge recorded\./)
    globalThis.fetch = original
  })
})

/* --- a refusal must not outlive the attempt it describes ------------------------------------- */

describe('a stale refusal', () => {
  it('clears as soon as the operator starts another attempt', async () => {
    const user = userEvent.setup()
    await open([makePayment()])
    await user.click(screen.getByRole('button', { name: /Refund the/i }))
    fetchStub.on('POST', '/refunds', {
      status: 409,
      body: envelope('CONFLICT', 'This refund exceeds the amount still refundable on the payment.'),
    })

    await user.type(screen.getByLabelText(/Refund amount/i), '180.00')
    await user.click(screen.getByRole('button', { name: 'Review refund' }))
    await user.click(screen.getByRole('button', { name: /Yes, record refund/i }))
    await screen.findByText(/nothing was recorded/i)

    /*
     * Found in the browser: the refusal banner from the previous attempt sat above the form
     * while a new amount was being typed, so two alerts were on screen and the first
     * described something the operator was no longer doing. Editing the amount retires it.
     */
    await user.click(screen.getByRole('button', { name: 'Back' }))
    await user.clear(screen.getByLabelText(/Refund amount/i))
    await user.type(screen.getByLabelText(/Refund amount/i), '5')

    expect(screen.queryByText(/nothing was recorded/i)).not.toBeInTheDocument()
    expect(screen.queryAllByRole('alert')).toHaveLength(0)
  })

  it('does the same on the charge form', async () => {
    const user = userEvent.setup()
    await open([])
    await user.click(screen.getByRole('button', { name: /Record a charge/i }))
    fetchStub.on('POST', '/payments', {
      status: 409,
      body: envelope('CONFLICT', 'A payment with this provider transaction reference has already been recorded.'),
    })

    await user.type(screen.getByLabelText(/Amount/), '10')
    await user.selectOptions(screen.getByLabelText('Status'), 'pending')
    await user.click(screen.getByRole('button', { name: 'Record charge' }))
    await screen.findByText(/nothing was recorded/i)

    await user.type(screen.getByLabelText(/Amount/), '0')

    expect(screen.queryByText(/nothing was recorded/i)).not.toBeInTheDocument()
  })
})
