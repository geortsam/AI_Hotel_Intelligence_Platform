import { Badge } from '@/components/ui/Badge'
import { formatCount, formatDate, formatMoney } from '@/lib/format'
import type { AvailabilityResult } from '@/types/availability'

import styles from './AvailabilityResults.module.css'

export interface AvailabilityResultsProps {
  readonly result: AvailabilityResult
}

/**
 * The server's answer to one availability search.
 *
 * ## The verdict is `sufficient`, and nothing here second-guesses it
 *
 * The headline reads `result.sufficient` and only that. It is **not** derived from whether
 * `room_types` is empty, and the difference is not hypothetical: a mixed request for two
 * Deluxe and ninety-nine Standard came back with both types listed, sixteen rooms free, and
 * `sufficient: false`. A screen that inferred the verdict from the list would have announced
 * the opposite of the truth while displaying the very data that disproved it.
 *
 * Every number below is likewise the server's: `available_rooms`, each type's
 * `available_count` and `requested_count`, and `nights`. Nothing is counted, summed or
 * compared here -- `rooms.length` is never read, because `available_count` is the field that
 * answers that question and re-deriving it would be a second answer.
 *
 * ## What "insufficient" is, and what it is not
 *
 * It is a successful answer meaning the hotel cannot meet this request on these dates. It is
 * not an error, not a failed request, and **not** "this hotel has no rooms" -- the copy says
 * which, because an operator who reads the second will go and look for a bug.
 *
 * ## A result is not a reservation
 *
 * Stated on screen because the schema states it: nothing is held, and a room named here can
 * be taken by another request a moment later. Only the booking transaction and the database's
 * exclusion constraint decide inventory.
 */
export function AvailabilityResults({ result }: AvailabilityResultsProps) {
  const mixed = result.room_types.some((type) => type.requested_count !== null)

  return (
    <div className={styles.results}>
      {/*
        * The verdict, announced. `role="status"` rather than `alert`: an insufficient answer
        * is an ordinary reply to an ordinary question, and announcing it as an alert would
        * make "we are full that week" sound like a fault.
        */}
      <div
        className={`${styles.verdict} ${result.sufficient ? styles.yes : styles.no}`}
        role="status"
      >
        <p className={styles.verdictHeadline}>
          {result.sufficient
            ? 'This stay can be accommodated.'
            : 'This stay cannot be accommodated.'}
        </p>
        <p className={styles.verdictDetail}>
          {formatDate(result.check_in)} to {formatDate(result.check_out)} &mdash;{' '}
          {formatCount(result.nights)} {result.nights === 1 ? 'night' : 'nights'}, leaving on{' '}
          {formatDate(result.check_out)}.{' '}
          {mixed
            ? 'Each room type below is answered on its own; the request is met only when every one of them can supply its share.'
            : `Asked for ${formatCount(result.rooms_required)} ${result.rooms_required === 1 ? 'room' : 'rooms'} of a single type.`}
        </p>
        <p className={styles.verdictDetail}>
          The server reports {formatCount(result.available_rooms)}{' '}
          {result.available_rooms === 1 ? 'room' : 'rooms'} free across the types listed.
        </p>
      </div>

      {result.room_types.length === 0 ? (
        <p className={styles.empty}>
          No room type at this property can take this stay on these dates. That is an answer
          about these dates and this request &mdash; not a statement that the property has no
          rooms.
        </p>
      ) : (
        <ul className={styles.list}>
          {result.room_types.map((type) => {
            // `requested_count` is present only on a mixed search; the server, not this
            // component, decided whether each share can be met.
            const shortfall =
              type.requested_count !== null && type.available_count < type.requested_count
            return (
              <li key={type.code} className={styles.typeCard}>
                <div className={styles.typeHeader}>
                  <div>
                    <h3 className={styles.typeName}>{type.name}</h3>
                    <p className={styles.typeMeta}>
                      <span className={styles.code}>{type.code}</span>
                      <span aria-hidden="true"> · </span>
                      sleeps {formatCount(type.standard_occupancy)} standard,{' '}
                      {formatCount(type.max_occupancy)} maximum
                      <span aria-hidden="true"> · </span>
                      {formatMoney(type.base_price, type.currency)} base
                    </p>
                  </div>
                  <div className={styles.counts}>
                    <Badge tone={shortfall ? 'danger' : 'success'}>
                      {formatCount(type.available_count)} free
                    </Badge>
                    {type.requested_count !== null ? (
                      <Badge tone="neutral">{formatCount(type.requested_count)} requested</Badge>
                    ) : null}
                  </div>
                </div>

                {shortfall ? (
                  <p className={styles.shortfall}>
                    This type cannot supply what was asked of it, so the whole request is
                    reported as not accommodated.
                  </p>
                ) : null}

                {type.rooms.length === 0 ? (
                  <p className={styles.noRooms}>The server listed no individual rooms here.</p>
                ) : (
                  <>
                    <h4 className={styles.roomsTitle}>Rooms free for the whole stay</h4>
                    <ul className={styles.rooms}>
                      {type.rooms.map((room) => (
                        <li key={room.room_number} className={styles.room}>
                          <span className={styles.roomNumber}>{room.room_number}</span>
                          {room.floor === null ? null : (
                            <span className={styles.roomFloor}>floor {room.floor}</span>
                          )}
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </li>
            )
          })}
        </ul>
      )}

      <p className={styles.caveat}>
        A result is not a reservation. Nothing is held by this search, and a room listed here
        can be taken by another booking a moment later &mdash; the booking itself is what
        settles inventory.
      </p>
    </div>
  )
}
