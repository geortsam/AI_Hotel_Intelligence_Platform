import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/Badge'
import { formatDateTime } from '@/lib/format'
import { guestPath } from '@/router/routes'
import type { Guest } from '@/types/guest'

import styles from './GuestCards.module.css'

export interface GuestCardsProps {
  readonly guests: readonly Guest[]
  readonly timeZone: string
}

/**
 * The guest list below 768px, as cards.
 *
 * The same guests and the same fields, not a subset: a seven-column table on a phone is
 * either a horizontal scroll nobody finds or four columns silently dropped, and the dropped
 * one is always the one somebody needed. Fields the record does not carry are left out
 * entirely rather than printed as em dashes -- a column of dashes is what makes a small
 * screen unreadable.
 *
 * A `<ul>` so the count is announced and each guest is one item. Keyed by `public_id`, which
 * unlike a ledger line or a review a guest actually has.
 */
export function GuestCards({ guests, timeZone }: GuestCardsProps) {
  return (
    <ul className={styles.list}>
      {guests.map((guest) => (
        <li key={guest.public_id} className={styles.card}>
          <div className={styles.header}>
            <h3 className={styles.name}>
              <Link className={styles.link} to={guestPath(guest.public_id)}>
                {guest.last_name}, {guest.first_name}
              </Link>
            </h3>
            <Badge tone={guest.marketing_opt_in ? 'success' : 'neutral'}>
              {guest.marketing_opt_in ? 'Opted in' : 'No consent'}
            </Badge>
          </div>

          <dl className={styles.fields}>
            {guest.email !== null ? <Field label="Email">{guest.email}</Field> : null}
            {guest.phone !== null ? <Field label="Phone">{guest.phone}</Field> : null}
            {guest.country_code !== null ? (
              <Field label="Country">{guest.country_code}</Field>
            ) : null}
            {guest.preferred_language !== null ? (
              <Field label="Language">{guest.preferred_language}</Field>
            ) : null}
            <Field label="Updated">{formatDateTime(guest.updated_at, timeZone)}</Field>
          </dl>
        </li>
      ))}
    </ul>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className={styles.field}>
      <dt className={styles.label}>{label}</dt>
      <dd className={styles.value}>{children}</dd>
    </div>
  )
}
