import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PlatformPage } from '@/pages/PlatformPage'
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
import type { PlatformAuditEvent } from '@/types/platform'

/**
 * The shared catalogues and the platform audit trail.
 *
 * Mocked at `fetch`, so the method, the URL, the query surface and the **exact request body**
 * are observable. Four things this file is mostly about:
 *
 * * these resources belong to **no hotel** — no request may carry a hotel segment or a
 *   `hotel_public_id`;
 * * **reads need only a session, writes need a platform administrator grant**, which is not a
 *   hotel role and is not ranked against one, so the 403 copy must not talk about owners;
 * * the `is_active` filter is real on the two category routes and **absent** on amenities,
 *   and sending it there would be silently ignored rather than refused;
 * * the audit trail is **append-only**, so the page offers nothing that could change it.
 */

/* --- fixtures --------------------------------------------------------------------------- */

function page(items: readonly unknown[], total = items.length, pages = 1) {
  return { items, total, page: 1, page_size: 20, pages }
}

function errorBody(code: string, message: string) {
  return { error: { code, message, details: [] } }
}

const AMENITIES = [
  { code: 'SEA_VIEW', name: 'Sea view', category: 'Outlook' },
  { code: 'WIFI', name: 'Wireless internet', category: null },
]

const REVENUE_CATEGORIES = [
  { code: 'FNB', name: 'Food and beverage', is_room_revenue: false, is_active: true },
  { code: 'ROOM_LEDGER', name: 'Room ledger', is_room_revenue: true, is_active: true },
  { code: 'SPA', name: 'Spa', is_room_revenue: false, is_active: false },
]

const EXPENSE_CATEGORIES = [
  { code: 'PAYROLL', name: 'Payroll', is_fixed_cost: true, is_active: true },
  { code: 'SUPPLIES', name: 'Supplies', is_fixed_cost: false, is_active: true },
]

function auditEvent(overrides: Partial<PlatformAuditEvent> = {}): PlatformAuditEvent {
  return {
    public_id: '5f1b0a42-6d33-4f7e-9a21-0c4b8e7d1100',
    occurred_at: '2026-09-11T18:20:00+03:00',
    action: 'amenity.created',
    resource_type: 'amenity',
    resource_reference: 'SEA_VIEW',
    actor_public_id: TEST_USER.public_id,
    actor_email: TEST_USER.email,
    request_id: 'req-0a1b2c3d',
    details: { name: 'Sea view' },
    ...overrides,
  }
}

/* --- harness ---------------------------------------------------------------------------- */

let fetchStub: FetchStub

beforeEach(() => {
  fetchStub = installFetchStub()
  seedStoredToken()
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage([TEST_HOTEL], 1) })
  fetchStub.on('GET', '/amenities?page=', { body: page(AMENITIES) })
  fetchStub.on('GET', '/revenue-categories?page=', { body: page(REVENUE_CATEGORIES) })
  fetchStub.on('GET', '/expense-categories?page=', { body: page(EXPENSE_CATEGORIES) })
  fetchStub.on('GET', '/platform/audit-events?page=', { body: page([auditEvent()]) })
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
  vi.unstubAllGlobals()
})

function renderPage() {
  return renderWithAuth([{ path: ROUTES.platform, element: <PlatformPage /> }], {
    initialEntries: [ROUTES.platform],
  })
}

function requestsFor(fragment: string, method = 'GET') {
  return fetchStub.calls.filter((call) => call.method === method && call.url.includes(fragment))
}

function urlOf(fragment: string, method = 'GET') {
  const matches = requestsFor(fragment, method)
  return new URL(matches[matches.length - 1]!.url, 'http://localhost')
}

function bodyOf(fragment: string, method: string): Record<string, unknown> {
  const matches = requestsFor(fragment, method)
  return JSON.parse(matches[matches.length - 1]!.body!) as Record<string, unknown>
}

async function openTab(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole('tab', { name }))
}

/* --- reading ----------------------------------------------------------------------------- */

