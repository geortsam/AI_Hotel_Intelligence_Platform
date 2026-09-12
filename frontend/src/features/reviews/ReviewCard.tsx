import { useId } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/Badge'
import { ModerationControls } from '@/features/reviews/ModerationControls'
import {
  describeReviewRating,
  formatReviewRating,
  publicationPresentation,
  SOURCE_TONE,
  sourceLabel,
} from '@/features/reviews/vocabulary'
import { formatDate, formatDateTime, UNAVAILABLE } from '@/lib/format'
import { bookingPath } from '@/router/routes'
import type { Review, ReviewModerationRequest } from '@/types/review'

import styles from './ReviewCard.module.css'

export interface ReviewCardProps {
  readonly review: Review
  readonly timeZone: string
  /** Absent when the page is not offering moderation. */
  readonly onModerate?: (
    bookingPublicId: string,
    payload: ReviewModerationRequest,
    message: string,
  ) => void
  readonly busy?: boolean
}

/**
 * One review.
 *
 * ## The guest's words are text, and only text
 *
 * `body`, `title` and `reviewer_name` are written by members of the public on platforms this
 * application does not control. They are rendered as React text nodes, which escape their
 * content by construction. There is no `dangerouslySetInnerHTML` here, no markdown parser,
 * no link autolinking, and no HTML of any kind: the schema calls `body` a `TEXT` column, not
 * a document format, and treating it as one would turn every review form on Booking.com into
 * an injection vector against this console.
 *
 * A long word -- a pasted URL, an unbroken identifier -- is wrapped by CSS rather than
 * truncated, so nothing a reviewer types can widen the page.
 *
 * ## The rating is shown on the scale it was given on
 *
 * Never converted; see `vocabulary.ts` for why, and why there are no stars. The visible text
 * carries the scale, and the accessible name spells it out as "Rated 4 out of 5".
 *
 * ## Why some reviews have no moderation controls
 *
 * A review is addressed through its booking. `booking_public_id` is nullable -- that is how a
 * review harvested from an external platform is stored when its reviewer cannot be matched to
 * a guest -- and such a review **has no URL**, so there is no request that could publish or
 * hide it. The card says so instead of showing a button that would have nowhere to go.
 */
export function ReviewCard({ review, timeZone, onModerate, busy = false }: ReviewCardProps) {
  const publication = publicationPresentation(review.is_published)
  // A review has no identifier, so the heading's id is generated rather than derived from
  // its contents: two byte-identical reviews are a legitimate state, and an id built from
  // the date and timestamp would collide and point both cards' labels at one heading.
  const headingId = useId()
  const bookingPublicId = review.booking_public_id

  return (
    <article className={styles.card} aria-labelledby={headingId}>
      <header className={styles.header}>
        <div className={styles.identity}>
          {/*
            * The heading is the reviewer's name when there is one. `reviewer_name` is the
            * display name on the review and is not necessarily the guest's own name -- the
            * schema says so -- which is why it is not labelled "Guest".
            */}
          <h3 className={styles.name} id={headingId}>
            {review.reviewer_name ?? 'Anonymous reviewer'}
          </h3>
          <p className={styles.meta}>
            <span>{formatDate(review.review_date)}</span>
            {review.language !== null ? (
              <>
                <span aria-hidden="true"> · </span>
                <span>Written in {review.language.toUpperCase()}</span>
              </>
            ) : null}
          </p>
        </div>

        <div className={styles.marks}>
          {/*
            * The scale is inside the accessible name, not implied by a graphic. Two scales
            * appear in one list, so "4 / 5" and "9 / 10" sit side by side and each says
            * which it is.
            */}
          <p className={styles.rating} aria-label={describeReviewRating(review.rating, review.rating_scale)}>
            <span aria-hidden="true">{formatReviewRating(review.rating, review.rating_scale)}</span>
          </p>
          <div className={styles.badges}>
            <Badge tone={SOURCE_TONE}>{sourceLabel(review.source)}</Badge>
            <Badge tone={publication.tone} withDot>
              {publication.label}
            </Badge>
          </div>
        </div>
      </header>

      {review.title !== null && review.title !== '' ? (
        <p className={styles.title}>{review.title}</p>
      ) : null}

      {review.body !== null && review.body !== '' ? (
        <p className={styles.body}>{review.body}</p>
      ) : (
        <p className={styles.noBody}>
          A rating with no written review. The schema allows it, and it is common.
        </p>
      )}

      <dl className={styles.details}>
        <div className={styles.detail}>
          <dt className={styles.detailLabel}>Stay</dt>
          <dd className={styles.detailValue}>
            {review.booking_public_id === null ? (
              <span className={styles.absent}>Not linked to a stay</span>
            ) : (
              <Link className={styles.link} to={bookingPath(review.booking_public_id)}>
                View booking
              </Link>
            )}
          </dd>
        </div>
        <div className={styles.detail}>
          <dt className={styles.detailLabel}>Guest record</dt>
          <dd className={styles.detailValue}>
            {/*
              * Present or absent, and nothing more. `guest_public_id` is an identifier with
              * no lookup this page is entitled to make: fetching a guest per review is the
              * N+1 the brief rules out, and printing the raw UUID would put an identifier on
              * screen that means nothing to a reader.
              */}
            {review.guest_public_id === null ? (
              <span className={styles.absent}>Not matched to a guest</span>
            ) : (
              'Matched'
            )}
          </dd>
        </div>
        <div className={styles.detail}>
          <dt className={styles.detailLabel}>Answered</dt>
          <dd className={styles.detailValue}>
            {review.responded_at === null ? (
              <span className={styles.absent}>{UNAVAILABLE}</span>
            ) : (
              formatDateTime(review.responded_at, timeZone)
            )}
          </dd>
        </div>
        {review.external_review_id !== null ? (
          <div className={styles.detail}>
            <dt className={styles.detailLabel}>Platform reference</dt>
            <dd className={styles.detailValue}>{review.external_review_id}</dd>
          </div>
        ) : null}
      </dl>

      {onModerate === undefined ? null : bookingPublicId !== null ? (
        <ModerationControls
          review={review}
          bookingPublicId={bookingPublicId}
          onModerate={onModerate}
          busy={busy}
        />
      ) : (
        <p className={styles.unmoderatable}>
          This review is not attached to a stay, so it has no address of its own and cannot be
          published, hidden or marked answered through the API.
        </p>
      )}
    </article>
  )
}
