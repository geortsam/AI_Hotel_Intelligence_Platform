import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AdministrationPage } from '@/pages/AdministrationPage'
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
import type { Member } from '@/types/member'

/**
 * Account and membership administration, as the UI reads and writes them.
 *
 * Mocked at `fetch`, so the method, the URL, the **exact request body** and the
 * `Authorization` header are all observable. Five things this file is mostly about:
 *
 * * the request surface is `page` and `page_size`, and an invented filter would be silently
 *   ignored by the backend rather than refused;
 * * listing needs **manager** and every write needs **owner**, so the two 403s mean
 *   different things and the copy distinguishes them;
 * * the **last-owner 409** is the server's rule, and nothing here predicts it;
 * * a password change **revokes the token that made the request**, so the replacement must
 *   be adopted or the next call fails;
 * * a **401 from a password change means the current password was wrong**, not that the
 *   session died -- ending it there would sign a user out for a typo.
 */

/* --- fixtures --------------------------------------------------------------------------- */

function member(overrides: Partial<Member> = {}): Member {
  return {
    user_public_id: TEST_USER.public_id,
    email: TEST_USER.email,
    full_name: TEST_USER.full_name,
    is_active: true,
    role: 'owner',
    joined_at: '2026-01-04T10:00:00+02:00',
    ...overrides,
  }
}

const COLLEAGUE = member({
  user_public_id: '7c2d1e40-91aa-4b6d-8f31-2a55d0c4e100',
  email: 'dana.reception@example.test',
  full_name: 'Dana Reception',
  role: 'staff',
})

function page(items: readonly unknown[], total = items.length, pages = 1) {
  return { items, total, page: 1, page_size: 20, pages }
}

function errorBody(code: string, message: string) {
  return { error: { code, message, details: [] } }
}

/* --- harness ---------------------------------------------------------------------------- */

let fetchStub: FetchStub

beforeEach(() => {
  fetchStub = installFetchStub()
  seedStoredToken()
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage([TEST_HOTEL], 1) })
  fetchStub.on('GET', '/members?page=', { body: page([member(), COLLEAGUE], 2) })
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
  vi.unstubAllGlobals()
})

function renderPage() {
  return renderWithAuth([{ path: ROUTES.administration, element: <AdministrationPage /> }], {
    initialEntries: [ROUTES.administration],
  })
}

function requestsFor(fragment: string, method = 'GET') {
  return fetchStub.calls.filter((call) => call.method === method && call.url.includes(fragment))
}

function lastBody(fragment: string, method: string): Record<string, unknown> {
  const matches = requestsFor(fragment, method)
  return JSON.parse(matches[matches.length - 1]!.body!) as Record<string, unknown>
}

/* --- reading ----------------------------------------------------------------------------- */

