import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { RoomDetailPage } from '@/pages/RoomDetailPage'
import { RoomsPage } from '@/pages/RoomsPage'
import { ROOM_DETAIL_PATTERN, roomPath, ROUTES } from '@/router/routes'
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
import type { Room, RoomType } from '@/types/room'

/**
 * Room management, as the UI reads and writes it.
 *
 * Mocked at `fetch`, so the method, the URL, **every query parameter** and **the exact request
 * body** are observable. Four things this file is mostly about:
 *
 * * rooms are listed **per room type**, so the URL must carry the type -- there is no
 *   hotel-wide collection to fall back on;
 * * the query surface is `page` and `page_size`, and an invented `status` filter would be
 *   silently ignored by the backend and would look like it worked;
 * * status has **no transition rules**, so all five are always offered;
 * * `room_number` and `room_type_code` are never sent on a PATCH -- both are 422s.
 */

/* --- fixtures --------------------------------------------------------------------------- */

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
    updated_at: '2026-01-04T10:00:00+02:00',
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

function room(overrides: Partial<Room> = {}): Room {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    room_type_code: 'DLX',
    room_number: '201',
    floor: 2,
    status: 'available',
    notes: null,
    is_active: true,
    created_at: '2026-01-04T10:00:00+02:00',
    updated_at: '2026-09-01T14:40:00+03:00',
    ...overrides,
  }
}

function page(items: readonly unknown[], total = items.length, pages = 1, pageNumber = 1) {
  return { items, total, page: pageNumber, page_size: 20, pages }
}

/* --- harness ---------------------------------------------------------------------------- */

let fetchStub: FetchStub

/**
 * Registration order is load-bearing: the stub matches the first handler whose fragment the
 * URL contains. `/room-types?page=` is the catalogue; `/rooms?page=` is a type's room list;
 * `/rooms/` is one room. None of the three is a prefix of another.
 */
beforeEach(() => {
  fetchStub = installFetchStub()
  seedStoredToken()
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage([TEST_HOTEL], 1) })
  fetchStub.on('GET', '/room-types?page=', { body: page([roomType(), STANDARD], 2, 1) })
  fetchStub.on('GET', '/rooms?page=', {
    body: page([room(), room({ room_number: '202', status: 'cleaning', notes: 'Sticky door.' })], 6, 1),
  })
  fetchStub.on('GET', '/rooms/', { body: room() })
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
})

function renderList() {
  return renderWithAuth([{ path: ROUTES.rooms, element: <RoomsPage /> }], {
    initialEntries: [ROUTES.rooms],
  })
}

