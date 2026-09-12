import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { todayInZone } from '@/features/dashboard/period'
import { FinancialsPage } from '@/pages/FinancialsPage'
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
import type {
  ExpenseBreakdownResponse,
  ExpenseEntry,
  RevenueBreakdownResponse,
  RevenueEntry,
} from '@/types/finance'

/**
 * The revenue and expense journals, as the UI reads and writes them.
 *
 * Mocked at `fetch`, so the method, the URL, **every query parameter** and **the exact
 * request body** are observable. On a financial screen those are most of what matters: an
 * invented filter that the backend silently ignores looks identical to one that worked, and
 * a figure that went through a float looks identical to one that did not until the cent that
 * disappears.
 *
 * Fixtures are the shapes the live API returned during contract discovery: decimals as
 * strings, a signed `amount`, no identifier on either row type, and a breakdown keyed by
 * `(category_code, currency)` with EUR and USD as separate buckets.
 *
 * The companion file `architecture.node.test.ts` asserts the same claim from the other side:
 * that the source contains no arithmetic capable of producing a total.
 */

/* --- fixtures --------------------------------------------------------------------------- */

const BOOKING_ID = 'e5b72bf8-11a1-456b-9755-5a5ac5219782'
const TODAY = todayInZone(TEST_HOTEL.timezone)

function revenue(overrides: Partial<RevenueEntry> = {}): RevenueEntry {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    category_code: 'FNB',
    booking_public_id: null,
    revenue_date: TODAY,
    amount: '150.00',
    tax_amount: '13.00',
    currency: 'EUR',
    description: 'Restaurant covers',
    reference: 'INV-4471',
    created_at: '2026-09-09T18:20:00+03:00',
    updated_at: '2026-09-09T18:20:00+03:00',
    ...overrides,
  }
}

function expense(overrides: Partial<ExpenseEntry> = {}): ExpenseEntry {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    category_code: 'UTILITIES',
    expense_date: TODAY,
    amount: '435.00',
    tax_amount: '0.00',
    currency: 'EUR',
    description: 'Electricity, August',
    vendor: 'Hellenic Power',
    invoice_reference: 'HP-2026-08',
    is_recurring: false,
    recurrence_interval: null,
    created_at: '2026-09-08T09:00:00+03:00',
    updated_at: '2026-09-08T09:00:00+03:00',
    ...overrides,
  }
}

function page(items: readonly unknown[], total = items.length, pages = 1, pageNumber = 1) {
  return { items, total, page: pageNumber, page_size: 20, pages }
}

const REVENUE_CATEGORIES = page([
  { code: 'FNB', name: 'Food & Beverage', is_room_revenue: false, is_active: true },
  { code: 'PARKING', name: 'Parking', is_room_revenue: false, is_active: true },
  { code: 'ROOM_LEDGER', name: 'Room revenue (ledger)', is_room_revenue: true, is_active: true },
  { code: 'SPA', name: 'Spa & Wellness', is_room_revenue: false, is_active: true },
])

const EXPENSE_CATEGORIES = page([
  { code: 'PAYROLL', name: 'Payroll', is_fixed_cost: true, is_active: true },
  { code: 'SUPPLIES', name: 'Housekeeping supplies', is_fixed_cost: false, is_active: true },
  { code: 'UTILITIES', name: 'Utilities', is_fixed_cost: false, is_active: true },
])

/**
 * Two currencies in the same breakdown, deliberately.
 *
 * `750.00` EUR and `80.00` USD are separate buckets because the server grouped them that
 * way. Several tests below assert that no combination of the two ever appears -- not their
 * sum, not a converted figure, not a "total" of any kind.
 */
const REVENUE_BREAKDOWN: RevenueBreakdownResponse = {
  hotel_public_id: TEST_HOTEL.public_id,
  range: { date_from: '2026-09-04', date_to: TODAY, days: 7 },
  categories: [
    {
      category_code: 'FNB',
      is_room_revenue: false,
      currency: 'EUR',
      amount: '750.00',
      tax_amount: '26.00',
      entry_count: 10,
    },
    {
      category_code: 'FNB',
      is_room_revenue: false,
      currency: 'USD',
      amount: '80.00',
      tax_amount: '0.00',
      entry_count: 2,
    },
  ],
}

