import { useMemo, useState, type ReactNode } from 'react'
import { AlertTriangle, Building2, MessageSquareOff, Plus, WifiOff } from 'lucide-react'

import { PageContainer } from '@/components/ui/PageContainer'
import { Button } from '@/components/ui/Button'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { Skeleton } from '@/components/ui/Skeleton'
import { Pagination } from '@/features/bookings/Pagination'
import {
  DEFAULT_PERIOD,
  describeRange,
  rangeFor,
  todayInZone,
  type PeriodId,
} from '@/features/dashboard/period'
import { PeriodSelector } from '@/features/dashboard/PeriodSelector'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { ReviewCard } from '@/features/reviews/ReviewCard'
import { ReviewFilters } from '@/features/reviews/ReviewFilters'
import { ReviewForm } from '@/features/reviews/ReviewForm'
import { ReviewSummary } from '@/features/reviews/ReviewSummary'
import { hasActiveReviewFilters, useReviews } from '@/features/reviews/useReviews'
import { formatCount } from '@/lib/format'
import { ApiError } from '@/services/api/ApiError'
import { describeFailure } from '@/services/api/failures'
import { useHotelContext } from '@/session/HotelProvider'

import styles from './ReviewsPage.module.css'

/** Every option is inside the router's own `ge=1, le=100`, so none can produce a 422. */
const PAGE_SIZE_OPTIONS = [20, 50, 100] as const

/** What a failure means on each request this page makes. Never the backend's own words. */
const LIST_FAILURE_COPY = {
  notFound: {
    title: 'Hotel not available',
    detail:
      'This property could not be found, or your access to it has been removed. Try selecting another hotel.',
    canRetry: false,
  },
  serverFault: {
    title: 'Reviews are temporarily unavailable',
    detail: 'The reviews could not be loaded. Nothing has been changed by this.',
    canRetry: true,
  },
} as const

const SUMMARY_FAILURE_COPY = {
  notFound: {
    title: 'Statistics unavailable',
    detail: 'The property asked for could not be found.',
    canRetry: false,
  },
  serverFault: {
    title: 'Review statistics are temporarily unavailable',
    detail:
      'The server could not summarise this period. The reviews below are unaffected — but they are one page of many, and nothing here totals or averages them.',
    canRetry: true,
  },
} as const

const MODERATION_FAILURE_COPY = {
  forbidden: {
    title: 'Nothing was changed',
    detail:
      'Publishing or hiding a review needs the staff role on this property, which this account does not have. Reading reviews does not — so the list above is complete, and nothing was written.',
    canRetry: false,
  },
  notFound: {
    title: 'Nothing was changed',
    detail:
      'That stay or its review could not be found, so nothing was updated. It may have been removed since this page was loaded.',
    canRetry: false,
  },
  conflict: {
    title: 'Nothing was changed',
    detail: 'The review changed since this page was loaded. Reload and try again.',
    canRetry: true,
  },
  serverFault: {
    title: 'Nothing was changed',
    detail: 'The review could not be updated. Its published state is unchanged.',
    canRetry: true,
  },
} as const

/**
 * The same statuses, said for a recording rather than a change.
 *
 * A 404 means different things on the two writes -- "that review is gone" when moderating,
 * "that stay is not one of ours" when recording -- and a 409 is only reachable on a
 * recording, where it is the one-review-per-stay rule rather than a lost update. Sharing one
 * set of copy described a failed recording as "nothing was updated", which was found in a
 * browser by recording against an identifier belonging to nothing.
 */
const CREATION_FAILURE_COPY = {
  forbidden: {
    title: 'Nothing was recorded',
    detail:
      'Recording a review needs the staff role on this property, which this account does not have. Reading reviews does not — so the list above is complete, and nothing was written.',
    canRetry: false,
  },
  notFound: {
    title: 'Nothing was recorded',
    detail:
      'No stay with that identifier belongs to this property, so there was nothing to attach the review to. Check the identifier on the booking’s own page.',
    canRetry: false,
  },
  conflict: {
    title: 'Nothing was recorded',
    detail:
      'That stay already has a review. A stay can have exactly one, and the existing review is in the list — hide it if it should not be shown.',
    canRetry: false,
  },
  serverFault: {
    title: 'Nothing was recorded',
    detail: 'The review could not be recorded. Nothing was written, so it can be tried again.',
    canRetry: true,
  },
} as const

