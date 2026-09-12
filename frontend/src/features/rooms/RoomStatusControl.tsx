import { useId } from 'react'

import { Button } from '@/components/ui/Button'
import { ROOM_STATUSES, statusPresentation } from '@/features/rooms/vocabulary'
import type { Room, RoomStatus } from '@/types/room'

import styles from './RoomStatusControl.module.css'

export interface RoomStatusControlProps {
  readonly room: Room
  readonly onChange: (status: RoomStatus) => void
  readonly busy: boolean
}

/**
 * Setting a room's status.
 *
 * ## All five, always, because that is the backend's policy
 *
 * The room service applies **no transition rules**: `update` writes the field. Every ordering
 * was accepted against the live API -- `available` straight to `occupied`, `out_of_order`
 * straight back to `available`, and each of the five in turn. So all five are offered from
 * any state.
 *
 * A tempting alternative was a housekeeping-shaped flow (available → occupied → cleaning →
 * available). That would be a state machine this platform does not have, and it would refuse
 * a legitimate change: a room found flooded goes to `out_of_order` from whatever it was. The
 * brief's rule and the honest reading agree here -- do not duplicate an undocumented state
 * machine, because there is no documented one to duplicate.
 *
 * ## Buttons, not a select
 *
 * Five short options, one of which is current. Radio-style buttons put every option one click
 * away and let the current one be marked as current rather than hidden inside a closed
 * control -- on a screen an operator uses at a corridor pace, that is the difference between
 * one interaction and three. The current status is `aria-current` and its button is disabled,
 * so it is announced and cannot be re-sent.
 */
export function RoomStatusControl({ room, onChange, busy }: RoomStatusControlProps) {
  const labelId = useId()

  return (
    <div className={styles.control}>
      <p className={styles.label} id={labelId}>
        Set status
      </p>
      {/*
        * A group rather than a radiogroup: these are actions that each issue a request, not a
        * pending selection to be submitted later. `aria-labelledby` names the group so the
        * buttons are not announced as five unrelated controls.
        */}
      <div className={styles.options} role="group" aria-labelledby={labelId}>
        {ROOM_STATUSES.map((status) => {
          const presentation = statusPresentation(status)
          const current = room.status === status
          return (
            <Button
              key={status}
              variant={current ? 'primary' : 'secondary'}
              size="sm"
              disabled={busy || current}
              {...(current ? { 'aria-current': 'true' as const } : {})}
              onClick={() => {
                onChange(status)
              }}
            >
              {presentation.label}
              {/*
                * The space is an explicit child, not leading whitespace inside the span: the
                * accessible-name algorithm concatenates element text without inserting any,
                * and a span whose own content began with a space produced
                * "Out of order(current)" -- one run-on word to a screen reader. Found by a
                * test that could not match the name it expected.
                */}
              {current ? (
                <>
                  {' '}
                  <span className={styles.currentMark}>(current)</span>
                </>
              ) : null}
            </Button>
          )
        })}
      </div>
      <p className={styles.note}>
        The room&rsquo;s current operational state. It is not availability: what a room is
        booked for is decided by reservations, and nothing here reads or writes them.
      </p>
    </div>
  )
}
