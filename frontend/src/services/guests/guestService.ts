import { api } from '@/services/api/client'
import type { Page } from '@/types/api'
import type { Guest, GuestCreateRequest, GuestUpdateRequest } from '@/types/guest'

/**
 * The guest calls this application makes, and the complete set the contract offers.
 *
 * ## Everything is hotel-scoped, and that is the identity model
 *
 * Every route is `/hotels/{hotel_public_id}/guests/...`. The hotel segment is the scope, not
 * decoration: **the same physical person at two properties is two rows with two public ids**,
 * and no portfolio-wide guest identity exists. So there is no cross-hotel listing to wrap,
 * and a guest of another property answers 404 through this hotel's URL -- verified live.
 *
 * ## Five verbs, and no sixth
 *
 * List, read, create, update, delete. Confirmed against the live OpenAPI document. The
 * collection itself takes only `POST` and `GET`: `PATCH`, `PUT` and `DELETE` against it are
 * **405**, verified.
 *
 * ## The list takes a page and nothing else
 *
 * `page` and `page_size` (max 100). **There is no search, no filter and no sort.** That was
 * checked rather than assumed, because the backend **ignores an unrecognised query parameter
 * silently** -- `?search=smith` and `?sort=email` each returned all 12 guests with a 200,
 * which on screen is indistinguishable from a filter that matched everything. Ordering is the
 * server's, by surname then forename, and is not configurable.
 *
 * Stage 5.5 wrapped only `get` here, and its note that the list "cannot answer who these
 * twenty bookings are for" still holds -- that is why the bookings list shows no guest names.
 * This stage adds the rest of the surface for the screen that manages guests directly.
 *
 * ## Authorization, verified live
 *
 * Reading requires **membership**. `POST` and `PATCH` require **`HotelRole.STAFF`**. `DELETE`
 * requires **`HotelRole.MANAGER`** -- a higher bar than the other two, declared on the route
 * itself, and the reason the delete control carries its own refusal copy.
 */

/** From `app.services.guest`. */
export const DEFAULT_PAGE_SIZE = 20
/** The router's own `le=100`. Asking for 101 is a 422, verified. */
export const MAX_PAGE_SIZE = 100

export const guestService = {
  /**
   * One page of a hotel's guests, ordered by surname then forename.
   *
   * Throws `ApiError`: **404** for an unknown hotel or one the caller cannot reach --
   * distinct from a known hotel with no guests, which is a 200 with an empty page; **422**
   * for a `page_size` above 100.
   */
  list(
    hotelPublicId: string,
    { page, pageSize }: { page: number; pageSize: number },
    signal?: AbortSignal,
  ): Promise<Page<Guest>> {
    return api.get<Page<Guest>>(`/hotels/${hotelPublicId}/guests`, {
      query: { page, page_size: pageSize },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * One guest, resolved by hotel **and** public id.
   *
   * A guest of another property is not reachable through this hotel's URL -- the lookup is
   * composite -- and answers 404 either way. A malformed identifier is a **422**, because the
   * path parameter is typed as a UUID.
   */
  get(hotelPublicId: string, guestPublicId: string, signal?: AbortSignal): Promise<Guest> {
    return api.get<Guest>(`/hotels/${hotelPublicId}/guests/${guestPublicId}`, {
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Create a guest at this hotel.
   *
   * Throws `ApiError`: **409** when another guest at this hotel already uses the email
   * address -- `uq_guests_hotel_id_email` is partial, so many guests may have none; **403**
   * without the staff role; **404** for an unknown hotel; **422** for an empty or over-long
   * name, an email shorter than three characters, a malformed country code or language, or
   * any field the schema does not have -- `public_id` included, since it is server-assigned.
   */
  create(hotelPublicId: string, payload: GuestCreateRequest, signal?: AbortSignal): Promise<Guest> {
    return api.post<Guest>(`/hotels/${hotelPublicId}/guests`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Update a guest. Only the fields present in the body are written.
   *
   * An omitted field is left untouched; an **explicit null** clears the column, which is how
   * an email or a note is removed. Both verified live, as is the empty payload -- a 200 that
   * changes nothing.
   *
   * Throws `ApiError`: **409** on an email another guest at this hotel already uses,
   * verified; **403** without the staff role; **404** for an unknown guest; **422** for an
   * empty name or an unknown field.
   */
  update(
    hotelPublicId: string,
    guestPublicId: string,
    payload: GuestUpdateRequest,
    signal?: AbortSignal,
  ): Promise<Guest> {
    return api.patch<Guest>(`/hotels/${hotelPublicId}/guests/${guestPublicId}`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Delete a guest. 204, with no body.
   *
   * Throws `ApiError`: **409** when the guest is still referenced -- and both referencing
   * tables block it, though the schema suggests otherwise. `bookings` is `ON DELETE RESTRICT`,
   * the expected refusal; `reviews` declares `ON DELETE SET NULL` over a composite key whose
   * `hotel_id` is `NOT NULL`, so that policy **cannot fire** and the delete fails too. The
   * backend verified this against PostgreSQL rather than inferring it, and reports both as one
   * conflict. Verified live: a seeded guest with reservations is a 409, and a guest with
   * neither deletes with a 204.
   *
   * Also **403** without the *manager* role -- higher than create and update -- and **404**
   * for an unknown guest.
   */
  remove(hotelPublicId: string, guestPublicId: string, signal?: AbortSignal): Promise<void> {
    return api.delete<void>(`/hotels/${hotelPublicId}/guests/${guestPublicId}`, {
      ...(signal ? { signal } : {}),
    })
  },
} as const