describe('the shared catalogues', () => {
  it('reads one catalogue on arrival, and no others', async () => {
    renderPage()
    await screen.findByText('Sea view')

    expect(
      fetchStub.calls.map((c) => new URL(c.url, 'http://localhost').pathname).sort(),
    ).toEqual(['/api/v1/amenities', '/api/v1/auth/me', '/api/v1/hotels'])
    // The other two catalogues are not fetched until their tab is opened.
    expect(requestsFor('/revenue-categories')).toHaveLength(0)
    expect(requestsFor('/expense-categories')).toHaveLength(0)
  })

  it('carries no hotel anywhere in the URL', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')
    await openTab(user, 'Revenue categories')
    await screen.findByText('Food and beverage')

    for (const call of fetchStub.calls) {
      const path = new URL(call.url, 'http://localhost').pathname
      if (path.includes('amenities') || path.includes('categories')) {
        expect(path).not.toContain('/hotels/')
        expect(path).not.toContain(TEST_HOTEL.public_id)
      }
    }
  })

  it('asks for amenities with a page and nothing else', async () => {
    renderPage()
    await screen.findByText('Sea view')

    const url = urlOf('/amenities?page=')
    expect(url.pathname).toBe('/api/v1/amenities')
    expect([...url.searchParams.keys()].sort()).toEqual(['page', 'page_size'])
  })

  it('never sends is_active to the amenity route, which has no such filter', async () => {
    renderPage()
    await screen.findByText('Sea view')

    // The backend would ignore it silently, which is worse than refusing it: the control
    // would look like it worked. So the page offers none.
    expect(screen.queryByLabelText('Filter by status')).not.toBeInTheDocument()
    for (const call of requestsFor('/amenities')) {
      expect(call.url).not.toContain('is_active')
    }
  })

  it('offers the status filter on the category routes, which do have one', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')
    await openTab(user, 'Revenue categories')
    await screen.findByText('Food and beverage')

    await user.selectOptions(screen.getByLabelText('Filter by status'), 'retired')

    await waitFor(() => {
      expect(urlOf('/revenue-categories?page=').searchParams.get('is_active')).toBe('false')
    })
  })

  it('omits the filter entirely when it is set to all', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')
    await openTab(user, 'Expense categories')
    await screen.findByText('Payroll')

    // `is_active=` is not the same request as no `is_active`, and the second means "all".
    expect(urlOf('/expense-categories?page=').searchParams.has('is_active')).toBe(false)
  })

  it('offers no search and no sort, because no route has either', async () => {
    renderPage()
    await screen.findByText('Sea view')

    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/search/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/^sort/i)).not.toBeInTheDocument()
  })

  it('never asks for a page larger than the routers allow', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')

    await user.selectOptions(screen.getByLabelText('Per page'), '100')
    await waitFor(() => {
      for (const call of requestsFor('/amenities')) {
        const size = new URL(call.url, 'http://localhost').searchParams.get('page_size')
        expect(Number(size)).toBeLessThanOrEqual(100)
      }
    })
  })

  it('names the flag column for the catalogue it belongs to', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')
    expect(screen.getByRole('columnheader', { name: 'Group' })).toBeInTheDocument()

    await openTab(user, 'Revenue categories')
    await screen.findByText('Food and beverage')
    expect(screen.getByRole('columnheader', { name: 'Room revenue' })).toBeInTheDocument()

    await openTab(user, 'Expense categories')
    await screen.findByText('Payroll')
    expect(screen.getByRole('columnheader', { name: 'Fixed cost' })).toBeInTheDocument()
  })

  it('shows a retired category as a word, and keeps it listed', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')
    await openTab(user, 'Revenue categories')
    await screen.findByText('Spa')

    const row = screen.getByText('Spa').closest('tr')!
    expect(within(row).getByText('Retired')).toBeInTheDocument()
  })

  it('renders an empty catalogue as a state, not a failure', async () => {
    fetchStub.on('GET', '/amenities?page=', { body: page([], 0, 0) })
    renderPage()

    const empty = (await screen.findByText('This catalogue is empty')).closest('div')!
    expect(within(empty).getByText(/needs a platform administrator grant/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('treats a malformed 200 as a failure', async () => {
    fetchStub.on('GET', '/amenities?page=', { body: { items: null } })
    renderPage()

    expect(await screen.findByText('Unexpected response')).toBeInTheDocument()
    expect(screen.queryByText('This catalogue is empty')).not.toBeInTheDocument()
  })

  it('renders a network failure as a failure', async () => {
    fetchStub.on('GET', '/amenities?page=', { networkError: true })
    renderPage()

    expect(await screen.findByText('Could not reach the server')).toBeInTheDocument()
  })
})

/* --- writing ------------------------------------------------------------------------------ */

describe('creating a catalogue entry', () => {
  async function openForm(user: ReturnType<typeof userEvent.setup>, label = /Add amenity/) {
    await user.click(screen.getByRole('button', { name: label }))
  }

  it('posts the documented body for an amenity', async () => {
    const user = userEvent.setup()
    fetchStub.on('POST', '/amenities', {
      status: 201,
      body: { code: 'POOL', name: 'Pool', category: null },
    })
    renderPage()
    await screen.findByText('Sea view')
    await openForm(user)

    await user.type(screen.getByLabelText('Code'), 'pool')
    await user.type(screen.getByLabelText('Name'), 'Pool')
    await user.click(screen.getByRole('button', { name: 'Create entry' }))

    await waitFor(() => {
      expect(requestsFor('/amenities', 'POST')).toHaveLength(1)
    })
    expect(urlOf('/amenities', 'POST').pathname).toBe('/api/v1/amenities')
    const body = bodyOf('/amenities', 'POST')
    // As typed; the schema upper-cases it.
    expect(body).toEqual({ code: 'pool', name: 'Pool', category: null })
    // The amenity schema has no such fields, and each would be a 422.
    expect(body).not.toHaveProperty('is_active')
    expect(body).not.toHaveProperty('hotel_public_id')
  })

  it('posts the documented body for a revenue category', async () => {
    const user = userEvent.setup()
    fetchStub.on('POST', '/revenue-categories', {
      status: 201,
      body: { code: 'EVENTS', name: 'Events', is_room_revenue: false, is_active: true },
    })
    renderPage()
    await screen.findByText('Sea view')
    await openTab(user, 'Revenue categories')
    await screen.findByText('Food and beverage')
    await openForm(user, /Add revenue category/)

    await user.type(screen.getByLabelText('Code'), 'EVENTS')
    await user.type(screen.getByLabelText('Name'), 'Events')
    await user.click(screen.getByRole('button', { name: 'Create entry' }))

    await waitFor(() => {
      expect(requestsFor('/revenue-categories', 'POST')).toHaveLength(1)
    })
    const body = bodyOf('/revenue-categories', 'POST')
    expect(body).toEqual({
      code: 'EVENTS',
      name: 'Events',
      is_room_revenue: false,
      is_active: true,
    })
    // The expense catalogue's flag, which this schema would refuse.
    expect(body).not.toHaveProperty('is_fixed_cost')
  })

  it('posts the expense catalogue’s own flag, not the revenue one', async () => {
    const user = userEvent.setup()
    fetchStub.on('POST', '/expense-categories', {
      status: 201,
      body: { code: 'LAUNDRY', name: 'Laundry', is_fixed_cost: true, is_active: true },
    })
    renderPage()
    await screen.findByText('Sea view')
    await openTab(user, 'Expense categories')
    await screen.findByText('Payroll')
    await openForm(user, /Add expense category/)

    await user.type(screen.getByLabelText('Code'), 'LAUNDRY')
    await user.type(screen.getByLabelText('Name'), 'Laundry')
    await user.click(screen.getByLabelText('This is a fixed cost'))
    await user.click(screen.getByRole('button', { name: 'Create entry' }))

    await waitFor(() => {
      expect(requestsFor('/expense-categories', 'POST')).toHaveLength(1)
    })
    const body = bodyOf('/expense-categories', 'POST')
    expect(body.is_fixed_cost).toBe(true)
    expect(body).not.toHaveProperty('is_room_revenue')
  })

  it('refuses a malformed code without a request', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')
    await openForm(user)

    await user.type(screen.getByLabelText('Code'), '_leading')
    await user.type(screen.getByLabelText('Name'), 'Nope')
    await user.click(screen.getByRole('button', { name: 'Create entry' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('starts with a letter or digit')
    expect(requestsFor('/amenities', 'POST')).toHaveLength(0)
  })

  it('explains a 409 as a code already in use across the installation', async () => {
    const user = userEvent.setup()
    fetchStub.on('POST', '/amenities', {
      status: 409,
      body: errorBody('CONFLICT', 'An amenity with that code already exists.'),
    })
    renderPage()
    await screen.findByText('Sea view')
    await openForm(user)

    await user.type(screen.getByLabelText('Code'), 'WIFI')
    await user.type(screen.getByLabelText('Name'), 'Wifi again')
    await user.click(screen.getByRole('button', { name: 'Create entry' }))

    const banner = await screen.findByRole('alert')
    expect(within(banner).getByText('Nothing was created')).toBeInTheDocument()
    expect(within(banner).getByText(/across the whole installation/)).toBeInTheDocument()
  })

  it('names the platform grant on a 403, and never a hotel role', async () => {
    const user = userEvent.setup()
    fetchStub.on('POST', '/amenities', {
      status: 403,
      body: errorBody('FORBIDDEN', 'This operation requires platform administrator privileges.'),
    })
    renderPage()
    await screen.findByText('Sea view')
    await openForm(user)

    await user.type(screen.getByLabelText('Code'), 'POOL')
    await user.type(screen.getByLabelText('Name'), 'Pool')
    await user.click(screen.getByRole('button', { name: 'Create entry' }))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('Nothing was created')).toBeInTheDocument()
    expect(within(alert).getByText(/platform administrator grant/)).toBeInTheDocument()
    expect(within(alert).getByText(/separate authority from any hotel role/)).toBeInTheDocument()
    expect(alert.textContent).not.toMatch(/\bowner\b|\bmanager\b/)
  })

  it('re-reads the catalogue after the server accepts', async () => {
    const user = userEvent.setup()
    fetchStub.on('POST', '/amenities', {
      status: 201,
      body: { code: 'POOL', name: 'Pool', category: null },
    })
    renderPage()
    await screen.findByText('Sea view')
    await openForm(user)

    const before = requestsFor('/amenities?page=').length
    await user.type(screen.getByLabelText('Code'), 'POOL')
    await user.type(screen.getByLabelText('Name'), 'Pool')
    await user.click(screen.getByRole('button', { name: 'Create entry' }))

    await waitFor(() => {
      expect(requestsFor('/amenities?page=').length).toBeGreaterThan(before)
    })
    expect(await screen.findByText(/Entry created/)).toBeInTheDocument()
  })

  it('sends one request when submitted twice', async () => {
    const user = userEvent.setup()
    fetchStub.on('POST', '/amenities', {
      status: 201,
      body: { code: 'POOL', name: 'Pool', category: null },
    })
    renderPage()
    await screen.findByText('Sea view')
    await openForm(user)

    await user.type(screen.getByLabelText('Code'), 'POOL')
    await user.type(screen.getByLabelText('Name'), 'Pool')
    const submit = screen.getByRole('button', { name: 'Create entry' })
    await Promise.all([user.click(submit), user.click(submit)])

    await waitFor(() => {
      expect(requestsFor('/amenities', 'POST').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/amenities', 'POST')).toHaveLength(1)
  })

  it('never renders the backend’s own words on a refusal', async () => {
    const user = userEvent.setup()
    fetchStub.on('POST', '/amenities', {
      status: 500,
      body: errorBody(
        'INTERNAL',
        'psycopg.errors.UniqueViolation: uq_amenities_code on table amenities',
      ),
    })
    renderPage()
    await screen.findByText('Sea view')
    await openForm(user)

    await user.type(screen.getByLabelText('Code'), 'POOL')
    await user.type(screen.getByLabelText('Name'), 'Pool')
    await user.click(screen.getByRole('button', { name: 'Create entry' }))

    expect(await screen.findByText('Nothing was created')).toBeInTheDocument()
    for (const leak of ['psycopg', 'uq_amenities', 'UniqueViolation', 'table amenities']) {
      expect(document.body.textContent ?? '').not.toContain(leak)
    }
  })
})

describe('editing a catalogue entry', () => {
  it('shows the code read-only and patches without it', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/amenities/', {
      body: { code: 'WIFI', name: 'Wi-Fi', category: null },
    })
    renderPage()
    await screen.findByText('Sea view')

    await user.click(screen.getByRole('button', { name: 'Edit WIFI' }))
    const code = screen.getByLabelText('Code')
    expect(code).toHaveValue('WIFI')
    expect(code).toHaveAttribute('readonly')
    expect(screen.getByText(/not something the API allows/)).toBeInTheDocument()

    await user.clear(screen.getByLabelText('Name'))
    await user.type(screen.getByLabelText('Name'), 'Wi-Fi')
    await user.click(screen.getByRole('button', { name: 'Save entry' }))

    await waitFor(() => {
      expect(requestsFor('/amenities/', 'PATCH')).toHaveLength(1)
    })
    expect(urlOf('/amenities/', 'PATCH').pathname).toBe('/api/v1/amenities/WIFI')
    const body = bodyOf('/amenities/', 'PATCH')
    expect(body).toEqual({ name: 'Wi-Fi', category: null })
    // Create-only, and a 422 on PATCH.
    expect(body).not.toHaveProperty('code')
  })

  it('clears an emptied optional with an explicit null', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/amenities/', {
      body: { code: 'SEA_VIEW', name: 'Sea view', category: null },
    })
    renderPage()
    await screen.findByText('Sea view')

    await user.click(screen.getByRole('button', { name: 'Edit SEA_VIEW' }))
    await user.clear(screen.getByLabelText('Group (optional)'))
    await user.click(screen.getByRole('button', { name: 'Save entry' }))

    await waitFor(() => {
      expect(requestsFor('/amenities/', 'PATCH')).toHaveLength(1)
    })
    const body = bodyOf('/amenities/', 'PATCH')
    expect('category' in body).toBe(true)
    expect(body.category).toBeNull()
  })

  it('retires a category through is_active rather than deleting it', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/revenue-categories/', {
      body: { code: 'FNB', name: 'Food and beverage', is_room_revenue: false, is_active: false },
    })
    renderPage()
    await screen.findByText('Sea view')
    await openTab(user, 'Revenue categories')
    await screen.findByText('Food and beverage')

    await user.click(screen.getByRole('button', { name: 'Edit FNB' }))
    await user.click(screen.getByLabelText('Available for new postings'))
    await user.click(screen.getByRole('button', { name: 'Save entry' }))

    await waitFor(() => {
      expect(requestsFor('/revenue-categories/', 'PATCH')).toHaveLength(1)
    })
    expect(bodyOf('/revenue-categories/', 'PATCH').is_active).toBe(false)
  })
})

