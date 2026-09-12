/**
 * The booking contracts, transcribed from `app.schemas.booking` and
 * `app.schemas.reconciliation`.
 *
 * Confirmed against the live API -- `GET /openapi.json` plus real payloads -- rather than
 * read from source alone. Three things that check settled:
 *
 * 1. **The list item and the detail response are the same shape.** `GET /bookings` returns
 *    full `BookingResponse` objects, allocations and nightly rates included; there is no
 *    summary projection. So the list needs no follow-up request per row, and there is no
 *    second type to keep in step.
 * 2. **Every `Decimal` is a JSON string** -- `"120.00"`. Money is `NUMERIC(14,2)`.
 * 3. **No internal key appears anywhere.** The schema's own docstring: "No internal key
 *    appears in it." Every identifier here is a UUID.
 */

/** A backend `Decimal` on the wire. See `@/lib/format` for why it stays a string. */
export type DecimalString = string

/**
 * The booking lifecycle, exactly as `BookingStatusLiteral` declares it.
 *
 * Six states and no more.
 *
 * Stage 5.5 said the backend's transition table was "deliberately not mirrored here",
 * because nothing needed it. Stage 5.6 needs it: an operations console that offered
 * `checked_out -> pending` and let the server refuse would be putting a button on screen
 * whose only outcome is an error. So the table now has a frontend copy, in
 * `features/bookings/transitions.ts` -- **as a presentation filter, never as enforcement.**
 * Every action still goes to the server, and the 409 the server returns for an illegal move
 * is rendered rather than assumed away. See that file for the guarantee and its limits.
 */
export type BookingStatus =
  | 'pending'
  | 'confirmed'
  | 'checked_in'
  | 'checked_out'
  | 'cancelled'
  | 'no_show'

/** Where the booking came from, exactly as `BookingSourceLiteral` declares it. */
export type BookingSource =
  | 'direct'
  | 'website'
  | 'phone'
  | 'walk_in'
  | 'booking_com'
  | 'expedia'
  | 'airbnb'
  | 'agoda'
  | 'other'

/** One priced night of one allocated room. */
export interface BookingRoomNight {
  /** `YYYY-MM-DD`. A calendar date, not an instant. */
  readonly stay_date: string
  /**
   * The rate for this night, in the booking's currency.
   *
   * **This is the authoritative accommodation figure's only source.** The sum of these is
   * what `reconciliation.accommodation_total` reports; `booking.total_amount` is a different
   * thing entirely (see `BookingReconciliation`).
   */
  readonly rate: DecimalString
  readonly rate_plan_code: string | null
  /** A complimentary night is occupied but not sold: it counts for occupancy, not for ADR. */
  readonly is_complimentary: boolean
}

/**
 * One allocated room, with its nights.
 *
 * `booking_rooms` is an **allocation**, not a pricing source -- the rates live on the nights
 * below it. `room_type_code` is carried so a consumer can locate the room in the Room
 * domain without a second lookup.
 */
export interface BookingRoom {
  readonly room_number: string
  readonly room_type_code: string
  readonly adults: number
  readonly children: number
  /**
   * The occupant named on this allocation, if one was given.
   *
   * **Not the booker.** The booking's guest record is reached through `guest_public_id`;
   * this is a per-room label, frequently null, for the person actually in that room.
   * Presenting it as "the guest" would be quietly wrong on any multi-room booking.
   */
  readonly guest_name: string | null
  /** A generated column, so it always agrees with the dates. */
  readonly nights: number
  readonly nightly_rates: readonly BookingRoomNight[]
}

/** `GET /hotels/{h}/bookings` items and `GET /hotels/{h}/bookings/{b}` -- the same shape. */
export interface Booking {
  readonly hotel_public_id: string
  readonly public_id: string
  /**
   * The booker's guest record.
   *
   * A UUID and nothing else: `BookingResponse` carries **no guest name, email or phone**.
   * Resolving it needs `GET /hotels/{h}/guests/{g}`, which is one request per booking -- fine
   * for a detail page, an N+1 for a list. See `BookingsPage` for what the list shows instead.
   */
  readonly guest_public_id: string
  /** The property's own human reference, e.g. `MH-00162`. What operations staff actually use. */
  readonly reference: string
  /** `YYYY-MM-DD`. Inclusive: the guest sleeps this night. */
  readonly check_in_date: string
  /** `YYYY-MM-DD`. **Exclusive**: the departure day is not a room night. */
  readonly check_out_date: string
  readonly status: BookingStatus
  readonly adults: number
  readonly children: number
  readonly source: BookingSource
  readonly channel_reference: string | null
  /**
   * The **contracted** total, and NOT the authoritative accommodation figure.
   *
   * Approved decision 10: this arrives from the client and is deliberately not defined as the
   * sum of the nightly rates -- discounts, taxes and packages legitimately break that
   * equality. The server can report a divergence but not explain one, because the schema
   * records no tax, fee or discount. Anywhere the authoritative amount is wanted, use
   * `BookingReconciliation.accommodation_total`.
   */
  readonly total_amount: DecimalString
  readonly currency: string
  readonly special_requests: string | null
  /** An instant, not a date. Non-null only for a cancelled booking. */
  readonly cancelled_at: string | null
  readonly cancellation_reason: string | null
  /** When the booking was taken. An instant. */
  readonly booked_at: string
  readonly created_at: string
  readonly updated_at: string
  readonly rooms: readonly BookingRoom[]
}

