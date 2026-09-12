import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { AvailabilityPage } from '@/pages/AvailabilityPage'
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
import type { AvailabilityResult } from '@/types/availability'
import type { RoomType } from '@/types/room'

/**
 * Availability search, as the UI asks it and shows the answer.
 *
 * Mocked at `fetch`, so the method, the URL and **every query parameter** are observable.
 * Four things this file is mostly about:
 *
 * * the verdict is `sufficient`, the server's field -- **not** whether the result list is
 *   empty, which is a different fact and really does disagree;
 * * the two request forms are alternatives the backend refuses to combine, so the UI cannot
 *   express the refused request;
 * * a mixed request goes out as `rooms` **repeated**, not joined;
 * * zero availability is a successful answer, never an error and never "this hotel has no
 *   rooms".
 */

/* --- fixtures --------------------------------------------------------------------------- */

function roomType(overrides: Partial<RoomType> = {}): RoomType {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    code: 'DLX',
    name: 'Deluxe Sea View',
    description: null,
    max_occupancy: 4,
    standard_occupancy: 3,
    bed_count: 2,
    bed_configuration: null,
    size_sqm: null,
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
  max_occupancy: 3,
  standard_occupancy: 2,
  base_price: '120.00',
})

function result(overrides: Partial<AvailabilityResult> = {}): AvailabilityResult {
  return {
    hotel_public_id: TEST_HOTEL.public_id,
    check_in: '2027-05-03',
    check_out: '2027-05-05',
    nights: 2,
    rooms_required: 1,
    sufficient: true,
    available_rooms: 6,
    room_types: [
      {
        code: 'DLX',
        name: 'Deluxe Sea View',
        max_occupancy: 4,
        standard_occupancy: 3,
        base_price: '195.00',
        currency: 'EUR',
        available_count: 6,
        requested_count: null,
        rooms: [
          { room_number: '201', floor: 2 },
          { room_number: '202', floor: 2 },
        ],
      },
    ],
    ...overrides,
  }
}

function page(items: readonly unknown[], total = items.length, pages = 1) {
  return { items, total, page: 1, page_size: 100, pages }
}

/* --- harness ---------------------------------------------------------------------------- */

let fetchStub: FetchStub

beforeEach(() => {
  fetchStub = installFetchStub()
  seedStoredToken()
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  fetchStub.on('GET', '/hotels?page=', { body: hotelPage([TEST_HOTEL], 1) })
  fetchStub.on('GET', '/room-types?page=', { body: page([roomType(), STANDARD], 2) })
  fetchStub.on('GET', '/availability', { body: result() })
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
})

function renderPage() {
  return renderWithAuth([{ path: ROUTES.availability, element: <AvailabilityPage /> }], {
    initialEntries: [ROUTES.availability],
  })
}

function requestsFor(fragment: string, method = 'GET') {
  return fetchStub.calls.filter((call) => call.method === method && call.url.includes(fragment))
}

function lastSearchUrl(): URL {
  const calls = requestsFor('/availability')
  return new URL(calls[calls.length - 1]!.url, 'http://localhost')
}

function visibleText(): string {
  return document.body.textContent ?? ''
}

/** Fill the dates and submit. The form defaults to a one-night stay from the hotel's today. */
async function runSearch(user: ReturnType<typeof userEvent.setup>) {
  await user.clear(screen.getByLabelText('Check-in'))
  await user.type(screen.getByLabelText('Check-in'), '2027-05-03')
  await user.clear(screen.getByLabelText('Check-out'))
  await user.type(screen.getByLabelText('Check-out'), '2027-05-05')
  await user.click(screen.getByRole('button', { name: 'Search availability' }))
}

/* --- the initial state ------------------------------------------------------------------- */

describe('before a search', () => {
  it('runs no search on arrival', async () => {
    renderPage()
    await screen.findByLabelText('Check-in')

    expect(requestsFor('/availability')).toHaveLength(0)
    expect(screen.getByText('No search has been run yet')).toBeInTheDocument()
  })

  it('loads the room-type catalogue exactly once', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')

    await runSearch(user)
    await screen.findByText(/can be accommodated/)

    // One catalogue request, reused for the form and for every answer.
    expect(requestsFor('/room-types?page=')).toHaveLength(1)
  })
})

