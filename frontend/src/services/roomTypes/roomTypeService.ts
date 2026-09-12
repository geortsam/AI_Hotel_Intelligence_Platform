import { api } from '@/services/api/client'
import type { Page } from '@/types/api'
import type { RoomType } from '@/types/room'
import type { RoomTypeCreateRequest, RoomTypeUpdateRequest } from '@/types/roomType'

/**
 * The room-type calls this application makes for **managing** them.
 *
 * ## Why this is a separate module from `roomService`
 *
 * Stage 5.11's `roomService.listTypes` reads the catalogue because a room cannot be listed
 * without naming its type. That is a *consumer* of room types; this is the surface that
 * creates, edits and removes them. Extending the locked room service with four mutations it
 * has no business owning would blur which stage owns what, so the management verbs live
 * here and `roomService` is untouched.
 *
 * ## The full surface, and its authorization
 *
 * List and read require **membership**; `POST`, `PATCH` and `DELETE` require
 * **`HotelRole.MANAGER`**, declared on the router itself -- one rung below the **owner** the
 * hotel's own update and delete require. `PUT` on a room type is a **405**, verified.
 *
 * ## `code` is the identity, and the path canonicalises it
 *
 * A room type has no `public_id`: `UNIQUE (hotel_id, code)` is its identity, and the router
 * upper-cases the path segment, so `.../room-types/dlx` resolves to `DLX` -- verified. The
 * code is settable at creation and absent from the update schema, exactly as a hotel's slug
 * is.
 *
 * ## The list takes a page and nothing else
 *
 * `page` and `page_size` (max 100), ordered by code. No search, no filter, no sort -- and
 * checked rather than assumed, because the backend **ignores an unrecognised query parameter
 * silently**: `?is_active=true` returned every type with a 200.
 */

/** From `app.services.room_type`. */
export const DEFAULT_PAGE_SIZE = 20
/** The router's own `le=100`. Asking for 101 is a 422, verified. */
export const MAX_PAGE_SIZE = 100

export const roomTypeService = {
  /**
   * One page of a hotel's room types, ordered by code.
   *
   * Throws `ApiError`: **404** for an unknown hotel or one the caller cannot reach --
   * distinct from an existing hotel with no room types, which is a 200 with an empty page.
   */
  list(
    hotelPublicId: string,
    { page, pageSize }: { page: number; pageSize: number },
    signal?: AbortSignal,
  ): Promise<Page<RoomType>> {
    return api.get<Page<RoomType>>(`/hotels/${hotelPublicId}/room-types`, {
      query: { page, page_size: pageSize },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * One room type, resolved by hotel **and** code.
   *
   * Another hotel's identical code is not reachable here -- it answers 404, verified.
   */
  get(hotelPublicId: string, code: string, signal?: AbortSignal): Promise<RoomType> {
    return api.get<RoomType>(
      `/hotels/${hotelPublicId}/room-types/${encodeURIComponent(code)}`,
      { ...(signal ? { signal } : {}) },
    )
  },

  /**
   * Create a room type.
   *
   * Throws `ApiError`: **409** when the hotel already has that code; **403** without the
   * manager role; **404** for an unknown hotel; **422** for `standard_occupancy` above
   * `max_occupancy`, an occupancy or bed count outside 1..99, a negative or
   * over-precise `base_price`, a `size_sqm` of zero, a malformed currency, or any field the
   * schema lacks -- `hotel_public_id` and `is_active` included.
   */
  create(
    hotelPublicId: string,
    payload: RoomTypeCreateRequest,
    signal?: AbortSignal,
  ): Promise<RoomType> {
    return api.post<RoomType>(`/hotels/${hotelPublicId}/room-types`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Update a room type. Only the fields present in the body are written.
   *
   * Throws `ApiError`: **422** for `code`, which is immutable; **409** when raising
   * `standard_occupancy` alone above the *stored* `max_occupancy` -- the schema cannot see
   * the stored value, so the service checks it and reports a conflict rather than a
   * validation error. Both verified, and the difference in status between the two is real.
   *
   * Also **403** without the manager role and **404** for an unknown code.
   */
  update(
    hotelPublicId: string,
    code: string,
    payload: RoomTypeUpdateRequest,
    signal?: AbortSignal,
  ): Promise<RoomType> {
    return api.patch<RoomType>(
      `/hotels/${hotelPublicId}/room-types/${encodeURIComponent(code)}`,
      { body: payload, ...(signal ? { signal } : {}) },
    )
  },

  /**
   * Delete a room type. 204, with no body.
   *
   * Throws `ApiError`: **409** when rooms are still assigned to it -- no cascade is
   * performed, verified against a seeded type; **403** without the manager role; **404** for
   * an unknown code.
   */
  remove(hotelPublicId: string, code: string, signal?: AbortSignal): Promise<void> {
    return api.delete<void>(
      `/hotels/${hotelPublicId}/room-types/${encodeURIComponent(code)}`,
      { ...(signal ? { signal } : {}) },
    )
  },
} as const
