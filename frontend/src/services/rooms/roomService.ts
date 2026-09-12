import { api } from '@/services/api/client'
import type { Page } from '@/types/api'
import type { Room, RoomCreateRequest, RoomType, RoomUpdateRequest } from '@/types/room'

/**
 * The room calls this application makes, and the complete set the contract offers.
 *
 * ## The URL states the whole ownership chain
 *
 * `/hotels/{hotel_public_id}/room-types/{room_type_code}/rooms/{room_number}`. Every segment
 * is a public identifier -- a UUID and two codes -- and `rooms.id` appears nowhere.
 *
 * **There is no hotel-wide room listing.** The only collection is a room type's own, which is
 * why the screen asks which type to show rather than presenting one flat list. Building that
 * flat list by looping the types would be one request per type, growing with the property --
 * and the totals would still have to come from somewhere.
 *
 * ## Room numbers are unique per hotel, though the URL suggests otherwise
 *
 * The `room-types` segment reads like a namespace and is not one: `UNIQUE (hotel_id,
 * room_number)` means number `101` identifies exactly one room in the property, whatever its
 * type. Verified -- creating the same number under a *second* type was a **409**, not a
 * success. That is worth knowing before writing a creation form that says "this type's
 * rooms".
 *
 * ## Five verbs, and no sixth
 *
 * List, read, create, update, delete. `PUT` on a room and `PATCH`/`DELETE` on the collection
 * are all **405**, verified.
 *
 * ## Authorization, verified live
 *
 * Reading requires **membership**. `POST`, `PATCH` and `DELETE` all require
 * **`HotelRole.MANAGER`** -- a uniformly higher bar than guests, where only deletion did.
 */

/** From `app.services.room`. */
export const DEFAULT_PAGE_SIZE = 20
/** The router's own `le=100`. Asking for 101 is a 422, verified. */
export const MAX_PAGE_SIZE = 100
/** `le=200` on the room-type list. The seeded catalogue holds two. */
export const ROOM_TYPE_PAGE_SIZE = 100

export const roomService = {
  /**
   * The hotel's room types.
   *
   * Read-only in this stage: a room cannot be listed without naming its type, so the
   * catalogue is fetched once to populate the chooser and to label the rooms beneath it.
   * Managing types is not part of this screen.
   *
   * Throws `ApiError`: 404 for an unknown hotel or one the caller cannot reach.
   */
  listTypes(hotelPublicId: string, signal?: AbortSignal): Promise<Page<RoomType>> {
    return api.get<Page<RoomType>>(`/hotels/${hotelPublicId}/room-types`, {
      query: { page: 1, page_size: ROOM_TYPE_PAGE_SIZE },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * One page of a room type's rooms, ordered by room number.
   *
   * `page` and `page_size` (max 100) are the entire query surface: **no search, no status
   * filter, no sort**. Checked rather than assumed, because the backend **ignores an
   * unrecognised query parameter silently** -- `?status=available` and `?search=101` each
   * returned every room with a 200, which on screen is indistinguishable from a filter that
   * matched everything.
   *
   * Throws `ApiError`: **404** for an unknown hotel *or* an unknown room type -- distinct
   * from a known type with no rooms, which is a 200 with an empty page; **422** above 100.
   */
  list(
    hotelPublicId: string,
    roomTypeCode: string,
    { page, pageSize }: { page: number; pageSize: number },
    signal?: AbortSignal,
  ): Promise<Page<Room>> {
    return api.get<Page<Room>>(
      `/hotels/${hotelPublicId}/room-types/${encodeURIComponent(roomTypeCode)}/rooms`,
      { query: { page, page_size: pageSize }, ...(signal ? { signal } : {}) },
    )
  },

  /**
   * One room, resolved against the whole chain.
   *
   * Neither another hotel's nor another room type's room of the same number is reachable
   * here: both answer 404, verified. The number is matched case-insensitively -- the router
   * upper-cases it -- so a link built from a response always resolves.
   */
  get(
    hotelPublicId: string,
    roomTypeCode: string,
    roomNumber: string,
    signal?: AbortSignal,
  ): Promise<Room> {
    return api.get<Room>(
      `/hotels/${hotelPublicId}/room-types/${encodeURIComponent(roomTypeCode)}/rooms/${encodeURIComponent(roomNumber)}`,
      { ...(signal ? { signal } : {}) },
    )
  },

  /**
   * Create a room of this type.
   *
   * Throws `ApiError`: **409** when the **hotel** already uses that number under any type --
   * not merely this one, verified; **403** without the manager role; **404** for an unknown
   * hotel or room type; **422** for a number outside `^[A-Z0-9][A-Z0-9._-]*$` or over 20
   * characters, a floor outside `-10..200`, a status the schema lacks, or any field it does
   * not have -- `room_type_code` included, since the path already says it.
   */
  create(
    hotelPublicId: string,
    roomTypeCode: string,
    payload: RoomCreateRequest,
    signal?: AbortSignal,
  ): Promise<Room> {
    return api.post<Room>(
      `/hotels/${hotelPublicId}/room-types/${encodeURIComponent(roomTypeCode)}/rooms`,
      { body: payload, ...(signal ? { signal } : {}) },
    )
  },

  /**
   * Update a room. Only the fields present in the body are written.
   *
   * Reaches `floor`, `status`, `notes` and `is_active` -- and nothing else. `room_number` and
   * `room_type_code` are both **422**, verified: the first is the URL identity and the second
   * would move the room to a different parent path.
   *
   * An omitted field is left untouched; an explicit null clears `floor` or `notes`. An empty
   * payload is a 200 that changes nothing. All verified.
   */
  update(
    hotelPublicId: string,
    roomTypeCode: string,
    roomNumber: string,
    payload: RoomUpdateRequest,
    signal?: AbortSignal,
  ): Promise<Room> {
    return api.patch<Room>(
      `/hotels/${hotelPublicId}/room-types/${encodeURIComponent(roomTypeCode)}/rooms/${encodeURIComponent(roomNumber)}`,
      { body: payload, ...(signal ? { signal } : {}) },
    )
  },

  /**
   * Delete a room. 204, with no body.
   *
   * Throws `ApiError`: **409** when reservations still reference it -- no cascade is
   * performed, and a seeded room with allocations was refused, verified; **403** without the
   * manager role; **404** for an unknown room.
   */
  remove(
    hotelPublicId: string,
    roomTypeCode: string,
    roomNumber: string,
    signal?: AbortSignal,
  ): Promise<void> {
    return api.delete<void>(
      `/hotels/${hotelPublicId}/room-types/${encodeURIComponent(roomTypeCode)}/rooms/${encodeURIComponent(roomNumber)}`,
      { ...(signal ? { signal } : {}) },
    )
  },
} as const
