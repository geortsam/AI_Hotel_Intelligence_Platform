import { api } from '@/services/api/client'
import type { AvailabilityResult, RoomTypeDemand } from '@/types/availability'

/**
 * The availability call this application makes. There is exactly one.
 *
 * `GET /hotels/{hotel_public_id}/availability` -- read-only, and the only verb the route
 * has: `POST`, `PATCH`, `PUT` and `DELETE` are all **405**, verified. A search writes
 * nothing and holds nothing.
 *
 * ## The server answers; this layer asks
 *
 * Nothing here inspects bookings, subtracts dates, counts free rooms or reasons about
 * overlap. The service's query mirrors the database's own exclusion constraint, and the
 * answer -- including `sufficient`, `available_rooms` and each type's `available_count` --
 * comes back computed. This module builds a URL and returns what arrives.
 *
 * ## Two request forms, and they are alternatives
 *
 * * **Single type**: optional `room_type_code`, optional `guests`, `rooms_required` (1..100).
 * * **Mixed**: `rooms` repeated as `CODE:COUNT`, at most 20 types.
 *
 * The backend refuses the combination -- `rooms` with either `room_type_code` or a
 * `rooms_required` other than 1 is a **422**, verified both ways -- so `search` takes one
 * shape or the other and cannot express the refused request.
 *
 * ## Case matters for one parameter and not the other
 *
 * `room_type_code=dlx` is a **404**; `rooms=dlx:1` succeeds, because the mixed parser
 * upper-cases and the single filter does not. Neither is normalised here: the code is sent
 * exactly as the catalogue gave it, which is correct for both.
 */

/** `MAX_ROOMS_REQUIRED` in `app.services.availability`. 0 and 101 are both 422s, verified. */
export const MAX_ROOMS_REQUIRED = 100
/** `MAX_ROOM_TYPE_DEMANDS`. A 21st type is a 422, verified. */
export const MAX_ROOM_TYPE_DEMANDS = 20
/** `MAX_STAY_NIGHTS`. 366 succeeds; 367 is a 422, verified. */
export const MAX_STAY_NIGHTS = 366

/** A search for rooms of one type, or of any type. */
export interface SingleTypeSearch {
  readonly kind: 'single'
  /** `YYYY-MM-DD`. The first night, inclusive. */
  readonly checkIn: string
  /** `YYYY-MM-DD`. The departure day, **exclusive** -- no night is sold on it. */
  readonly checkOut: string
  /** A code from this hotel's catalogue, sent verbatim. Omitted to search every type. */
  readonly roomTypeCode?: string
  /** Only types whose `max_occupancy` seats this many. Omitted to ignore occupancy. */
  readonly guests?: number
  /** Rooms of a single type needed for the whole stay. 1..100. */
  readonly roomsRequired?: number
}

/** A search naming several types and how many of each. */
export interface MixedSearch {
  readonly kind: 'mixed'
  readonly checkIn: string
  readonly checkOut: string
  /** At most 20, each count 1..100, no type repeated. All three bounds are server-enforced. */
  readonly demands: readonly RoomTypeDemand[]
}

export type AvailabilitySearch = SingleTypeSearch | MixedSearch

export const availabilityService = {
  /**
   * Search one hotel's inventory for a stay.
   *
   * **A 200 with an empty `room_types` is a successful answer meaning "nothing free"**, and
   * so is `sufficient: false`. Neither is an error, and neither is a 404.
   *
   * Throws `ApiError`: **404** for an unknown hotel, one the caller cannot reach, or a
   * `room_type_code` that is not a code at this hotel -- including one that belongs to
   * another property; **422** for a `check_out` not later than `check_in`, a stay over 366
   * nights, `rooms_required` outside 1..100, `guests` below 1, a malformed or repeated
   * `rooms` entry, more than 20 types, or a mixed request combined with the single-type
   * parameters; **401** without a token.
   */
  search(
    hotelPublicId: string,
    search: AvailabilitySearch,
    signal?: AbortSignal,
  ): Promise<AvailabilityResult> {
    const query =
      search.kind === 'mixed'
        ? {
            check_in: search.checkIn,
            check_out: search.checkOut,
            // Repeated, not joined: `rooms=DLX:2&rooms=STD:1`. See `QueryParams`.
            rooms: search.demands.map((demand) => `${demand.code}:${demand.count}`),
          }
        : {
            check_in: search.checkIn,
            check_out: search.checkOut,
            // Omitted rather than sent empty: `room_type_code=` is not the same request as
            // no `room_type_code`, and the second is the one that means "any type".
            ...(search.roomTypeCode ? { room_type_code: search.roomTypeCode } : {}),
            ...(search.guests === undefined ? {} : { guests: search.guests }),
            ...(search.roomsRequired === undefined
              ? {}
              : { rooms_required: search.roomsRequired }),
          }

    return api.get<AvailabilityResult>(`/hotels/${hotelPublicId}/availability`, {
      query,
      ...(signal ? { signal } : {}),
    })
  },
} as const
