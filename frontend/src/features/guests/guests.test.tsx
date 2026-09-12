import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { GuestDetailPage } from '@/pages/GuestDetailPage'
import { GuestsPage } from '@/pages/GuestsPage'
import { GUEST_DETAIL_PATTERN, guestPath, ROUTES } from '@/router/routes'
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
import type { Guest } from '@/types/guest'

/**
 * Guest management, as the UI reads and writes it.
 *
 * Mocked at `fetch`, so the method, the URL, **every query parameter** and **the exact request
 * body** are observable. Four things this file is mostly about:
 *
 * * the query surface is `page` and `page_size` and nothing else -- an invented `search` would
 *   be silently ignored by the backend and would look like it worked;
 * * the difference between an **omitted** field and an **explicit null** on PATCH, which is
 *   the difference between leaving a value alone and erasing it;
 * * the three refusals a delete has, which are not the refusals an update has;
 * * guest-supplied text is text.
 */

/* --- fixtures --------------------------------------------------------------------------- */

const GUEST_ID = '98c235eb-5ae6-4c75-a743-836e1a2b38da'
const OTHER_GUEST_ID = '3da9427e-28e3-48d2-8d49-7f3ef570f891'

function guest(overrides: Partial<Guest> = {}): Guest {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    public_id: GUEST_ID,
    first_name: 'Elena',
    last_name: 'Papadakis',
    email: 'elena.papadakis@example.test',
    phone: '+30 210 555 0142',
    country_code: 'GR',
    preferred_language: 'el',
    date_of_birth: '1984-06-02',
    marketing_opt_in: true,
    notes: 'Prefers a high floor.',
    created_at: '2026-02-11T09:15:00+02:00',
    updated_at: '2026-09-01T14:40:00+03:00',
    ...overrides,
  }
}

function secondGuest(overrides: Partial<Guest> = {}): Guest {
  return guest({
    public_id: OTHER_GUEST_ID,
    first_name: 'Marcus',
    last_name: 'Lindqvist',
    email: null,
    phone: null,
    country_code: null,
    preferred_language: null,
    date_of_birth: null,
    marketing_opt_in: false,
    notes: null,
    ...overrides,
  })
}

function page(items: readonly unknown[], total = items.length, pages = 1, pageNumber = 1) {
  return { items, total, page: pageNumber, page_size: 20, pages }
}

/* --- harness ---------------------------------------------------------------------------- */

let fetchStub: FetchStub

/**
 * Registration order is load-bearing: the stub matches the first handler whose fragment the
 * URL contains, so `/hotels?page=` must be specific enough not to swallow
 * `/hotels/{id}/guests`. The list URL carries `?page=` and the detail URL carries `/guests/`,
 * which is what tells the two apart.
 */
beforeEach(() => {
  fetchStub = installFetchStub()
  seedStoredToken()
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage([TEST_HOTEL], 1) })
  fetchStub.on('GET', '/guests?page=', { body: page([guest(), secondGuest()], 12, 1) })
  fetchStub.on('GET', '/guests/', { body: guest() })
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
})

function renderList() {
  return renderWithAuth([{ path: ROUTES.guests, element: <GuestsPage /> }], {
    initialEntries: [ROUTES.guests],
  })
}

function renderDetail(id = GUEST_ID) {
  return renderWithAuth(
    [
      { path: ROUTES.guests, element: <GuestsPage /> },
      { path: GUEST_DETAIL_PATTERN, element: <GuestDetailPage /> },
    ],
    { initialEntries: [guestPath(id)] },
  )
}

function requestsFor(fragment: string, method = 'GET') {
  return fetchStub.calls.filter((call) => call.method === method && call.url.includes(fragment))
}

function lastRequestFor(fragment: string, method = 'GET') {
  const matches = requestsFor(fragment, method)
  return matches[matches.length - 1]
}

function visibleText(): string {
  return document.body.textContent ?? ''
}

/* --- the list --------------------------------------------------------------------------- */