describe('deleting a catalogue entry', () => {
  it('asks first, and says the deletion is installation-wide', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')

    await user.click(screen.getByRole('button', { name: 'Delete WIFI' }))
    expect(requestsFor('/amenities/', 'DELETE')).toHaveLength(0)

    const confirm = screen.getByRole('group', { name: 'Confirm deleting this entry' })
    expect(within(confirm).getByText(/WIFI/)).toBeInTheDocument()
    expect(
      within(confirm).getByText(/removed for/, { exact: false }),
    ).toBeInTheDocument()
  })

  it('deletes only after the confirmation, at the right URL', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/amenities/', { status: 204 })
    renderPage()
    await screen.findByText('Sea view')

    await user.click(screen.getByRole('button', { name: 'Delete WIFI' }))
    await user.click(screen.getByRole('button', { name: 'Yes, delete from every property' }))

    await waitFor(() => {
      expect(requestsFor('/amenities/', 'DELETE')).toHaveLength(1)
    })
    expect(urlOf('/amenities/', 'DELETE').pathname).toBe('/api/v1/amenities/WIFI')
    expect(await screen.findByText(/Entry deleted/)).toBeInTheDocument()
  })

  it('cancels without sending anything', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')

    await user.click(screen.getByRole('button', { name: 'Delete WIFI' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(requestsFor('/amenities/', 'DELETE')).toHaveLength(0)
  })

  it('explains a 409 and points at retiring instead', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/revenue-categories/', {
      status: 409,
      body: errorBody('CONFLICT', 'Revenue entries still reference this category.'),
    })
    renderPage()
    await screen.findByText('Sea view')
    await openTab(user, 'Revenue categories')
    await screen.findByText('Food and beverage')

    await user.click(screen.getByRole('button', { name: 'Delete FNB' }))
    await user.click(screen.getByRole('button', { name: 'Yes, delete from every property' }))

    expect(await screen.findByText('The entry was not deleted')).toBeInTheDocument()
    expect(screen.getByText(/Retire it instead/)).toBeInTheDocument()
    // Still listed, because it still exists.
    expect(screen.getByText('Food and beverage')).toBeInTheDocument()
  })

  it('sends one request when the confirmation is clicked twice', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/amenities/', { status: 204 })
    renderPage()
    await screen.findByText('Sea view')

    await user.click(screen.getByRole('button', { name: 'Delete WIFI' }))
    const confirm = screen.getByRole('button', { name: 'Yes, delete from every property' })
    await Promise.all([user.click(confirm), user.click(confirm)])

    await waitFor(() => {
      expect(requestsFor('/amenities/', 'DELETE').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/amenities/', 'DELETE')).toHaveLength(1)
  })
})