describe('the member list', () => {
  it('reads one page of members, and nothing per row', async () => {
    renderPage()
    await screen.findByText('Dana Reception')

    expect(requestsFor('/members?page=')).toHaveLength(1)
    expect(fetchStub.calls.map((c) => new URL(c.url, 'http://localhost').pathname)).toEqual([
      '/api/v1/auth/me',
      '/api/v1/hotels',
      `/api/v1/hotels/${TEST_HOTEL.public_id}/members`,
    ])
  })

  it('asks for members with only the parameters the endpoint has', async () => {
    renderPage()
    await screen.findByText('Dana Reception')

    const url = new URL(requestsFor('/members?page=')[0]!.url, 'http://localhost')
    expect(url.pathname).toBe(`/api/v1/hotels/${TEST_HOTEL.public_id}/members`)
    expect([...url.searchParams.keys()].sort()).toEqual(['page', 'page_size'])
    expect(requestsFor('/members?page=')[0]!.authorization).toMatch(/^Bearer /)
  })

  it('offers no search, filter or sort, because the endpoint has none', async () => {
    renderPage()
    await screen.findByText('Dana Reception')

    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/search/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/filter/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/^sort/i)).not.toBeInTheDocument()
  })

  it('never asks for a page larger than the router allows', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Dana Reception')

    await user.selectOptions(screen.getByLabelText('Per page'), '100')
    await waitFor(() => {
      for (const call of requestsFor('/members?page=')) {
        const url = new URL(call.url, 'http://localhost')
        expect(Number(url.searchParams.get('page_size'))).toBeLessThanOrEqual(100)
      }
    })
  })

  it('shows the fields the response carries, and marks your own row', async () => {
    renderPage()
    await screen.findByText('Dana Reception')

    // The signed-in account appears twice by design -- once as an identity, once as a
    // member of this property -- and only the member row is marked.
    const table = screen.getByRole('table', { name: /Members of this property/ })
    expect(within(table).getByText(TEST_USER.email)).toBeInTheDocument()
    expect(within(table).getByText('(you)')).toBeInTheDocument()
    expect(within(table).getByText('dana.reception@example.test')).toBeInTheDocument()
    expect(screen.getByText('2 members. Counted by the server across every page.')).toBeInTheDocument()
  })

  it('says the account is disabled, not the membership', async () => {
    fetchStub.on('GET', '/members?page=', {
      body: page([member(), member({ ...COLLEAGUE, is_active: false })], 2),
    })
    renderPage()
    await screen.findByText('Dana Reception')

    // The word "Disabled" sits in the Account column, never beside the role.
    const row = screen.getByText('Dana Reception').closest('tr')!
    expect(within(row).getByText('Disabled')).toBeInTheDocument()
    expect(within(row).getByRole('combobox', { name: 'Role for dana.reception@example.test' })).toHaveValue('staff')
  })

  it('renders a 403 on the listing as a manager-level refusal', async () => {
    fetchStub.on('GET', '/members?page=', {
      status: 403,
      body: errorBody('FORBIDDEN', 'This operation requires the manager role at this hotel.'),
    })
    renderPage()

    const refusal = await screen.findByRole('alert')
    expect(within(refusal).getByText('You cannot see this property’s members')).toBeInTheDocument()
    expect(within(refusal).getByText(/needs the manager role/)).toBeInTheDocument()
    // The account section is unaffected, and the copy says so.
    expect(within(refusal).getByText(/Your own account details above are unaffected/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Your account' })).toBeInTheDocument()
  })

  it('renders a 404 without claiming to know which cause', async () => {
    fetchStub.on('GET', '/members?page=', {
      status: 404,
      body: errorBody('NOT_FOUND', 'Hotel not found.'),
    })
    renderPage()

    expect(await screen.findByText('Members not available')).toBeInTheDocument()
    expect(screen.getByText(/answers the same way for both/)).toBeInTheDocument()
  })

  it('treats a malformed 200 as a failure', async () => {
    fetchStub.on('GET', '/members?page=', { body: { items: null } })
    renderPage()

    expect(await screen.findByText('Unexpected response')).toBeInTheDocument()
  })

  it('renders a network failure as a failure', async () => {
    fetchStub.on('GET', '/members?page=', { networkError: true })
    renderPage()

    expect(await screen.findByText('Could not reach the server')).toBeInTheDocument()
  })

  it('explains an empty page rather than claiming the property has no members', async () => {
    fetchStub.on('GET', '/members?page=', { body: page([], 2, 1) })
    renderPage()

    // A hotel always keeps an owner, so an empty list is a paging artefact, not a state.
    expect(await screen.findByText('No members on this page')).toBeInTheDocument()
    expect(screen.getByText(/always keeps at least one owner/)).toBeInTheDocument()
  })
})

/* --- adding ------------------------------------------------------------------------------ */