function renderDetail(typeCode = 'DLX', number = '201') {
  return renderWithAuth(
    [
      { path: ROUTES.rooms, element: <RoomsPage /> },
      { path: ROOM_DETAIL_PATTERN, element: <RoomDetailPage /> },
    ],
    { initialEntries: [roomPath(typeCode, number)] },
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

describe('the room list', () => {
  it('asks for the first type’s rooms, with only the parameters the endpoint has', async () => {
    renderList()
    await screen.findByRole('link', { name: '201' })

    const request = lastRequestFor('/rooms?page=')
    const url = new URL(request!.url, 'http://localhost')
    expect(url.pathname).toBe(`/api/v1/hotels/${TEST_HOTEL.public_id}/room-types/DLX/rooms`)
    expect([...url.searchParams.keys()].sort()).toEqual(['page', 'page_size'])
    expect(url.searchParams.get('page')).toBe('1')
    expect(url.searchParams.get('page_size')).toBe('20')
    expect(request!.authorization).toMatch(/^Bearer /)
  })

  it('fetches the room-type catalogue exactly once, and no type per room', async () => {
    renderList()
    await screen.findByRole('link', { name: '201' })

    expect(requestsFor('/room-types?page=')).toHaveLength(1)
    expect(requestsFor('/rooms?page=')).toHaveLength(1)
    // Two rooms rendered, and not one extra request between them.
    expect(requestsFor('/rooms/')).toHaveLength(0)
    expect(fetchStub.calls).toHaveLength(4) // auth/me, hotels, room-types, rooms
  })

  it('renders each room’s own fields', async () => {
    renderList()
    await screen.findByRole('link', { name: '201' })

    expect(screen.getByText('Available')).toBeInTheDocument()
    expect(screen.getByText('Cleaning')).toBeInTheDocument()
    expect(screen.getByText('Sticky door.')).toBeInTheDocument()
    expect(screen.getAllByText('In service')).toHaveLength(2)
  })

  it('shows the selected type’s own details without a further request', async () => {
    renderList()
    await screen.findByRole('link', { name: '201' })

    expect(screen.getByRole('heading', { name: 'Deluxe Sea View' })).toBeInTheDocument()
    expect(screen.getByText('Corner room with a harbour view.')).toBeInTheDocument()
    // `base_price` is a decimal string, formatted with its currency code.
    expect(screen.getByText('EUR 195.00')).toBeInTheDocument()
    expect(requestsFor('/room-types?page=')).toHaveLength(1)
  })

  it('switches type, and asks the new type’s URL from page 1', async () => {
    const user = userEvent.setup()
    renderList()
    await screen.findByRole('link', { name: '201' })

    await user.selectOptions(screen.getByLabelText('Room type'), 'STD')

    await waitFor(() => {
      const url = new URL(lastRequestFor('/rooms?page=')!.url, 'http://localhost')
      expect(url.pathname).toBe(`/api/v1/hotels/${TEST_HOTEL.public_id}/room-types/STD/rooms`)
      expect(url.searchParams.get('page')).toBe('1')
    })
  })

  it('returns to page 1 when the type changes, because pages are per type', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/rooms?page=', { body: page([room()], 60, 3) })
    renderList()
    await screen.findByText(/Page 1 of 3/)

    await user.click(screen.getByRole('button', { name: /Next/ }))
    await waitFor(() => {
      expect(
        new URL(lastRequestFor('/rooms?page=')!.url, 'http://localhost').searchParams.get('page'),
      ).toBe('2')
    })

    await user.selectOptions(screen.getByLabelText('Room type'), 'STD')
    await waitFor(() => {
      const url = new URL(lastRequestFor('/rooms?page=')!.url, 'http://localhost')
      expect(url.searchParams.get('page')).toBe('1')
      expect(url.pathname).toContain('/room-types/STD/rooms')
    })
  })

  it('offers no search, status filter or sort, because the endpoint has none', async () => {
    renderList()
    await screen.findByRole('link', { name: '201' })

    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/search/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/sort/i)).not.toBeInTheDocument()
    // The only select on the page is the room-type chooser, which is a real path segment.
    expect(screen.getAllByRole('combobox').map((n) => n.getAttribute('id'))).toHaveLength(2) // type + per page
  })

  it('never sends a parameter the backend would silently ignore', async () => {
    const user = userEvent.setup()
    renderList()
    await screen.findByRole('link', { name: '201' })

    await user.selectOptions(screen.getByLabelText('Per page'), '100')
    await waitFor(() => {
      expect(
        new URL(lastRequestFor('/rooms?page=')!.url, 'http://localhost').searchParams.get(
          'page_size',
        ),
      ).toBe('100')
    })

    for (const call of requestsFor('/rooms?page=')) {
      const url = new URL(call.url, 'http://localhost')
      expect([...url.searchParams.keys()].sort()).toEqual(['page', 'page_size'])
      expect(Number(url.searchParams.get('page_size'))).toBeLessThanOrEqual(100)
    }
  })

  it('says a type has no rooms without calling that a failure', async () => {
    fetchStub.on('GET', '/rooms?page=', { body: page([], 0, 0) })
    renderList()

    expect(await screen.findByText('No rooms of this type')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('treats a malformed 200 as a failure, not an empty type', async () => {
    fetchStub.on('GET', '/rooms?page=', { body: { items: null, total: 0 } })
    renderList()

    expect(await screen.findByText('Unexpected response')).toBeInTheDocument()
    expect(screen.queryByText('No rooms of this type')).not.toBeInTheDocument()
  })

  it('renders a 404 on the room list as an unreachable type', async () => {
    fetchStub.on('GET', '/rooms?page=', {
      status: 404,
      body: { error: { code: 'NOT_FOUND', message: 'Room type not found for this hotel.' } },
    })
    renderList()

    expect(await screen.findByText('Rooms not available')).toBeInTheDocument()
  })

  it('stops at the catalogue when the catalogue fails', async () => {
    fetchStub.on('GET', '/room-types?page=', {
      status: 500,
      body: { error: { code: 'INTERNAL_ERROR', message: 'Internal server error.' } },
    })
    renderList()

    expect(await screen.findByText('Room types are temporarily unavailable')).toBeInTheDocument()
    // Rooms are listed by type, so there is nothing to ask for without one.
    expect(requestsFor('/rooms?page=')).toHaveLength(0)
  })

  it('renders a network failure as a failure', async () => {
    fetchStub.on('GET', '/rooms?page=', { networkError: true })
    renderList()

    expect(await screen.findByText('Could not reach the server')).toBeInTheDocument()
  })
})

/* --- creating --------------------------------------------------------------------------- */

describe('creating a room', () => {
  async function openForm() {
    const user = userEvent.setup()
    renderList()
    await screen.findByRole('link', { name: '201' })
    await user.click(screen.getByRole('button', { name: /Add room/ }))
    return user
  }

  it('posts to the selected type’s URL with only the schema’s fields', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/rooms', { status: 201, body: room({ room_number: '299' }) })

    await user.type(screen.getByLabelText('Room number'), '299')
    await user.click(screen.getByRole('button', { name: 'Create room' }))

    await waitFor(() => {
      expect(requestsFor('/rooms', 'POST')).toHaveLength(1)
    })
    const request = requestsFor('/rooms', 'POST')[0]!
    expect(new URL(request.url, 'http://localhost').pathname).toBe(
      `/api/v1/hotels/${TEST_HOTEL.public_id}/room-types/DLX/rooms`,
    )
    const body = JSON.parse(request.body!) as Record<string, unknown>
    expect(body).toEqual({ room_number: '299', status: 'available', is_active: true })
    // The type is in the path; sending it is a 422.
    expect(body).not.toHaveProperty('room_type_code')
    expect(body).not.toHaveProperty('hotel_public_id')
  })

  it('sends the number as typed, leaving canonicalisation to the server', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/rooms', { status: 201, body: room({ room_number: '12A' }) })

    await user.type(screen.getByLabelText('Room number'), '12a')
    await user.type(screen.getByLabelText('Floor (optional)'), '1')
    await user.click(screen.getByRole('button', { name: 'Create room' }))

    await waitFor(() => {
      expect(requestsFor('/rooms', 'POST')).toHaveLength(1)
    })
    const body = JSON.parse(requestsFor('/rooms', 'POST')[0]!.body!) as Record<string, unknown>
    // As typed: the schema upper-cases it, and doing so here too would be a second
    // implementation of one rule.
    expect(body.room_number).toBe('12a')
    expect(body.floor).toBe(1)
  })

  it('requires a number, without a request', async () => {
    const user = await openForm()

    await user.click(screen.getByRole('button', { name: 'Create room' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('needs a number')
    expect(requestsFor('/rooms', 'POST')).toHaveLength(0)
  })

  it('refuses a number the schema’s pattern rejects', async () => {
    const user = await openForm()

    await user.type(screen.getByLabelText('Room number'), '-101')
    await user.click(screen.getByRole('button', { name: 'Create room' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('starts with a letter or digit')
    expect(requestsFor('/rooms', 'POST')).toHaveLength(0)
  })

  it('refuses a floor outside the schema’s bounds', async () => {
    const user = await openForm()

    await user.type(screen.getByLabelText('Room number'), '299')
    await user.type(screen.getByLabelText('Floor (optional)'), '201')
    await user.click(screen.getByRole('button', { name: 'Create room' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('between -10 and 200')
    expect(requestsFor('/rooms', 'POST')).toHaveLength(0)
  })

  it('offers every status the schema has and no more', async () => {
    await openForm()

    const options = within(screen.getByLabelText('Status'))
      .getAllByRole('option')
      .map((o) => (o as HTMLOptionElement).value)
    expect(options).toEqual([
      'available',
      'occupied',
      'cleaning',
      'maintenance',
      'out_of_order',
    ])
  })

  it('sends one request when the button is clicked twice', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/rooms', { status: 201, body: room() })

    await user.type(screen.getByLabelText('Room number'), '299')
    const submit = screen.getByRole('button', { name: 'Create room' })
    await Promise.all([user.click(submit), user.click(submit)])

    await waitFor(() => {
      expect(requestsFor('/rooms', 'POST').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/rooms', 'POST')).toHaveLength(1)
  })

  it('explains a 409 as a property-wide number clash', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/rooms', {
      status: 409,
      body: {
        error: {
          code: 'CONFLICT',
          message: "This hotel already has a room numbered '201'.",
        },
      },
    })

    await user.type(screen.getByLabelText('Room number'), '201')
    await user.click(screen.getByRole('button', { name: 'Create room' }))

    expect(await screen.findByText('Nothing was created')).toBeInTheDocument()
    // The URL reads like a per-type namespace and is not one -- the copy has to say so.
    expect(screen.getByText(/unique across the whole hotel/)).toBeInTheDocument()
  })

  it('names the manager role on a 403', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/rooms', {
      status: 403,
      body: { error: { code: 'FORBIDDEN', message: 'Requires manager role.' } },
    })

    await user.type(screen.getByLabelText('Room number'), '299')
    await user.click(screen.getByRole('button', { name: 'Create room' }))

    expect(await screen.findByText('Nothing was created')).toBeInTheDocument()
    expect(screen.getByText(/needs the manager role/)).toBeInTheDocument()
  })

  it('re-reads the list after the server accepts', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/rooms', { status: 201, body: room({ room_number: '299' }) })

    const before = requestsFor('/rooms?page=').length
    await user.type(screen.getByLabelText('Room number'), '299')
    await user.click(screen.getByRole('button', { name: 'Create room' }))

    await waitFor(() => {
      expect(requestsFor('/rooms?page=').length).toBeGreaterThan(before)
    })
    expect(await screen.findByText(/Room created/)).toBeInTheDocument()
  })

  it('never renders the backend’s own words on a refusal', async () => {
    const user = await openForm()
    fetchStub.on('POST', '/rooms', {
      status: 500,
      body: {
        error: {
          code: 'INTERNAL',
          message: 'psycopg.errors.UniqueViolation: uq_rooms_hotel_id_room_number on table rooms',
        },
      },
    })

    await user.type(screen.getByLabelText('Room number'), '299')
    await user.click(screen.getByRole('button', { name: 'Create room' }))

    expect(await screen.findByText('Nothing was created')).toBeInTheDocument()
    for (const leak of ['psycopg', 'uq_rooms', 'UniqueViolation', 'table rooms']) {
      expect(visibleText()).not.toContain(leak)
    }
  })
})

/* --- the detail page --------------------------------------------------------------------- */

describe('one room', () => {
  it('is addressed by type and number together', async () => {
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Room 201' })

    const url = new URL(lastRequestFor('/rooms/')!.url, 'http://localhost')
    expect(url.pathname).toBe(
      `/api/v1/hotels/${TEST_HOTEL.public_id}/room-types/DLX/rooms/201`,
    )
  })

  it('shows every field the record carries', async () => {
    fetchStub.on('GET', '/rooms/', { body: room({ notes: 'Balcony door sticks.' }) })
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Room 201' })

    const record = screen.getByRole('heading', { level: 2, name: 'Room record' }).closest('section')!
    expect(within(record).getByText('DLX')).toBeInTheDocument()
    expect(within(record).getByText('Available')).toBeInTheDocument()
    expect(within(record).getByText('In service')).toBeInTheDocument()
    expect(within(record).getByText('Balcony door sticks.')).toBeInTheDocument()
  })

  it('reads a 404 as "not under that type" rather than "deleted"', async () => {
    fetchStub.on('GET', '/rooms/', {
      status: 404,
      body: { error: { code: 'NOT_FOUND', message: 'Room not found for this hotel and room type.' } },
    })
    renderDetail()

    expect(await screen.findByText('Room not available')).toBeInTheDocument()
    expect(screen.getByText(/same number under a different type/)).toBeInTheDocument()
  })

  it('treats a malformed 200 as a failure', async () => {
    fetchStub.on('GET', '/rooms/', { body: { room_number: 201 } })
    renderDetail()

    expect(await screen.findByText('Unexpected response')).toBeInTheDocument()
  })
})

/* --- status ------------------------------------------------------------------------------ */

describe('room status', () => {
  it('offers all five states from any state, because the backend has no transitions', async () => {
    fetchStub.on('GET', '/rooms/', { body: room({ status: 'out_of_order' }) })
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Room 201' })

    const group = screen.getByRole('group', { name: 'Set status' })
    for (const label of ['Available', 'Occupied', 'Cleaning', 'Maintenance']) {
      expect(within(group).getByRole('button', { name: label })).toBeEnabled()
    }
    // The current one is marked and cannot be re-sent.
    const current = within(group).getByRole('button', { name: /Out of order \(current\)/ })
    expect(current).toBeDisabled()
    expect(current).toHaveAttribute('aria-current', 'true')
  })

  it('patches only the status field', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/rooms/', { body: room({ status: 'maintenance' }) })
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Room 201' })

    const group = screen.getByRole('group', { name: 'Set status' })
    await user.click(within(group).getByRole('button', { name: 'Maintenance' }))

    await waitFor(() => {
      expect(requestsFor('/rooms/', 'PATCH')).toHaveLength(1)
    })
    const body = JSON.parse(requestsFor('/rooms/', 'PATCH')[0]!.body!) as Record<string, unknown>
    expect(body).toEqual({ status: 'maintenance' })
    // Both are 422s on this endpoint.
    expect(body).not.toHaveProperty('room_number')
    expect(body).not.toHaveProperty('room_type_code')
  })

  it('adopts the server’s row after a status change', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/rooms/', { body: room({ status: 'occupied' }) })
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Room 201' })

    const group = screen.getByRole('group', { name: 'Set status' })
    await user.click(within(group).getByRole('button', { name: 'Occupied' }))

    expect(await screen.findByText(/Status set to occupied/)).toBeInTheDocument()
    expect(within(group).getByRole('button', { name: /Occupied \(current\)/ })).toBeDisabled()
  })

  it('leaves the status as it was when the server refuses', async () => {
    const user = userEvent.setup()
    fetchStub.on('PATCH', '/rooms/', {
      status: 403,
      body: { error: { code: 'FORBIDDEN', message: 'Requires manager role.' } },
    })
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Room 201' })

    const group = screen.getByRole('group', { name: 'Set status' })
    await user.click(within(group).getByRole('button', { name: 'Occupied' }))

    expect(await screen.findByText('Nothing was saved')).toBeInTheDocument()
    expect(screen.getByText(/needs the manager role/)).toBeInTheDocument()
    // Still available: nothing was optimistic.
    expect(within(group).getByRole('button', { name: /Available \(current\)/ })).toBeDisabled()
  })
})

/* --- updating ---------------------------------------------------------------------------- */

describe('updating a room', () => {
  async function openEdit() {
    const user = userEvent.setup()
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Room 201' })
    await user.click(screen.getByRole('button', { name: 'Edit room' }))
    return user
  }

  it('shows the number read-only, because PATCH refuses it', async () => {
    await openEdit()

    const number = screen.getByLabelText('Room number')
    expect(number).toHaveValue('201')
    expect(number).toHaveAttribute('readonly')
    expect(screen.getByText(/not something the API allows/)).toBeInTheDocument()
  })

  it('patches the four writable fields and nothing else', async () => {
    const user = await openEdit()
    fetchStub.on('PATCH', '/rooms/', { body: room({ floor: 5 }) })

    await user.clear(screen.getByLabelText('Floor (optional)'))
    await user.type(screen.getByLabelText('Floor (optional)'), '5')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => {
      expect(requestsFor('/rooms/', 'PATCH')).toHaveLength(1)
    })
    const body = JSON.parse(requestsFor('/rooms/', 'PATCH')[0]!.body!) as Record<string, unknown>
    expect(Object.keys(body).sort()).toEqual(['floor', 'is_active', 'notes', 'status'])
    expect(body.floor).toBe(5)
  })

  it('clears an emptied floor with an explicit null', async () => {
    const user = await openEdit()
    fetchStub.on('PATCH', '/rooms/', { body: room({ floor: null }) })

    await user.clear(screen.getByLabelText('Floor (optional)'))
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => {
      expect(requestsFor('/rooms/', 'PATCH')).toHaveLength(1)
    })
    const body = JSON.parse(requestsFor('/rooms/', 'PATCH')[0]!.body!) as Record<string, unknown>
    expect('floor' in body).toBe(true)
    expect(body.floor).toBeNull()
  })

  it('withdraws a room from service without touching its status', async () => {
    const user = await openEdit()
    fetchStub.on('PATCH', '/rooms/', { body: room({ is_active: false }) })

    await user.click(screen.getByRole('checkbox', { name: /in service/ }))
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => {
      expect(requestsFor('/rooms/', 'PATCH')).toHaveLength(1)
    })
    const body = JSON.parse(requestsFor('/rooms/', 'PATCH')[0]!.body!) as Record<string, unknown>
    expect(body.is_active).toBe(false)
    // The two fields are independent; the status went along unchanged, not derived.
    expect(body.status).toBe('available')
    expect(await screen.findByText('Withdrawn')).toBeInTheDocument()
  })

  it('sends one request when saved twice', async () => {
    const user = await openEdit()
    fetchStub.on('PATCH', '/rooms/', { body: room() })

    const save = screen.getByRole('button', { name: 'Save changes' })
    await Promise.all([user.click(save), user.click(save)])

    await waitFor(() => {
      expect(requestsFor('/rooms/', 'PATCH').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/rooms/', 'PATCH')).toHaveLength(1)
  })
})

/* --- deleting ---------------------------------------------------------------------------- */

describe('deleting a room', () => {
  it('asks before it deletes, naming the room and its type', async () => {
    const user = userEvent.setup()
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Room 201' })

    await user.click(screen.getByRole('button', { name: 'Delete room' }))
    expect(requestsFor('/rooms/', 'DELETE')).toHaveLength(0)

    const confirm = screen.getByRole('group', { name: 'Confirm deleting this room' })
    expect(within(confirm).getByText(/room 201/)).toBeInTheDocument()
    expect(within(confirm).getByText(/cannot be undone/)).toBeInTheDocument()
  })

  it('deletes only after the confirmation', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/rooms/', { status: 204 })
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Room 201' })

    await user.click(screen.getByRole('button', { name: 'Delete room' }))
    await user.click(screen.getByRole('button', { name: 'Yes, delete this room' }))

    await waitFor(() => {
      expect(requestsFor('/rooms/', 'DELETE')).toHaveLength(1)
    })
    expect(new URL(requestsFor('/rooms/', 'DELETE')[0]!.url, 'http://localhost').pathname).toBe(
      `/api/v1/hotels/${TEST_HOTEL.public_id}/room-types/DLX/rooms/201`,
    )
    expect(await screen.findByText('Room deleted')).toBeInTheDocument()
  })

  it('cancels without sending anything', async () => {
    const user = userEvent.setup()
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Room 201' })

    await user.click(screen.getByRole('button', { name: 'Delete room' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(requestsFor('/rooms/', 'DELETE')).toHaveLength(0)
    expect(screen.getByRole('heading', { level: 1, name: 'Room 201' })).toBeInTheDocument()
  })

  it('explains a 409 and points at withdrawing instead', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/rooms/', {
      status: 409,
      body: {
        error: {
          code: 'CONFLICT',
          message: 'This room cannot be deleted because it is still referenced by existing rows.',
        },
      },
    })
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Room 201' })

    await user.click(screen.getByRole('button', { name: 'Delete room' }))
    await user.click(screen.getByRole('button', { name: 'Yes, delete this room' }))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('The room was not deleted')).toBeInTheDocument()
    // Scoped: the section's standing note says the same thing before the click, which is
    // deliberate -- the refusal is the expected outcome for any room ever sold.
    expect(within(alert).getByText(/Withdrawing it from service/)).toBeInTheDocument()
    // Still on screen, because it still exists.
    expect(screen.getByRole('heading', { level: 1, name: 'Room 201' })).toBeInTheDocument()
  })

  it('sends one request when the confirmation is clicked twice', async () => {
    const user = userEvent.setup()
    fetchStub.on('DELETE', '/rooms/', { status: 204 })
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Room 201' })

    await user.click(screen.getByRole('button', { name: 'Delete room' }))
    const confirm = screen.getByRole('button', { name: 'Yes, delete this room' })
    await Promise.all([user.click(confirm), user.click(confirm)])

    await waitFor(() => {
      expect(requestsFor('/rooms/', 'DELETE').length).toBeGreaterThan(0)
    })
    expect(requestsFor('/rooms/', 'DELETE')).toHaveLength(1)
  })
})

