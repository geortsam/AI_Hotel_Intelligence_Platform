import { useEffect, useId, useState } from 'react'

import { Button } from '@/components/ui/Button'
import { BookingStatusBadge } from '@/features/bookings/BookingStatusBadge'
import { isDestructive, isTerminal, nextStatuses } from '@/features/bookings/transitions'
import { statusPresentation } from '@/features/bookings/vocabulary'
import type { BookingStatus } from '@/types/booking'

import styles from './StatusActions.module.css'

export interface StatusActionsProps {
  readonly current: BookingStatus
  readonly onChange: (target: BookingStatus, reason?: string) => void
  /** True while any mutation is in flight, so nothing here can be fired twice. */
  readonly busy: boolean
  /** The operation actually running, so the pressed control can say what it is doing. */
  readonly runningTarget: BookingStatus | null
}

/**
 * The booking's status, and the moves the lifecycle allows from it.
 *
 * ## Only reachable states are offered
 *
 * The buttons come from the backend's own transition table (see `transitions.ts`). A
 * `checked_out` booking shows no buttons at all, because there is nowhere for it to go --
 * rather than a row of controls whose only outcome is a 409. The self-transition each state
 * permits is excluded too: "set the status to the one it already has" is not an action.
 *
 * **This is presentation, not enforcement.** The server re-reads the committed status under
 * a row lock and refuses anything it does not permit; if this component and the backend ever
 * disagree, the 409 is rendered and the backend wins.
 *
 * ## Destructive moves confirm inline
 *
 * Cancelling and marking a no-show are terminal: the backend will refuse to move back out of
 * either, so there is no undo. Both therefore ask first.
 *
 * The confirmation is **inline rather than a modal**, and that is a deliberate choice. The
 * design system has no dialog primitive, and a hand-built modal means a focus trap, an
 * Escape contract, an `aria-modal` region and a backdrop -- four things to get subtly wrong
 * for a two-option question. Replacing the row in place needs none of them: focus moves to
 * the confirm button, the question sits where the button the user just pressed was, and
 * "Keep booking" is a real button rather than a dismissal gesture that has to be discovered.
 * `window.confirm` is not used, per the brief and because it cannot be styled, labelled or
 * tested.
 *
 * The cancellation reason is optional, because the API makes it optional. It is offered only
 * for cancellation, since that is the only status the field means anything for.
 */
export function StatusActions({ current, onChange, busy, runningTarget }: StatusActionsProps) {
  const [confirming, setConfirming] = useState<BookingStatus | null>(null)
  const [reason, setReason] = useState('')
  const reasonId = useId()

  const targets = nextStatuses(current)

  // A status change from elsewhere -- the server's answer -- retires any open question.
  useEffect(() => {
    setConfirming(null)
    setReason('')
  }, [current])

  if (isTerminal(current)) {
    return (
      <div className={styles.wrap}>
        <div className={styles.currentRow}>
          <span className={styles.label}>Current status</span>
          <BookingStatusBadge status={current} />
        </div>
        <p className={styles.terminal}>
          This booking is {statusPresentation(current).label.toLowerCase()}. The lifecycle is
          closed, so its status can no longer change.
        </p>
      </div>
    )
  }

  if (confirming !== null) {
    const target = statusPresentation(confirming)
    return (
      <div className={styles.wrap}>
        <div className={styles.currentRow}>
          <span className={styles.label}>Current status</span>
          <BookingStatusBadge status={current} />
        </div>

        {/*
         * `role="group"` with a name, not `alertdialog`: this is not modal, does not trap
         * focus and does not take over the page, and claiming a dialog role for something
         * that behaves like a region would misdescribe it to a screen reader.
         */}
        <div className={styles.confirm} role="group" aria-label={`Confirm ${target.label}`}>
          <p className={styles.confirmQuestion}>
            Mark this booking <strong>{target.label.toLowerCase()}</strong>? This is a terminal
            state &mdash; the booking cannot be moved out of it afterwards.
          </p>

          {confirming === 'cancelled' ? (
            <div className={styles.reasonField}>
              <label className={styles.reasonLabel} htmlFor={reasonId}>
                Reason (optional)
              </label>
              <input
                id={reasonId}
                className={styles.reasonInput}
                type="text"
                maxLength={500}
                value={reason}
                disabled={busy}
                placeholder="e.g. Guest cancelled by telephone"
                onChange={(event) => {
                  setReason(event.target.value)
                }}
              />
            </div>
          ) : null}

          <div className={styles.confirmActions}>
            <Button
              /*
               * Focus moves here as the question appears, so a keyboard user is not left
               * where a button no longer is. `autoFocus` rather than a ref because the
               * design system's `Button` is a plain function component: giving it
               * `forwardRef` to serve one caller would be changing a Stage 5.2 primitive
               * for a need only this file has.
               */
              autoFocus
              variant="danger"
              size="sm"
              disabled={busy}
              onClick={() => {
                onChange(confirming, confirming === 'cancelled' ? reason : undefined)
              }}
            >
              {busy && runningTarget === confirming ? 'Working…' : `Yes, mark ${target.label.toLowerCase()}`}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={busy}
              onClick={() => {
                setConfirming(null)
                setReason('')
              }}
            >
              Keep booking
            </Button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className={styles.wrap}>
      <div className={styles.currentRow}>
        <span className={styles.label}>Current status</span>
        <BookingStatusBadge status={current} />
      </div>

      <div className={styles.actions}>
        {targets.map((target) => {
          const presentation = statusPresentation(target)
          const destructive = isDestructive(target)
          const Icon = presentation.icon
          return (
            <Button
              key={target}
              variant={destructive ? 'secondary' : 'primary'}
              size="sm"
              disabled={busy}
              onClick={() => {
                if (destructive) {
                  setConfirming(target)
                } else {
                  onChange(target)
                }
              }}
            >
              <Icon size={14} aria-hidden="true" />
              {busy && runningTarget === target ? 'Working…' : `Mark ${presentation.label.toLowerCase()}`}
              {/* Terminality is said in words, not signalled by the button's colour alone. */}
              {destructive ? (
                <>
                  {/* An explicit space child: a leading space inside the span is dropped by
                    * JSX, and the accessible name came out as "Mark cancelled(terminal...". */}
                  {' '}
                  <span className={styles.srOnly}>(terminal — asks to confirm)</span>
                </>
              ) : null}
            </Button>
          )
        })}
      </div>
    </div>
  )
}
