import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/Badge'
import { activePresentation, statusPresentation } from '@/features/rooms/vocabulary'
import { formatDateTime, UNAVAILABLE } from '@/lib/format'
import { roomPath } from '@/router/routes'
import type { Room } from '@/types/room'

import styles from './RoomTable.module.css'

export interface RoomTableProps {
  readonly rooms: readonly Room[]
  readonly timeZone: string
}

/**
 * One page of a room type's rooms, as a table.
 *
 * ## The room type is on the row, not fetched for it
 *
 * `room_type_code` is a field of every `RoomResponse`, so the column costs nothing. Resolving
 * the type per room would be one request per row -- the N+1 the brief rules out -- and the
 * response makes it unnecessary. The type's descriptive fields (name, occupancy, price) come
 * from the catalogue the page already holds, fetched once.
 *
 * ## Status and service state are two columns, because they are two fields
 *
 * `status` is the room's current operational state; `is_active` is whether it is in service
 * at all. The schema keeps them apart and a room can be `available` and withdrawn at the same
 * time, so nothing here folds one into the other.
 *
 * ## The link is the number, not the row
 *
 * A whole `<tr>` wired to `onClick` cannot be tabbed to, opened in a new tab or announced as
 * a destination. The room number is a real anchor; the row is inert.
 */
export function RoomTable({ rooms, timeZone }: RoomTableProps) {
  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption className={styles.caption}>
          Rooms of this type, ordered by room number.
        </caption>
        <thead>
          <tr>
            <th scope="col">Room</th>
            <th scope="col">Type</th>
            <th scope="col">Floor</th>
            <th scope="col">Status</th>
            <th scope="col">Service</th>
            <th scope="col">Notes</th>
            <th scope="col">Updated</th>
          </tr>
        </thead>
        <tbody>
          {rooms.map((room) => {
            const status = statusPresentation(room.status)
            const active = activePresentation(room.is_active)
            return (
              <tr key={room.room_number}>
                <th scope="row" className={styles.numberCell}>
                  <Link
                    className={styles.link}
                    to={roomPath(room.room_type_code, room.room_number)}
                  >
                    {room.room_number}
                  </Link>
                </th>
                <td>
                  <span className={styles.code}>{room.room_type_code}</span>
                </td>
                <td className={styles.numeric}>
                  {room.floor === null ? (
                    <span className={styles.absent}>{UNAVAILABLE}</span>
                  ) : (
                    room.floor
                  )}
                </td>
                <td>
                  <Badge tone={status.tone} withDot>
                    {status.label}
                  </Badge>
                </td>
                <td>
                  <Badge tone={active.tone}>{active.label}</Badge>
                </td>
                <td className={styles.notes}>
                  {room.notes === null || room.notes === '' ? (
                    <span className={styles.absent}>{UNAVAILABLE}</span>
                  ) : (
                    room.notes
                  )}
                </td>
                <td className={styles.timestamp}>{formatDateTime(room.updated_at, timeZone)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