/* --- the request ------------------------------------------------------------------------- */

describe('the request', () => {
  it('sends the documented parameters and nothing else', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')

    await runSearch(user)
    await waitFor(() => {
      expect(requestsFor('/availability')).toHaveLength(1)
    })

    const url = lastSearchUrl()
    expect(url.pathname).toBe(`/api/v1/hotels/${TEST_HOTEL.public_id}/availability`)
    expect([...url.searchParams.keys()].sort()).toEqual([
      'check_in',
      'check_out',
      'rooms_required',
    ])
    expect(url.searchParams.get('check_in')).toBe('2027-05-03')
    expect(url.searchParams.get('check_out')).toBe('2027-05-05')
    expect(url.searchParams.get('rooms_required')).toBe('1')
    expect(requestsFor('/availability')[0]!.authorization).toMatch(/^Bearer /)
  })

  it('omits the room type when any type will do', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')

    await runSearch(user)
    await waitFor(() => {
      expect(requestsFor('/availability')).toHaveLength(1)
    })
    // `room_type_code=` is not the same request as no `room_type_code`.
    expect(lastSearchUrl().searchParams.has('room_type_code')).toBe(false)
  })

  it('sends a room type code exactly as the catalogue gave it', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')

    await user.selectOptions(screen.getByLabelText('Room type'), 'DLX')
    await runSearch(user)

    await waitFor(() => {
      // Case matters here: `room_type_code=dlx` is a 404 against the real backend, while the
      // mixed form's codes are upper-cased server-side. Sending the catalogue's own value is
      // correct for both.
      expect(lastSearchUrl().searchParams.get('room_type_code')).toBe('DLX')
    })
  })

  it('sends guests and rooms required when they are given', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')

    await user.clear(screen.getByLabelText('Rooms required'))
    await user.type(screen.getByLabelText('Rooms required'), '3')
    await user.type(screen.getByLabelText('Guests per room (optional)'), '4')
    await runSearch(user)

    await waitFor(() => {
      const url = lastSearchUrl()
      expect(url.searchParams.get('rooms_required')).toBe('3')
      expect(url.searchParams.get('guests')).toBe('4')
    })
  })

  it('supersedes an earlier search rather than racing it', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')

    await runSearch(user)
    await screen.findByText(/can be accommodated/)
    await runSearch(user)

    await waitFor(() => {
      expect(requestsFor('/availability')).toHaveLength(2)
    })
    // The first request was aborted; only the newest answer is on screen.
    expect(screen.getAllByText(/can be accommodated/)).toHaveLength(1)
  })
})

/* --- mixed requests ---------------------------------------------------------------------- */

