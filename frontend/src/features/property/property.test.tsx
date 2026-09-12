import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PropertyPage } from '@/pages/PropertyPage'
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
import type { Hotel } from '@/types/hotel'
import type { RoomType } from '@/types/room'

/**
 * Property and room-type management, as the UI reads and writes them.
 *
 * Mocked at `fetch`, so the method, the URL and **the exact request body** are observable.
 * Four things this file is mostly about:
 *
 * * `slug` and `code` are settable at creation and refused by PATCH, so the forms must not
 *   send them;
 * * `is_active` is the mirror image -- update-only, refused at creation;
 * * the two resources need **different roles** (owner vs manager), and the copy says which;
 * * the query surface is `page` and `page_size`, and an invented filter would be silently
 *   ignored by the backend.
 */

/* --- fixtures --------------------------------------------------------------------------- */

/** The full `HotelResponse`, not the trimmed shape the shell uses. */
function hotel(overrides: Partial<Hotel> = {}): Hotel {
  return {
    ...TEST_HOTEL,
    address_line1: '12 Harbour Road',
    address_line2: null,
    region: 'Attica',
    postal_code: '18538',
    latitude: '37.942500',
    longitude: '23.646700',
    email: 'front.desk@meridian.test',
    phone: '+30 210 555 0100',
    website: 'https://meridian.test',
    created_at: '2026-01-04T10:00:00+02:00',
    updated_at: '2026-09-01T14:40:00+03:00',
    ...overrides,
  }
}

function roomType(overrides: Partial<RoomType> = {}): RoomType {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    code: 'DLX',
    name: 'Deluxe Sea View',
    description: 'Corner room with a harbour view.',
    max_occupancy: 4,
    standard_occupancy: 3,
    bed_count: 2,
    bed_configuration: '1 king + 1 sofa bed',
    size_sqm: '32.50',
    base_price: '195.00',
    currency: 'EUR',
    is_active: true,
    created_at: '2026-01-04T10:00:00+02:00',
    updated_at: '2026-09-01T14:40:00+03:00',
    ...overrides,
  }
}

const STANDARD = roomType({
  code: 'STD',
  name: 'Standard Double',
  description: null,
  max_occupancy: 3,
  standard_occupancy: 2,
  base_price: '120.00',
  size_sqm: null,
  bed_configuration: null,
})

function page(items: readonly unknown[], total = items.length, pages = 1) {
  return { items, total, page: 1, page_size: 20, pages }
}

/* --- harness ---------------------------------------------------------------------------- */

let fetchStub: FetchStub

/**
 * Registration order is load-bearing. `/hotels?page=` is the membership list the shell
 * reads; `/hotels/{id}` is this page's own read of the record. The second fragment must be
 * specific enough not to catch the first, which is why it carries the id.
 */
beforeEach(() => {
  fetchStub = installFetchStub()
  seedStoredToken()
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage([TEST_HOTEL], 1) })
  fetchStub.on('GET', '/room-types?page=', { body: page([roomType(), STANDARD], 2) })
  fetchStub.on('GET', `/hotels/${TEST_HOTEL.public_id}`, { body: hotel() })
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
})

function renderPage() {
  return renderWithAuth([{ path: ROUTES.property, element: <PropertyPage /> }], {
    initialEntries: [ROUTES.property],
  })
}

