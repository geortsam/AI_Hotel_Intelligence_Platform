import { api } from '@/services/api/client'
import type { Page } from '@/types/api'
import type {
  Booking,
  BookingReconciliation,
  BookingStatusUpdate,
  StayExtensionRequest,
  StayModificationRequest,
  StayModificationResponse,
} from '@/types/booking'

/**
 * The booking calls this application makes: three reads, and three operational mutations.
 *
 * ## What the backend offers, and what is deliberately absent here
 *
 * Six of the router's methods are wrapped: three reads (Stage 5.5) and the three operational
 * mutations (Stage 5.6). Still absent, and deliberately:
 *
 * * `POST` (create a booking) -- there is no booking-creation screen;
 * * `DELETE` -- destroying a booking is not an operational act, it requires `MANAGER`, and
 *   the domain's way to end a booking is `cancelled`, which the status workflow already does.
 *
 * A typed client method for an endpoint with no interface behind it is an invitation to
 * build one in the wrong stage, so neither is here.
 *
 * The three reads require **membership only**. The three mutations require `HotelRole.STAFF`,
 * declared on the router -- verified live: a `viewer` receives 403 on all three and 200 on
 * all reads.
 *
 * ## The filtering that does not exist
 *
 * `GET /bookings` accepts exactly two query parameters -- `page` and `page_size` -- verified
 * against the live OpenAPI document, not inferred. There is no status filter, no date filter,
 * no guest search and no sort parameter, on this endpoint or on `/guests`.
 *
 * **And unsupported parameters are silently ignored.** Sending `?status=confirmed` returns
 * all 165 bookings with a 200, exactly as if the filter had been applied to a set where
 * everything matched. That is measured, not assumed, and it is the reason this module sends
 * only the two parameters that exist: an invented one would appear to work.
 */

/** The page size the backend defaults to. Restated so the UI can show what it asked for. */
export const DEFAULT_PAGE_SIZE = 20

/**
 * The largest page the API will serve, from the router's own `le=100`.
 *
 * Asking for more is a 422, so the UI offers no size above it. It matters more here than it
 * would elsewhere: with no server-side filtering, page size is the only lever on how much an
 * operator can look through at once.
 */
export const MAX_PAGE_SIZE = 100

export interface BookingPageQuery {
  /** 1-based. The backend rejects 0 with a 422. */
  readonly page: number
  /** 1..100. */
  readonly pageSize: number
}