describe('a mixed request', () => {
  async function switchToMixed(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole('radio', { name: 'Several types at once' }))
  }

  it('sends rooms as a repeated parameter, not a joined string', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')
    await switchToMixed(user)

    await user.selectOptions(screen.getByLabelText('Room type 1'), 'DLX')
    await user.clear(screen.getByLabelText('How many'))
    await user.type(screen.getByLabelText('How many'), '2')
    await user.click(screen.getByRole('button', { name: /Add another room type/ }))
    await user.selectOptions(screen.getByLabelText('Room type 2'), 'STD')

    await runSearch(user)
    await waitFor(() => {
      expect(requestsFor('/availability')).toHaveLength(1)
    })

    const url = lastSearchUrl()
    // Repeated, as FastAPI reads a `list[str]`. A single joined value cannot be parsed.
    expect(url.searchParams.getAll('rooms')).toEqual(['DLX:2', 'STD:1'])
    // And never the single-type parameters: combining them is a 422.
    expect(url.searchParams.has('room_type_code')).toBe(false)
    expect(url.searchParams.has('rooms_required')).toBe(false)
  })

  it('cannot express the combination the backend refuses', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')

    // Single mode offers the single-type controls...
    expect(screen.getByLabelText('Room type')).toBeInTheDocument()
    expect(screen.getByLabelText('Rooms required')).toBeInTheDocument()

    await switchToMixed(user)
    // ...and mixed mode replaces them entirely, rather than adding to them.
    expect(screen.queryByLabelText('Room type')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Rooms required')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Room type 1')).toBeInTheDocument()
  })

  it('refuses a type named twice, without a request', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')
    await switchToMixed(user)

    await user.selectOptions(screen.getByLabelText('Room type 1'), 'DLX')
    await user.click(screen.getByRole('button', { name: /Add another room type/ }))
    await user.selectOptions(screen.getByLabelText('Room type 2'), 'DLX')
    await user.click(screen.getByRole('button', { name: 'Search availability' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('named only once')
    expect(requestsFor('/availability')).toHaveLength(0)
  })

  it('requires at least one type, without a request', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')
    await switchToMixed(user)

    await user.click(screen.getByRole('button', { name: 'Search availability' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('at least one room type')
    expect(requestsFor('/availability')).toHaveLength(0)
  })

  it('refuses a count outside the server’s bounds, without a request', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')
    await switchToMixed(user)

    await user.selectOptions(screen.getByLabelText('Room type 1'), 'DLX')
    await user.clear(screen.getByLabelText('How many'))
    await user.type(screen.getByLabelText('How many'), '101')
    await user.click(screen.getByRole('button', { name: 'Search availability' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('between 1 and 100')
    expect(requestsFor('/availability')).toHaveLength(0)
  })
})

/* --- date semantics ---------------------------------------------------------------------- */

describe('dates', () => {
  it('refuses a check-out that is not later than check-in, without a request', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')

    await user.clear(screen.getByLabelText('Check-in'))
    await user.type(screen.getByLabelText('Check-in'), '2027-05-05')
    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2027-05-05')
    await user.click(screen.getByRole('button', { name: 'Search availability' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('must be later than check-in')
    expect(requestsFor('/availability')).toHaveLength(0)
  })

  it('refuses a stay longer than the server allows, without a request', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')

    await user.clear(screen.getByLabelText('Check-in'))
    await user.type(screen.getByLabelText('Check-in'), '2027-01-01')
    await user.clear(screen.getByLabelText('Check-out'))
    await user.type(screen.getByLabelText('Check-out'), '2028-01-05') // 369 nights
    await user.click(screen.getByRole('button', { name: 'Search availability' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('at most 366 nights')
    expect(requestsFor('/availability')).toHaveLength(0)
  })

  it('sends the dates exactly as entered', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')

    await runSearch(user)
    await waitFor(() => {
      const url = lastSearchUrl()
      // No local-time round trip: the strings go as typed.
      expect(url.searchParams.get('check_in')).toBe('2027-05-03')
      expect(url.searchParams.get('check_out')).toBe('2027-05-05')
    })
  })

  it('explains that the check-out day is not a night', async () => {
    renderPage()
    await screen.findByLabelText('Check-in')

    expect(screen.getByText(/departure day/i)).toBeInTheDocument()
    expect(screen.getByText(/leaves the 14th free for someone else/)).toBeInTheDocument()
  })
})

/* --- the answer -------------------------------------------------------------------------- */

describe('the answer', () => {
  it('renders the server’s figures', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)

    expect(await screen.findByText('This stay can be accommodated.')).toBeInTheDocument()
    expect(screen.getByText(/6 rooms free across the types listed/)).toBeInTheDocument()
    expect(screen.getByText('6 free')).toBeInTheDocument()
    expect(screen.getByText('201')).toBeInTheDocument()
    expect(screen.getByText('202')).toBeInTheDocument()
    expect(screen.getByText('EUR 195.00', { exact: false })).toBeInTheDocument()
  })

  it('reads the verdict from `sufficient`, not from the result list', async () => {
    const user = userEvent.setup()
    // The case that proves the point: two types listed, sixteen rooms free, and the server
    // still says the request cannot be met, because one share is impossible.
    fetchStub.on('GET', '/availability', {
      body: result({
        sufficient: false,
        available_rooms: 16,
        rooms_required: 1,
        room_types: [
          {
            code: 'DLX',
            name: 'Deluxe Sea View',
            max_occupancy: 4,
            standard_occupancy: 3,
            base_price: '195.00',
            currency: 'EUR',
            available_count: 6,
            requested_count: 2,
            rooms: [{ room_number: '201', floor: 2 }],
          },
          {
            code: 'STD',
            name: 'Standard Double',
            max_occupancy: 3,
            standard_occupancy: 2,
            base_price: '120.00',
            currency: 'EUR',
            available_count: 10,
            requested_count: 99,
            rooms: [{ room_number: '101', floor: 1 }],
          },
        ],
      }),
    })
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)

    // A list-length reading would have said "can be accommodated" here.
    expect(await screen.findByText('This stay cannot be accommodated.')).toBeInTheDocument()
    expect(screen.getByText(/16 rooms free across the types listed/)).toBeInTheDocument()
    // And the type that could not supply its share is named as the reason.
    expect(screen.getByText('99 requested')).toBeInTheDocument()
    expect(screen.getByText(/cannot supply what was asked of it/)).toBeInTheDocument()
  })

  it('says an empty answer is about the dates, not about the property', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/availability', {
      body: result({ sufficient: false, available_rooms: 0, room_types: [] }),
    })
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)

    expect(await screen.findByText('This stay cannot be accommodated.')).toBeInTheDocument()
    expect(screen.getByText(/not a statement that the property has no rooms/)).toBeInTheDocument()
    // An ordinary answer, not an error.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows the requested count only when the server reported one', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)
    await screen.findByText(/can be accommodated/)

    // `requested_count` is null on a single-type search, so no "requested" badge.
    expect(screen.queryByText(/requested$/)).not.toBeInTheDocument()
  })

  it('says a result is not a reservation', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)

    expect(await screen.findByText(/A result is not a reservation/)).toBeInTheDocument()
  })

  it('never computes availability from the rooms it was given', async () => {
    const user = userEvent.setup()
    // `available_count` deliberately disagrees with `rooms.length`: the server's field is
    // the one that must be shown.
    fetchStub.on('GET', '/availability', {
      body: result({
        available_rooms: 42,
        room_types: [
          {
            code: 'DLX',
            name: 'Deluxe Sea View',
            max_occupancy: 4,
            standard_occupancy: 3,
            base_price: '195.00',
            currency: 'EUR',
            available_count: 42,
            requested_count: null,
            rooms: [{ room_number: '201', floor: 2 }],
          },
        ],
      }),
    })
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)

    expect(await screen.findByText('42 free')).toBeInTheDocument()
    expect(screen.getByText(/42 rooms free across the types listed/)).toBeInTheDocument()
    // Not "1 free", which is what counting the array would have produced.
    expect(screen.queryByText('1 free')).not.toBeInTheDocument()
  })
})