describe('adding a member', () => {
  async function openForm() {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Dana Reception')
    await user.click(screen.getByRole('button', { name: /Add member/ }))
    return user
  }

  it('posts exactly the two documented fields', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/members', { status: 201, body: COLLEAGUE })

    await user.type(screen.getByLabelText('Email address'), 'new.person@example.test')
    await user.selectOptions(screen.getByLabelText('Role'), 'manager')
    await user.click(screen.getByRole('button', { name: 'Add member' }))

    await waitFor(() => {
      expect(requestsFor('/members', 'POST')).toHaveLength(1)
    })
    const request = requestsFor('/members', 'POST')[0]!
    expect(new URL(request.url, 'http://localhost').pathname).toBe(
      `/api/v1/hotels/${TEST_HOTEL.public_id}/members`,
    )
    const body = JSON.parse(request.body!) as Record<string, unknown>
    expect(body).toEqual({ email: 'new.person@example.test', role: 'manager' })
    // The hotel is in the path; sending it is a 422.
    expect(body).not.toHaveProperty('hotel_public_id')
    // There is no such field, and inventing one would be a 422 too.
    expect(body).not.toHaveProperty('full_name')
    expect(body).not.toHaveProperty('password')
  })

  it('defaults to the lowest role', async () => {
    await openForm()

    expect(screen.getByLabelText('Role')).toHaveValue('viewer')
  })

  it('offers exactly the four roles the enum declares', async () => {
    await openForm()

    expect(
      within(screen.getByLabelText('Role'))
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['viewer', 'staff', 'manager', 'owner'])
  })

  it('sends the address as typed, leaving case to the server', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/members', { status: 201, body: COLLEAGUE })

    await user.type(screen.getByLabelText('Email address'), '  Dana.Reception@Example.test  ')
    await user.click(screen.getByRole('button', { name: 'Add member' }))

    await waitFor(() => {
      expect(requestsFor('/members', 'POST')).toHaveLength(1)
    })
    // Trimmed, because that is a typing artefact -- but not lower-cased, because the
    // backend matches case-insensitively and normalising twice hides which side does it.
    expect(lastBody('/members', 'POST').email).toBe('Dana.Reception@Example.test')
  })

  it('refuses a malformed address without a request', async () => {
    const user = await openForm()

    await user.type(screen.getByLabelText('Email address'), 'not-an-address')
    await user.click(screen.getByRole('button', { name: 'Add member' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('existing account')
    expect(requestsFor('/members', 'POST')).toHaveLength(0)
  })

  it('explains a 404 as an address with no account, and says it cannot create one', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/members', {
      status: 404,
      body: errorBody('NOT_FOUND', 'No account exists for that email address.'),
    })

    await user.type(screen.getByLabelText('Email address'), 'nobody@example.test')
    await user.click(screen.getByRole('button', { name: 'Add member' }))

    expect(await screen.findByText('Nobody was added')).toBeInTheDocument()
    expect(screen.getByText(/cannot create one/)).toBeInTheDocument()
    expect(screen.getByText(/register first/)).toBeInTheDocument()
  })

  it('explains a 409 as already a member', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/members', {
      status: 409,
      body: errorBody('CONFLICT', 'That user is already a member of this hotel.'),
    })

    await user.type(screen.getByLabelText('Email address'), 'dana.reception@example.test')
    await user.click(screen.getByRole('button', { name: 'Add member' }))

    expect(await screen.findByText('Nobody was added')).toBeInTheDocument()
    expect(screen.getByText(/already a member/)).toBeInTheDocument()
  })

  it('names the OWNER role on a 403, and says the list needs only manager', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/members', {
      status: 403,
      body: errorBody('FORBIDDEN', 'This operation requires the owner role at this hotel.'),
    })

    await user.type(screen.getByLabelText('Email address'), 'new.person@example.test')
    await user.click(screen.getByRole('button', { name: 'Add member' }))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('Nobody was added')).toBeInTheDocument()
    expect(within(alert).getByText(/needs the owner role/)).toBeInTheDocument()
    expect(within(alert).getByText(/Seeing the list needs only manager/)).toBeInTheDocument()
  })

  it('re-reads the list after the server accepts', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/members', { status: 201, body: COLLEAGUE })

    const before = requestsFor('/members?page=').length
    await user.type(screen.getByLabelText('Email address'), 'new.person@example.test')
    await user.click(screen.getByRole('button', { name: 'Add member' }))

    await waitFor(() => {
      expect(requestsFor('/members?page=').length).toBeGreaterThan(before)
    })
    expect(await screen.findByText(/Member added/)).toBeInTheDocument()
  })

  it('sends one request when the button is clicked twice', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/members', { status: 201, body: COLLEAGUE })

    await user.type(screen.getByLabelText('Email address'), 'new.person@example.test')
    const submit = screen.getByRole('button', { name: 'Add member' })
    await Promise.all([user.click(submit), user.click(submit)])

    await waitFor(() => {
      expect(requestsFor('/members', 'POST').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/members', 'POST')).toHaveLength(1)
  })

  it('never renders the backend’s own words on a refusal', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/members', {
      status: 500,
      body: errorBody(
        'INTERNAL',
        'psycopg.errors.UniqueViolation: uq_user_hotels_user_id_hotel_id on table user_hotels',
      ),
    })

    await user.type(screen.getByLabelText('Email address'), 'new.person@example.test')
    await user.click(screen.getByRole('button', { name: 'Add member' }))

    expect(await screen.findByText('Nobody was added')).toBeInTheDocument()
    for (const leak of ['psycopg', 'uq_user_hotels', 'UniqueViolation', 'user_hotels']) {
      expect(document.body.textContent ?? '').not.toContain(leak)
    }
  })
})