export const bookingService = {
  /**
   * One page of a hotel's bookings, **most recent arrival date first**.
   *
   * The ordering is the backend's and is not configurable. Each item is a complete
   * `Booking` -- allocations and nightly rates included -- so a list needs no follow-up
   * request per row.
   *
   * Throws `ApiError`: 404 for an unknown hotel (distinct from a known hotel with no
   * bookings, which is a 200 with an empty page), 422 for a page below 1 or a size above
   * 100, 401 for a rejected token.
   */
  listForHotel(
    hotelPublicId: string,
    { page, pageSize }: BookingPageQuery,
    signal?: AbortSignal,
  ): Promise<Page<Booking>> {
    return api.get<Page<Booking>>(`/hotels/${hotelPublicId}/bookings`, {
      query: { page, page_size: pageSize },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * One booking, resolved by hotel **and** public id.
   *
   * That pairing is the tenant boundary, and it is verified: a booking belonging to another
   * property, requested through this hotel's URL, answers `404 "Booking not found for this
   * hotel."` -- the same status and the same message as a booking that does not exist. The UI
   * must therefore not claim to know which of the two happened.
   */
  get(hotelPublicId: string, bookingPublicId: string, signal?: AbortSignal): Promise<Booking> {
    return api.get<Booking>(`/hotels/${hotelPublicId}/bookings/${bookingPublicId}`, {
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * The booking's financial position, derived by the server at read time.
   *
   * **This is the only authoritative source of what a stay is worth.** `booking.total_amount`
   * is the contracted figure and is not defined as the sum of the nightly rates; this
   * endpoint reports both and whether they agree. Nothing in the frontend may compute an
   * alternative.
   *
   * Membership only, like reading the booking itself.
   */
  getReconciliation(
    hotelPublicId: string,
    bookingPublicId: string,
    signal?: AbortSignal,
  ): Promise<BookingReconciliation> {
    return api.get<BookingReconciliation>(
      `/hotels/${hotelPublicId}/bookings/${bookingPublicId}/reconciliation`,
      { ...(signal ? { signal } : {}) },
    )
  },
} as const

/* --- mutations ---------------------------------------------------------------------------
 *
 * All three carry `HotelRole.STAFF`, and all three answer a booking that belongs to another
 * property with `404 "Booking not found for this hotel."` -- the same response as a booking
 * that does not exist. Verified against the live API for each of them individually.
 */

export const bookingMutations = {
  /**
   * Move a booking to another status.
   *
   * `PATCH /hotels/{h}/bookings/{b}` with a `BookingUpdate`. **Returns the full updated
   * booking**, so a caller needs no follow-up read -- and must not manufacture one.
   *
   * The server takes the booking's row lock before reading the status the transition is
   * judged against, so two concurrent requests cannot both validate against a status neither
   * still holds. What that means here: a rejected move is authoritative, not a race this
   * client should retry.
   *
   * Throws `ApiError`: **409** for a move the lifecycle forbids, for a terminal booking, and
   * for a `confirmed` whose room has since been taken (the status cascades to the allocations
   * and re-triggers the exclusion constraint); **403** without the staff role; **404** for an
   * unknown booking or one of another hotel; **422** for a malformed payload.
   */
  updateStatus(
    hotelPublicId: string,
    bookingPublicId: string,
    payload: BookingStatusUpdate,
    signal?: AbortSignal,
  ): Promise<Booking> {
    return api.patch<Booking>(`/hotels/${hotelPublicId}/bookings/${bookingPublicId}`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Restate a booking's whole stay: new dates, and the complete allocation that goes with them.
   *
   * `PATCH /hotels/{h}/bookings/{b}/stay` with a `StayModification`. Permitted only for
   * `pending` and `confirmed`; a `checked_in` booking is refused with a 409 and served by
   * {@link extendStay} instead.
   *
   * **No amount travels in this payload, and none can.** The nights carry no `rate` field at
   * all, and `additionalProperties: false` applies at every level -- sending one is a 422
   * naming the field rather than a number the server quietly ignores. The rates are derived
   * server-side from the room type's configured price.
   *
   * Returns the updated booking **and** what the change did to the money.
   */
  modifyStay(
    hotelPublicId: string,
    bookingPublicId: string,
    payload: StayModificationRequest,
    signal?: AbortSignal,
  ): Promise<StayModificationResponse> {
    return api.patch<StayModificationResponse>(
      `/hotels/${hotelPublicId}/bookings/${bookingPublicId}/stay`,
      { body: payload, ...(signal ? { signal } : {}) },
    )
  },

  /**
   * Keep an in-house guest longer by moving check-out outward.
   *
   * `POST /hotels/{h}/bookings/{b}/stay/extension` with a `StayExtension` -- one field.
   * `checked_in` only. The nights already slept keep the rates they were sold at; only the
   * added nights are priced, by the server.
   *
   * POST rather than PATCH because it appends nights rather than restating a representation,
   * and it is **deliberately not idempotent**: a repeat of the same target date is a 409
   * saying so, rather than a cheerful 200 claiming a second extension happened. That is the
   * shape a retried request takes, which is exactly why this client never retries a mutation.
   */
  extendStay(
    hotelPublicId: string,
    bookingPublicId: string,
    payload: StayExtensionRequest,
    signal?: AbortSignal,
  ): Promise<StayModificationResponse> {
    return api.post<StayModificationResponse>(
      `/hotels/${hotelPublicId}/bookings/${bookingPublicId}/stay/extension`,
      { body: payload, ...(signal ? { signal } : {}) },
    )
  },
} as const