const EXPENSE_BREAKDOWN: ExpenseBreakdownResponse = {
  hotel_public_id: TEST_HOTEL.public_id,
  range: { date_from: '2026-09-04', date_to: TODAY, days: 7 },
  categories: [
    {
      category_code: 'UTILITIES',
      is_fixed_cost: false,
      currency: 'EUR',
      amount: '435.00',
      tax_amount: '0.00',
      entry_count: 9,
    },
  ],
}

/* --- harness ---------------------------------------------------------------------------- */

let fetchStub: FetchStub

/**
 * Registration order is load-bearing.
 *
 * The stub matches the first handler whose fragment the URL **contains**, so
 * `/analytics/revenue-by-category` must be registered before anything matching `/revenue`,
 * and `/hotels?page=` must be specific enough not to swallow `/hotels/{id}/revenue`.
 */
beforeEach(() => {
  fetchStub = installFetchStub()
  seedStoredToken()
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage([TEST_HOTEL], 1) })
  fetchStub.on('GET', '/analytics/revenue-by-category', { body: REVENUE_BREAKDOWN })
  fetchStub.on('GET', '/analytics/expenses-by-category', { body: EXPENSE_BREAKDOWN })
  fetchStub.on('GET', '/revenue-categories', { body: REVENUE_CATEGORIES })
  fetchStub.on('GET', '/expense-categories', { body: EXPENSE_CATEGORIES })
  fetchStub.on('GET', '/revenue?', { body: page([revenue()]) })
  fetchStub.on('GET', '/expenses?', { body: page([expense()]) })
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
})

function renderFinancials() {
  return renderWithAuth([{ path: ROUTES.financials, element: <FinancialsPage /> }], {
    initialEntries: [ROUTES.financials],
  })
}

/** Requests to one endpoint, identified by a fragment of its path. */
function requestsFor(fragment: string, method = 'GET') {
  return fetchStub.calls.filter((call) => call.method === method && call.url.includes(fragment))
}

function lastRequestFor(fragment: string, method = 'GET') {
  const matches = requestsFor(fragment, method)
  return matches[matches.length - 1]
}

/** Everything rendered, as one string, for "this figure appears nowhere" assertions. */
function visibleText(): string {
  return document.body.textContent ?? ''
}

/* --- reading the journal ---------------------------------------------------------------- */

describe('the revenue journal', () => {
  it('asks the server for the period, and renders the line it sends back', async () => {
    renderFinancials()

    expect(await screen.findByText('Restaurant covers')).toBeInTheDocument()

    const request = lastRequestFor('/revenue?')
    expect(request).toBeDefined()
    const url = new URL(request!.url, 'http://localhost')
    expect(url.searchParams.get('date_to')).toBe(TODAY)
    expect(url.searchParams.get('date_from')).not.toBeNull()
    expect(url.searchParams.get('page')).toBe('1')
    expect(url.searchParams.get('page_size')).toBe('20')
    // The Authorization header is assembled in one place; this proves it reached the ledger.
    expect(request!.authorization).toMatch(/^Bearer /)
  })

  it('prints each amount in the currency of its own line, never converted', async () => {
    fetchStub.on('GET', '/revenue?', {
      body: page([revenue(), revenue({ amount: '80.00', currency: 'USD', reference: 'INV-4472' })]),
    })
    renderFinancials()

    const journal = await screen.findByRole('table', { name: /Revenue lines, newest first/ })
    expect(within(journal).getByText('EUR 150.00')).toBeInTheDocument()
    expect(within(journal).getByText('USD 80.00')).toBeInTheDocument()
    // Neither the sum nor a converted figure. 230 is 150 + 80 read as bare numbers.
    expect(visibleText()).not.toMatch(/230/)
  })

  it('marks a negative line as a correction rather than showing a bare minus', async () => {
    fetchStub.on('GET', '/revenue?', { body: page([revenue({ amount: '-25.00' })]) })
    renderFinancials()

    expect(await screen.findByText('Correction')).toBeInTheDocument()
    expect(screen.getByText('-EUR 25.00')).toBeInTheDocument()
  })

  it('links a line to its booking, and shows nothing when it has none', async () => {
    fetchStub.on('GET', '/revenue?', {
      body: page([
        revenue({ booking_public_id: BOOKING_ID }),
        revenue({ reference: 'INV-4473', description: 'Minibar' }),
      ]),
    })
    renderFinancials()

    const link = await screen.findByRole('link', { name: 'View booking' })
    expect(link).toHaveAttribute('href', `/bookings/${BOOKING_ID}`)
    // One link for two rows: the second line has no booking.
    expect(screen.getAllByRole('link', { name: 'View booking' })).toHaveLength(1)
  })

  it('reports the count the server gave, not the number of rows on screen', async () => {
    fetchStub.on('GET', '/revenue?', { body: page([revenue()], 124, 7) })
    renderFinancials()

    // 124 lines across 7 pages, one of which is rendered. Nothing here counts the array.
    expect(await screen.findByText(/124 lines in this period/)).toBeInTheDocument()
    expect(screen.getByText(/Page 1 of 7/)).toBeInTheDocument()
  })

  it('says a period is empty without calling it zero', async () => {
    fetchStub.on('GET', '/revenue?', { body: page([], 0, 0) })
    renderFinancials()

    expect(
      await screen.findByText('No revenue was posted in this period'),
    ).toBeInTheDocument()
    expect(screen.getByText(/not a period totalling zero/)).toBeInTheDocument()
    // The distinction the dashboard stage established: an absence is not a measurement.
    expect(visibleText()).not.toMatch(/EUR 0\.00 total/)
  })

  it('treats a 200 that is not the documented shape as a failure, not an empty journal', async () => {
    fetchStub.on('GET', '/revenue?', { body: { items: null, total: 0 } })
    renderFinancials()

    expect(await screen.findByText('Unexpected response')).toBeInTheDocument()
    expect(screen.queryByText(/No revenue was posted/)).not.toBeInTheDocument()
  })
})

