import { Building2 } from 'lucide-react'

import { useHotelContext } from '@/session/HotelProvider'

import styles from './Header.module.css'

/**
 * Which hotel the application is showing, in the header.
 *
 * This replaces Stage 5.2's static "No hotel selected" chip. That placeholder existed
 * because the frontend had no way to know the hotel context; reading the backend showed that
 * it does. `GET /hotels` is filtered by the server to the caller's own memberships, so the
 * list rendered here is a membership query's answer -- not a set of names this application
 * chose, and not something it can widen.
 *
 * **Selecting a hotel grants nothing.** Authorization is re-decided from `user_hotels` on
 * every hotel-scoped request; a tampered selection produces a 404, identical to the response
 * for a hotel that does not exist. So this control changes what is *asked for*, never what
 * is *permitted*.
 *
 * With a single membership -- the ordinary case -- there is nothing to choose between and
 * the name is shown as plain text. A `<select>` appears only when the account genuinely
 * belongs to more than one property. Rendering a one-option dropdown would be offering a
 * decision that does not exist.
 */
export function HotelContextChip() {
  const { status, hotels, selected, hasMore, select } = useHotelContext()

  if (status === 'loading' || status === 'idle') {
    return (
      <div className={styles.hotelContext}>
        <Building2 size={15} aria-hidden="true" />
        <span className={styles.hotelContextLabel} role="status">
          Loading hotels…
        </span>
      </div>
    )
  }

  if (status === 'error') {
    return (
      <div className={styles.hotelContext}>
        <Building2 size={15} aria-hidden="true" />
        {/* Not "No hotel selected". The difference between "you have none" and "we could
         * not ask" is exactly the distinction the dashboard's states are built around, and
         * the header must not blur it. */}
        <span className={styles.hotelContextLabel}>Hotels unavailable</span>
      </div>
    )
  }

  if (status === 'empty' || selected === null) {
    return (
      <div className={styles.hotelContext}>
        <Building2 size={15} aria-hidden="true" />
        <span className={styles.hotelContextLabel}>No hotel linked</span>
      </div>
    )
  }

  if (hotels.length === 1) {
    return (
      <div className={styles.hotelContext} title={selected.name}>
        <Building2 size={15} aria-hidden="true" />
        <span className={styles.hotelContextLabel}>{selected.name}</span>
      </div>
    )
  }

  return (
    <div className={styles.hotelContext}>
      <Building2 size={15} aria-hidden="true" />
      {/*
       * A native <select>. It carries its own keyboard behaviour, its own mobile picker and
       * its own screen-reader semantics, none of which a custom listbox gets right for free.
       * The label is on the element rather than beside it because the header has no room for
       * a visible one and the icon is decorative.
       */}
      <select
        className={styles.hotelSelect}
        aria-label="Selected hotel"
        value={selected.public_id}
        onChange={(event) => {
          select(event.target.value)
        }}
      >
        {hotels.map((hotel) => (
          <option key={hotel.public_id} value={hotel.public_id}>
            {hotel.name}
          </option>
        ))}
      </select>
      {hasMore ? (
        // Said out loud rather than hidden: a list that silently shows some of the user's
        // properties is worse than one that admits it is partial.
        <span className={styles.hotelContextMore} title="More properties exist than are listed">
          +
        </span>
      ) : null}
    </div>
  )
}