describe('the guest list', () => {
  it('asks the documented endpoint with only the parameters it has', async () => {
    renderList()
    await screen.findByText(/Papadakis/)

    const request = lastRequestFor('/guests?page=')
    expect(request).toBeDefined()
    const url = new URL(request!.url, 'http://localhost')
    expect(url.pathname).toBe(`/api/v1/hotels/${TEST_HOTEL.public_id}/guests`)
    expect([...url.searchParams.keys()].sort()).toEqual(['page', 'page_size'])
    expect(url.searchParams.get('page')).toBe('1')
    expect(url.searchParams.get('page_size')).toBe('20')
    expect(request!.authorization).toMatch(/^Bearer /)
  })

  it('renders each guest’s own fields', async () => {
    renderList()

    expect(await screen.findByRole('link', { name: 'Papadakis, Elena' })).toBeInTheDocument()
    expect(screen.getByText('elena.papadakis@example.test')).toBeInTheDocument()
    expect(screen.getByText('+30 210 555 0142')).toBeInTheDocument()
    expect(screen.getByText('Opted in')).toBeInTheDocument()
    expect(screen.getByText('No consent')).toBeInTheDocument()
  })

  it('reports the count the server gave, not the rows on screen', async () => {
    renderList()

    // Two rows rendered; 12 is the server's count.
    expect(await screen.findByText(/12 guests on file/)).toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(3) // header + two guests
  })

  it('offers no search box, because the endpoint has no search', async () => {
    renderList()
    await screen.findByText(/Papadakis/)

    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/search/i)).not.toBeInTheDocument()
    expect(screen.queryByPlaceholderText(/search/i)).not.toBeInTheDocument()
    // And nothing resembling a filter or a sort, which the endpoint also lacks.
    expect(screen.queryByLabelText(/filter/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/sort/i)).not.toBeInTheDocument()
  })

  it('never sends a parameter the backend would silently ignore', async () => {
    const user = userEvent.setup()
    renderList()
    await screen.findByText(/Papadakis/)

    await user.selectOptions(screen.getByLabelText('Per page'), '100')
    await waitFor(() => {
      const url = new URL(lastRequestFor('/guests?page=')!.url, 'http://localhost')
      expect(url.searchParams.get('page_size')).toBe('100')
    })

    // Across every request this page made: only ever these two keys.
    for (const call of requestsFor('/guests?page=')) {
      const url = new URL(call.url, 'http://localhost')
      expect([...url.searchParams.keys()].sort()).toEqual(['page', 'page_size'])
    }
  })

  it('never asks for a page larger than the router allows', async () => {
    const user = userEvent.setup()
    renderList()
    await screen.findByText(/Papadakis/)

    for (const size of ['20', '50', '100']) {
      await user.selectOptions(screen.getByLabelText('Per page'), size)
      await waitFor(() => {
        const url = new URL(lastRequestFor('/guests?page=')!.url, 'http://localhost')
        // `le=100`; 101 is a 422.
        expect(Number(url.searchParams.get('page_size'))).toBeLessThanOrEqual(100)
      })
    }
  })

  it('pages through the server’s pages', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/guests?page=', { body: page([guest(), secondGuest()], 12, 3) })
    renderList()
    await screen.findByText(/Page 1 of 3/)

    await user.click(screen.getByRole('button', { name: /Next/ }))
    await waitFor(() => {
      const url = new URL(lastRequestFor('/guests?page=')!.url, 'http://localhost')
      expect(url.searchParams.get('page')).toBe('2')
    })
  })

  it('returns to the first page when the page size changes', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/guests?page=', { body: page([guest(), secondGuest()], 120, 6) })
    renderList()
    await screen.findByText(/Page 1 of 6/)

    await user.click(screen.getByRole('button', { name: /Next/ }))
    await waitFor(() => {
      expect(
        new URL(lastRequestFor('/guests?page=')!.url, 'http://localhost').searchParams.get('page'),
      ).toBe('2')
    })

    await user.selectOptions(screen.getByLabelText('Per page'), '100')
    await waitFor(() => {
      const url = new URL(lastRequestFor('/guests?page=')!.url, 'http://localhost')
      expect(url.searchParams.get('page')).toBe('1')
      expect(url.searchParams.get('page_size')).toBe('100')
    })
  })

  it('says a property has no guests without calling that a failure', async () => {
    fetchStub.on('GET', '/guests?page=', { body: page([], 0, 0) })
    renderList()

    expect(await screen.findByText('No guests on file')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('treats a 200 that is not the documented shape as a failure, not an empty list', async () => {
    fetchStub.on('GET', '/guests?page=', { body: { items: null, total: 0 } })
    renderList()

    expect(await screen.findByText('Unexpected response')).toBeInTheDocument()
    expect(screen.queryByText('No guests on file')).not.toBeInTheDocument()
  })

  it('renders a 404 as an unreachable property, not as an absence of guests', async () => {
    fetchStub.on('GET', '/guests?page=', {
      status: 404,
      body: { error: { code: 'NOT_FOUND', message: 'Hotel not found.' } },
    })
    renderList()

    expect(await screen.findByText('Hotel not available')).toBeInTheDocument()
    expect(screen.queryByText('No guests on file')).not.toBeInTheDocument()
  })

  it('renders a network failure as a failure', async () => {
    fetchStub.on('GET', '/guests?page=', { networkError: true })
    renderList()

    expect(await screen.findByText('Could not reach the server')).toBeInTheDocument()
  })

  it('makes no request per guest', async () => {
    renderList()
    await screen.findByText(/Papadakis/)

    // One list request, and nothing keyed by a guest id.
    expect(requestsFor('/guests?page=')).toHaveLength(1)
    expect(requestsFor('/guests/')).toHaveLength(0)
  })
})

/* --- creating --------------------------------------------------------------------------- */

describe('creating a guest', () => {
  async function openForm() {
    const user = userEvent.setup()
    renderList()
    await screen.findByText(/Papadakis/)
    await user.click(screen.getByRole('button', { name: /Add guest/ }))
    return user
  }

  it('posts only the fields that were filled in', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/guests', { status: 201, body: guest() })

    await user.type(screen.getByLabelText('First name'), 'Nora')
    await user.type(screen.getByLabelText('Last name'), 'Haddad')
    await user.click(screen.getByRole('button', { name: 'Create guest' }))

    await waitFor(() => {
      expect(requestsFor('/guests', 'POST')).toHaveLength(1)
    })
    const request = requestsFor('/guests', 'POST')[0]!
    expect(new URL(request.url, 'http://localhost').pathname).toBe(
      `/api/v1/hotels/${TEST_HOTEL.public_id}/guests`,
    )
    const body = JSON.parse(request.body!) as Record<string, unknown>
    // Empty optionals are omitted, not sent as null: there is nothing to clear on a row that
    // does not exist yet, and `extra="forbid"` rewards sending less.
    expect(body).toEqual({ first_name: 'Nora', last_name: 'Haddad', marketing_opt_in: false })
    // Server-assigned identity is never sent; both are a 422.
    expect(body).not.toHaveProperty('public_id')
    expect(body).not.toHaveProperty('hotel_public_id')
  })

  it('sends the optional fields as typed, leaving case to the server', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/guests', { status: 201, body: guest() })

    await user.type(screen.getByLabelText('First name'), 'Nora')
    await user.type(screen.getByLabelText('Last name'), 'Haddad')
    await user.type(screen.getByLabelText('Email (optional)'), 'nora@example.test')
    await user.type(screen.getByLabelText('Country (optional)'), 'gb')
    await user.type(screen.getByLabelText('Preferred language (optional)'), 'EN')
    await user.click(screen.getByRole('checkbox', { name: /consented to marketing/ }))
    await user.click(screen.getByRole('button', { name: 'Create guest' }))

    await waitFor(() => {
      expect(requestsFor('/guests', 'POST')).toHaveLength(1)
    })
    const body = JSON.parse(requestsFor('/guests', 'POST')[0]!.body!) as Record<string, unknown>
    expect(body).toMatchObject({
      email: 'nora@example.test',
      // As typed. The schema upper-cases the country and lower-cases the language itself;
      // doing it here too would be a second implementation of one rule.
      country_code: 'gb',
      preferred_language: 'EN',
      marketing_opt_in: true,
    })
  })

  it('requires both names, without a request', async () => {
    const user = await openForm()

    await user.type(screen.getByLabelText('First name'), 'Nora')
    await user.click(screen.getByRole('button', { name: 'Create guest' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('both a first and a last name')
    expect(requestsFor('/guests', 'POST')).toHaveLength(0)
  })

  it('accepts an email the backend accepts, rather than inventing a format rule', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/guests', { status: 201, body: guest() })

    await user.type(screen.getByLabelText('First name'), 'Nora')
    await user.type(screen.getByLabelText('Last name'), 'Haddad')
    // `"not-an-email"` was accepted with a 201 by the real backend: there is no format check
    // in the schema and no CHECK on the column. The UI must not be stricter than the system.
    await user.type(screen.getByLabelText('Email (optional)'), 'not-an-email')
    await user.click(screen.getByRole('button', { name: 'Create guest' }))

    await waitFor(() => {
      expect(requestsFor('/guests', 'POST')).toHaveLength(1)
    })
    expect(JSON.parse(requestsFor('/guests', 'POST')[0]!.body!)).toMatchObject({
      email: 'not-an-email',
    })
  })

  it('refuses an email shorter than the column allows', async () => {
    const user = await openForm()

    await user.type(screen.getByLabelText('First name'), 'Nora')
    await user.type(screen.getByLabelText('Last name'), 'Haddad')
    await user.type(screen.getByLabelText('Email (optional)'), 'ab')
    await user.click(screen.getByRole('button', { name: 'Create guest' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('between 3 and 254 characters')
    expect(requestsFor('/guests', 'POST')).toHaveLength(0)
  })

  it('sends one request when the button is clicked twice', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/guests', { status: 201, body: guest() })

    await user.type(screen.getByLabelText('First name'), 'Nora')
    await user.type(screen.getByLabelText('Last name'), 'Haddad')
    const submit = screen.getByRole('button', { name: 'Create guest' })
    await Promise.all([user.click(submit), user.click(submit)])

    await waitFor(() => {
      expect(requestsFor('/guests', 'POST').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/guests', 'POST')).toHaveLength(1)
  })

  it('explains a 409 as the one-email-per-property rule, and does not retry', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/guests', {
      status: 409,
      body: {
        error: {
          code: 'CONFLICT',
          message: 'Another guest at this hotel already uses that email address.',
        },
      },
    })

    await user.type(screen.getByLabelText('First name'), 'Nora')
    await user.type(screen.getByLabelText('Last name'), 'Haddad')
    await user.type(screen.getByLabelText('Email (optional)'), 'taken@example.test')
    await user.click(screen.getByRole('button', { name: 'Create guest' }))

    expect(await screen.findByText('Nothing was created')).toBeInTheDocument()
    expect(screen.getByText(/already uses that email address/)).toBeInTheDocument()
    expect(requestsFor('/guests', 'POST')).toHaveLength(1)
  })

  it('names the staff role on a 403', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/guests', {
      status: 403,
      body: { error: { code: 'FORBIDDEN', message: 'Requires staff role.' } },
    })

    await user.type(screen.getByLabelText('First name'), 'Nora')
    await user.type(screen.getByLabelText('Last name'), 'Haddad')
    await user.click(screen.getByRole('button', { name: 'Create guest' }))

    expect(await screen.findByText('Nothing was created')).toBeInTheDocument()
    expect(screen.getByText(/needs the staff role/)).toBeInTheDocument()
  })

  it('re-reads the list after the server accepts', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/guests', { status: 201, body: guest() })

    const before = requestsFor('/guests?page=').length
    await user.type(screen.getByLabelText('First name'), 'Nora')
    await user.type(screen.getByLabelText('Last name'), 'Haddad')
    await user.click(screen.getByRole('button', { name: 'Create guest' }))

    await waitFor(() => {
      expect(requestsFor('/guests?page=').length).toBeGreaterThan(before)
    })
    expect(await screen.findByText(/Guest created/)).toBeInTheDocument()
  })

  it('never renders the backend’s own words on a refusal', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/guests', {
      status: 500,
      body: {
        error: {
          code: 'INTERNAL',
          message: 'psycopg.errors.UniqueViolation: uq_guests_hotel_id_email on table guests',
        },
      },
    })

    await user.type(screen.getByLabelText('First name'), 'Nora')
    await user.type(screen.getByLabelText('Last name'), 'Haddad')
    await user.click(screen.getByRole('button', { name: 'Create guest' }))

    expect(await screen.findByText('Nothing was created')).toBeInTheDocument()
    for (const leak of ['psycopg', 'uq_guests', 'UniqueViolation', 'table guests']) {
      expect(visibleText()).not.toContain(leak)
    }
  })
})