/** Requests whose path is exactly this, since `/hotels/{id}` is a prefix of its sub-routes. */
function requestsAt(pathname: string, method = 'GET') {
  return fetchStub.calls.filter(
    (call) => call.method === method && new URL(call.url, 'http://localhost').pathname === pathname,
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

/* --- reading ----------------------------------------------------------------------------- */

describe('the property record', () => {
  it('reads the hotel and its room types, and nothing per row', async () => {
    renderPage()
    await screen.findByText('12 Harbour Road, Piraeus, Attica, 18538, GR')

    expect(requestsAt(`/api/v1/hotels/${TEST_HOTEL.public_id}`)).toHaveLength(1)
    expect(requestsFor('/room-types?page=')).toHaveLength(1)
    // Two types rendered, and not one extra request between them.
    expect(fetchStub.calls.map((call) => new URL(call.url, 'http://localhost').pathname)).toEqual([
      '/api/v1/auth/me',
      '/api/v1/hotels',
      `/api/v1/hotels/${TEST_HOTEL.public_id}`,
      `/api/v1/hotels/${TEST_HOTEL.public_id}/room-types`,
    ])
  })

  it('shows the fields the response carries', async () => {
    renderPage()
    await screen.findByRole('heading', { level: 3, name: 'Meridian Harbour Hotel' })

    expect(screen.getByText('meridian-harbour')).toBeInTheDocument()
    expect(screen.getByText('Europe/Athens')).toBeInTheDocument()
    expect(screen.getByText('front.desk@meridian.test')).toBeInTheDocument()
    expect(screen.getByText('4 of 5')).toBeInTheDocument()
    expect(screen.getByText('37.942500, 23.646700')).toBeInTheDocument()
  })

  it('asks for room types with only the parameters the endpoint has', async () => {
    renderPage()
    await screen.findByText('Deluxe Sea View')

    const url = new URL(lastRequestFor('/room-types?page=')!.url, 'http://localhost')
    expect(url.pathname).toBe(`/api/v1/hotels/${TEST_HOTEL.public_id}/room-types`)
    expect([...url.searchParams.keys()].sort()).toEqual(['page', 'page_size'])
    expect(lastRequestFor('/room-types?page=')!.authorization).toMatch(/^Bearer /)
  })

  it('offers no search, filter or sort, because the endpoints have none', async () => {
    renderPage()
    await screen.findByText('Deluxe Sea View')

    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/search/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/filter/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/sort/i)).not.toBeInTheDocument()
  })

  it('never asks for a page larger than the router allows', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Deluxe Sea View')

    await user.selectOptions(screen.getByLabelText('Per page'), '100')
    await waitFor(() => {
      for (const call of requestsFor('/room-types?page=')) {
        const url = new URL(call.url, 'http://localhost')
        expect(Number(url.searchParams.get('page_size'))).toBeLessThanOrEqual(100)
      }
    })
  })

  it('says a property has no room types without calling that a failure', async () => {
    fetchStub.on('GET', '/room-types?page=', { body: page([], 0, 0) })
    renderPage()

    expect(await screen.findByText('No room types yet')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('treats a malformed 200 as a failure', async () => {
    fetchStub.on('GET', '/room-types?page=', { body: { items: null } })
    renderPage()

    expect(await screen.findByText('Unexpected response')).toBeInTheDocument()
    expect(screen.queryByText('No room types yet')).not.toBeInTheDocument()
  })

  it('renders a 404 on the hotel as an unreachable property', async () => {
    fetchStub.on('GET', `/hotels/${TEST_HOTEL.public_id}`, {
      status: 404,
      body: { error: { code: 'NOT_FOUND', message: 'Hotel not found.' } },
    })
    renderPage()

    expect(await screen.findByText('Property not available')).toBeInTheDocument()
    // The same answer covers "gone" and "not yours", and the copy does not pretend otherwise.
    expect(screen.getByText(/answers the same way for both/)).toBeInTheDocument()
  })

  it('renders a network failure as a failure', async () => {
    fetchStub.on('GET', `/hotels/${TEST_HOTEL.public_id}`, { networkError: true })
    renderPage()

    expect(await screen.findByText('Could not reach the server')).toBeInTheDocument()
  })

  it('keeps the room types readable when the hotel record fails', async () => {
    fetchStub.on('GET', `/hotels/${TEST_HOTEL.public_id}`, {
      status: 500,
      body: { error: { code: 'INTERNAL_ERROR', message: 'Internal server error.' } },
    })
    renderPage()

    expect(await screen.findByText('The property is temporarily unavailable')).toBeInTheDocument()
    expect(screen.getByText('Deluxe Sea View')).toBeInTheDocument()
  })
})

/* --- editing the hotel -------------------------------------------------------------------- */

describe('editing the property', () => {
  async function openForm() {
    const user = userEvent.setup()
    renderPage()
    await screen.findByRole('heading', { level: 3, name: 'Meridian Harbour Hotel' })
    await user.click(screen.getByRole('button', { name: 'Edit property' }))
    return user
  }

  it('shows the slug read-only, because PATCH refuses it', async () => {
    await openForm()

    const slug = screen.getByLabelText('Slug')
    expect(slug).toHaveValue('meridian-harbour')
    expect(slug).toBeDisabled()
    expect(screen.getByText(/does not allow changing it/)).toBeInTheDocument()
  })

  it('patches the hotel without the slug and with is_active', async () => {
    const user = await openForm()
    fetchStub.on('PATCH', `/hotels/${TEST_HOTEL.public_id}`, { body: hotel({ name: 'Renamed' }) })

    await user.clear(screen.getByLabelText('Name'))
    await user.type(screen.getByLabelText('Name'), 'Renamed')
    await user.click(screen.getByRole('button', { name: 'Save property' }))

    await waitFor(() => {
      expect(requestsFor(`/hotels/${TEST_HOTEL.public_id}`, 'PATCH')).toHaveLength(1)
    })
    const body = JSON.parse(
      requestsFor(`/hotels/${TEST_HOTEL.public_id}`, 'PATCH')[0]!.body!,
    ) as Record<string, unknown>
    expect(body.name).toBe('Renamed')
    // Immutable, and a 422 if sent.
    expect(body).not.toHaveProperty('slug')
    expect(body).not.toHaveProperty('public_id')
    // Update-only, and present.
    expect(body).toHaveProperty('is_active')
  })

  it('sends codes as typed, leaving case to the server', async () => {
    const user = await openForm()
    fetchStub.on('PATCH', `/hotels/${TEST_HOTEL.public_id}`, { body: hotel({ country_code: 'GB' }) })

    await user.clear(screen.getByLabelText('Country'))
    await user.type(screen.getByLabelText('Country'), 'gb')
    await user.click(screen.getByRole('button', { name: 'Save property' }))

    await waitFor(() => {
      expect(requestsFor(`/hotels/${TEST_HOTEL.public_id}`, 'PATCH')).toHaveLength(1)
    })
    const body = JSON.parse(
      requestsFor(`/hotels/${TEST_HOTEL.public_id}`, 'PATCH')[0]!.body!,
    ) as Record<string, unknown>
    // As typed: the schema upper-cases it.
    expect(body.country_code).toBe('gb')
    // And the server's row is what is shown afterwards, not the payload.
    expect(
      await screen.findByText('12 Harbour Road, Piraeus, Attica, 18538, GB'),
    ).toBeInTheDocument()
  })

  it('clears an emptied optional with an explicit null', async () => {
    const user = await openForm()
    fetchStub.on('PATCH', `/hotels/${TEST_HOTEL.public_id}`, { body: hotel({ email: null }) })

    await user.clear(screen.getByLabelText('Email (optional)'))
    await user.click(screen.getByRole('button', { name: 'Save property' }))

    await waitFor(() => {
      expect(requestsFor(`/hotels/${TEST_HOTEL.public_id}`, 'PATCH')).toHaveLength(1)
    })
    const body = JSON.parse(
      requestsFor(`/hotels/${TEST_HOTEL.public_id}`, 'PATCH')[0]!.body!,
    ) as Record<string, unknown>
    expect('email' in body).toBe(true)
    expect(body.email).toBeNull()
  })

  it('refuses a star rating outside the schema’s range, without a request', async () => {
    const user = await openForm()

    await user.clear(screen.getByLabelText('Star rating (optional)'))
    await user.type(screen.getByLabelText('Star rating (optional)'), '6')
    await user.click(screen.getByRole('button', { name: 'Save property' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('between 1 and 5')
    expect(requestsFor(`/hotels/${TEST_HOTEL.public_id}`, 'PATCH')).toHaveLength(0)
  })

  it('sends one request when saved twice', async () => {
    const user = await openForm()
    fetchStub.on('PATCH', `/hotels/${TEST_HOTEL.public_id}`, { body: hotel() })

    const save = screen.getByRole('button', { name: 'Save property' })
    await Promise.all([user.click(save), user.click(save)])

    await waitFor(() => {
      expect(requestsFor(`/hotels/${TEST_HOTEL.public_id}`, 'PATCH').length).toBeGreaterThan(0)
    })
    expect(requestsFor(`/hotels/${TEST_HOTEL.public_id}`, 'PATCH')).toHaveLength(1)
  })

  it('names the OWNER role on a 403, and says room types may still be editable', async () => {
    const user = await openForm()
    fetchStub.on('PATCH', `/hotels/${TEST_HOTEL.public_id}`, {
      status: 403,
      body: { error: { code: 'FORBIDDEN', message: 'Requires owner role.' } },
    })

    await user.click(screen.getByRole('button', { name: 'Save property' }))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('Nothing was saved')).toBeInTheDocument()
    expect(within(alert).getByText(/needs the owner role/)).toBeInTheDocument()
    // The two resources really do need different roles.
    expect(within(alert).getByText(/room types needs only manager/)).toBeInTheDocument()
  })

  it('never renders the backend’s own words on a refusal', async () => {
    const user = await openForm()
    fetchStub.on('PATCH', `/hotels/${TEST_HOTEL.public_id}`, {
      status: 500,
      body: {
        error: {
          code: 'INTERNAL',
          message: 'psycopg.errors.UniqueViolation: uq_hotels_slug on table hotels',
        },
      },
    })

    await user.click(screen.getByRole('button', { name: 'Save property' }))

    expect(await screen.findByText('Nothing was saved')).toBeInTheDocument()
    for (const leak of ['psycopg', 'uq_hotels', 'UniqueViolation', 'table hotels']) {
      expect(visibleText()).not.toContain(leak)
    }
  })

  it('offers no control for deleting the property', async () => {
    renderPage()
    await screen.findByRole('heading', { level: 3, name: 'Meridian Harbour Hotel' })

    expect(screen.queryByRole('button', { name: /delete property/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /delete this property/i })).not.toBeInTheDocument()
  })
})

/* --- room types --------------------------------------------------------------------------- */

describe('creating a room type', () => {
  async function openForm() {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Deluxe Sea View')
    await user.click(screen.getByRole('button', { name: /Add room type/ }))
    return user
  }

  it('posts the documented body, with the code and without is_active', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/room-types', { status: 201, body: roomType({ code: 'SUI' }) })

    await user.type(screen.getByLabelText('Code'), 'sui')
    await user.type(screen.getByLabelText('Name'), 'Suite')
    await user.clear(screen.getByLabelText('Base price'))
    await user.type(screen.getByLabelText('Base price'), '320.00')
    await user.click(screen.getByRole('button', { name: 'Create room type' }))

    await waitFor(() => {
      expect(requestsFor('/room-types', 'POST')).toHaveLength(1)
    })
    const request = requestsFor('/room-types', 'POST')[0]!
    expect(new URL(request.url, 'http://localhost').pathname).toBe(
      `/api/v1/hotels/${TEST_HOTEL.public_id}/room-types`,
    )
    const body = JSON.parse(request.body!) as Record<string, unknown>
    // As typed; the schema upper-cases it.
    expect(body.code).toBe('sui')
    // A decimal string, never a number.
    expect(body.base_price).toBe('320.00')
    expect(typeof body.base_price).toBe('string')
    // Update-only, and a 422 at creation.
    expect(body).not.toHaveProperty('is_active')
    // The hotel is in the path; sending it is a 422.
    expect(body).not.toHaveProperty('hotel_public_id')
  })

  it('defaults the currency to the property’s', async () => {
    await openForm()

    expect(screen.getByLabelText('Currency')).toHaveValue('EUR')
  })

  it('refuses a standard occupancy above the maximum, without a request', async () => {
    const user = await openForm()

    await user.type(screen.getByLabelText('Code'), 'SUI')
    await user.type(screen.getByLabelText('Name'), 'Suite')
    await user.type(screen.getByLabelText('Base price'), '320.00')
    await user.clear(screen.getByLabelText('Standard occupancy'))
    await user.type(screen.getByLabelText('Standard occupancy'), '9')
    await user.click(screen.getByRole('button', { name: 'Create room type' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('cannot exceed maximum occupancy')
    expect(requestsFor('/room-types', 'POST')).toHaveLength(0)
  })

  it('refuses a price with too much precision, without a request', async () => {
    const user = await openForm()

    await user.type(screen.getByLabelText('Code'), 'SUI')
    await user.type(screen.getByLabelText('Name'), 'Suite')
    await user.type(screen.getByLabelText('Base price'), '1.555')
    await user.click(screen.getByRole('button', { name: 'Create room type' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('at most two decimal places')
    expect(requestsFor('/room-types', 'POST')).toHaveLength(0)
  })

  it('explains a 409 as a code already in use', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/room-types', {
      status: 409,
      body: {
        error: {
          code: 'CONFLICT',
          message: "This hotel already has a room type with code 'DLX'.",
        },
      },
    })

    await user.type(screen.getByLabelText('Code'), 'DLX')
    await user.type(screen.getByLabelText('Name'), 'Deluxe')
    await user.type(screen.getByLabelText('Base price'), '195.00')
    await user.click(screen.getByRole('button', { name: 'Create room type' }))

    expect(await screen.findByText('Nothing was created')).toBeInTheDocument()
    expect(screen.getByText(/already has a room type with that code/)).toBeInTheDocument()
  })

  it('names the MANAGER role on a 403', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/room-types', {
      status: 403,
      body: { error: { code: 'FORBIDDEN', message: 'Requires manager role.' } },
    })

    await user.type(screen.getByLabelText('Code'), 'SUI')
    await user.type(screen.getByLabelText('Name'), 'Suite')
    await user.type(screen.getByLabelText('Base price'), '320.00')
    await user.click(screen.getByRole('button', { name: 'Create room type' }))

    expect(await screen.findByText('Nothing was created')).toBeInTheDocument()
    expect(screen.getByText(/needs the manager role/)).toBeInTheDocument()
  })

  it('re-reads the list after the server accepts', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/room-types', { status: 201, body: roomType({ code: 'SUI' }) })

    const before = requestsFor('/room-types?page=').length
    await user.type(screen.getByLabelText('Code'), 'SUI')
    await user.type(screen.getByLabelText('Name'), 'Suite')
    await user.type(screen.getByLabelText('Base price'), '320.00')
    await user.click(screen.getByRole('button', { name: 'Create room type' }))

    await waitFor(() => {
      expect(requestsFor('/room-types?page=').length).toBeGreaterThan(before)
    })
    expect(await screen.findByText(/Room type created/)).toBeInTheDocument()
  })

  it('sends one request when the button is clicked twice', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/room-types', { status: 201, body: roomType({ code: 'SUI' }) })

    await user.type(screen.getByLabelText('Code'), 'SUI')
    await user.type(screen.getByLabelText('Name'), 'Suite')
    await user.type(screen.getByLabelText('Base price'), '320.00')
    const submit = screen.getByRole('button', { name: 'Create room type' })
    await Promise.all([user.click(submit), user.click(submit)])

    await waitFor(() => {
      expect(requestsFor('/room-types', 'POST').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/room-types', 'POST')).toHaveLength(1)
  })
})

describe('editing a room type', () => {
  async function openEdit() {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Deluxe Sea View')
    await user.click(screen.getByRole('button', { name: 'Edit DLX' }))
    return user
  }

  it('shows the code read-only, because PATCH refuses it', async () => {
    await openEdit()

    const code = screen.getByLabelText('Code')
    expect(code).toHaveValue('DLX')
    expect(code).toHaveAttribute('readonly')
    expect(screen.getByText(/not something the API allows/)).toBeInTheDocument()
  })

  it('patches without the code and with is_active', async () => {
    const user = await openEdit()
    fetchStub.on('PATCH', '/room-types/', { body: roomType({ name: 'Renamed' }) })

    await user.clear(screen.getByLabelText('Name'))
    await user.type(screen.getByLabelText('Name'), 'Renamed')
    await user.click(screen.getByRole('button', { name: 'Save room type' }))

    await waitFor(() => {
      expect(requestsFor('/room-types/', 'PATCH')).toHaveLength(1)
    })
    const request = requestsFor('/room-types/', 'PATCH')[0]!
    expect(new URL(request.url, 'http://localhost').pathname).toBe(
      `/api/v1/hotels/${TEST_HOTEL.public_id}/room-types/DLX`,
    )
    const body = JSON.parse(request.body!) as Record<string, unknown>
    expect(body.name).toBe('Renamed')
    expect(body).not.toHaveProperty('code')
    expect(body).toHaveProperty('is_active')
  })

  it('explains the 409 the partial-update occupancy rule produces', async () => {
    const user = await openEdit()
    fetchStub.on('PATCH', '/room-types/', {
      status: 409,
      body: {
        error: {
          code: 'CONFLICT',
          message: 'standard_occupancy must not exceed max_occupancy (9 > 4).',
        },
      },
    })

    await user.click(screen.getByRole('button', { name: 'Save room type' }))

    expect(await screen.findByText('Nothing was saved')).toBeInTheDocument()
    expect(screen.getByText(/standard occupancy above the stored maximum/)).toBeInTheDocument()
  })
})

describe('deleting a room type', () => {
  it('asks before it deletes, naming the code', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Deluxe Sea View')

    await user.click(screen.getByRole('button', { name: 'Delete DLX' }))
    expect(requestsFor('/room-types/', 'DELETE')).toHaveLength(0)

    const confirm = screen.getByRole('group', { name: 'Confirm deleting this room type' })
    expect(within(confirm).getByText(/DLX/)).toBeInTheDocument()
    expect(within(confirm).getByText(/cannot be undone/)).toBeInTheDocument()
  })

  it('deletes only after the confirmation, at the right URL', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/room-types/', { status: 204 })
    renderPage()
    await screen.findByText('Deluxe Sea View')

    await user.click(screen.getByRole('button', { name: 'Delete DLX' }))
    await user.click(screen.getByRole('button', { name: 'Yes, delete this room type' }))

    await waitFor(() => {
      expect(requestsFor('/room-types/', 'DELETE')).toHaveLength(1)
    })
    expect(
      new URL(requestsFor('/room-types/', 'DELETE')[0]!.url, 'http://localhost').pathname,
    ).toBe(`/api/v1/hotels/${TEST_HOTEL.public_id}/room-types/DLX`)
    expect(await screen.findByText(/Room type deleted/)).toBeInTheDocument()
  })

  it('cancels without sending anything', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Deluxe Sea View')

    await user.click(screen.getByRole('button', { name: 'Delete DLX' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(requestsFor('/room-types/', 'DELETE')).toHaveLength(0)
  })

  it('explains a 409 and points at withdrawing instead', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/room-types/', {
      status: 409,
      body: {
        error: {
          code: 'CONFLICT',
          message: 'This room type cannot be deleted because rooms are still assigned to it.',
        },
      },
    })
    renderPage()
    await screen.findByText('Deluxe Sea View')

    await user.click(screen.getByRole('button', { name: 'Delete DLX' }))
    await user.click(screen.getByRole('button', { name: 'Yes, delete this room type' }))

    expect(await screen.findByText('The room type was not deleted')).toBeInTheDocument()
    expect(screen.getByText(/withdraw the type from sale instead/)).toBeInTheDocument()
    // Still listed, because it still exists.
    expect(screen.getByText('Deluxe Sea View')).toBeInTheDocument()
  })

  it('sends one request when the confirmation is clicked twice', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/room-types/', { status: 204 })
    renderPage()
    await screen.findByText('Deluxe Sea View')

    await user.click(screen.getByRole('button', { name: 'Delete DLX' }))
    const confirm = screen.getByRole('button', { name: 'Yes, delete this room type' })
    await Promise.all([user.click(confirm), user.click(confirm)])

    await waitFor(() => {
      expect(requestsFor('/room-types/', 'DELETE').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/room-types/', 'DELETE')).toHaveLength(1)
  })
})

/* --- untrusted content, security, accessibility --------------------------------------------- */

describe('safety', () => {
  const PAYLOAD = '<img src=x onerror="document.title=\'pwned\'">'

  it('renders markup in a name or description as text', async () => {
    fetchStub.on('GET', `/hotels/${TEST_HOTEL.public_id}`, { body: hotel({ name: PAYLOAD }) })
    fetchStub.on('GET', '/room-types?page=', {
      body: page([roomType({ name: PAYLOAD, description: PAYLOAD })], 1),
    })
    renderPage()

    expect(await screen.findAllByText(PAYLOAD, { exact: false })).not.toHaveLength(0)
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.title).not.toBe('pwned')
    expect(document.body.innerHTML).toContain('&lt;img')
  })

  it('puts no credential in the page and no internal identifier on screen', async () => {
    renderPage()
    await screen.findByText('Deluxe Sea View')

    expect(document.body.innerHTML).not.toContain('header.payload.signature-test-only')
    for (const call of fetchStub.calls) {
      expect(call.url).not.toContain('header.payload.signature-test-only')
    }
  })
})

describe('accessibility', () => {
  it('gives the room-type table real column headers', async () => {
    renderPage()
    await screen.findByText('Deluxe Sea View')

    const table = screen.getByRole('table', { name: /Room types of this property/ })
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((n) => n.textContent),
    ).toEqual([
      'Code',
      'Name',
      'Sleeps',
      'Beds',
      'Size',
      'Base price',
      'Status',
      'Updated',
      'Actions',
    ])
    expect(within(table).getAllByRole('rowheader')).toHaveLength(2)
  })

  it('names each row’s controls by its code', async () => {
    renderPage()
    await screen.findByText('Deluxe Sea View')

    // Not nine identical "Edit" buttons.
    expect(screen.getByRole('button', { name: 'Edit DLX' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit STD' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete DLX' })).toBeInTheDocument()
  })

  it('labels every control in both forms', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByRole('heading', { level: 3, name: 'Meridian Harbour Hotel' })

    await user.click(screen.getByRole('button', { name: 'Edit property' }))
    for (const label of ['Name', 'Address line 1', 'City', 'Country', 'Time zone', 'Currency']) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    await user.click(screen.getByRole('button', { name: /Add room type/ }))
    for (const label of [
      'Code',
      'Name',
      'Maximum occupancy',
      'Standard occupancy',
      'Bed count',
      'Base price',
      'Currency',
    ]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }
  })

  it('keeps a sane heading hierarchy', async () => {
    renderPage()
    await screen.findByText('Deluxe Sea View')

    expect(screen.getByRole('heading', { level: 1, name: 'Property' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Property details' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Room types' })).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 3, name: 'Meridian Harbour Hotel' }),
    ).toBeInTheDocument()
  })

  it('states active state as a word, not only as colour', async () => {
    fetchStub.on('GET', '/room-types?page=', {
      body: page([roomType(), roomType({ code: 'STD', name: 'Standard', is_active: false })], 2),
    })
    renderPage()
    await screen.findByText('Deluxe Sea View')

    expect(screen.getByText('Bookable')).toBeInTheDocument()
    expect(screen.getByText('Withdrawn')).toBeInTheDocument()
    // And the property's own state, likewise a word.
    expect(screen.getByText('Active')).toBeInTheDocument()
  })
})

describe('the compact layout', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders cards instead of a table at phone width, losing no field', async () => {
    // jsdom does not evaluate media queries, so the breakpoint is stubbed. The assertion is
    // about STRUCTURE: a nine-column table is not made readable at 375px by narrowing it, so
    // a different one is rendered -- carrying the same fields, because dropping four of them
    // on a phone drops the ones somebody needed.
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    )
    renderPage()
    await screen.findByText('Deluxe Sea View')

    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Deluxe Sea View' })).toBeInTheDocument()
    expect(screen.getByText('3 standard, 4 maximum')).toBeInTheDocument()
    expect(screen.getByText('32.50 m²')).toBeInTheDocument()
    // And the per-row controls keep their codes, so they stay distinguishable by name.
    expect(screen.getByRole('button', { name: 'Edit DLX' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete STD' })).toBeInTheDocument()
  })
})