/* --- changing a role ---------------------------------------------------------------------- */

describe('changing a role', () => {
  it('patches the one documented field, at the member’s own URL', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/members/', { body: { ...COLLEAGUE, role: 'manager' } })
    renderPage()
    await screen.findByText('Dana Reception')

    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Role for dana.reception@example.test' }),
      'manager',
    )

    await waitFor(() => {
      expect(requestsFor('/members/', 'PATCH')).toHaveLength(1)
    })
    const request = requestsFor('/members/', 'PATCH')[0]!
    expect(new URL(request.url, 'http://localhost').pathname).toBe(
      `/api/v1/hotels/${TEST_HOTEL.public_id}/members/${COLLEAGUE.user_public_id}`,
    )
    // Exactly one field. `role` is required, and nothing else is accepted.
    expect(JSON.parse(request.body!)).toEqual({ role: 'manager' })
  })

  it('sends nothing when the role is reasserted unchanged', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Dana Reception')

    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Role for dana.reception@example.test' }),
      'staff',
    )

    // A documented no-op on the backend; not sending it keeps a request that cannot change
    // anything off the wire.
    expect(requestsFor('/members/', 'PATCH')).toHaveLength(0)
  })

  it('explains the last-owner 409 and what to do about it', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/members/', {
      status: 409,
      body: errorBody('CONFLICT', 'This hotel would be left without an owner.'),
    })
    renderPage()
    await screen.findByText('Dana Reception')

    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Role for reception@example.test' }),
      'manager',
    )

    expect(await screen.findByText('The role was not changed')).toBeInTheDocument()
    expect(screen.getByText(/must always keep at least one/)).toBeInTheDocument()
    expect(screen.getByText(/grant the owner role to somebody else first/)).toBeInTheDocument()
  })

  it('re-reads the list rather than patching the row in place', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/members/', { body: { ...COLLEAGUE, role: 'manager' } })
    renderPage()
    await screen.findByText('Dana Reception')

    const before = requestsFor('/members?page=').length
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Role for dana.reception@example.test' }),
      'manager',
    )

    await waitFor(() => {
      expect(requestsFor('/members?page=').length).toBeGreaterThan(before)
    })
    expect(await screen.findByText(/Role changed/)).toBeInTheDocument()
  })
})

/* --- removing ---------------------------------------------------------------------------- */

