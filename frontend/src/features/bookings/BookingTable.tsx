import { Link } from 'react-router-dom'

import { Skeleton } from '@/components/ui/Skeleton'
import { BookingStatusBadge } from '@/features/bookings/BookingStatusBadge'
import { sourceLabel } from '@/features/bookings/vocabulary'
import {
  formatCount,
  formatDate,
  formatDayAndMonth,
  formatDateTime,
  formatMoney,
  UNAVAILABLE,
} from '@/lib/format'
import { bookingPath } from '@/router/routes'
import type { Booking } from '@/types/booking'

import styles from './BookingTable.module.css'

export interface BookingTableProps {
  readonly bookings: readonly Booking[]
  /** The hotel's IANA zone. Timestamps are instants and have to be shown in some zone. */
  readonly timeZone: string
  /** Renders placeholder rows of the right shape instead of data. */
  readonly isLoading?: boolean
  /** How many skeleton rows to draw while loading. Matched to the page size. */
  readonly skeletonRows?: number
}

/** The occupant names an allocation carries, or null when none does. */
function occupants(booking: Booking): string | null {
  const names = booking.rooms
    .map((room) => room.guest_name)
    .filter((name): name is string => name !== null && name.trim() !== '')
  return names.length === 0 ? null : [...new Set(names)].join(', ')
}

function roomSummary(booking: Booking): string {
  if (booking.rooms.length === 0) {
    return UNAVAILABLE
  }
  return booking.rooms.map((room) => `${room.room_number} (${room.room_type_code})`).join(', ')
}

/** The stay, as one readable span. The year is stated once unless the stay crosses one. */
function stayLabel(booking: Booking): string {
  const sameYear = booking.check_in_date.slice(0, 4) === booking.check_out_date.slice(0, 4)
  const from = sameYear
    ? formatDayAndMonth(booking.check_in_date)
    : formatDate(booking.check_in_date)
  return `${from} – ${formatDate(booking.check_out_date)}`
}

/** Nights, from the allocation the backend already counted. Never recomputed from the dates. */
function nightCount(booking: Booking): number | null {
  if (booking.rooms.length === 0) {
    return null
  }
  // `nights` is a generated column on each allocation, so every room on one booking reports
  // the same span; the first is representative and no arithmetic is done here.
  return booking.rooms[0]!.nights
}

/**
 * The bookings table.
 *
 * ## The row's interactive element is a real link
 *
 * The reference cell holds a `<Link>`, and that is the whole navigation affordance. A
 * `<tr onClick>` -- the usual shortcut -- is invisible to a keyboard, unreachable by Tab,
 * announced as nothing, and breaks middle-click and "open in new tab". The stage brief calls
 * this out and it is right to: an operations console is used by people who navigate by
 * keyboard all day.
 *
 * The link's accessible name is the reference plus the stay, so a screen-reader user moving
 * link-by-link hears "MH-00162, 17 Sept to 18 Sept" rather than forty identical "MH-…"
 * entries with no context.
 *
 * ## The money column is labelled "contracted", deliberately
 *
 * `booking.total_amount` is the figure the client supplied and is explicitly **not** defined
 * as the sum of the nightly rates. Calling this column "Total" would present it as what the
 * stay is worth, which only `reconciliation.accommodation_total` can say -- and that is one
 * request per booking, so the list does not claim it. The detail page does.
 *
 * ## Guest
 *
 * The column shows the occupant named on the allocation, not the booker: `BookingResponse`
 * carries no guest name at all, only `guest_public_id`. Resolving the real guest for every
 * row would be an N+1. The header says "Occupant" so the two are not confused.
 */
export function BookingTable({
  bookings,
  timeZone,
  isLoading = false,
  skeletonRows = 8,
}: BookingTableProps) {
  return (
    <div className={styles.scroll}>
      <table className={styles.table} aria-busy={isLoading || undefined}>
        <caption className={styles.caption}>
          Bookings for this hotel, most recent arrival first. Select a reference to open the
          booking.
        </caption>
        <thead>
          <tr>
            <th scope="col">Reference</th>
            <th scope="col">Occupant</th>
            <th scope="col">Stay</th>
            <th scope="col">Rooms</th>
            <th scope="col">Status</th>
            <th scope="col" className={styles.numeric}>
              Contracted total
            </th>
            <th scope="col">Booked</th>
          </tr>
        </thead>
        <tbody>
          {isLoading
            ? Array.from({ length: skeletonRows }, (_, index) => (
                <tr key={index}>
                  {Array.from({ length: 7 }, (__, cell) => (
                    <td key={cell}>
                      <Skeleton height="1rem" />
                    </td>
                  ))}
                </tr>
              ))
            : bookings.map((booking) => {
                const nights = nightCount(booking)
                return (
                  <tr key={booking.public_id}>
                    <th scope="row" className={styles.referenceCell}>
                      <Link
                        className={styles.reference}
                        to={bookingPath(booking.public_id)}
                        aria-label={`Booking ${booking.reference}, ${stayLabel(booking)}`}
                      >
                        {booking.reference}
                      </Link>
                      <span className={styles.sourceLine}>{sourceLabel(booking.source)}</span>
                    </th>
                    <td>{occupants(booking) ?? <span className={styles.muted}>{UNAVAILABLE}</span>}</td>
                    <td>
                      <span className={styles.stay}>{stayLabel(booking)}</span>
                      {nights !== null ? (
                        <span className={styles.nights}>
                          {formatCount(nights)} night{nights === 1 ? '' : 's'}
                        </span>
                      ) : null}
                    </td>
                    <td className={styles.rooms}>{roomSummary(booking)}</td>
                    <td>
                      <BookingStatusBadge status={booking.status} />
                    </td>
                    <td className={styles.numeric}>
                      {formatMoney(booking.total_amount, booking.currency)}
                    </td>
                    <td className={styles.booked}>
                      {formatDateTime(booking.booked_at, timeZone)}
                    </td>
                  </tr>
                )
              })}
        </tbody>
      </table>
    </div>
  )
}