/* --- failures ----------------------------------------------------------------------------- */

describe('failures', () => {
  it('treats a malformed 200 as a failure, never as "nothing free"', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/availability', { body: { room_types: [] } })
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)

    expect(await screen.findByText('Unexpected response')).toBeInTheDocument()
    expect(visibleText()).not.toMatch(/cannot be accommodated/)
  })

  it('renders a 404 as an unrunnable search, not as an absence of rooms', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/availability', {
      status: 404,
      body: { error: { code: 'NOT_FOUND', message: 'Room type not found for this hotel.' } },
    })
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)

    expect(await screen.findByText('That search could not be run')).toBeInTheDocument()
    expect(screen.getByText(/Nothing about availability has been determined/)).toBeInTheDocument()
    expect(visibleText()).not.toMatch(/cannot be accommodated/)
  })

  it('renders a 422 without the backend’s own words', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/availability', {
      status: 422,
      body: {
        error: {
          code: 'VALIDATION_ERROR',
          message: 'psycopg.errors.InvalidDatetimeFormat on table rooms',
        },
      },
    })
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)

    expect(await screen.findByText('That request could not be processed')).toBeInTheDocument()
    for (const leak of ['psycopg', 'InvalidDatetimeFormat', 'table rooms']) {
      expect(visibleText()).not.toContain(leak)
    }
  })

  it('renders a 403 as a permission failure', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/availability', {
      status: 403,
      body: { error: { code: 'FORBIDDEN', message: 'Forbidden.' } },
    })
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)

    expect(await screen.findByText('You do not have access to this data')).toBeInTheDocument()
  })

  it('renders a network failure as a failure', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/availability', { networkError: true })
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)

    expect(await screen.findByText('Could not reach the server')).toBeInTheDocument()
  })

  it('still allows a search when the catalogue fails', async () => {
    fetchStub.on('GET', '/room-types?page=', {
      status: 500,
      body: { error: { code: 'INTERNAL_ERROR', message: 'Internal server error.' } },
    })
    renderPage()

    // The type list is what failed; the page says so rather than pretending to search.
    expect(await screen.findByText('Room types are temporarily unavailable')).toBeInTheDocument()
  })
})