/* --- the audit trail ------------------------------------------------------------------------ */

describe('the platform audit trail', () => {
  async function load(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole('button', { name: /Load audit trail/ }))
  }

  it('is not fetched until it is asked for', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')

    expect(requestsFor('/platform/audit-events')).toHaveLength(0)
    expect(screen.getByText(/Not loaded yet/)).toBeInTheDocument()

    await load(user)
    await waitFor(() => {
      expect(requestsFor('/platform/audit-events')).toHaveLength(1)
    })
  })

  it('renders a 403 as a state about a grant, not as a fault', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/platform/audit-events?page=', {
      status: 403,
      body: errorBody('FORBIDDEN', 'This operation requires platform administrator privileges.'),
    })
    renderPage()
    await screen.findByText('Sea view')
    await load(user)

    expect(
      await screen.findByText('The platform audit trail needs a platform administrator grant'),
    ).toBeInTheDocument()
    expect(screen.getByText(/not a junior platform administrator/)).toBeInTheDocument()
    // The catalogues above are unaffected and still listed.
    expect(screen.getByText('Sea view')).toBeInTheDocument()
    // A refusal is not an alert: it is the ordinary answer for most accounts.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows what the server recorded, as text', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')
    await load(user)

    const trail = await screen.findByRole('table', {
      name: /Changes to the resources every property/,
    })
    expect(within(trail).getByText('amenity.created')).toBeInTheDocument()
    expect(within(trail).getByText('SEA_VIEW')).toBeInTheDocument()
    expect(within(trail).getByText('req-0a1b2c3d')).toBeInTheDocument()
    expect(within(trail).getByText('name: Sea view')).toBeInTheDocument()
  })

  it('offers the two filters the endpoint documents, and no others', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')
    await load(user)
    await screen.findByRole('table', { name: /Changes to the resources every property/ })

    await user.selectOptions(screen.getByLabelText('Filter by action'), 'amenity.deleted')
    await waitFor(() => {
      expect(urlOf('/platform/audit-events?page=').searchParams.get('action')).toBe(
        'amenity.deleted',
      )
    })

    await user.selectOptions(screen.getByLabelText('Filter by resource'), 'revenue_category')
    await waitFor(() => {
      expect(urlOf('/platform/audit-events?page=').searchParams.get('resource_type')).toBe(
        'revenue_category',
      )
    })
  })

  it('offers only the actions that can occur at platform scope', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')
    await load(user)
    await screen.findByRole('table', { name: /Changes to the resources every property/ })

    const options = within(screen.getByLabelText('Filter by action'))
      .getAllByRole('option')
      .map((o) => o.getAttribute('value'))
    // A hotel-only action would return an empty page forever rather than an error, which is
    // a worse control than none.
    expect(options).not.toContain('booking.created')
    expect(options).not.toContain('membership.created')
    expect(options).toContain('auth.password_changed')
    expect(options).toContain('expense_category.deleted')
  })

  it('offers nothing that could change an event', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')
    await load(user)
    await screen.findByRole('table', { name: /Changes to the resources every property/ })

    const trail = screen.getByRole('table', { name: /Changes to the resources every property/ })
    expect(within(trail).queryAllByRole('button')).toHaveLength(0)
    expect(screen.getByText(/append-only/)).toBeInTheDocument()
  })

  it('renders an empty result as a state', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/platform/audit-events?page=', { body: page([], 0, 0) })
    renderPage()
    await screen.findByText('Sea view')
    await load(user)

    expect(await screen.findByText('No events match')).toBeInTheDocument()
  })

  it('treats a malformed 200 as a failure', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/platform/audit-events?page=', { body: { items: 'nope' } })
    renderPage()
    await screen.findByText('Sea view')
    await load(user)

    expect(await screen.findByText('Unexpected response')).toBeInTheDocument()
  })
})

