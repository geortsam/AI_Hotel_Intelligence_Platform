import { Link } from 'react-router-dom'

import { Skeleton } from '@/components/ui/Skeleton'
import { BookingStatusBadge } from '@/features/bookings/BookingStatusBadge'
import { sourceLabel } from '@/features/bookings/vocabulary'
import { formatCount, formatDate, formatMoney, UNAVAILABLE } from '@/lib/format'
import { bookingPath } from '@/router/routes'
import type { Booking } from '@/types/booking'

import styles from './BookingCards.module.css'

export interface BookingCardsProps {
  readonly bookings: readonly Booking[]
  readonly isLoading?: boolean
  readonly skeletonRows?: number
}

/**
 * The bookings list at phone width.
 *
 * A different structure, not a narrower table. Seven columns cannot be read on a 375px
 * screen, and the stage brief rules out shrinking one until it is unreadable; this drops the
 * grid entirely and gives each booking a card with its fields stacked and labelled.
 *
 * ## It is a real list, and the whole card is not a link
 *
 * `<ul>`/`<li>` gives a screen reader the count and lets it move item by item. Inside each
 * card the reference is the link -- one interactive element with a meaningful name, rather
 * than a card-sized anchor that swallows the status badge and the amount into its own
 * accessible name and reads as one enormous run-on.
 *
 * ## The same honesty as the table
 *
 * "Contracted total" is labelled as such: `total_amount` is the client's figure and not what
 * the server can prove the stay is worth. "Occupant" is the name on the allocation, not the
 * booker, whom the list endpoint does not carry.
 */
export function BookingCards({ bookings, isLoading = false, skeletonRows = 6 }: BookingCardsProps) {
  if (isLoading) {
    return (
      <ul className={styles.list} aria-busy="true" aria-label="Loading bookings">
        {Array.from({ length: skeletonRows }, (_, index) => (
          <li key={index} className={styles.card}>
            <Skeleton width="40%" height="1rem" />
            <Skeleton width="70%" height="0.75rem" />
            <Skeleton width="55%" height="0.75rem" />
          </li>
        ))}
      </ul>
    )
  }

  return (
    <ul className={styles.list}>
      {bookings.map((booking) => {
        const nights = booking.rooms[0]?.nights ?? null
        const occupant = booking.rooms
          .map((room) => room.guest_name)
          .filter((name): name is string => name !== null && name.trim() !== '')[0]

        return (
          <li key={booking.public_id} className={styles.card}>
            <div className={styles.head}>
              <Link
                className={styles.reference}
                to={bookingPath(booking.public_id)}
                aria-label={`Booking ${booking.reference}, ${formatDate(
                  booking.check_in_date,
                )} to ${formatDate(booking.check_out_date)}`}
              >
                {booking.reference}
              </Link>
              <BookingStatusBadge status={booking.status} />
            </div>

            <dl className={styles.facts}>
              <div className={styles.fact}>
                <dt>Stay</dt>
                <dd>
                  {formatDate(booking.check_in_date)} – {formatDate(booking.check_out_date)}
                  {nights !== null ? (
                    <span className={styles.nights}>
                      {' '}
                      · {formatCount(nights)} night{nights === 1 ? '' : 's'}
                    </span>
                  ) : null}
                </dd>
              </div>
              <div className={styles.fact}>
                <dt>Rooms</dt>
                <dd>
                  {booking.rooms.length === 0
                    ? UNAVAILABLE
                    : booking.rooms
                        .map((room) => `${room.room_number} (${room.room_type_code})`)
                        .join(', ')}
                </dd>
              </div>
              <div className={styles.fact}>
                <dt>Occupant</dt>
                <dd>{occupant ?? UNAVAILABLE}</dd>
              </div>
              <div className={styles.fact}>
                <dt>Contracted total</dt>
                <dd className={styles.numeric}>
                  {formatMoney(booking.total_amount, booking.currency)}
                </dd>
              </div>
              <div className={styles.fact}>
                <dt>Source</dt>
                <dd>{sourceLabel(booking.source)}</dd>
              </div>
            </dl>
          </li>
        )
      })}
    </ul>
  )
}