/* --- the category catalogue -------------------------------------------------------------- */

describe('the shared category catalogue', () => {
  it('asks for a page the routers will actually serve', async () => {
    renderFinancials()
    await screen.findByText('Restaurant covers')

    const url = new URL(lastRequestFor('/revenue-categories')!.url, 'http://localhost')
    // `MAX_PAGE_SIZE = 100` on every list route, category routes included. This test exists
    // because a guess of 200 shipped, returned 422 against the real backend, and left the
    // category control silently empty -- the request succeeded in every mocked test.
    expect(Number(url.searchParams.get('page_size'))).toBeLessThanOrEqual(100)
    expect(url.searchParams.get('is_active')).toBe('true')
  })

  it('offers a typed code, and says why, when the catalogue cannot be read', async () => {
    fetchStub.on('GET', '/revenue-categories', {
      status: 500,
      body: { error: { code: 'INTERNAL_ERROR', message: 'Internal server error.' } },
    })
    const user = userEvent.setup()
    renderFinancials()
    await screen.findByText('Restaurant covers')

    // The journal still reads, and each row still shows the code the server recorded.
    // (`FNB` appears in the totals table too, which is the point: neither needs the
    // catalogue, because both render codes the server sent.)
    const journal = screen.getByRole('table', { name: /Revenue lines, newest first/ })
    expect(within(journal).getByText('FNB')).toBeInTheDocument()
    expect(screen.getByText(/category list could not be read/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Post revenue line/ }))
    // A text field rather than an empty dropdown, so a posting is still possible.
    const field = screen.getByLabelText('Category')
    expect(field.tagName).toBe('INPUT')
    expect(screen.getByText(/server checks it and refuses an unknown one/)).toBeInTheDocument()
  })

  it('posts a typed code unchanged, and renders the server’s refusal of an unknown one', async () => {
    fetchStub.on('GET', '/revenue-categories', {
      status: 500,
      body: { error: { code: 'INTERNAL_ERROR', message: 'Internal server error.' } },
    })
    fetchStub.on('POST', '/revenue', {
      status: 404,
      body: { error: { code: 'NOT_FOUND', message: 'Revenue category not found.' } },
    })
    const user = userEvent.setup()
    renderFinancials()
    await screen.findByText('Restaurant covers')
    await user.click(screen.getByRole('button', { name: /Post revenue line/ }))

    await user.type(screen.getByLabelText('Category'), 'NOSUCH')
    await user.type(screen.getByLabelText('Amount'), '10.00')
    await user.click(screen.getByRole('button', { name: 'Review line' }))
    await user.click(screen.getByRole('button', { name: 'Yes, post this line' }))

    await waitFor(() => {
      expect(requestsFor('/revenue', 'POST')).toHaveLength(1)
    })
    const body = JSON.parse(requestsFor('/revenue', 'POST')[0]!.body!) as Record<string, unknown>
    expect(body.category_code).toBe('NOSUCH')
    expect(await screen.findByText('Nothing was posted')).toBeInTheDocument()
  })
})

