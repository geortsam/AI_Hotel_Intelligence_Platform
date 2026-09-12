import type { DecimalString } from '@/types/analytics'

/**
 * The room-type **request** contracts, transcribed from `app.schemas.room_type` and
 * confirmed against the live OpenAPI document and real requests.
 *
 * The response shape already has a home: `RoomType` in `@/types/room`, written for Stage
 * 5.11's room screens. It is imported from there rather than restated -- two definitions of
 * one response is exactly the drift this codebase avoids elsewhere -- and that file is not
 * modified by this stage.
 *
 * Four facts that check established:
 *
 * 1. **`code` is settable at creation and nowhere else.** It is the URL identity, treated
 *    exactly as a hotel's `slug` is; sending it to `PATCH` is a 422, verified.
 * 2. **`code` and `currency` are upper-cased by the schema**, so `s513` and `eur` are
 *    accepted and stored canonically -- verified live. The path is upper-cased too, so
 *    `.../room-types/dlx` resolves.
 * 3. **`is_active` is update-only.** Sending it at creation is a 422, verified; a room type
 *    is created active.
 * 4. **The occupancy rule is enforced twice, with different statuses.** Creating with
 *    `standard_occupancy > max_occupancy` is a **422** from the schema's own validator;
 *    raising `standard_occupancy` alone above the *stored* max is a **409** from the
 *    service, because the schema cannot see the stored value. Both verified.
 */

/**
 * `POST /hotels/{h}/room-types` -- the `RoomTypeCreate` payload.
 *
 * The hotel is not in the body: it comes from the URL, and sending `hotel_public_id` is a
 * 422, verified.
 *
 * Required: `code`, `name`, `max_occupancy`, `standard_occupancy`, `bed_count`,
 * `base_price`, `currency`.
 */
export interface RoomTypeCreateRequest {
  /** 1..20 chars, `^[A-Z0-9][A-Z0-9_-]*$` after the schema upper-cases it. */
  readonly code: string
  readonly name: string
  readonly description?: string | null
  /** 1..99. */
  readonly max_occupancy: number
  /** 1..99, and never above `max_occupancy`. */
  readonly standard_occupancy: number
  /** 1..99. */
  readonly bed_count: number
  readonly bed_configuration?: string | null
  /** `NUMERIC(6,2)`, greater than zero. A string, like every decimal on this wire. */
  readonly size_sqm?: DecimalString | null
  /** `NUMERIC(14,2)`, zero or more. Three decimal places is a 422, verified. */
  readonly base_price: DecimalString
  readonly currency: string
}

/**
 * `PATCH /hotels/{h}/room-types/{code}` -- the `RoomTypeUpdate` payload.
 *
 * Every field optional. An omitted field is left untouched; an explicit null clears a
 * nullable column; an empty payload is a 200 that changes nothing. All verified.
 *
 * `code` is absent -- it is the URL identity. `is_active` is present, which create's is not.
 *
 * Requires the **manager** role, where the hotel itself requires owner.
 */
export interface RoomTypeUpdateRequest {
  readonly name?: string
  readonly description?: string | null
  readonly max_occupancy?: number
  readonly standard_occupancy?: number
  readonly bed_count?: number
  readonly bed_configuration?: string | null
  readonly size_sqm?: DecimalString | null
  readonly base_price?: DecimalString
  readonly currency?: string
  readonly is_active?: boolean
}