/** How a booking stands against its ledger. Derived at read time; nothing is stored. */
export type PaymentState = 'unpaid' | 'partially_paid' | 'paid' | 'overpaid'

/**
 * `GET /hotels/{h}/bookings/{b}/reconciliation` -- Stage 4.5.9.
 *
 * **Two totals, and the difference between them is the finding.** The backend's own words:
 * `accommodation_total` is "what the server can prove the stay is worth", the sum of its
 * nightly rates. `declared_total` is `bookings.total_amount`, echoed "so a divergence is
 * visible". Reconciliation "states both figures and whether they agree, rather than picking
 * a winner or pretending the difference is not there" -- and neither does this UI.
 */
export interface BookingReconciliation {
  readonly booking_public_id: string
  readonly currency: string
  /** SERVER-AUTHORITATIVE. The sum of this booking's nightly rates. */
  readonly accommodation_total: DecimalString
  /** INFORMATIONAL. What the client contracted. Not authoritative. */
  readonly declared_total: DecimalString
  /** False when the two disagree. The server cannot say why, and neither may this UI. */
  readonly totals_agree: boolean
  readonly charged_total: DecimalString
  readonly refunded_total: DecimalString
  /** `charged_total - refunded_total`. May legitimately be negative. */
  readonly net_paid: DecimalString
  /** `accommodation_total - net_paid`. Positive means still owed, negative means overpaid. */
  readonly outstanding_amount: DecimalString
  readonly payment_state: PaymentState
}

/* --- mutations ------------------------------------------------------------------------ */

/**
 * `PATCH /hotels/{h}/bookings/{b}` -- the `BookingUpdate` payload, narrowed.
 *
 * The backend schema also accepts `adults`, `children`, `source`, `channel_reference`,
 * `special_requests` and **`total_amount`**. This type carries only the two fields the
 * status workflow needs, and the omission of `total_amount` is deliberate and load-bearing:
 * it is the CONTRACTED total, and letting an operations screen rewrite it would let the UI
 * silently change what a booking claims to be worth. A field this application must never
 * send is a field its request type should not have.
 *
 * `cancelled_at` is absent from the backend schema too -- the service derives it from the
 * status, because `ck_bookings_cancellation_consistent` is a biconditional and the two must
 * not be able to disagree.
 */
export interface BookingStatusUpdate {
  readonly status: BookingStatus
  /** Only meaningful alongside `status: 'cancelled'`. Max 500 characters. */
  readonly cancellation_reason?: string
}

/** One night of a requested stay. **There is no `rate` field, and that is the contract.** */
export interface StayNightRequest {
  /** `YYYY-MM-DD`. */
  readonly stay_date: string
  readonly rate_plan_code?: string | null
  readonly is_complimentary?: boolean
}

/** One room of a requested stay, with every night it covers and no price for any of them. */
export interface StayRoomRequest {
  readonly room_number: string
  readonly adults: number
  readonly children: number
  readonly guest_name?: string | null
  readonly nights: readonly StayNightRequest[]
}

/**
 * `PATCH /hotels/{h}/bookings/{b}/stay` -- the `StayModification` payload.
 *
 * **The whole stay is restated, not patched.** Moving the dates rewrites every allocation and
 * every night row, so the payload carries all of them. The server validates that each room
 * prices *exactly* the nights of `[check_in_date, check_out_date)` -- a mismatch is a 422
 * naming the missing and unexpected dates.
 *
 * Nothing here carries an amount. `additionalProperties: false` on every level makes that
 * enforceable rather than merely documented: a `rate` on a night is a 422, verified against
 * the live API, not silently dropped.
 */
export interface StayModificationRequest {
  readonly check_in_date: string
  readonly check_out_date: string
  readonly rooms: readonly StayRoomRequest[]
}

/** `POST /hotels/{h}/bookings/{b}/stay/extension` -- one field, and the absences are the contract. */
export interface StayExtensionRequest {
  /** `YYYY-MM-DD`. Must be strictly later than the current check-out. */
  readonly check_out_date: string
}

/**
 * What a stay change did to the money. Every figure is server-derived.
 *
 * **Not a receipt.** The backend's own words: `additional_amount_due` has not been charged
 * and `refundable_amount` has not been refunded -- the platform owns no payment processor,
 * and settling either is a separate, deliberate act. Verified live: reducing a stay by
 * EUR 360.00 against a booking that had paid nothing produced `refundable_amount: "0.00"`
 * and `adjustment: "none"`. A decrease is not automatically a refund.
 */
export interface StayRepricing {
  readonly currency: string
  /** The stay's value before the change: the sum of its old nightly rates. */
  readonly previous_total: DecimalString
  /** And after. Both are SUM(booking_room_nights.rate) -- reconciliation's authoritative figure. */
  readonly new_total: DecimalString
  /** `new_total - previous_total`. Negative when the stay got cheaper. */
  readonly difference: DecimalString
  /** Owed as a result. Zero unless the stay got dearer. */
  readonly additional_amount_due: DecimalString
  /** Returnable as a result, capped by what was actually collected. */
  readonly refundable_amount: DecimalString
  /** The balance afterwards. Positive still owed, negative overpaid. */
  readonly outstanding_after: DecimalString
  readonly adjustment: 'none' | 'amount_due' | 'refundable'
}

/** The response shared by stay modification and extension: the booking as it now stands, and what it cost. */
export interface StayModificationResponse {
  readonly booking: Booking
  readonly repricing: StayRepricing
}
