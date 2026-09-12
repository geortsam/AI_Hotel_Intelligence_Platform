import type { DecimalString } from '@/types/analytics'

/**
 * The availability contract, transcribed from `app.schemas.availability` and confirmed
 * against the live OpenAPI document and real searches.
 *
 * Six facts that check established, each of which shaped the UI:
 *
 * 1. **The stay is half-open, `[check_in, check_out)`.** Verified against a real seeded
 *    booking: room 205 is busy for 17→18 September, free for 16→17, and free again for a
 *    stay *beginning* on the 18th. Nothing here re-derives that; the dates go to the server
 *    as typed.
 * 2. **`sufficient` is the answer, and it is the server's.** It is not "the list is
 *    non-empty": a mixed request for `DLX:2` and `STD:99` came back with **both types
 *    listed, sixteen rooms free, and `sufficient: false`**, because one share could not be
 *    met. A UI reading the list instead of the flag would report the opposite of the truth.
 * 3. **Zero availability is a 200 with an empty list**, never a 404. "Nothing free on those
 *    dates" is a fact about the dates, not a failed request.
 * 4. **The two request forms are alternatives.** `rooms` names types and counts; it cannot
 *    be combined with `room_type_code` or `rooms_required` -- both are 422s, verified. The
 *    interface therefore offers a mode, not a pile of fields that can contradict each other.
 * 5. **`room_type_code` is case-sensitive; `rooms` is not.** `room_type_code=dlx` is a
 *    **404**, while `rooms=dlx:1` succeeds -- the mixed parser upper-cases and the single
 *    filter does not. So a code is always sent exactly as the catalogue gave it.
 * 6. **A result is not a reservation.** The schema says so in as many words: nothing is held,
 *    and a room named here may be taken a moment later. The screen repeats it.
 */

/** One physical room the hotel could allocate for the requested stay. */
export interface AvailableRoom {
  /** Unique per hotel. The same identifier the room endpoints put in their paths. */
  readonly room_number: string
  readonly floor: number | null
}

/** A room type, and the rooms of it that are free for the requested stay. */
export interface AvailableRoomType {
  readonly code: string
  readonly name: string
  readonly max_occupancy: number
  readonly standard_occupancy: number
  /** A `Decimal`, so a string on the wire. Formatted for display, never used in arithmetic. */
  readonly base_price: DecimalString
  readonly currency: string
  /**
   * How many rooms of this type are free.
   *
   * The server derives it from the actual allocatable rooms rather than a stored figure.
   * Nothing in this application recomputes it, and nothing compares it to a requested count
   * to decide an outcome -- `sufficient` already carries that answer.
   */
  readonly available_count: number
  /**
   * How many of this type the caller asked for.
   *
   * Present only when the request named types and counts. `null` for a search that named
   * none, because there was no per-type request to report.
   */
  readonly requested_count: number | null
  readonly rooms: readonly AvailableRoom[]
}

/** The answer to one availability search. */
export interface AvailabilityResult {
  readonly hotel_public_id: string
  readonly check_in: string
  readonly check_out: string
  /** `check_out - check_in`, and never zero: the server refuses a stay of no nights. */
  readonly nights: number
  /** How many rooms of a single type were asked for. 1 by default, and 1 for a mixed search. */
  readonly rooms_required: number
  /**
   * **The verdict.** Whether the request can be met.
   *
   * For a single-type search: whether some type can supply `rooms_required` for the whole
   * stay. For a mixed search: whether *every* named type can supply its own share. `false`
   * is a successful answer, not an error -- and it can arrive alongside a non-empty list of
   * types, which is exactly why this flag is read rather than the list's length.
   */
  readonly sufficient: boolean
  /** Free rooms across the types listed. The server's count. */
  readonly available_rooms: number
  readonly room_types: readonly AvailableRoomType[]
}

/** One entry of a mixed request: a room type and how many of it are wanted. */
export interface RoomTypeDemand {
  readonly code: string
  readonly count: number
}