/* --- the detail page --------------------------------------------------------------------- */

describe('one guest', () => {
  it('is addressed by public identifier, and requests that URL', async () => {
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Elena Papadakis' })

    const url = new URL(lastRequestFor('/guests/')!.url, 'http://localhost')
    expect(url.pathname).toBe(`/api/v1/hotels/${TEST_HOTEL.public_id}/guests/${GUEST_ID}`)
  })

  it('shows every field the record carries', async () => {
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Elena Papadakis' })

    expect(screen.getByText('elena.papadakis@example.test')).toBeInTheDocument()
    expect(screen.getByText('+30 210 555 0142')).toBeInTheDocument()
    expect(screen.getByText('GR')).toBeInTheDocument()
    expect(screen.getByText('el')).toBeInTheDocument()
    expect(screen.getByText('Prefers a high floor.')).toBeInTheDocument()
    expect(screen.getByText('Opted in')).toBeInTheDocument()
  })

  it('says which fields the record does not carry', async () => {
    fetchStub.on('GET', '/guests/', { body: secondGuest() })
    renderDetail(OTHER_GUEST_ID)
    await screen.findByRole('heading', { level: 1, name: 'Marcus Lindqvist' })

    expect(screen.getAllByText('Not recorded').length).toBeGreaterThan(0)
    expect(screen.getByText('None')).toBeInTheDocument()
  })

  it('reads a 404 as "not available here" rather than "deleted"', async () => {
    fetchStub.on('GET', '/guests/', {
      status: 404,
      body: { error: { code: 'NOT_FOUND', message: 'Guest not found for this hotel.' } },
    })
    renderDetail()

    expect(await screen.findByText('Guest not available')).toBeInTheDocument()
    // A guest of another property answers the same 404, and the client cannot tell which.
    expect(screen.getByText(/may belong to another hotel/)).toBeInTheDocument()
  })

  it('treats a malformed 200 as a failure', async () => {
    fetchStub.on('GET', '/guests/', { body: { public_id: 42 } })
    renderDetail()

    expect(await screen.findByText('Unexpected response')).toBeInTheDocument()
  })
})

