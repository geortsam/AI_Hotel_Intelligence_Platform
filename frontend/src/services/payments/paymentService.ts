import { api } from '@/services/api/client'
import type { Page } from '@/types/api'
import type { ChargeRequest, Payment, RefundRequest } from '@/types/payment'

/**
 * The payment calls this application makes.
 *
 * ## The API is booking-scoped, and so is the UI
 *
 * Every route lives under
 * `/hotels/{hotel_public_id}/bookings/{booking_public_id}/payments`. **There is no
 * hotel-wide payment listing** -- no `/hotels/{h}/payments`, no search, no filter by date or
 * provider. Confirmed against the live OpenAPI document. So the ledger is presented inside
 * the booking it settles, which is what the contract can actually answer.
 *
 * ## Append-only, and that is the contract
 *
 * There is no PATCH and no DELETE on any payment route, and the router's own docstring says
 * neither should be added: a payment is a financial record, and a mistake is corrected by
 * posting a reversal. Nothing here wraps a verb that does not exist, and the absence is the
 * reason the UI offers no edit or delete control.
 *
 * ## Authorization
 *
 * The two reads require **membership only**; the two writes require **`HotelRole.STAFF`**,
 * declared on the router. Verified live: a viewer receives 403 on both writes.
 */

/** From `app.services.payment`. Restated so the UI can show what it asked for. */
export const DEFAULT_PAGE_SIZE = 20
/** The router's own `le=100`. Asking for more is a 422. */
export const MAX_PAGE_SIZE = 100

export const paymentService = {
  /**
   * One page of a booking's postings, **oldest first**.
   *
   * Charges and refunds arrive interleaved in one list, distinguished by `kind`. The
   * ordering is the backend's and is not configurable.
   *
   * A booking with no payments is a **200 with an empty page**, not a 404 -- the 404 is
   * reserved for an unknown hotel or booking, which is a different answer and is rendered
   * differently.
   */
  listForBooking(
    hotelPublicId: string,
    bookingPublicId: string,
    { page, pageSize }: { page: number; pageSize: number },
    signal?: AbortSignal,
  ): Promise<Page<Payment>> {
    return api.get<Page<Payment>>(
      `/hotels/${hotelPublicId}/bookings/${bookingPublicId}/payments`,
      { query: { page, page_size: pageSize }, ...(signal ? { signal } : {}) },
    )
  },

  /**
   * Post a charge against a booking.
   *
   * Throws `ApiError`: **409** when the `(provider, transaction_reference)` pair has already
   * been recorded -- payment providers deliver webhooks at least once, so a repeat must not
   * create a second row; **403** without the staff role; **404** for an unknown booking or
   * one belonging to another hotel; **422** for a non-positive amount, a `captured` status
   * without `paid_at`, a client-supplied `kind`, or any field the schema does not have.
   *
   * The caller supplies `amount` as a decimal string. It is never parsed here.
   */
  createCharge(
    hotelPublicId: string,
    bookingPublicId: string,
    payload: ChargeRequest,
    signal?: AbortSignal,
  ): Promise<Payment> {
    return api.post<Payment>(`/hotels/${hotelPublicId}/bookings/${bookingPublicId}/payments`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Post a refund against an existing payment on the same booking.
   *
   * **The server decides whether it is allowed, under the parent's row lock.** It refuses,
   * all with 409: a refund against another refund; a currency other than the parent's; a
   * parent whose status says no money moved (`failed`, `cancelled`); and an amount exceeding
   * `parent.amount - already_refunded`, where the already-refunded sum excludes voided rows
   * and is read inside the lock.
   *
   * That last figure is **not exposed by any endpoint** -- there is no field for it and no
   * preview route. It appears only in the 409 message. So nothing in this application
   * displays a remaining-refundable amount, because nothing in this application is entitled
   * to state one.
   *
   * A 404 means the named payment is not on this booking, which is the same answer as a
   * payment that does not exist.
   */
  createRefund(
    hotelPublicId: string,
    bookingPublicId: string,
    payload: RefundRequest,
    signal?: AbortSignal,
  ): Promise<Payment> {
    return api.post<Payment>(
      `/hotels/${hotelPublicId}/bookings/${bookingPublicId}/payments/refunds`,
      { body: payload, ...(signal ? { signal } : {}) },
    )
  },
} as const
