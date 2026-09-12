import type { DecimalString } from '@/types/analytics'

/**
 * The physical-room contracts, transcribed from `app.schemas.room` and confirmed against the
 * live OpenAPI document and real requests.
 *
 * Five facts that check established, each of which shaped the UI:
 *
 * 1. **A room has no `public_id`.** `room_number` is the natural identifier, and it appears
 *    in the URL. It is **upper-cased** by both the schema and the router, so `101a` and
 *    `101A` are one room -- verified: creating `s511c` produced `S511C`, and fetching
 *    `.../rooms/s511a` returned `S511A`.
 * 2. **Rooms are listed per room type, never per hotel.** The only list endpoint is
 *    `/hotels/{h}/room-types/{code}/rooms`. There is no hotel-wide room collection, which is
 *    why this screen asks which type to show rather than presenting one long list.
 * 3. **Room numbers are unique per HOTEL, not per type.** The schema is explicit and the
 *    server agrees: creating `S511A` under a *second* room type was a 409, not a success.
 *    The `room-types` segment is an assertion the room must satisfy, not part of its key.
 * 4. **`status` has five values and no transition rules.** Every ordering was accepted --
 *    `available` straight to `occupied`, `out_of_order` straight to `available` -- so the UI
 *    offers all five and invents no state machine. It is the room's *current operational*
 *    state, not its availability over time.
 * 5. **Neither the number nor the type can be changed by PATCH.** Both are 422s, verified:
 *    the number is the URL identity and moving a room between types would change its parent
 *    path.
 */

/**
 * Mirrors `RoomStatus` in `app.models.enums`, which generates the database CHECK.
 *
 * Sending anything else is a 422 naming all five, verified.
 */
export type RoomStatus = 'available' | 'occupied' | 'cleaning' | 'maintenance' | 'out_of_order'

/** One physical room, as the API returns it. */
export interface Room {
  readonly hotel_public_id: string
  /** The owning type's code. Carried so a client can rebuild the room's URL from the payload. */
  readonly room_type_code: string
  /** The identifier. Upper-case, 1..20 chars, `^[A-Z0-9][A-Z0-9._-]*$`. */
  readonly room_number: string
  /** `-10..200`, or null. Verified: -11 and 201 are both 422s. */
  readonly floor: number | null
  readonly status: RoomStatus
  readonly notes: string | null
  /**
   * Whether the room is in service.
   *
   * Distinct from `status`: a room can be `available` and inactive. The schema keeps them
   * apart and so does this application -- nothing here derives one from the other.
   */
  readonly is_active: boolean
  readonly created_at: string
  readonly updated_at: string
}

/**
 * `POST /hotels/{h}/room-types/{code}/rooms` -- the `RoomCreate` payload.
 *
 * Neither the hotel nor the room type appears: both come from the URL, and accepting them
 * here would let the two disagree. Sending `room_type_code` is a 422, verified.
 *
 * `status` defaults to `available` and `is_active` to `true`, in the schema and in the
 * columns alike.
 */
export interface RoomCreateRequest {
  readonly room_number: string
  readonly floor?: number | null
  readonly status?: RoomStatus
  readonly notes?: string | null
  readonly is_active?: boolean
}

/**
 * `PATCH .../rooms/{room_number}` -- the `RoomUpdate` payload.
 *
 * Four fields, and **not** `room_number` or `room_type_code`: both are 422s. An omitted field
 * is left untouched; an explicit null clears `floor` or `notes`. An empty payload is a 200
 * that changes nothing. All verified live.
 */
export interface RoomUpdateRequest {
  readonly floor?: number | null
  readonly status?: RoomStatus
  readonly notes?: string | null
  readonly is_active?: boolean
}

/**
 * A room type, as `GET /hotels/{h}/room-types` returns it.
 *
 * Read-only here. This stage manages **rooms**; the type collection is consumed because a
 * room cannot be listed without naming its type, and for the descriptive fields shown beside
 * the rooms. Creating or editing a type is not part of this screen.
 *
 * `base_price` and `size_sqm` are `Decimal`s and therefore arrive as **strings**, like every
 * other numeric of that kind in this API.
 */
export interface RoomType {
  readonly hotel_public_id: string
  readonly code: string
  readonly name: string
  readonly description: string | null
  readonly max_occupancy: number
  readonly standard_occupancy: number
  readonly bed_count: number
  readonly bed_configuration: string | null
  readonly size_sqm: DecimalString | null
  readonly base_price: DecimalString
  readonly currency: string
  readonly is_active: boolean
  readonly created_at: string
  readonly updated_at: string
}