/**
 * Reviews &mdash; what guests said about one property.
 *
 * ## What the backend gives, and what this page therefore is
 *
 * `GET /hotels/{h}/reviews` takes `source`, `is_published`, `page` and `page_size`. That is
 * the entire query surface: no search, no rating filter, no date range, no sort. Verified
 * against the live OpenAPI document, and worth verifying, because **the backend ignores
 * unrecognised query parameters silently** -- `?rating=5` returns all 55 rows with a 200,
 * which looks exactly like a filter that matched everything.
 *
 * The one mutation is `PATCH .../bookings/{b}/review`, and it reaches two columns:
 * `is_published` and `responded_at`. There is no DELETE anywhere -- 405 on every review URL
 * -- and no approve, reject or flag, because the schema has one boolean and one timestamp.
 *
 * ## No statistic on this page was computed in the browser
 *
 * The counts, the average and the distribution come from `analytics/reviews`, where
 * PostgreSQL computes them. The list's own `total` is the server's count for the current
 * filter. Nothing here averages ratings -- which would be meaningless anyway, since a
 * five-point and a ten-point score share the list and cannot be averaged without the
 * normalization the database already applies.
 *
 * ## The period governs the statistics, not the list
 *
 * The two are honestly separate because the API makes them separate: the aggregate demands a
 * date range and the list does not accept one. So the period control is labelled as belonging
 * to the summary, and the list says it is showing every review the filters match, of any
 * date. Pretending one range covered both would put a figure and a list side by side that
 * describe different things.
 */
