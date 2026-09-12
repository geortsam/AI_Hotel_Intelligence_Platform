import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/Badge'
import { formatDateTime, UNAVAILABLE } from '@/lib/format'
import { guestPath } from '@/router/routes'
import type { Guest } from '@/types/guest'

import styles from './GuestTable.module.css'

export interface GuestTableProps {
  readonly guests: readonly Guest[]
  readonly timeZone: string
}

/**
 * One page of guests, as a table.
 *
 * ## Contact details are shown in full here, and that is a deliberate difference
 *
 * The booking screen masks a guest's email and phone -- a detail page read over the desk by
 * whoever is standing at it has no need for them. This screen is the one where those details
 * are *managed*: an operator correcting a mistyped address has to be able to read it first,
 * and a masked field that becomes editable on the next screen is a worse privacy story than
 * an honest one, not a better one. The masking on `BookingDetailPage` is unchanged.
 *
 * ## The link is the name, not the row
 *
 * A whole `<tr>` wired to `onClick` is not a link: it cannot be tabbed to, opened in a new
 * tab, or announced as a destination. The guest's name is a real anchor to a real route, and
 * the row is inert.
 *
 * ## Every value here came off the wire
 *
 * Name, email, phone, country, language, consent and `updated_at` are all fields of
 * `GuestResponse`. Nothing is fetched per row to fill a column -- the response is already
 * complete, and a request per guest would be the N+1 the brief rules out.
 *
 * `date_of_birth` and `notes` are not columns here. They are on the detail page, where
 * there is room to show them beside the form that edits them; putting a date of birth in a
 * list that a receptionist has open all day is more exposure than the screen needs.
 */
export function GuestTable({ guests, timeZone }: GuestTableProps) {
  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption className={styles.caption}>
          Guests of this property, ordered by surname then forename.
        </caption>
        <thead>
          <tr>
            <th scope="col">Name</th>
            <th scope="col">Email</th>
            <th scope="col">Phone</th>
            <th scope="col">Country</th>
            <th scope="col">Language</th>
            <th scope="col">Marketing</th>
            <th scope="col">Updated</th>
          </tr>
        </thead>
        <tbody>
          {guests.map((guest) => (
            <tr key={guest.public_id}>
              <th scope="row" className={styles.nameCell}>
                <Link className={styles.link} to={guestPath(guest.public_id)}>
                  {guest.last_name}, {guest.first_name}
                </Link>
              </th>
              <td className={styles.contact}>
                {guest.email ?? <span className={styles.absent}>{UNAVAILABLE}</span>}
              </td>
              <td className={styles.contact}>
                {guest.phone ?? <span className={styles.absent}>{UNAVAILABLE}</span>}
              </td>
              <td>{guest.country_code ?? <span className={styles.absent}>{UNAVAILABLE}</span>}</td>
              <td>
                {guest.preferred_language ?? (
                  <span className={styles.absent}>{UNAVAILABLE}</span>
                )}
              </td>
              <td>
                {/* A word, not a colour: consent is the field most likely to be acted on. */}
                <Badge tone={guest.marketing_opt_in ? 'success' : 'neutral'}>
                  {guest.marketing_opt_in ? 'Opted in' : 'No consent'}
                </Badge>
              </td>
              <td className={styles.timestamp}>{formatDateTime(guest.updated_at, timeZone)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