/* --- the server's totals ---------------------------------------------------------------- */

describe('the totals for the period', () => {
  it('shows one row per category and currency, as the server grouped them', async () => {
    renderFinancials()

    const totals = await screen.findByRole('table', {
      name: /Totals computed by the server/,
    })
    expect(within(totals).getByText('EUR 750.00')).toBeInTheDocument()
    expect(within(totals).getByText('USD 80.00')).toBeInTheDocument()
    expect(within(totals).getByText('10')).toBeInTheDocument()
  })

  it('never combines two currencies into one figure', async () => {
    renderFinancials()
    await screen.findByText('EUR 750.00')

    // 830.00 is 750 + 80 across currencies -- the number that is not money.
    expect(visibleText()).not.toMatch(/830/)
    // Nor a per-currency grand total, which the endpoint also does not report.
    expect(visibleText()).not.toMatch(/\bTotal revenue\b/i)
  })

  it('states that the journal filters do not narrow it', async () => {
    renderFinancials()

    expect(
      await screen.findByText(/The journal.s filters below do not narrow them/),
    ).toBeInTheDocument()
  })

  it('does not refetch the totals when a filter changes, because they cannot be filtered', async () => {
    const user = userEvent.setup()
    renderFinancials()
    await screen.findByText('Restaurant covers')

    const before = requestsFor('/analytics/revenue-by-category').length
    await user.selectOptions(screen.getByLabelText('Filter by category'), 'FNB')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))

    await waitFor(() => {
      expect(requestsFor('/revenue?').length).toBeGreaterThan(1)
    })
    expect(requestsFor('/analytics/revenue-by-category')).toHaveLength(before)
  })

  it('keeps the journal readable when the aggregation fails', async () => {
    fetchStub.on('GET', '/analytics/revenue-by-category', {
      status: 500,
      body: { error: { code: 'INTERNAL_ERROR', message: 'Internal server error.' } },
    })
    renderFinancials()

    expect(await screen.findByText('Totals are temporarily unavailable')).toBeInTheDocument()
    // The rows are still there, and nothing offers to add them up instead.
    expect(screen.getByText('Restaurant covers')).toBeInTheDocument()
    expect(visibleText()).not.toMatch(/750/)
  })
})

/* --- filters ---------------------------------------------------------------------------- */

describe('the journal filters', () => {
  it('sends only the parameters the endpoint has', async () => {
    renderFinancials()
    await screen.findByText('Restaurant covers')

    const url = new URL(lastRequestFor('/revenue?')!.url, 'http://localhost')
    expect([...url.searchParams.keys()].sort()).toEqual([
      'date_from',
      'date_to',
      'page',
      'page_size',
    ])
  })

  it('sends the category as the server names it', async () => {
    const user = userEvent.setup()
    renderFinancials()
    await screen.findByText('Restaurant covers')

    await user.selectOptions(screen.getByLabelText('Filter by category'), 'PARKING')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))

    await waitFor(() => {
      const url = new URL(lastRequestFor('/revenue?')!.url, 'http://localhost')
      expect(url.searchParams.get('category_code')).toBe('PARKING')
    })
  })

  it('sends a booking identifier, and offers that filter only on revenue', async () => {
    const user = userEvent.setup()
    renderFinancials()
    await screen.findByText('Restaurant covers')

    await user.type(screen.getByLabelText('Filter by booking'), BOOKING_ID)
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))

    await waitFor(() => {
      const url = new URL(lastRequestFor('/revenue?')!.url, 'http://localhost')
      expect(url.searchParams.get('booking_public_id')).toBe(BOOKING_ID)
    })

    // The expense journal has no booking column, so it must not offer the control.
    await user.click(screen.getByRole('radio', { name: 'Expenses' }))
    await screen.findByText('Electricity, August')
    expect(screen.queryByLabelText('Filter by booking')).not.toBeInTheDocument()
  })

  it('does not request a filter for every keystroke of an identifier', async () => {
    const user = userEvent.setup()
    renderFinancials()
    await screen.findByText('Restaurant covers')

    const before = requestsFor('/revenue?').length
    await user.type(screen.getByLabelText('Filter by booking'), 'abcdef')
    expect(requestsFor('/revenue?')).toHaveLength(before)
  })

  it('reads a 404 as a wrong filter rather than an empty result', async () => {
    const user = userEvent.setup()
    renderFinancials()
    await screen.findByText('Restaurant covers')

    // The live API answers 404 for an unknown category code -- distinct from a filter that
    // matches nothing, which is a 200 with zero rows.
    fetchStub.on('GET', '/revenue?', {
      status: 404,
      body: { error: { code: 'NOT_FOUND', message: 'Revenue category not found.' } },
    })
    await user.selectOptions(screen.getByLabelText('Filter by category'), 'SPA')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))

    expect(await screen.findByText('Nothing to show for that filter')).toBeInTheDocument()
    expect(screen.queryByText(/No revenue was posted/)).not.toBeInTheDocument()
  })

  it('returns to the first page when the filter narrows the result', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/revenue?', { body: page([revenue()], 124, 7) })
    renderFinancials()
    await screen.findByText(/124 lines/)

    await user.click(screen.getByRole('button', { name: /Next/ }))
    await waitFor(() => {
      const url = new URL(lastRequestFor('/revenue?')!.url, 'http://localhost')
      expect(url.searchParams.get('page')).toBe('2')
    })

    await user.selectOptions(screen.getByLabelText('Filter by category'), 'FNB')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))

    await waitFor(() => {
      const url = new URL(lastRequestFor('/revenue?')!.url, 'http://localhost')
      expect(url.searchParams.get('page')).toBe('1')
      expect(url.searchParams.get('category_code')).toBe('FNB')
    })
  })
})

