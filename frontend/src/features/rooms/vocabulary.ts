import type { BadgeTone } from '@/components/ui/Badge'
import type { RoomStatus } from '@/types/room'

/**
 * The words this application puts on room concepts.
 *
 * Presentation over values the backend owns. Nothing here gates a control or changes a
 * request: which statuses may be set is decided by the server, and the answer is "all of
 * them" (see below).
 */

/**
 * Every value of the database CHECK, in the order the schema lists them.
 *
 * **This is a list of options, not a state machine.** The room service applies no transition
 * policy -- `update` writes the field -- and every ordering was accepted live, including
 * `available` straight to `occupied` and `out_of_order` straight back to `available`. So all
 * five are always offered, and the current one is simply marked as current. Inventing a
 * progression here would be an interface refusing something the platform permits.
 */
export const ROOM_STATUSES: readonly RoomStatus[] = [
  'available',
  'occupied',
  'cleaning',
  'maintenance',
  'out_of_order',
]

const STATUS_PRESENTATION: Readonly<Record<RoomStatus, { label: string; tone: BadgeTone }>> = {
  available: { label: 'Available', tone: 'success' },
  occupied: { label: 'Occupied', tone: 'info' },
  cleaning: { label: 'Cleaning', tone: 'warning' },
  maintenance: { label: 'Maintenance', tone: 'warning' },
  out_of_order: { label: 'Out of order', tone: 'danger' },
}

/**
 * How a status reads, and how it is coloured.
 *
 * Takes a plain `string` rather than `RoomStatus`: a value added to the backend later would
 * otherwise arrive as a lookup miss and render `undefined`. The fallback prints the code as
 * the server sent it, which is true if inelegant.
 *
 * Colour is never the only signal -- `Badge` always shows the label -- because "occupied" and
 * "out of order" differing only by hue is unreadable to anyone with a colour vision
 * deficiency, and this is a screen someone scans quickly.
 */
export function statusPresentation(status: string): { label: string; tone: BadgeTone } {
  return STATUS_PRESENTATION[status as RoomStatus] ?? { label: status, tone: 'neutral' }
}

/**
 * Whether the room is in service, as a word.
 *
 * Deliberately separate from `status`: the schema keeps `is_active` and `status` apart, and a
 * room really can be `available` and inactive at once. Nothing in this application derives
 * one from the other, so both are shown.
 */
export function activePresentation(isActive: boolean): { label: string; tone: BadgeTone } {
  return isActive ? { label: 'In service', tone: 'neutral' } : { label: 'Withdrawn', tone: 'danger' }
}

/**
 * The shape a room number must take, mirroring `RoomNumberField`.
 *
 * `^[A-Z0-9][A-Z0-9._-]*$`, 1..20 characters -- checked case-insensitively here because the
 * server upper-cases before validating, so `12a` is as acceptable as `12A`. Matched at the
 * field so a refusal arrives where it was caused rather than as a banner.
 */
export const ROOM_NUMBER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/

/** `FloorField`: `-10..200`. Verified -- -11 and 201 are both 422s. */
export const MIN_FLOOR = -10
export const MAX_FLOOR = 200