export function ReviewsPage() {
  const hotelContext = useHotelContext()
  const hotel = hotelContext.selected

  const [period, setPeriod] = useState<PeriodId>(DEFAULT_PERIOD)
  const timeZone = hotel?.timezone ?? 'UTC'
  // Memoised so a render does not produce a new range object and refetch from the hook.
  const range = useMemo(() => rangeFor(period, timeZone), [period, timeZone])
  const today = useMemo(() => todayInZone(timeZone), [timeZone])
  const [recording, setRecording] = useState(false)

  const reviews = useReviews({ hotelPublicId: hotel?.public_id ?? null, range })

  if (hotelContext.status === 'empty') {
    return (
      <Frame>
        <StateMessage
          icon={Building2}
          tone="status"
          title="No hotel is linked to your account"
          detail="Reviews are held per property, and this account is not yet a member of one. An owner or manager can add you to a hotel from its member list."
        />
      </Frame>
    )
  }

  if (hotelContext.status === 'error') {
    return (
      <Frame>
        <StateMessage
          icon={WifiOff}
          tone="alert"
          title="Could not load your hotels"
          detail="The list of properties you have access to could not be retrieved. Check your connection and try again."
          onRetry={hotelContext.retry}
        />
      </Frame>
    )
  }

  const listFailure =
    reviews.error === null ? null : describeFailure(reviews.error, LIST_FAILURE_COPY)
  const summaryFailure =
    reviews.analyticsError === null
      ? null
      : describeFailure(reviews.analyticsError, SUMMARY_FAILURE_COPY)
  const moderationFailure =
    reviews.moderationError === null
      ? null
      : describeFailure(
          reviews.moderationError,
          reviews.failedWrite === 'creation' ? CREATION_FAILURE_COPY : MODERATION_FAILURE_COPY,
        )

  return (
    <Frame>
      {/* --- the server's statistics ------------------------------------------------------ */}

      <section className={styles.block} aria-labelledby="review-summary">
        <div className={styles.blockHeader}>
          <div>
            <h2 className={styles.blockTitle} id="review-summary">
              Summary for this period
            </h2>
            <p className={styles.blockNote}>
              Computed by the server over {describeRange(range)}, in{' '}
              {hotel?.name ?? 'this property'}&rsquo;s own calendar ({timeZone}). Averages use
              the normalized rating, which is the only figure comparable across channels that
              score out of five and out of ten. The list below is not limited to this period.
            </p>
          </div>
          <PeriodSelector value={period} onChange={setPeriod} />
        </div>

        {summaryFailure ? (
          <div className={styles.failure} role="alert">
            <AlertTriangle size={16} aria-hidden="true" />
            <div>
              <p className={styles.failureTitle}>{summaryFailure.title}</p>
              <p className={styles.failureDetail}>{summaryFailure.detail}</p>
            </div>
          </div>
        ) : reviews.analytics === null ? (
          <div className={styles.summarySkeleton} role="status" aria-busy="true">
            <Skeleton height="5rem" label="Loading review statistics" />
          </div>
        ) : (
          <ReviewSummary analytics={reviews.analytics} />
        )}
      </section>

      {/* --- the reviews ------------------------------------------------------------------ */}

      <section className={styles.block} aria-labelledby="review-list">
        <div className={styles.blockHeader}>
          <div>
            <h2 className={styles.blockTitle} id="review-list">
              Reviews
            </h2>
            <p className={styles.blockNote}>
              Every review recorded against this property, newest first, of any date. A review
              is published or hidden; the guest&rsquo;s rating and words are not editable here.
            </p>
          </div>
          {recording ? null : (
            <Button
              variant="primary"
              size="sm"
              onClick={() => {
                reviews.dismiss()
                setRecording(true)
              }}
            >
              <Plus size={16} aria-hidden="true" />
              Record a review
            </Button>
          )}
        </div>

        {reviews.moderated ? (
          <p className={styles.success} role="status">
            {reviews.moderated.message}{' '}
            {reviews.moderated.field === 'response'
              ? reviews.moderated.review.responded_at === null
                ? 'The server recorded it as not answered.'
                : 'The server recorded the time it was answered.'
              : `The server recorded it as ${reviews.moderated.review.is_published ? 'published' : 'hidden'}.`}
          </p>
        ) : null}

        {moderationFailure ? (
          <div className={styles.failure} role="alert">
            <AlertTriangle size={16} aria-hidden="true" />
            <div>
              <p className={styles.failureTitle}>{moderationFailure.title}</p>
              <p className={styles.failureDetail}>{moderationFailure.detail}</p>
            </div>
          </div>
        ) : null}

        {recording ? (
          <div className={styles.formPanel}>
            <ReviewForm
              today={today}
              busy={reviews.pending !== null}
              onDirty={reviews.dismiss}
              onCancel={() => {
                setRecording(false)
              }}
              onSubmit={(bookingPublicId, payload) => {
                void reviews.create(bookingPublicId, payload).then((accepted) => {
                  if (accepted) {
                    setRecording(false)
                  }
                })
              }}
            />
          </div>
        ) : null}

        <ReviewFilters
          value={reviews.filters}
          onChange={(next) => {
            reviews.dismiss()
            reviews.setFilters(next)
          }}
          disabled={reviews.status === 'loading'}
        />

        {listFailure ? (
          <StateMessage
            icon={reviews.error?.code === ApiError.NETWORK_CODE ? WifiOff : AlertTriangle}
            tone="alert"
            title={listFailure.title}
            detail={listFailure.detail}
            {...(listFailure.canRetry ? { onRetry: reviews.reload } : {})}
          />
        ) : reviews.status === 'loading' || reviews.status === 'idle' ? (
          <div className={styles.listSkeleton} role="status" aria-busy="true">
            <Skeleton height="9rem" label="Loading reviews" />
            <Skeleton height="9rem" />
          </div>
        ) : reviews.reviews.length === 0 ? (
          <StateMessage
            icon={MessageSquareOff}
            tone="status"
            title={
              hasActiveReviewFilters(reviews.filters)
                ? 'No reviews match those filters'
                : 'No reviews yet'
            }
            detail={
              hasActiveReviewFilters(reviews.filters)
                ? 'The server searched every review for this property, not just this page. Clearing the filters will show them all.'
                : 'This property has no reviews recorded. Reviews arrive with a stay or are imported from a booking channel.'
            }
          />
        ) : (
          <>
            <p className={styles.count} role="status">
              {formatCount(reviews.total)} {reviews.total === 1 ? 'review' : 'reviews'}
              {hasActiveReviewFilters(reviews.filters) ? ' matching the filters' : ''}. Counted
              by the server across every page.
            </p>

            {/*
              * A list, so the count is announced and each review is one item. The cards are
              * laid out by CSS grid; the semantics stay a list at every width.
              */}
            <ul className={styles.list}>
              {reviews.reviews.map((review, index) => (
                <li
                  /*
                   * The index within the page. A review has no identifier -- `reviews` has no
                   * `public_id` -- and two byte-identical reviews are a legitimate state, so a
                   * key built from the contents would collide. The list is fully replaced on
                   * every fetch and never reordered locally, which is the case an index key is
                   * safe in.
                   */
                  key={index}
                  className={styles.item}
                >
                  <ReviewCard
                    review={review}
                    timeZone={timeZone}
                    busy={reviews.pending !== null}
                    onModerate={(bookingPublicId, payload, message) => {
                      void reviews.moderate(bookingPublicId, payload, message)
                    }}
                  />
                </li>
              ))}
            </ul>

            <Pagination
              page={reviews.page}
              pageSize={reviews.pageSize}
              total={reviews.total}
              pages={reviews.pages}
              onPageChange={reviews.setPage}
              onPageSizeChange={reviews.setPageSize}
              pageSizeOptions={PAGE_SIZE_OPTIONS}
              itemNoun={{ singular: 'review', plural: 'reviews' }}
            />
          </>
        )}
      </section>
    </Frame>
  )
}

/** The page heading, shared by every state so the frame never jumps. */
function Frame({ children }: { children: ReactNode }) {
  return (
    <PageContainer>
      <SectionHeader
        as="h1"
        title="Reviews"
        description="What guests said about this property, across every channel it collects reviews from. A review can be published or hidden; its rating and words belong to the guest and are not editable here."
      />
      <div className={styles.page}>{children}</div>
    </PageContainer>
  )
}