/* --- updating ---------------------------------------------------------------------------- */

describe('updating a guest', () => {
  async function openEdit() {
    const user = userEvent.setup()
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Elena Papadakis' })
    await user.click(screen.getByRole('button', { name: 'Edit guest' }))
    return user
  }

  it('patches the guest’s own URL', async () => {
    const user = await openEdit()
    fetchStub.on('PATCH', '/guests/', { body: guest({ phone: '+30 210 555 0199' }) })

    await user.clear(screen.getByLabelText('Phone (optional)'))
    await user.type(screen.getByLabelText('Phone (optional)'), '+30 210 555 0199')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => {
      expect(requestsFor('/guests/', 'PATCH')).toHaveLength(1)
    })
    const request = requestsFor('/guests/', 'PATCH')[0]!
    expect(new URL(request.url, 'http://localhost').pathname).toBe(
      `/api/v1/hotels/${TEST_HOTEL.public_id}/guests/${GUEST_ID}`,
    )
    expect(JSON.parse(request.body!)).toMatchObject({ phone: '+30 210 555 0199' })
  })

  it('clears an emptied field with an explicit null', async () => {
    const user = await openEdit()
    fetchStub.on('PATCH', '/guests/', { body: guest({ email: null }) })

    await user.clear(screen.getByLabelText('Email (optional)'))
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => {
      expect(requestsFor('/guests/', 'PATCH')).toHaveLength(1)
    })
    const body = JSON.parse(requestsFor('/guests/', 'PATCH')[0]!.body!) as Record<string, unknown>
    // An explicit null, not an omitted key: omitting it would leave the address in place,
    // which is the opposite of what clearing the field means.
    expect('email' in body).toBe(true)
    expect(body.email).toBeNull()
  })

  it('shows the server’s row after saving, not the payload that was sent', async () => {
    const user = await openEdit()
    // The server normalises the case; the response is the authority.
    fetchStub.on('PATCH', '/guests/', { body: guest({ country_code: 'GB' }) })

    await user.clear(screen.getByLabelText('Country (optional)'))
    await user.type(screen.getByLabelText('Country (optional)'), 'gb')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText(/Guest updated/)).toBeInTheDocument()
    // `GB`, as stored -- not the `gb` that was typed.
    expect(screen.getByText('GB')).toBeInTheDocument()
  })

  it('sends one request when saved twice', async () => {
    const user = await openEdit()
    fetchStub.on('PATCH', '/guests/', { body: guest() })

    const save = screen.getByRole('button', { name: 'Save changes' })
    await Promise.all([user.click(save), user.click(save)])

    await waitFor(() => {
      expect(requestsFor('/guests/', 'PATCH').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/guests/', 'PATCH')).toHaveLength(1)
  })

  it('explains a 409 and leaves the record as it was', async () => {
    const user = await openEdit()
    fetchStub.on('PATCH', '/guests/', {
      status: 409,
      body: {
        error: {
          code: 'CONFLICT',
          message: 'Another guest at this hotel already uses that email address.',
        },
      },
    })

    await user.clear(screen.getByLabelText('Email (optional)'))
    await user.type(screen.getByLabelText('Email (optional)'), 'taken@example.test')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText('Nothing was saved')).toBeInTheDocument()
    expect(screen.getByText(/already uses that email address/)).toBeInTheDocument()
    expect(requestsFor('/guests/', 'PATCH')).toHaveLength(1)
  })

  it('names the staff role on a 403', async () => {
    const user = await openEdit()
    fetchStub.on('PATCH', '/guests/', {
      status: 403,
      body: { error: { code: 'FORBIDDEN', message: 'Requires staff role.' } },
    })

    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    expect(await screen.findByText('Nothing was saved')).toBeInTheDocument()
    expect(screen.getByText(/needs the staff role/)).toBeInTheDocument()
  })
})

