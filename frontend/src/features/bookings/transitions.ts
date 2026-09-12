import type { BookingStatus } from '@/types/booking'

/**
 * The booking lifecycle graph, as the backend defines it.
 *
 * ## What this is, and emphatically is not
 *
 * It is a **presentation filter**: it decides which buttons an operations screen offers.
 * It is **not** enforcement, and nothing in this application treats it as such. Every
 * action still travels to the server, the server re-reads the committed status under the
 * booking's row lock, and a move it does not permit comes back as a 409 that the UI renders.
 * If this file and the backend ever disagree, the backend wins and the user is told so.
 *
 * Stage 5.5 refused to copy this table at all, on the grounds that a duplicate of a domain
 * rule drifts. That reasoning was right for a read-only screen and is wrong for this one:
 * the alternative is offering `checked_out -> pending` and letting the server refuse, which
 * is a button whose only possible outcome is an error message. The copy is the lesser cost,
 * and the drift is bounded by making it visible -- `transitions.test.ts` states the whole
 * table explicitly, so a backend change that this file has not followed fails a test rather
 * than silently offering an impossible action.
 *
 * ## Provenance
 *
 * Transcribed from `app.models.enums.BOOKING_STATUS_TRANSITIONS`, read out of the **running**
 * backend rather than from source, on 2026-09-10:
 *
 *     pending      -> cancelled, confirmed
 *     confirmed    -> cancelled, checked_in, no_show
 *     checked_in   -> checked_out
 *     checked_out  -> (terminal)
 *     cancelled    -> (terminal)
 *     no_show      -> (terminal)
 *
 * Each state also permits itself. That self-transition is a deliberate backend no-op -- it
 * makes a retried request safe -- and it is excluded from what this offers, because "change
 * the status to the one it already has" is not an action worth a button.
 *
 * Two refusals worth naming, because both look like oversights and are not:
 *
 * * `confirmed -> checked_out` is refused. A guest who checked out first checked in, and
 *   skipping the arrival would put a stay in the occupancy figures the property never
 *   recorded anyone arriving for.
 * * `pending -> no_show` is refused. A no-show held a reservation and did not arrive; a
 *   pending booking holds no room and was never promised anything, so its honest disposal
 *   is `cancelled`.
 */
const TRANSITIONS: Readonly<Record<BookingStatus, readonly BookingStatus[]>> = {
  pending: ['confirmed', 'cancelled'],
  confirmed: ['checked_in', 'cancelled', 'no_show'],
  checked_in: ['checked_out'],
  checked_out: [],
  cancelled: [],
  no_show: [],
}

/**
 * The statuses a booking may move to from here, excluding itself.
 *
 * Ordered by how a front desk would reach for them: the ordinary next step first, the
 * disposals after. An unrecognised status yields an empty list -- a state this build does
 * not know is a state it must not guess actions for.
 */
export function nextStatuses(current: BookingStatus): readonly BookingStatus[] {
  return TRANSITIONS[current] ?? []
}

/** True when the lifecycle is closed. Derived from the table, so the two cannot disagree. */
export function isTerminal(status: BookingStatus): boolean {
  return nextStatuses(status).length === 0
}

/**
 * The transitions that end the booking rather than advancing it.
 *
 * These get a confirmation step. They are irreversible in the domain's own terms: all three
 * targets are terminal, so there is no undo, and the backend will refuse any attempt to move
 * back out of them.
 */
const DESTRUCTIVE: readonly BookingStatus[] = ['cancelled', 'no_show']

export function isDestructive(target: BookingStatus): boolean {
  return DESTRUCTIVE.includes(target)
}

/**
 * Whether the whole stay may be restated, from `MODIFIABLE_BOOKING_STATUSES`.
 *
 * `checked_in` is excluded, and the backend's reasoning is worth keeping in front of anyone
 * who reads this: a checked-in stay is physically in progress, some of its nights have
 * already been slept, and moving its room means moving a person. That case is served by the
 * extension below, which may only push the departure outward.
 */
export function canModifyStay(status: BookingStatus): boolean {
  return status === 'pending' || status === 'confirmed'
}

/** Whether an in-house stay may be extended, from `EXTENDABLE_BOOKING_STATUSES`. */
export function canExtendStay(status: BookingStatus): boolean {
  return status === 'checked_in'
}

/**
 * The most nights one extension may add, from `app.services.booking.MAX_EXTENSION_NIGHTS`.
 *
 * Restated so the form can refuse an impossible request before sending it. The server still
 * checks -- verified live: 398 nights returns a 422 naming the limit -- and this only saves
 * a round trip on a mistake nobody makes on purpose.
 */
export const MAX_EXTENSION_NIGHTS = 366