/* --- posting a revenue line -------------------------------------------------------------- */

describe('posting a revenue line', () => {
  async function openForm() {
    const user = userEvent.setup()
    renderFinancials()
    await screen.findByText('Restaurant covers')
    await user.click(screen.getByRole('button', { name: /Post revenue line/ }))
    return user
  }

  it('sends exactly the documented body, with the amount as a string', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/revenue', { status: 201, body: revenue({ amount: '42.50' }) })

    await user.selectOptions(screen.getByLabelText('Category'), 'FNB')
    await user.type(screen.getByLabelText('Amount'), '42.50')
    await user.click(screen.getByRole('button', { name: 'Review line' }))
    await user.click(screen.getByRole('button', { name: 'Yes, post this line' }))

    await waitFor(() => {
      expect(requestsFor('/revenue', 'POST')).toHaveLength(1)
    })
    const body = JSON.parse(requestsFor('/revenue', 'POST')[0]!.body!) as Record<string, unknown>
    expect(body).toEqual({
      category_code: 'FNB',
      revenue_date: TODAY,
      amount: '42.50',
      currency: 'EUR',
    })
    // A string, not a number that happens to print the same way.
    expect(typeof body.amount).toBe('string')
    // Blank optionals are omitted, not sent empty: `extra="forbid"` is unforgiving.
    expect(body).not.toHaveProperty('tax_amount')
    expect(body).not.toHaveProperty('booking_public_id')
    expect(body).not.toHaveProperty('description')
  })

  it('sends the optional fields that were filled in', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/revenue', { status: 201, body: revenue() })

    await user.selectOptions(screen.getByLabelText('Category'), 'SPA')
    await user.type(screen.getByLabelText('Amount'), '90')
    await user.type(screen.getByLabelText('Tax (optional)'), '9.90')
    await user.type(screen.getByLabelText('Booking (optional)'), BOOKING_ID)
    await user.type(screen.getByLabelText('Reference (optional)'), 'S58-UI')
    await user.click(screen.getByRole('button', { name: 'Review line' }))
    await user.click(screen.getByRole('button', { name: 'Yes, post this line' }))

    await waitFor(() => {
      expect(requestsFor('/revenue', 'POST')).toHaveLength(1)
    })
    const body = JSON.parse(requestsFor('/revenue', 'POST')[0]!.body!) as Record<string, unknown>
    expect(body).toMatchObject({
      category_code: 'SPA',
      amount: '90',
      tax_amount: '9.90',
      booking_public_id: BOOKING_ID,
      reference: 'S58-UI',
    })
    // The amount is sent as typed -- not padded to two decimal places by this application,
    // which does not own the column's scale.
    expect(body.amount).toBe('90')
  })

  it('does not post until the confirmation is given', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/revenue', { status: 201, body: revenue() })

    await user.selectOptions(screen.getByLabelText('Category'), 'FNB')
    await user.type(screen.getByLabelText('Amount'), '10.00')
    await user.click(screen.getByRole('button', { name: 'Review line' }))

    expect(requestsFor('/revenue', 'POST')).toHaveLength(0)
    expect(screen.getByText(/cannot be edited or deleted/)).toBeInTheDocument()
  })

  it('names a negative amount as a correction before it is posted', async () => {
    const user = await openForm()

    await user.selectOptions(screen.getByLabelText('Category'), 'FNB')
    await user.type(screen.getByLabelText('Amount'), '-25.00')
    await user.click(screen.getByRole('button', { name: 'Review line' }))

    expect(screen.getByText(/as a correction/)).toBeInTheDocument()
  })

  it('refuses a negative tax at the field, without a request', async () => {
    const user = await openForm()

    await user.selectOptions(screen.getByLabelText('Category'), 'FNB')
    await user.type(screen.getByLabelText('Amount'), '10.00')
    await user.type(screen.getByLabelText('Tax (optional)'), '-1.00')
    await user.click(screen.getByRole('button', { name: 'Review line' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Tax cannot be negative')
    expect(requestsFor('/revenue', 'POST')).toHaveLength(0)
  })

  it('refuses more precision than the column has', async () => {
    const user = await openForm()

    await user.selectOptions(screen.getByLabelText('Category'), 'FNB')
    await user.type(screen.getByLabelText('Amount'), '1.555')
    await user.click(screen.getByRole('button', { name: 'Review line' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('at most two decimal places')
    expect(requestsFor('/revenue', 'POST')).toHaveLength(0)
  })

  it('posts once when the confirmation is clicked twice', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/revenue', { status: 201, body: revenue() })

    await user.selectOptions(screen.getByLabelText('Category'), 'FNB')
    await user.type(screen.getByLabelText('Amount'), '10.00')
    await user.click(screen.getByRole('button', { name: 'Review line' }))

    const confirm = screen.getByRole('button', { name: 'Yes, post this line' })
    await Promise.all([user.click(confirm), user.click(confirm)])

    await waitFor(() => {
      expect(requestsFor('/revenue', 'POST').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/revenue', 'POST')).toHaveLength(1)
  })

  it('re-reads the journal and the totals after the server accepts', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/revenue', { status: 201, body: revenue() })

    const listsBefore = requestsFor('/revenue?').length
    const totalsBefore = requestsFor('/analytics/revenue-by-category').length

    await user.selectOptions(screen.getByLabelText('Category'), 'FNB')
    await user.type(screen.getByLabelText('Amount'), '10.00')
    await user.click(screen.getByRole('button', { name: 'Review line' }))
    await user.click(screen.getByRole('button', { name: 'Yes, post this line' }))

    await waitFor(() => {
      expect(requestsFor('/revenue?').length).toBeGreaterThan(listsBefore)
      expect(requestsFor('/analytics/revenue-by-category').length).toBeGreaterThan(totalsBefore)
    })
  })

  it('says so when the posted line falls outside the period on screen', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/revenue', {
      status: 201,
      body: revenue({ revenue_date: '2025-01-15', amount: '10.00' }),
    })

    await user.selectOptions(screen.getByLabelText('Category'), 'FNB')
    await user.type(screen.getByLabelText('Amount'), '10.00')
    await user.click(screen.getByRole('button', { name: 'Review line' }))
    await user.click(screen.getByRole('button', { name: 'Yes, post this line' }))

    expect(
      await screen.findByText(/falls outside the period shown/),
    ).toBeInTheDocument()
  })

  it('adds no row and does not retry when the server refuses', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/revenue', {
      status: 403,
      body: { error: { code: 'FORBIDDEN', message: 'Requires staff role.' } },
    })

    await user.selectOptions(screen.getByLabelText('Category'), 'FNB')
    await user.type(screen.getByLabelText('Amount'), '999.00')
    await user.click(screen.getByRole('button', { name: 'Review line' }))
    await user.click(screen.getByRole('button', { name: 'Yes, post this line' }))

    expect(await screen.findByText('Nothing was posted')).toBeInTheDocument()
    expect(screen.getByText(/needs the staff role/)).toBeInTheDocument()
    // Not the read-refusal copy: this account can read the journal it was refused a write to.
    expect(visibleText()).not.toMatch(/permission to view/)
    // Exactly one attempt, and no optimistic row for the amount that was refused.
    expect(requestsFor('/revenue', 'POST')).toHaveLength(1)
    // The figure is still in the field it was typed into; what must not exist is a row for
    // it. An optimistic line would appear in the journal below.
    const journal = screen.getByRole('table', { name: /Revenue lines, newest first/ })
    expect(within(journal).queryByText(/999/)).not.toBeInTheDocument()
  })

  it('never renders the backend’s own words on a refusal', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/revenue', {
      status: 500,
      body: {
        error: {
          code: 'INTERNAL',
          message:
            'psycopg.errors.CheckViolation: ck_revenue_tax_amount_non_negative on table revenue',
        },
      },
    })

    await user.selectOptions(screen.getByLabelText('Category'), 'FNB')
    await user.type(screen.getByLabelText('Amount'), '10.00')
    await user.click(screen.getByRole('button', { name: 'Review line' }))
    await user.click(screen.getByRole('button', { name: 'Yes, post this line' }))

    expect(await screen.findByText('Nothing was posted')).toBeInTheDocument()
    for (const leak of ['psycopg', 'ck_revenue', 'CheckViolation', 'table revenue']) {
      expect(visibleText()).not.toContain(leak)
    }
  })

  it('clears a stale refusal as soon as the next attempt is typed', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/revenue', {
      status: 500,
      body: { error: { code: 'INTERNAL_ERROR', message: 'Internal server error.' } },
    })

    await user.selectOptions(screen.getByLabelText('Category'), 'FNB')
    await user.type(screen.getByLabelText('Amount'), '10.00')
    await user.click(screen.getByRole('button', { name: 'Review line' }))
    await user.click(screen.getByRole('button', { name: 'Yes, post this line' }))
    expect(await screen.findByText('Nothing was posted')).toBeInTheDocument()

    // A banner about the previous attempt must not sit above the next one. Going Back
    // returns to the fields; the first keystroke is what clears the stale refusal.
    await user.click(screen.getByRole('button', { name: 'Back' }))
    await user.type(screen.getByLabelText('Amount'), '5')
    await waitFor(() => {
      expect(screen.queryByText('Nothing was posted')).not.toBeInTheDocument()
    })
  })
})