/* --- deleting ---------------------------------------------------------------------------- */

describe('deleting a guest', () => {
  it('asks before it deletes, naming the guest', async () => {
    const user = userEvent.setup()
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Elena Papadakis' })

    await user.click(screen.getByRole('button', { name: 'Delete guest' }))
    // Nothing has been sent yet.
    expect(requestsFor('/guests/', 'DELETE')).toHaveLength(0)

    const confirm = screen.getByRole('group', { name: 'Confirm deleting this guest' })
    expect(within(confirm).getByText(/Elena Papadakis/)).toBeInTheDocument()
    expect(within(confirm).getByText(/cannot be undone/)).toBeInTheDocument()
  })

  it('deletes only after the confirmation', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/guests/', { status: 204 })
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Elena Papadakis' })

    await user.click(screen.getByRole('button', { name: 'Delete guest' }))
    await user.click(screen.getByRole('button', { name: 'Yes, delete this guest' }))

    await waitFor(() => {
      expect(requestsFor('/guests/', 'DELETE')).toHaveLength(1)
    })
    expect(new URL(requestsFor('/guests/', 'DELETE')[0]!.url, 'http://localhost').pathname).toBe(
      `/api/v1/hotels/${TEST_HOTEL.public_id}/guests/${GUEST_ID}`,
    )
    expect(await screen.findByText('Guest deleted')).toBeInTheDocument()
  })

  it('cancels without sending anything', async () => {
    const user = userEvent.setup()
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Elena Papadakis' })

    await user.click(screen.getByRole('button', { name: 'Delete guest' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(requestsFor('/guests/', 'DELETE')).toHaveLength(0)
    expect(screen.getByRole('heading', { level: 1, name: 'Elena Papadakis' })).toBeInTheDocument()
  })

  it('explains a 409 as a record something still depends on', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/guests/', {
      status: 409,
      body: {
        error: {
          code: 'CONFLICT',
          message:
            'This guest cannot be deleted because existing reservations or reviews still reference them.',
        },
      },
    })
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Elena Papadakis' })

    await user.click(screen.getByRole('button', { name: 'Delete guest' }))
    await user.click(screen.getByRole('button', { name: 'Yes, delete this guest' }))

    expect(await screen.findByText('The guest was not deleted')).toBeInTheDocument()
    expect(screen.getByText(/reservations or reviews still refer/)).toBeInTheDocument()
    // The guest is still on screen, because they still exist.
    expect(screen.getByRole('heading', { level: 1, name: 'Elena Papadakis' })).toBeInTheDocument()
  })

  it('names the MANAGER role on a 403, which is not the role editing needs', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/guests/', {
      status: 403,
      body: { error: { code: 'FORBIDDEN', message: 'Requires manager role.' } },
    })
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Elena Papadakis' })

    await user.click(screen.getByRole('button', { name: 'Delete guest' }))
    await user.click(screen.getByRole('button', { name: 'Yes, delete this guest' }))

    // Scoped to the refusal: the section's standing note also mentions the manager role,
    // which is the point -- the requirement is stated before the click as well as after it.
    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('The guest was not deleted')).toBeInTheDocument()
    expect(within(alert).getByText(/needs the manager role/)).toBeInTheDocument()
    // Not the staff wording used by create and update: the two bars are different.
    expect(visibleText()).not.toMatch(/needs the staff role/)
  })

  it('sends one request when the confirmation is clicked twice', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/guests/', { status: 204 })
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Elena Papadakis' })

    await user.click(screen.getByRole('button', { name: 'Delete guest' }))
    const confirm = screen.getByRole('button', { name: 'Yes, delete this guest' })
    await Promise.all([user.click(confirm), user.click(confirm)])

    await waitFor(() => {
      expect(requestsFor('/guests/', 'DELETE').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/guests/', 'DELETE')).toHaveLength(1)
  })
})