describe('revoking access', () => {
  it('asks before it removes, naming the address and what survives', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Dana Reception')

    await user.click(
      screen.getByRole('button', { name: 'Remove dana.reception@example.test' }),
    )
    expect(requestsFor('/members/', 'DELETE')).toHaveLength(0)

    const confirm = screen.getByRole('group', { name: 'Confirm revoking access' })
    expect(within(confirm).getByText(/dana.reception@example.test/)).toBeInTheDocument()
    expect(within(confirm).getByText(/account is not deleted/)).toBeInTheDocument()
    expect(within(confirm).getByText(/other properties is unaffected/)).toBeInTheDocument()
  })

  it('warns when the membership being revoked is your own', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Dana Reception')

    await user.click(screen.getByRole('button', { name: 'Remove reception@example.test' }))

    const confirm = screen.getByRole('group', { name: 'Confirm revoking access' })
    expect(within(confirm).getByText(/This is your own membership/)).toBeInTheDocument()
    expect(within(confirm).getByText(/could not restore it yourself/)).toBeInTheDocument()
  })

  it('deletes only after the confirmation, at the right URL', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/members/', { status: 204 })
    renderPage()
    await screen.findByText('Dana Reception')

    await user.click(screen.getByRole('button', { name: 'Remove dana.reception@example.test' }))
    await user.click(screen.getByRole('button', { name: 'Yes, revoke access' }))

    await waitFor(() => {
      expect(requestsFor('/members/', 'DELETE')).toHaveLength(1)
    })
    expect(
      new URL(requestsFor('/members/', 'DELETE')[0]!.url, 'http://localhost').pathname,
    ).toBe(`/api/v1/hotels/${TEST_HOTEL.public_id}/members/${COLLEAGUE.user_public_id}`)
    expect(await screen.findByText(/only their access to this property was revoked/)).toBeInTheDocument()
  })

  it('cancels without sending anything', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Dana Reception')

    await user.click(screen.getByRole('button', { name: 'Remove dana.reception@example.test' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(requestsFor('/members/', 'DELETE')).toHaveLength(0)
  })

  it('explains the last-owner 409 on removal', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/members/', {
      status: 409,
      body: errorBody('CONFLICT', 'This hotel would be left without an owner.'),
    })
    renderPage()
    await screen.findByText('Dana Reception')

    await user.click(screen.getByRole('button', { name: 'Remove reception@example.test' }))
    await user.click(screen.getByRole('button', { name: 'Yes, revoke access' }))

    expect(await screen.findByText('Nobody was removed')).toBeInTheDocument()
    expect(screen.getByText(/must always keep at least one/)).toBeInTheDocument()
    // Still listed, because they are still a member.
    expect(screen.getByText('Dana Reception')).toBeInTheDocument()
  })

  it('sends one request when the confirmation is clicked twice', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/members/', { status: 204 })
    renderPage()
    await screen.findByText('Dana Reception')

    await user.click(screen.getByRole('button', { name: 'Remove dana.reception@example.test' }))
    const confirm = screen.getByRole('button', { name: 'Yes, revoke access' })
    await Promise.all([user.click(confirm), user.click(confirm)])

    await waitFor(() => {
      expect(requestsFor('/members/', 'DELETE').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/members/', 'DELETE')).toHaveLength(1)
  })
})

/* --- the account half ---------------------------------------------------------------------- */

