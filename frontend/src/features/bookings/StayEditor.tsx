import { useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import { formatDate, nightsBetween } from '@/lib/format'
import type { Booking, StayModificationRequest } from '@/types/booking'

import styles from './StayForms.module.css'

export interface StayEditorProps {
  readonly booking: Booking
  readonly onSubmit: (payload: StayModificationRequest) => void
  readonly onCancel: () => void
  readonly busy: boolean
}

/** Every calendar date in `[from, to)`. The half-open span the schema requires. */
function nightsInSpan(from: string, to: string): string[] {
  const count = nightsBetween(from, to)
  if (count === null || count <= 0) {
    return []
  }
  const [year, month, day] = from.split('-').map(Number) as [number, number, number]
  return Array.from({ length: count }, (_, offset) =>
    new Date(Date.UTC(year, month - 1, day + offset)).toISOString().slice(0, 10),
  )
}

/**
 * Moving a booking's stay.
 *
 * ## What this sends, and what it cannot
 *
 * `PATCH .../stay` **restates the whole stay** rather than patching it: moving the dates
 * rewrites every allocation and every night row, so the payload carries all of them. The
 * server then checks that each room prices *exactly* the nights of `[check_in, check_out)`
 * -- a mismatch is a 422 naming the missing and unexpected dates.
 *
 * So this form builds the full payload: the new span, and each existing allocation carried
 * over verbatim with its nights regenerated for that span. Enumerating the dates the user
 * chose is not a business calculation; it is the shape the contract demands.
 *
 * **No amount is sent, and none can be.** A night request has no `rate` field at all, and
 * `additionalProperties: false` applies at every level -- verified live, sending a rate is a
 * 422 naming the field rather than a number the server quietly ignores. Rates are derived
 * server-side from the room type's configured price, and the financial consequence comes
 * back in the response for the page to display.
 *
 * ## Rooms are preserved, not edited
 *
 * The contract does allow reallocating rooms here. This form deliberately does not offer it:
 * a room picker needs the hotel's room inventory, and choosing between rooms without showing
 * availability is a poor tool while showing availability would mean reproducing in React the
 * exclusion constraint that is the database's alone. Dates are the operation a front desk
 * actually performs; reallocation is named in the report as outstanding.
 *
 * ## Dates stay dates
 *
 * `<input type="date">` yields `YYYY-MM-DD` and that string is sent unchanged. It is never
 * parsed into a `Date` and re-serialised, because that is the step that turns a calendar date
 * into an instant and shifts it by a day in the wrong time zone.
 */
export function StayEditor({ booking, onSubmit, onCancel, busy }: StayEditorProps) {
  const checkInId = useId()
  const checkOutId = useId()
  const errorId = useId()

  const [checkIn, setCheckIn] = useState(booking.check_in_date)
  const [checkOut, setCheckOut] = useState(booking.check_out_date)
  const [problem, setProblem] = useState<string | null>(null)

  const nights = nightsBetween(checkIn, checkOut)
  const unchanged = checkIn === booking.check_in_date && checkOut === booking.check_out_date

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    if (busy) {
      return
    }

    // Checked here only to keep an obviously impossible request off the wire. The server
    // applies the same rule and remains the authority; this is not a second opinion about
    // what a valid stay is, it is a way to answer instantly instead of after a round trip.
    if (nights === null || nights <= 0) {
      setProblem('Check-out must be after check-in. A stay is at least one night.')
      return
    }
    setProblem(null)

    const dates = nightsInSpan(checkIn, checkOut)
    onSubmit({
      check_in_date: checkIn,
      check_out_date: checkOut,
      rooms: booking.rooms.map((room) => ({
        room_number: room.room_number,
        adults: room.adults,
        children: room.children,
        guest_name: room.guest_name,
        // One entry per night of the new span, carrying no price.
        nights: dates.map((stay_date) => ({ stay_date })),
      })),
    })
  }

  return (
    <form className={styles.form} onSubmit={handleSubmit} noValidate>
      <p className={styles.intro}>
        Moving the stay rewrites every night at the server&rsquo;s own rates. The rooms
        allocated to this booking are kept as they are.
      </p>

      <div className={styles.fields}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={checkInId}>
            Check-in
          </label>
          <input
            id={checkInId}
            className={styles.input}
            type="date"
            value={checkIn}
            disabled={busy}
            required
            aria-describedby={problem ? errorId : undefined}
            aria-invalid={problem !== null || undefined}
            onChange={(event) => {
              setCheckIn(event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={checkOutId}>
            Check-out
          </label>
          <input
            id={checkOutId}
            className={styles.input}
            type="date"
            value={checkOut}
            disabled={busy}
            required
            aria-describedby={problem ? errorId : undefined}
            aria-invalid={problem !== null || undefined}
            onChange={(event) => {
              setCheckOut(event.target.value)
            }}
          />
        </div>
      </div>

      <p className={styles.hint}>
        {nights !== null && nights > 0
          ? `${nights} night${nights === 1 ? '' : 's'}: ${formatDate(checkIn)} to ${formatDate(checkOut)}. The departure day is not charged.`
          : 'Check-out must be after check-in.'}
      </p>

      {problem ? (
        <p className={styles.error} id={errorId} role="alert">
          {problem}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button type="submit" variant="primary" size="sm" disabled={busy || unchanged}>
          {busy ? 'Saving…' : 'Save stay'}
        </Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