/* --- posting an expense line ------------------------------------------------------------- */

describe('posting an expense line', () => {
  async function openExpenseForm() {
    const user = userEvent.setup()
    renderFinancials()
    await screen.findByText('Restaurant covers')
    await user.click(screen.getByRole('radio', { name: 'Expenses' }))
    await screen.findByText('Electricity, August')
    await user.click(screen.getByRole('button', { name: /Post expense line/ }))
    return user
  }

  it('offers no booking field, because the column does not exist', async () => {
    await openExpenseForm()
    expect(screen.queryByLabelText(/Booking/)).not.toBeInTheDocument()
  })

  it('sends is_recurring and the interval together, or neither', async () => {
    const user = await openExpenseForm()
    fetchStub.on('POST', '/expenses', { status: 201, body: expense() })

    await user.selectOptions(screen.getByLabelText('Category'), 'UTILITIES')
    await user.type(screen.getByLabelText('Amount'), '80.00')
    await user.click(screen.getByRole('button', { name: 'Review line' }))
    await user.click(screen.getByRole('button', { name: 'Yes, post this line' }))

    await waitFor(() => {
      expect(requestsFor('/expenses', 'POST')).toHaveLength(1)
    })
    const plain = JSON.parse(requestsFor('/expenses', 'POST')[0]!.body!) as Record<string, unknown>
    expect(plain).not.toHaveProperty('is_recurring')
    expect(plain).not.toHaveProperty('recurrence_interval')
  })

  it('sends both keys when the cost repeats', async () => {
    const user = await openExpenseForm()
    fetchStub.on('POST', '/expenses', { status: 201, body: expense() })

    await user.selectOptions(screen.getByLabelText('Category'), 'PAYROLL')
    await user.type(screen.getByLabelText('Amount'), '5000.00')
    await user.click(screen.getByRole('checkbox', { name: 'This cost repeats' }))
    await user.selectOptions(screen.getByLabelText('Interval'), 'quarterly')
    await user.click(screen.getByRole('button', { name: 'Review line' }))
    await user.click(screen.getByRole('button', { name: 'Yes, post this line' }))

    await waitFor(() => {
      expect(requestsFor('/expenses', 'POST')).toHaveLength(1)
    })
    const body = JSON.parse(requestsFor('/expenses', 'POST')[0]!.body!) as Record<string, unknown>
    expect(body).toMatchObject({ is_recurring: true, recurrence_interval: 'quarterly' })
  })

  it('shows the recurrence of a line the server sent', async () => {
    fetchStub.on('GET', '/expenses?', {
      body: page([expense({ is_recurring: true, recurrence_interval: 'monthly' })]),
    })
    const user = userEvent.setup()
    renderFinancials()
    await screen.findByText('Restaurant covers')
    await user.click(screen.getByRole('radio', { name: 'Expenses' }))

    expect(await screen.findByText('Monthly')).toBeInTheDocument()
  })
})

