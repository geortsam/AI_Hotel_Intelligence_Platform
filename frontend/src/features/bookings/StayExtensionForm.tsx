import { useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import { MAX_EXTENSION_NIGHTS } from '@/features/bookings/transitions'
import { formatDate, nightsBetween } from '@/lib/format'
import type { Booking, StayExtensionRequest } from '@/types/booking'

import styles from './StayForms.module.css'

export interface StayExtensionFormProps {
  readonly booking: Booking
  readonly onSubmit: (payload: StayExtensionRequest) => void
  readonly onCancel: () => void
  readonly busy: boolean
}

/** The day after a `YYYY-MM-DD`, for the earliest departure an extension can name. */
function dayAfter(isoDate: string): string {
  const [year, month, day] = isoDate.split('-').map(Number) as [number, number, number]
  return new Date(Date.UTC(year, month - 1, day + 1)).toISOString().slice(0, 10)
}

/**
 * Keeping an in-house guest longer.
 *
 * ## A separate operation, not a variant of editing the stay
 *
 * The backend gives this its own route, and the separation is the design: a checked-in stay
 * is physically in progress. Its check-in is in the past, some of its nights have been
 * slept, and its room holds someone's belongings. So an extension may only push the
 * departure outward -- it cannot move check-in, cannot reassign a room, and cannot shorten.
 * Merging it into the general stay editor would put "may this guest's room be reassigned?"
 * behind an omitted key.
 *
 * The payload has exactly one field, and the fields it lacks are the contract.
 *
 * ## What the server refuses, and why this form does not pretend otherwise
 *
 * * A date not strictly later than the current departure is a **409**, including a repeat of
 *   the current one. That is the shape a retried request takes, and answering it with a
 *   cheerful 200 would claim a second extension happened. The input's `min` is the day after
 *   the current check-out, which keeps the obvious mistake off the wire -- but the server is
 *   still the authority, and its refusal is rendered.
 * * More than {@link MAX_EXTENSION_NIGHTS} added nights is a **422** naming the limit.
 * * The room being taken for the added nights is a **409** from the exclusion constraint.
 *   Nothing here checks availability first: the database decides, and a room that looked
 *   free a moment ago may not be.
 *
 * The nights already slept keep the rates they were sold at. Only the added nights are
 * priced, by the server, and no amount can be proposed from here.
 */
export function StayExtensionForm({ booking, onSubmit, onCancel, busy }: StayExtensionFormProps) {
  const dateId = useId()
  const errorId = useId()
  const earliest = dayAfter(booking.check_out_date)

  const [checkOut, setCheckOut] = useState(earliest)
  const [problem, setProblem] = useState<string | null>(null)

  const added = nightsBetween(booking.check_out_date, checkOut)

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    if (busy) {
      return
    }
    if (added === null || added <= 0) {
      setProblem(
        `A new departure must be later than ${formatDate(booking.check_out_date)}. An extension cannot shorten a stay in progress.`,
      )
      return
    }
    if (added > MAX_EXTENSION_NIGHTS) {
      setProblem(`An extension may add at most ${MAX_EXTENSION_NIGHTS} nights; this adds ${added}.`)
      return
    }
    setProblem(null)
    onSubmit({ check_out_date: checkOut })
  }

  return (
    <form className={styles.form} onSubmit={handleSubmit} noValidate>
      <p className={styles.intro}>
        The guest is in the property. Extending moves the departure later and keeps the same
        room; the nights already stayed keep the rates they were sold at, and the added nights
        are priced by the server.
      </p>

      <div className={styles.fields}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={dateId}>
            New check-out
          </label>
          <input
            id={dateId}
            className={styles.input}
            type="date"
            value={checkOut}
            min={earliest}
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
        {added !== null && added > 0
          ? `Adds ${added} night${added === 1 ? '' : 's'}, moving departure from ${formatDate(booking.check_out_date)} to ${formatDate(checkOut)}.`
          : `Currently departing ${formatDate(booking.check_out_date)}. Choose a later date.`}
      </p>

      {problem ? (
        <p className={styles.error} id={errorId} role="alert">
          {problem}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button type="submit" variant="primary" size="sm" disabled={busy}>
          {busy ? 'Extending…' : 'Extend stay'}
        </Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