/* --- untrusted content, security and accessibility ---------------------------------------- */

describe('guest data is untrusted content', () => {
  const PAYLOAD = '<img src=x onerror="document.title=\'pwned\'"> <script>alert(1)</script>'

  it('renders markup in a guest’s own fields as text', async () => {
    fetchStub.on('GET', '/guests/', {
      body: guest({ first_name: PAYLOAD, notes: PAYLOAD, email: PAYLOAD }),
    })
    renderDetail()

    expect(await screen.findAllByText(PAYLOAD, { exact: false })).not.toHaveLength(0)
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.querySelector('script')).toBeNull()
    expect(document.title).not.toBe('pwned')
    expect(document.body.innerHTML).toContain('&lt;img')
  })

  it('escapes a payload in the list too', async () => {
    fetchStub.on('GET', '/guests?page=', { body: page([guest({ last_name: PAYLOAD })], 1, 1) })
    renderList()
    await screen.findByText(new RegExp('Elena'))

    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.body.innerHTML).toContain('&lt;img')
  })

  it('puts no credential in the page and no internal identifier on screen', async () => {
    renderList()
    await screen.findByText(/Papadakis/)

    expect(document.body.innerHTML).not.toContain('header.payload.signature-test-only')
    expect(visibleText()).not.toContain('header.payload.signature-test-only')
    // No numeric path segment anywhere: the API exposes no BIGINT and neither does a link.
    for (const href of screen.getAllByRole('link').map((n) => n.getAttribute('href') ?? '')) {
      for (const segment of href.split('/')) {
        expect(segment).not.toMatch(/^\d+$/)
      }
    }
  })

  it('links each guest by public identifier', async () => {
    renderList()
    await screen.findByText(/Papadakis/)

    expect(screen.getByRole('link', { name: 'Papadakis, Elena' })).toHaveAttribute(
      'href',
      `/guests/${GUEST_ID}`,
    )
  })
})