/* --- safety and accessibility ---------------------------------------------------------------- */

describe('safety', () => {
  const PAYLOAD = '<img src=x onerror="document.title=\'pwned\'">'

  it('renders markup in a catalogue name as text', async () => {
    fetchStub.on('GET', '/amenities?page=', {
      body: page([{ code: 'XSS', name: PAYLOAD, category: PAYLOAD }]),
    })
    renderPage()

    expect(await screen.findAllByText(PAYLOAD, { exact: false })).not.toHaveLength(0)
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.title).not.toBe('pwned')
    expect(document.body.innerHTML).toContain('&lt;img')
  })

  it('renders markup inside audit details as text', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/platform/audit-events?page=', {
      body: page([auditEvent({ details: { name: PAYLOAD }, resource_reference: PAYLOAD })]),
    })
    renderPage()
    await screen.findByText('Sea view')
    await user.click(screen.getByRole('button', { name: /Load audit trail/ }))

    expect(await screen.findAllByText(PAYLOAD, { exact: false })).not.toHaveLength(0)
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.title).not.toBe('pwned')
  })

  it('puts no credential in the page and none in a URL', async () => {
    renderPage()
    await screen.findByText('Sea view')

    expect(document.body.innerHTML).not.toContain(TEST_TOKEN)
    for (const call of fetchStub.calls) {
      expect(call.url).not.toContain(TEST_TOKEN)
      expect(call.url).not.toContain('token')
    }
  })
})

