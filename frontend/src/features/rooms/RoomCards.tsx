import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/Badge'
import { activePresentation, statusPresentation } from '@/features/rooms/vocabulary'
import { formatDateTime } from '@/lib/format'
import { roomPath } from '@/router/routes'
import type { Room } from '@/types/room'

import styles from './RoomCards.module.css'

export interface RoomCardsProps {
  readonly rooms: readonly Room[]
  readonly timeZone: string
}

/**
 * The room list below 768px, as cards.
 *
 * The same rooms and the same fields, not a subset: a seven-column table on a phone is either
 * a horizontal scroll nobody finds or four columns silently dropped. Fields the record does
 * not carry are left out rather than printed as em dashes.
 *
 * A `<ul>` so the count is announced and each room is one item. Keyed by `room_number`, which
 * is unique within the hotel and is the room's identity.
 */
export function RoomCards({ rooms, timeZone }: RoomCardsProps) {
  return (
    <ul className={styles.list}>
      {rooms.map((room) => {
        const status = statusPresentation(room.status)
        const active = activePresentation(room.is_active)
        return (
          <li key={room.room_number} className={styles.card}>
            <div className={styles.header}>
              <h3 className={styles.number}>
                <Link className={styles.link} to={roomPath(room.room_type_code, room.room_number)}>
                  {room.room_number}
                </Link>
              </h3>
              <div className={styles.badges}>
                <Badge tone={status.tone} withDot>
                  {status.label}
                </Badge>
                <Badge tone={active.tone}>{active.label}</Badge>
              </div>
            </div>

            <dl className={styles.fields}>
              <Field label="Type">{room.room_type_code}</Field>
              {room.floor !== null ? <Field label="Floor">{room.floor}</Field> : null}
              {room.notes !== null && room.notes !== '' ? (
                <Field label="Notes">{room.notes}</Field>
              ) : null}
              <Field label="Updated">{formatDateTime(room.updated_at, timeZone)}</Field>
            </dl>
          </li>
        )
      })}
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
