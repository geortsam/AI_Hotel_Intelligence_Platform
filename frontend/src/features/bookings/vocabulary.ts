import {
  BedDouble,
  CalendarCheck,
  CalendarClock,
  CircleSlash,
  LogOut,
  UserX,
  type LucideIcon,
} from 'lucide-react'

import type { BadgeTone } from '@/components/ui/Badge'
import type { BookingSource, BookingStatus, PaymentState } from '@/types/booking'

/**
 * How the backend's vocabularies are *shown*. Presentation only.
 *
 * ## What this file deliberately does not contain
 *
 * **The transition table.** The backend owns `BOOKING_STATUS_TRANSITIONS` -- which moves are
 * legal from each state, which states are terminal -- and enforces it with a 409. Mirroring
 * it here would put a second, unenforced copy of a domain rule in a React application, and
 * the two would drift the first time the backend gained a state. This stage renders status;
 * it does not reason about what may follow one.
 *
 * ## Colour is never the signal
 *
 * Every status renders as text plus an icon plus a tone. The text carries the meaning; the
 * tone repeats it. Roughly one man in twelve cannot rely on a red/green distinction, and
 * status is most of what an operations console displays.
 *
 * The tones are chosen for operational reading rather than sentiment: `checked_in` is the
 * one that means "a person is in the building right now", so it is the one that stands out.
 * `checked_out` is a completed stay and is deliberately quiet -- it is the commonest state in
 * any history and colouring it green would make a list of finished bookings shout.
 */

export interface StatusPresentation {
  readonly label: string
  readonly tone: BadgeTone
  readonly icon: LucideIcon
  /** Read to a screen reader in place of the bare code. */
  readonly description: string
}

const STATUS_PRESENTATION: Readonly<Record<BookingStatus, StatusPresentation>> = {
  pending: {
    label: 'Pending',
    tone: 'warning',
    icon: CalendarClock,
    description: 'Pending — not yet confirmed and holding no room',
  },
  confirmed: {
    label: 'Confirmed',
    tone: 'info',
    icon: CalendarCheck,
    description: 'Confirmed — the room is held for these dates',
  },
  checked_in: {
    label: 'Checked in',
    tone: 'success',
    icon: BedDouble,
    description: 'Checked in — the guest is in the property now',
  },
  checked_out: {
    label: 'Checked out',
    tone: 'neutral',
    icon: LogOut,
    description: 'Checked out — the stay is complete',
  },
  cancelled: {
    label: 'Cancelled',
    tone: 'danger',
    icon: CircleSlash,
    description: 'Cancelled — the booking will not be honoured',
  },
  no_show: {
    label: 'No show',
    tone: 'danger',
    icon: UserX,
    description: 'No show — the guest held a reservation and did not arrive',
  },
}

/**
 * Presentation for a status the backend sent.
 *
 * An unrecognised value is rendered as itself rather than dropped or replaced with a
 * default. The six-state union makes that unreachable through the type system, but a
 * response is runtime data: if the backend ever gains a seventh state, an operator should
 * see the unfamiliar word and ask, not see it silently rendered as "Pending".
 */
export function statusPresentation(status: BookingStatus): StatusPresentation {
  return (
    STATUS_PRESENTATION[status] ?? {
      label: status,
      tone: 'neutral' as BadgeTone,
      icon: CalendarClock,
      description: `Status reported by the server as "${status}"`,
    }
  )
}

/** Every status, in lifecycle order, for a filter control. */
export const BOOKING_STATUSES: readonly BookingStatus[] = [
  'pending',
  'confirmed',
  'checked_in',
  'checked_out',
  'cancelled',
  'no_show',
]

/** Booking sources, as they read in an interface rather than as enum codes. */
const SOURCE_LABELS: Readonly<Record<BookingSource, string>> = {
  direct: 'Direct',
  website: 'Website',
  phone: 'Phone',
  walk_in: 'Walk-in',
  booking_com: 'Booking.com',
  expedia: 'Expedia',
  airbnb: 'Airbnb',
  agoda: 'Agoda',
  other: 'Other',
}

export function sourceLabel(source: BookingSource): string {
  return SOURCE_LABELS[source] ?? source
}

/** How a booking stands against its ledger, as the reconciliation endpoint reports it. */
const PAYMENT_STATE: Readonly<Record<PaymentState, { label: string; tone: BadgeTone }>> = {
  unpaid: { label: 'Unpaid', tone: 'warning' },
  partially_paid: { label: 'Partly paid', tone: 'warning' },
  paid: { label: 'Paid', tone: 'success' },
  // Not an error, and not styled as one: more was refunded or charged than the stay is
  // worth, which the API reports honestly rather than clamping. It needs attention, not alarm.
  overpaid: { label: 'Overpaid', tone: 'info' },
}

export function paymentStatePresentation(state: PaymentState): { label: string; tone: BadgeTone } {
  return PAYMENT_STATE[state] ?? { label: state, tone: 'neutral' as BadgeTone }
}