/* --- untrusted content, security and accessibility ---------------------------------------- */

describe('room data is untrusted content', () => {
  const PAYLOAD = '<img src=x onerror="document.title=\'pwned\'"> <script>alert(1)</script>'

  it('renders markup in a room’s notes as text', async () => {
    fetchStub.on('GET', '/rooms/', { body: room({ notes: PAYLOAD }) })
    renderDetail()

    expect(await screen.findAllByText(PAYLOAD, { exact: false })).not.toHaveLength(0)
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.querySelector('script')).toBeNull()
    expect(document.title).not.toBe('pwned')
    expect(document.body.innerHTML).toContain('&lt;img')
  })

  it('escapes a payload in a room type’s description too', async () => {
    fetchStub.on('GET', '/room-types?page=', {
      body: page([roomType({ description: PAYLOAD })], 1, 1),
    })
    renderList()
    await screen.findByRole('link', { name: '201' })

    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.body.innerHTML).toContain('&lt;img')
  })

  it('puts no credential in the page and no internal identifier in a route', async () => {
    renderList()
    await screen.findByRole('link', { name: '201' })

    expect(document.body.innerHTML).not.toContain('header.payload.signature-test-only')
    expect(visibleText()).not.toContain('header.payload.signature-test-only')
    // Room numbers are legitimately numeric, so this checks the SHAPE of the route instead:
    // every room link is `/rooms/{type}/{number}` and carries no database id.
    for (const link of screen.getAllByRole('link')) {
      const href = link.getAttribute('href') ?? ''
      if (href.startsWith('/rooms/')) {
        expect(href).toMatch(/^\/rooms\/[A-Za-z0-9._%-]+\/[A-Za-z0-9._%-]+$/)
      }
    }
  })

  it('links each room by type and number', async () => {
    renderList()
    await screen.findByRole('link', { name: '201' })

    expect(screen.getByRole('link', { name: '201' })).toHaveAttribute('href', '/rooms/DLX/201')
    expect(screen.getByRole('link', { name: '202' })).toHaveAttribute('href', '/rooms/DLX/202')
  })
})