/* --- the two journals, and the period ---------------------------------------------------- */

describe('the page as a whole', () => {
  it('loads one journal at a time', async () => {
    const user = userEvent.setup()
    renderFinancials()
    await screen.findByText('Restaurant covers')

    // Revenue is showing; nothing has asked for expenses.
    expect(requestsFor('/expenses?')).toHaveLength(0)

    await user.click(screen.getByRole('radio', { name: 'Expenses' }))
    await screen.findByText('Electricity, August')
    expect(requestsFor('/expenses?')).toHaveLength(1)
  })

  it('does not leave the previous journal on screen under the new heading', async () => {
    const user = userEvent.setup()
    renderFinancials()
    await screen.findByText('Restaurant covers')

    await user.click(screen.getByRole('radio', { name: 'Expenses' }))
    // The remount is what guarantees this: no revenue row survives the switch.
    await waitFor(() => {
      expect(screen.queryByText('Restaurant covers')).not.toBeInTheDocument()
    })
  })

  it('sends the same range to the journal and to the totals', async () => {
    renderFinancials()
    await screen.findByText('Restaurant covers')

    const journal = new URL(lastRequestFor('/revenue?')!.url, 'http://localhost')
    const totals = new URL(
      lastRequestFor('/analytics/revenue-by-category')!.url,
      'http://localhost',
    )
    expect(journal.searchParams.get('date_from')).toBe(totals.searchParams.get('date_from'))
    expect(journal.searchParams.get('date_to')).toBe(totals.searchParams.get('date_to'))
  })

  it('changing the period refetches both with the new range', async () => {
    const user = userEvent.setup()
    renderFinancials()
    await screen.findByText('Restaurant covers')

    const before = new URL(lastRequestFor('/revenue?')!.url, 'http://localhost').searchParams.get(
      'date_from',
    )
    await user.click(screen.getByRole('radio', { name: 'Last 90 days' }))

    await waitFor(() => {
      const after = new URL(lastRequestFor('/revenue?')!.url, 'http://localhost').searchParams.get(
        'date_from',
      )
      expect(after).not.toBe(before)
    })
    const totals = new URL(
      lastRequestFor('/analytics/revenue-by-category')!.url,
      'http://localhost',
    )
    expect(totals.searchParams.get('date_from')).toBe(
      new URL(lastRequestFor('/revenue?')!.url, 'http://localhost').searchParams.get('date_from'),
    )
  })

  it('resolves the period in the hotel’s zone and says which zone that is', async () => {
    renderFinancials()

    expect(await screen.findByText(new RegExp(TEST_HOTEL.timezone))).toBeInTheDocument()
  })

  it('offers no control for editing or deleting a line', async () => {
    renderFinancials()
    await screen.findByText('Restaurant covers')

    for (const name of [/edit/i, /delete/i, /remove/i, /undo/i, /void/i]) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
  })

  it('puts no credential in the rendered page', async () => {
    renderFinancials()
    await screen.findByText('Restaurant covers')

    expect(visibleText()).not.toContain('header.payload.signature-test-only')
    expect(document.body.innerHTML).not.toContain('header.payload.signature-test-only')
  })

  it('exposes no internal identifier', async () => {
    fetchStub.on('GET', '/revenue?', {
      body: page([revenue({ booking_public_id: BOOKING_ID })]),
    })
    renderFinancials()
    await screen.findByText('Restaurant covers')

    // The only identifier anywhere is the booking's UUID, and only inside an href.
    const links = screen.getAllByRole('link').map((node) => node.getAttribute('href') ?? '')
    for (const href of links) {
      for (const segment of href.split('/')) {
        expect(segment).not.toMatch(/^\d+$/)
      }
    }
  })
})
