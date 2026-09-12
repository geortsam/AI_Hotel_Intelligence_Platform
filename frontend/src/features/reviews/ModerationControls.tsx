import { useState } from 'react'

import { Button } from '@/components/ui/Button'
import type { Review, ReviewModerationRequest } from '@/types/review'

import styles from './ModerationControls.module.css'

export interface ModerationControlsProps {
  readonly review: Review
  /** The review's address. Non-null by construction: the card checks before rendering this. */
  readonly bookingPublicId: string
  readonly onModerate: (
    bookingPublicId: string,
    payload: ReviewModerationRequest,
    message: string,
  ) => void
  readonly busy: boolean
}

/**
 * The moderation the API actually offers, and nothing beyond it.
 *
 * ## Two columns, three actions
 *
 * `ReviewModerationUpdate` accepts `is_published` and `responded_at`. That is the whole
 * payload -- sending `rating` or `body` is a 422, verified both ways -- so the controls are:
 *
 * * **Hide** / **Publish**, which is `{is_published: false | true}`;
 * * **Mark answered**, which is `{responded_at: <now>}`;
 * * **Clear response**, which is `{responded_at: null}` -- an *explicit* null, because an
 *   omitted field is left untouched and a cleared one has to be said out loud.
 *
 * What is deliberately absent: **approve**, **reject**, **flag**, **delete**, and any notion
 * of a review having been judged. The schema has one boolean and one timestamp; a workflow
 * with states the database cannot store would be an interface making promises the backend
 * would not keep. There is no DELETE on this route at all -- 405, verified -- and hiding is
 * the withdrawal the schema provides.
 *
 * ## Hiding asks first
 *
 * Taking a review down changes what the property's published rating is computed from, and
 * `published_count` moves with it. It is reversible -- Publish puts it back -- so the
 * confirmation is a single inline question rather than a modal, but it is not a bare click.
 * Publishing again needs no confirmation: making a review visible is the schema's default
 * state, not a withdrawal.
 *
 * ## Nothing is optimistic
 *
 * These controls report intent; the parent hook re-reads the list and the statistics after
 * the server confirms. The badge on the card does not flip until the server has said so.
 */
export function ModerationControls({
  review,
  bookingPublicId,
  onModerate,
  busy,
}: ModerationControlsProps) {
  const [confirmingHide, setConfirmingHide] = useState(false)

  if (confirmingHide) {
    return (
      <div className={styles.bar}>
        <div className={styles.confirm} role="group" aria-label="Confirm hiding this review">
          <p className={styles.confirmQuestion}>
            Hide this review? It stops being published and stops counting towards the
            published total. It can be published again at any time.
          </p>
          <div className={styles.actions}>
            <Button
              autoFocus
              variant="danger"
              size="sm"
              disabled={busy}
              onClick={() => {
                setConfirmingHide(false)
                onModerate(bookingPublicId, { is_published: false }, 'Review hidden.')
              }}
            >
              Yes, hide it
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={busy}
              onClick={() => {
                setConfirmingHide(false)
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className={styles.bar}>
      {review.is_published ? (
        <Button
          variant="secondary"
          size="sm"
          disabled={busy}
          onClick={() => {
            setConfirmingHide(true)
          }}
        >
          Hide review
        </Button>
      ) : (
        <Button
          variant="secondary"
          size="sm"
          disabled={busy}
          onClick={() => {
            onModerate(bookingPublicId, { is_published: true }, 'Review published.')
          }}
        >
          Publish review
        </Button>
      )}

      {review.responded_at === null ? (
        <Button
          variant="secondary"
          size="sm"
          disabled={busy}
          onClick={() => {
            // The instant is generated at the click, not typed: `responded_at` records when
            // the hotel answered, and the only honest value for that is now.
            onModerate(
              bookingPublicId,
              { responded_at: new Date().toISOString() },
              'Review marked answered.',
            )
          }}
        >
          Mark answered
        </Button>
      ) : (
        <Button
          variant="ghost"
          size="sm"
          disabled={busy}
          onClick={() => {
            // An explicit null. Omitting the field would leave the column untouched, which is
            // the opposite of what this button says.
            onModerate(bookingPublicId, { responded_at: null }, 'Response mark cleared.')
          }}
        >
          Clear answered mark
        </Button>
      )}
    </div>
  )
}