describe('your account', () => {
  it('shows the identity the session already holds, without a second request', async () => {
    renderPage()
    await screen.findByText('Dana Reception')

    expect(
      screen.getByRole('heading', { level: 3, name: TEST_USER.full_name }),
    ).toBeInTheDocument()
    expect(screen.getAllByText(TEST_USER.email).length).toBeGreaterThan(0)
    // One `/auth/me`, issued by session restoration -- not one per page.
    expect(requestsFor('/auth/me')).toHaveLength(1)
  })

  it('offers no control for editing a name, an address or an account’s state', async () => {
    renderPage()
    await screen.findByText('Dana Reception')

    expect(screen.queryByRole('button', { name: /edit (your )?account/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /delete (your )?account/i })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Full name')).not.toBeInTheDocument()
    expect(screen.getByText(/no endpoint that changes them/)).toBeInTheDocument()
  })

  it('offers no way to reset somebody else’s password', async () => {
    renderPage()
    await screen.findByText('Dana Reception')

    expect(screen.queryByRole('button', { name: /reset password/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /send.*reset/i })).not.toBeInTheDocument()
    // The only password form on the page is the one aimed by the token at its own account.
    expect(screen.getAllByLabelText(/password/i)).toHaveLength(3)
  })
})

describe('changing your own password', () => {
  async function fill(user: ReturnType<typeof userEvent.setup>, next = 'a-much-longer-one') {
    await user.type(screen.getByLabelText('Current password'), 'the-current-one')
    await user.type(screen.getByLabelText('New password'), next)
    await user.type(screen.getByLabelText('Confirm new password'), next)
  }

  it('posts exactly the two documented fields, and never an identifier', async () => {
    const user = userEvent.setup()
    fetchStub.on('POST', '/auth/change-password', {
      body: { access_token: 'replacement.token.value', token_type: 'bearer', expires_in: 1800 },
    })
    renderPage()
    await screen.findByText('Dana Reception')

    await fill(user)
    await user.click(screen.getByRole('button', { name: 'Change password' }))

    await waitFor(() => {
      expect(requestsFor('/auth/change-password', 'POST')).toHaveLength(1)
    })
    const body = lastBody('/auth/change-password', 'POST')
    expect(Object.keys(body).sort()).toEqual(['current_password', 'new_password'])
    // Whose password changes is decided by the token. This route cannot be aimed.
    expect(body).not.toHaveProperty('email')
    expect(body).not.toHaveProperty('public_id')
    expect(body).not.toHaveProperty('user_public_id')
  })

  it('adopts the replacement token, because the old one is now revoked', async () => {
    const user = userEvent.setup()
    fetchStub.on('POST', '/auth/change-password', {
      body: { access_token: 'replacement.token.value', token_type: 'bearer', expires_in: 1800 },
    })
    renderPage()
    await screen.findByText('Dana Reception')

    await fill(user)
    await user.click(screen.getByRole('button', { name: 'Change password' }))
    await screen.findByText(/Password changed/)

    // The next request must carry the NEW token: every token minted before the change,
    // including the one that made it, has been revoked.
    await user.selectOptions(screen.getByLabelText('Per page'), '50')
    await waitFor(() => {
      const listCalls = requestsFor('/members?page=')
      expect(listCalls[listCalls.length - 1]!.authorization).toBe(
        'Bearer replacement.token.value',
      )
    })
  })

  it('keeps the session when the CURRENT password is wrong', async () => {
    const user = userEvent.setup()
    fetchStub.on('POST', '/auth/change-password', {
      status: 401,
      body: errorBody('AUTHENTICATION_FAILED', 'Invalid email or password.'),
    })
    renderPage()
    await screen.findByText('Dana Reception')

    await fill(user)
    await user.click(screen.getByRole('button', { name: 'Change password' }))

    expect(await screen.findByText(/not your current password/)).toBeInTheDocument()
    // The session survives: a typo must not sign somebody out, least of all while they are
    // securing their account.
    expect(screen.getByText('Dana Reception')).toBeInTheDocument()
    expect(window.sessionStorage.getItem('ahip.access_token')).toBe(TEST_TOKEN)
  })

  it('refuses a short new password without a request', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Dana Reception')

    await fill(user, 'short')
    await user.click(screen.getByRole('button', { name: 'Change password' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('at least 12 characters')
    expect(requestsFor('/auth/change-password', 'POST')).toHaveLength(0)
  })

  it('refuses a mismatched confirmation without a request', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Dana Reception')

    await user.type(screen.getByLabelText('Current password'), 'the-current-one')
    await user.type(screen.getByLabelText('New password'), 'a-much-longer-one')
    await user.type(screen.getByLabelText('Confirm new password'), 'a-much-longer-two')
    await user.click(screen.getByRole('button', { name: 'Change password' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('do not match')
    expect(requestsFor('/auth/change-password', 'POST')).toHaveLength(0)
  })

  it('explains a 429 with the wait the server named', async () => {
    const user = userEvent.setup()
    fetchStub.on('POST', '/auth/change-password', {
      status: 429,
      headers: { 'Retry-After': '30' },
      body: errorBody('RATE_LIMITED', 'Too many requests.'),
    })
    renderPage()
    await screen.findByText('Dana Reception')

    await fill(user)
    await user.click(screen.getByRole('button', { name: 'Change password' }))

    expect(await screen.findByText(/Try again in 30 seconds/)).toBeInTheDocument()
  })

  it('sends one request when submitted twice', async () => {
    const user = userEvent.setup()
    fetchStub.on('POST', '/auth/change-password', {
      body: { access_token: 'replacement.token.value', token_type: 'bearer', expires_in: 1800 },
    })
    renderPage()
    await screen.findByText('Dana Reception')

    await fill(user)
    const submit = screen.getByRole('button', { name: 'Change password' })
    await Promise.all([user.click(submit), user.click(submit)])

    await waitFor(() => {
      expect(requestsFor('/auth/change-password', 'POST').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/auth/change-password', 'POST')).toHaveLength(1)
  })
})

/* --- tenant isolation, safety, accessibility ------------------------------------------------ */

describe('tenant isolation', () => {
  it('reads members only through the selected hotel’s own URL', async () => {
    renderPage()
    await screen.findByText('Dana Reception')

    for (const call of fetchStub.calls) {
      const path = new URL(call.url, 'http://localhost').pathname
      if (path.includes('/members')) {
        expect(path.startsWith(`/api/v1/hotels/${TEST_HOTEL.public_id}/`)).toBe(true)
      }
    }
    // There is no flat route, and nothing here asks for one.
    expect(fetchStub.calls.some((c) => c.url.includes('/users/'))).toBe(false)
  })
})

describe('safety', () => {
  const PAYLOAD = '<img src=x onerror="document.title=\'pwned\'">'

  it('renders markup in a name as text', async () => {
    fetchStub.on('GET', '/members?page=', {
      body: page([member(), member({ ...COLLEAGUE, full_name: PAYLOAD })], 2),
    })
    renderPage()

    expect(await screen.findAllByText(PAYLOAD, { exact: false })).not.toHaveLength(0)
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.title).not.toBe('pwned')
    expect(document.body.innerHTML).toContain('&lt;img')
  })

  it('puts no credential in the page and none in a URL', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Dana Reception')

    await user.type(screen.getByLabelText('Current password'), 'the-current-one')

    expect(document.body.innerHTML).not.toContain(TEST_TOKEN)
    /*
     * The typed password is deliberately NOT asserted absent from the DOM: a controlled
     * input holds what was typed, `input.value` is readable in any browser, and a test
     * demanding otherwise would be testing React rather than security. What must hold is
     * that no credential is ever put somewhere it would be logged or shared -- a URL, a
     * query string, a referrer.
     */
    for (const call of fetchStub.calls) {
      expect(call.url).not.toContain(TEST_TOKEN)
      expect(call.url).not.toContain('password')
      expect(call.url).not.toContain('the-current-one')
    }
  })

  it('masks every password field', async () => {
    renderPage()
    await screen.findByText('Dana Reception')

    for (const label of ['Current password', 'New password', 'Confirm new password']) {
      expect(screen.getByLabelText(label)).toHaveAttribute('type', 'password')
    }
  })
})

describe('accessibility', () => {
  it('gives the member table real column headers', async () => {
    renderPage()
    await screen.findByText('Dana Reception')

    const table = screen.getByRole('table', { name: /Members of this property/ })
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((n) => n.textContent),
    ).toEqual(['Member', 'Account', 'Role', 'Joined', 'Actions'])
    expect(within(table).getAllByRole('rowheader')).toHaveLength(2)
  })

  it('names each row’s controls by the member they act on', async () => {
    renderPage()
    await screen.findByText('Dana Reception')

    expect(
      screen.getByRole('combobox', { name: 'Role for dana.reception@example.test' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Remove dana.reception@example.test' }),
    ).toBeInTheDocument()
    // Not two identical "Remove" buttons.
    expect(screen.queryAllByRole('button', { name: 'Remove' })).toHaveLength(0)
  })

  it('keeps a sane heading hierarchy', async () => {
    renderPage()
    await screen.findByText('Dana Reception')

    expect(screen.getByRole('heading', { level: 1, name: 'Administration' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Your account' })).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 2, name: /^Members of/ }),
    ).toBeInTheDocument()
  })

  it('labels every control in both forms', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Dana Reception')

    for (const label of ['Current password', 'New password', 'Confirm new password']) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }
    await user.click(screen.getByRole('button', { name: /Add member/ }))
    expect(screen.getByLabelText('Email address')).toBeInTheDocument()
    expect(screen.getByLabelText('Role')).toBeInTheDocument()
  })

  it('explains what each role means, in text', async () => {
    renderPage()
    await screen.findByText('Dana Reception')

    expect(screen.getByText(/Can read this property’s data/)).toBeInTheDocument()
    expect(screen.getByText(/Full control, including/)).toBeInTheDocument()
  })
})

describe('the compact layout', () => {
  it('renders cards instead of a table at phone width, losing no member', async () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    )
    renderPage()
    await screen.findByText('Dana Reception')

    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    // Both members, both roles and both controls survive the switch: an administrative list
    // is not improved by hiding who has owner access.
    expect(screen.getByRole('heading', { level: 3, name: 'Dana Reception' })).toBeInTheDocument()
    expect(
      screen.getByRole('combobox', { name: 'Role for reception@example.test' }),
    ).toHaveValue('owner')
    expect(
      screen.getByRole('button', { name: 'Remove dana.reception@example.test' }),
    ).toBeInTheDocument()
  })
})