describe('accessibility', () => {
  it('gives the list a table with real column headers', async () => {
    renderList()
    await screen.findByRole('link', { name: '201' })

    const table = screen.getByRole('table', { name: /Rooms of this type/ })
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((n) => n.textContent),
    ).toEqual(['Room', 'Type', 'Floor', 'Status', 'Service', 'Notes', 'Updated'])
    expect(within(table).getAllByRole('rowheader')).toHaveLength(2)
  })

  it('labels the room-type chooser and the form’s controls', async () => {
    const user = userEvent.setup()
    renderList()
    await screen.findByRole('link', { name: '201' })

    expect(screen.getByLabelText('Room type')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Add room/ }))
    for (const label of ['Room number', 'Floor (optional)', 'Status', 'Notes (optional)']) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }
    expect(screen.getByRole('checkbox', { name: /in service/ })).toBeInTheDocument()
  })

  it('keeps a sane heading hierarchy on the detail page', async () => {
    renderDetail()
    await screen.findByRole('heading', { level: 1, name: 'Room 201' })

    for (const name of ['Room record', 'Status', 'Delete this room']) {
      expect(screen.getByRole('heading', { level: 2, name })).toBeInTheDocument()
    }
    expect(screen.queryByRole('heading', { level: 3 })).not.toBeInTheDocument()
  })

  it('states status and service as words, not only as colour', async () => {
    renderList()
    await screen.findByRole('link', { name: '201' })

    expect(screen.getByText('Available')).toBeInTheDocument()
    expect(screen.getByText('Cleaning')).toBeInTheDocument()
    expect(screen.getAllByText('In service')).toHaveLength(2)
  })
})