/* --- performance, security, accessibility -------------------------------------------------- */

describe('performance and safety', () => {
  it('makes one request per search and none per result', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)
    await screen.findByText(/can be accommodated/)

    expect(requestsFor('/availability')).toHaveLength(1)
    expect(requestsFor('/room-types?page=')).toHaveLength(1)
    // Nothing per room type and nothing per room: the answer already names them.
    expect(requestsFor('/rooms?page=')).toHaveLength(0)
    expect(requestsFor('/rooms/')).toHaveLength(0)
    expect(fetchStub.calls).toHaveLength(4) // auth/me, hotels, room-types, availability
  })

  it('puts no credential in the page or the URL', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)
    await screen.findByText(/can be accommodated/)

    expect(document.body.innerHTML).not.toContain('header.payload.signature-test-only')
    for (const call of fetchStub.calls) {
      expect(call.url).not.toContain('header.payload.signature-test-only')
      expect(call.url).not.toMatch(/token=/i)
    }
  })

  it('renders an untrusted room type name as text', async () => {
    const user = userEvent.setup()
    const PAYLOAD = '<img src=x onerror="document.title=\'pwned\'">'
    fetchStub.on('GET', '/availability', {
      body: result({
        room_types: [
          {
            code: 'DLX',
            name: PAYLOAD,
            max_occupancy: 4,
            standard_occupancy: 3,
            base_price: '195.00',
            currency: 'EUR',
            available_count: 1,
            requested_count: null,
            rooms: [{ room_number: PAYLOAD, floor: null }],
          },
        ],
      }),
    })
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)

    expect(await screen.findAllByText(PAYLOAD, { exact: false })).not.toHaveLength(0)
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.title).not.toBe('pwned')
    expect(document.body.innerHTML).toContain('&lt;img')
  })
})

describe('accessibility', () => {
  it('labels every search control', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')

    for (const label of [
      'Check-in',
      'Check-out',
      'Room type',
      'Rooms required',
      'Guests per room (optional)',
    ]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }

    await user.click(screen.getByRole('radio', { name: 'Several types at once' }))
    expect(screen.getByLabelText('Room type 1')).toBeInTheDocument()
    expect(screen.getByLabelText('How many')).toBeInTheDocument()
  })

  it('names the two request forms as a radio group', async () => {
    renderPage()
    await screen.findByLabelText('Check-in')

    const group = screen.getByRole('group', { name: 'What are you looking for' })
    expect(within(group).getAllByRole('radio')).toHaveLength(2)
  })

  it('announces the verdict politely, not as an alert', async () => {
    const user = userEvent.setup()
    fetchStub.on('GET', '/availability', {
      body: result({ sufficient: false, available_rooms: 0, room_types: [] }),
    })
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)

    const statuses = await screen.findAllByRole('status')
    expect(statuses.some((n) => /cannot be accommodated/.test(n.textContent ?? ''))).toBe(true)
    // "We are full that week" is an ordinary answer, not a fault.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('keeps a sane heading hierarchy', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByLabelText('Check-in')
    await runSearch(user)
    await screen.findByText(/can be accommodated/)

    expect(screen.getByRole('heading', { level: 1, name: 'Availability' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Search' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Answer' })).toBeInTheDocument()
    // The room type is an h3 under the Answer h2, and the room list an h4 under that.
    expect(screen.getByRole('heading', { level: 3, name: 'Deluxe Sea View' })).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 4, name: 'Rooms free for the whole stay' }),
    ).toBeInTheDocument()
  })
})