describe('accessibility', () => {
  it('gives the catalogue table real column headers', async () => {
    renderPage()
    await screen.findByText('Sea view')

    const table = screen.getByRole('table', { name: /Entries in this shared catalogue/ })
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((n) => n.textContent),
    ).toEqual(['Code', 'Name', 'Group', 'Actions'])
    expect(within(table).getAllByRole('rowheader')).toHaveLength(2)
  })

  it('names each row’s controls by its code', async () => {
    renderPage()
    await screen.findByText('Sea view')

    expect(screen.getByRole('button', { name: 'Edit WIFI' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete SEA_VIEW' })).toBeInTheDocument()
    expect(screen.queryAllByRole('button', { name: 'Edit' })).toHaveLength(0)
  })

  it('exposes the catalogue selector as a real tab list', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')

    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual([
      'Amenities',
      'Revenue categories',
      'Expense categories',
    ])
    expect(screen.getByRole('tab', { name: 'Amenities' })).toHaveAttribute(
      'aria-selected',
      'true',
    )

    await user.click(screen.getByRole('tab', { name: 'Revenue categories' }))
    expect(screen.getByRole('tab', { name: 'Revenue categories' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(screen.getByRole('tabpanel')).toBeInTheDocument()
  })

  it('labels every control in the form', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Sea view')

    await user.click(screen.getByRole('button', { name: /Add amenity/ }))
    for (const label of ['Code', 'Name', 'Group (optional)']) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }
  })

  it('keeps a sane heading hierarchy', async () => {
    renderPage()
    await screen.findByText('Sea view')

    expect(screen.getByRole('heading', { level: 1, name: 'Platform' })).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 2, name: 'Shared catalogues' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 2, name: 'Platform audit trail' }),
    ).toBeInTheDocument()
  })

  it('says on the page that nothing here is hotel-scoped', async () => {
    renderPage()
    await screen.findByText('Sea view')

    expect(screen.getByText(/Nothing on this page is scoped to a hotel/)).toBeInTheDocument()
    // Said again beside the catalogue itself, where somebody about to delete an entry reads it.
    expect(screen.getAllByText(/belong to no property/).length).toBeGreaterThan(0)
  })
})

describe('the compact layout', () => {
  it('renders cards instead of a table at phone width, losing no field', async () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    )
    renderPage()
    await screen.findByText('Sea view')

    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Sea view' })).toBeInTheDocument()
    expect(screen.getByText('Outlook')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit WIFI' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete SEA_VIEW' })).toBeInTheDocument()
  })
})