describe('accessibility', () => {
  it('gives the list a table with real column headers', async () => {
    renderList()
    await screen.findByText(/Papadakis/)

    const table = screen.getByRole('table', { name: /Guests of this property/ })
    const headers = within(table)
      .getAllByRole('columnheader')
      .map((node) => node.textContent)
    expect(headers).toEqual([
      'Name',
      'Email',
      'Phone',
      'Country',
      'Language',
      'Marketing',
      'Updated',
    ])
    // The name is also the row header, so a screen reader announces it with every cell.
    expect(within(table).getAllByRole('rowheader')).toHaveLength(2)
  })

  it('keeps a sane heading hierarchy on the detail page', async () => {
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Elena Papadakis' })

    expect(screen.getByRole('heading', { level: 2, name: 'Guest record' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Delete this guest' })).toBeInTheDocument()
    // No h3 without an h2 above it, and no level is skipped.
    expect(screen.queryByRole('heading', { level: 3 })).not.toBeInTheDocument()
  })

  it('labels every control in the form', async () => {
    const user = userEvent.setup()
    renderList()
    await screen.findByText(/Papadakis/)
    await user.click(screen.getByRole('button', { name: /Add guest/ }))

    for (const label of [
      'First name',
      'Last name',
      'Email (optional)',
      'Phone (optional)',
      'Country (optional)',
      'Preferred language (optional)',
      'Date of birth (optional)',
      'Notes (optional)',
    ]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }
    expect(screen.getByRole('checkbox', { name: /consented to marketing/ })).toBeInTheDocument()
  })

  it('states marketing consent as a word, not only as a colour', async () => {
    renderList()
    await screen.findByText(/Papadakis/)

    expect(screen.getByText('Opted in')).toBeInTheDocument()
    expect(screen.getByText('No consent')).toBeInTheDocument()
  })
})
