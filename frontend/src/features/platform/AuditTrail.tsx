import { formatDateTime } from '@/lib/format'
import { useIsCompact } from '@/lib/useIsCompact'
import type { PlatformAuditEvent } from '@/types/platform'

import styles from './AuditTrail.module.css'

export interface AuditTrailProps {
  readonly events: readonly PlatformAuditEvent[]
  readonly timeZone: string
}

/**
 * Platform-scoped audit history, newest first.
 *
 * ## Everything here is rendered as text
 *
 * `details` is a free-form object written by the backend. It is stringified and printed, not
 * interpreted: guessing at its shape per action would be inventing a schema the API does not
 * publish, and rendering it as markup would turn an audit record into a script host. The
 * backend promises it never carries a credential, a token, a card fragment or an internal
 * key -- and it is still only ever text here.
 *
 * ## No figure is derived
 *
 * No counts per actor, no "most changed", no before-and-after reconstructed by pairing rows.
 * The trail is what the server recorded, shown as recorded; the total comes from the page
 * envelope.
 *
 * ## There is no action column beyond reading
 *
 * The trail is append-only -- no endpoint writes, edits or deletes an event -- so there is no
 * control that could change one, and none is shown.
 */
export function AuditTrail({ events, timeZone }: AuditTrailProps) {
  const isCompact = useIsCompact()

  if (isCompact) {
    return (
      <ul className={styles.cards}>
        {events.map((event) => (
          <li key={event.public_id} className={styles.card}>
            <div className={styles.cardHeader}>
              <span className={styles.action}>{event.action}</span>
              <span className={styles.when}>{formatDateTime(event.occurred_at, timeZone)}</span>
            </div>
            <dl className={styles.fields}>
              <Field label="Resource">
                {event.resource_type} &middot; {event.resource_reference}
              </Field>
              <Field label="By">{event.actor_email ?? 'Not recorded'}</Field>
              {event.request_id === null ? null : (
                <Field label="Request">{event.request_id}</Field>
              )}
              <Field label="Details">
                <span className={styles.details}>{describe(event.details)}</span>
              </Field>
            </dl>
          </li>
        ))}
      </ul>
    )
  }

  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption className={styles.caption}>
          Changes to the resources every property shares, newest first. Read-only: the trail
          is append-only and no endpoint alters it.
        </caption>
        <thead>
          <tr>
            <th scope="col">When</th>
            <th scope="col">Action</th>
            <th scope="col">Resource</th>
            <th scope="col">By</th>
            <th scope="col">Request</th>
            <th scope="col">Details</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => (
            <tr key={event.public_id}>
              <th scope="row" className={styles.whenCell}>
                {formatDateTime(event.occurred_at, timeZone)}
              </th>
              <td>
                <span className={styles.action}>{event.action}</span>
              </td>
              <td className={styles.resourceCell}>
                <span className={styles.resourceType}>{event.resource_type}</span>
                <span className={styles.reference}>{event.resource_reference}</span>
              </td>
              <td className={styles.actorCell}>
                {event.actor_email ?? <span className={styles.absent}>Not recorded</span>}
              </td>
              <td className={styles.actorCell}>
                {/* The X-Request-ID, so an event and the log lines for that request can be
                    read together. Shown here as well as on the card layout: the wider view
                    must not carry less than the narrower one. */}
                {event.request_id ?? <span className={styles.absent}>Not recorded</span>}
              </td>
              <td className={styles.details}>{describe(event.details)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className={styles.field}>
      <dt className={styles.fieldLabel}>{label}</dt>
      <dd className={styles.fieldValue}>{children}</dd>
    </div>
  )
}

/**
 * The `details` object as one readable line.
 *
 * `key: value` pairs rather than raw JSON braces, because the object is small and an operator
 * reading a trail should not have to parse punctuation. Values are stringified with
 * `String()`, so a nested object becomes `[object Object]` rather than being walked -- the
 * backend's details are flat, and a recursive renderer would be guessing at a shape the API
 * does not publish.
 */
function describe(details: Readonly<Record<string, unknown>>): string {
  const entries = Object.entries(details)
  if (entries.length === 0) {
    return '—'
  }
  return entries.map(([key, value]) => `${key}: ${String(value)}`).join(', ')
}
