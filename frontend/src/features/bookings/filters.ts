import type { Booking, BookingStatus } from '@/types/booking'

/**
 * Narrowing the bookings **on the current page**, and being loud about that scope.
 *
 * ## Why this is client-side, and why it is only the page
 *
 * `GET /hotels/{h}/bookings` accepts `page` and `page_size` and nothing else. There is no
 * status parameter, no date parameter, no guest search and no sort -- confirmed against the
 * live OpenAPI document. Sending an invented one is worse than useless here, because the
 * backend **ignores unrecognised query parameters silently**: `?status=confirmed` returns
 * all 165 bookings with a 200, which looks exactly like a filter that matched everything.
 *
 * The other tempting option is to fetch every page and filter the union. That is the thing
 * the stage brief forbids and it deserves the reason: with 100 rows per page a hotel with a
 * year of history is dozens of sequential requests for one keystroke, it grows without bound
 * as the property does, and it moves a decision the database is built to make into a browser.
 *
 * So filtering is honestly limited to the rows already fetched, and **every part of the UI
 * says so** -- the control's own label, the result count and the empty state. A filter whose
 * scope is invisible is the genuinely harmful version: an operator who searches a reference
 * that sits on page 3 and is told "no bookings" has been misled by the interface.
 *
 * The page-size control is the lever that makes this useful rather than a toy: 100 rows can
 * be pulled and narrowed in one request.
 */

/** `all` is a real option, not a null: it is what the control shows when nothing is chosen. */
export type StatusFilter = BookingStatus | 'all'

export interface BookingFilterState {
  readonly status: StatusFilter
  /** Free text, matched against the fields an operator would actually type. */
  readonly search: string
}

export const EMPTY_FILTERS: BookingFilterState = { status: 'all', search: '' }

export function hasActiveFilters(filters: BookingFilterState): boolean {
  return filters.status !== 'all' || filters.search.trim() !== ''
}

/**
 * The fields free text is matched against.
 *
 * Chosen from what someone at a desk has in front of them: a reference on a printout, a room
 * number on a key card, the name written on the allocation. **Not** the guest's email or
 * phone -- the list does not hold them, and making contact details searchable is a different
 * feature with a different privacy question attached to it.
 *
 * The booking's own UUID is excluded too. Nobody types one, and matching it would let a
 * pasted identifier confirm which page a booking is on.
 */
function searchableText(booking: Booking): string {
  const rooms = booking.rooms
    .map((room) => `${room.room_number} ${room.room_type_code} ${room.guest_name ?? ''}`)
    .join(' ')
  return `${booking.reference} ${booking.channel_reference ?? ''} ${rooms}`.toLowerCase()
}

/**
 * Apply the filters to one page of bookings.
 *
 * Order is preserved, so the backend's "most recent arrival first" survives filtering. This
 * never reorders, because sorting is not something the API offers and a client-side sort of
 * one page would claim an ordering the other 8 pages do not share.
 */
export function applyFilters(
  bookings: readonly Booking[],
  filters: BookingFilterState,
): readonly Booking[] {
  const term = filters.search.trim().toLowerCase()
  if (filters.status === 'all' && term === '') {
    return bookings
  }
  return bookings.filter((booking) => {
    if (filters.status !== 'all' && booking.status !== filters.status) {
      return false
    }
    return term === '' || searchableText(booking).includes(term)
  })
}
