import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import {
  DEFAULT_PAGE_SIZE,
  reviewService,
  type ReviewAnalyticsRange,
} from '@/services/reviews/reviewService'
import type {
  Review,
  ReviewAnalyticsResponse,
  ReviewCreateRequest,
  ReviewModerationRequest,
  ReviewSource,
} from '@/types/review'

/**
 * A hotel's reviews: a page of them, the server's statistics over them, and the one
 * mutation the API offers.
 *
 * ## No review statistic is computed here
 *
 * This hook holds rows the server sent and an aggregate the server computed. It contains no
 * `reduce`, no averaging, no counting of ratings and no bucketing. It does not:
 *
 * * average the ratings on a page -- the page is one of eleven, so that figure would be a
 *   partial mean wearing the name of a whole one, and averaging a five-point score with a
 *   ten-point one produces a number that means nothing at all;
 * * count how many reviews are published -- `published_count` is in the aggregate, and the
 *   list's own `total` is the server's count for the current filter;
 * * derive a rating distribution -- `analytics/reviews` returns five buckets with their
 *   bounds, and re-deriving them in a browser would mean re-implementing a boundary rule
 *   ("upper bound exclusive except in bucket 5") that lives in SQL.
 *
 * The only arithmetic in this file is on `page` numbers.
 *
 * ## Two requests per view, and nothing per row
 *
 * A filter change issues one request; the range drives a second for the aggregate. A review
 * row arrives complete -- rating, source, body, and the public ids of its booking and guest
 * -- so **nothing is fetched per review**. There is no guest lookup and no booking lookup;
 * doing either per row would be the N+1 the brief rules out, and the backend already avoids
 * it internally by resolving both parents in two queries rather than two per row.
 *
 * ## Moderation is not optimistic, and is never retried
 *
 * A successful PATCH re-reads the list and the aggregate rather than editing the local row:
 * hiding a review changes `published_count` and can change which page it belongs to under a
 * publication filter, and both are the server's to decide. A **failed** PATCH changes
 * nothing on screen, and nothing is retried automatically.
 *
 * `inFlight` is a ref rather than state because two clicks in the same tick both read the
 * same stale `false` from a state variable, and both fire.
 */

export type ReviewsStatus = 'idle' | 'loading' | 'ready' | 'error'

/** What narrows the list. Both map to real query parameters; there are no others. */
export interface ReviewFilters {
  /** A channel, or `''` for every channel. */
  readonly source: ReviewSource | ''
  /** `'published'`, `'hidden'`, or `''` for both. */
  readonly publication: 'published' | 'hidden' | ''
}

export const NO_REVIEW_FILTERS: ReviewFilters = { source: '', publication: '' }

export function hasActiveReviewFilters(filters: ReviewFilters): boolean {
  return filters.source !== '' || filters.publication !== ''
}

/** The confirmation shown after the server accepted a moderation change. */
export interface ModerationOutcome {
  /** Copy written here, never the backend's message. */
  readonly message: string
  /** The row the server returned, so the confirmation restates what actually changed. */
  readonly review: Review
  /**
   * Which column the change was about.
   *
   * The confirmation reads back the field that moved, and only that field. Without this it
   * read back `is_published` after every change -- so clearing an answered mark announced
   * "The server recorded it as hidden", which is true of the review and irrelevant to what
   * was just done. Found in the browser, on a real PATCH.
   */
  readonly field: 'publication' | 'response' | 'creation'
}

export interface ReviewsState {
  readonly status: ReviewsStatus
  readonly reviews: readonly Review[]
  /** The server's row count for the current filter. Never derived from the rows held. */
  readonly total: number
  readonly pages: number
  readonly page: number
  readonly pageSize: number
  readonly error: ApiError | null

  /** The server's statistics for the range. `null` until loaded, or when it failed. */
  readonly analytics: ReviewAnalyticsResponse | null
  /** Kept apart from `error`: the list can load when the aggregate does not. */
  readonly analyticsError: ApiError | null

  readonly filters: ReviewFilters
  /** The booking whose review is being moderated, or null. */
  readonly pending: string | null
  readonly moderated: ModerationOutcome | null
  /** The last refusal. Rendered through `describeFailure`, never raw. */
  readonly moderationError: ApiError | null
  /**
   * Which write the refusal came from.
   *
   * The two share a banner, and their 404s mean different things: a moderation 404 is "that
   * review is gone", a creation 404 is "that stay is not one of ours". Without this the page
   * described a failed *recording* as "nothing was updated" -- found in a browser, by
   * recording a review against an identifier that belongs to nothing.
   */
  readonly failedWrite: 'creation' | 'moderation' | null

  readonly setPage: (page: number) => void
  readonly setPageSize: (pageSize: number) => void
  readonly setFilters: (filters: ReviewFilters) => void
  readonly reload: () => void
  readonly moderate: (
    bookingPublicId: string,
    payload: ReviewModerationRequest,
    message: string,
  ) => Promise<boolean>
  /**
   * Record the review of a stay.
   *
   * Shares `pending` and the in-flight ref with moderation, so a create and a hide cannot be
   * in the air at once -- both change what the list and the aggregate say, and two
   * overlapping writes would race the re-read that follows each of them.
   */
  readonly create: (
    bookingPublicId: string,
    payload: ReviewCreateRequest,
  ) => Promise<boolean>
  readonly dismiss: () => void
}

export interface UseReviewsOptions {
  readonly hotelPublicId: string | null
  /** The reporting period for the aggregate, resolved in the hotel's own zone. */
  readonly range: ReviewAnalyticsRange
}

export function useReviews({ hotelPublicId, range }: UseReviewsOptions): ReviewsState {
  const [status, setStatus] = useState<ReviewsStatus>('idle')
  const [reviews, setReviews] = useState<readonly Review[]>([])
  const [total, setTotal] = useState(0)
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSizeState] = useState(DEFAULT_PAGE_SIZE)
  const [error, setError] = useState<ApiError | null>(null)

  const [analytics, setAnalytics] = useState<ReviewAnalyticsResponse | null>(null)
  const [analyticsError, setAnalyticsError] = useState<ApiError | null>(null)

  const [filters, setFiltersState] = useState<ReviewFilters>(NO_REVIEW_FILTERS)
  const [attempt, setAttempt] = useState(0)

  const [pending, setPending] = useState<string | null>(null)
  const [moderated, setModerated] = useState<ModerationOutcome | null>(null)
  const [moderationError, setModerationError] = useState<ApiError | null>(null)
  const [failedWrite, setFailedWrite] = useState<'creation' | 'moderation' | null>(null)

  const inFlight = useRef(false)

  const { dateFrom, dateTo } = range
  const { source, publication } = filters

  /* --- the list ------------------------------------------------------------------------ */

  useEffect(() => {
    if (hotelPublicId === null) {
      setStatus('idle')
      setReviews([])
      setTotal(0)
      setPages(0)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setStatus('loading')
    setError(null)

    reviewService
      .list(
        hotelPublicId,
        {
          page,
          pageSize,
          ...(source !== '' ? { source } : {}),
          ...(publication !== '' ? { isPublished: publication === 'published' } : {}),
        },
        controller.signal,
      )
      .then((result) => {
        if (cancelled) {
          return
        }
        /*
         * A 200 is not proof of the documented shape. The client validates nothing at
         * runtime, so a proxy or a moved route can answer 200 with something else -- and
         * `reviews.map` on that would take the page down from a render, past this `catch`.
         * An unreadable answer is a failure, never a hotel with no reviews. This exact class
         * of bug took the dashboard down in Stage 5.4.
         */
        if (!Array.isArray((result as { items?: unknown }).items)) {
          setReviews([])
          setError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The reviews response was not in the expected format.',
            ),
          )
          setStatus('error')
          return
        }
        setReviews(result.items)
        setTotal(result.total)
        setPages(result.pages)
        setStatus('ready')
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setReviews([])
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The reviews could not be loaded.'),
        )
        setStatus('error')
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [hotelPublicId, source, publication, page, pageSize, attempt])

  /* --- the server's statistics ---------------------------------------------------------- */

  useEffect(() => {
    if (hotelPublicId === null) {
      setAnalytics(null)
      return
    }

    const controller = new AbortController()
    let cancelled = false
    setAnalyticsError(null)

    reviewService
      .getAnalytics(hotelPublicId, { dateFrom, dateTo }, controller.signal)
      .then((result) => {
        if (cancelled) {
          return
        }
        if (
          !Array.isArray((result as { rating_distribution?: unknown }).rating_distribution) ||
          !Array.isArray((result as { by_source?: unknown }).by_source) ||
          typeof (result as { totals?: unknown }).totals !== 'object'
        ) {
          setAnalytics(null)
          setAnalyticsError(
            new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The review statistics were not in the expected format.',
            ),
          )
          return
        }
        setAnalytics(result)
      })
      .catch((cause: unknown) => {
        if (cancelled || controller.signal.aborted) {
          return
        }
        setAnalytics(null)
        setAnalyticsError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The review statistics could not be loaded.'),
        )
      })

    return () => {
      cancelled = true
      controller.abort()
    }
    // Deliberately NOT keyed on the filters: the analytics endpoint takes a date range and
    // nothing else -- no source parameter, no publication parameter. Refetching on a filter
    // change would issue an identical request and imply the figures had followed the filter.
  }, [hotelPublicId, dateFrom, dateTo, attempt])

  const reload = useCallback(() => {
    setAttempt((n) => n + 1)
  }, [])

  const setFilters = useCallback((next: ReviewFilters) => {
    setFiltersState(next)
    // A narrower list has fewer pages; staying on page 8 of a two-page result shows an empty
    // screen that reads as "no reviews" rather than "no such page".
    setPage(1)
  }, [])

  const setPageSize = useCallback((next: number) => {
    setPageSizeState(next)
    setPage(1)
  }, [])

  const moderate = useCallback(
    async (
      bookingPublicId: string,
      payload: ReviewModerationRequest,
      message: string,
    ): Promise<boolean> => {
      // Which field this call is about, read off the payload rather than passed separately:
      // the two are then incapable of disagreeing.
      const field: ModerationOutcome['field'] =
        payload.is_published === undefined ? 'response' : 'publication'
      if (inFlight.current || hotelPublicId === null) {
        return false
      }
      inFlight.current = true
      setPending(bookingPublicId)
      setModerationError(null)
      setFailedWrite(null)
      setModerated(null)

      try {
        const review = await reviewService.moderate(hotelPublicId, bookingPublicId, payload)
        if (!isReview(review)) {
          throw new ApiError(
            200,
            ApiError.MALFORMED_CODE,
            'The moderation response was not in the expected format.',
          )
        }
        setModerated({ message, review, field })
        // Re-read rather than patch the local row: see the module docstring.
        setAttempt((n) => n + 1)
        return true
      } catch (cause: unknown) {
        setFailedWrite('moderation')
        setModerationError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The review could not be updated.'),
        )
        return false
      } finally {
        inFlight.current = false
        setPending(null)
      }
    },
    [hotelPublicId],
  )

  const create = useCallback(
    async (bookingPublicId: string, payload: ReviewCreateRequest): Promise<boolean> => {
      if (inFlight.current || hotelPublicId === null) {
        return false
      }
      inFlight.current = true
      setPending(bookingPublicId)
      setModerationError(null)
      setFailedWrite(null)
      setModerated(null)

      try {
        const review = await reviewService.create(hotelPublicId, bookingPublicId, payload)
        if (!isReview(review)) {
          throw new ApiError(
            201,
            ApiError.MALFORMED_CODE,
            'The response to the new review was not in the expected format.',
          )
        }
        setModerated({ message: 'Review recorded.', review, field: 'creation' })
        // Re-read rather than prepend: the list is server-ordered by review date, and where
        // a new review belongs in it is the server's to decide -- as is `review_count`.
        setAttempt((n) => n + 1)
        return true
      } catch (cause: unknown) {
        setFailedWrite('creation')
        setModerationError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, ApiError.NETWORK_CODE, 'The review could not be recorded.'),
        )
        return false
      } finally {
        inFlight.current = false
        setPending(null)
      }
    },
    [hotelPublicId],
  )

  const dismiss = useCallback(() => {
    setModerated(null)
    setModerationError(null)
    setFailedWrite(null)
  }, [])

  return {
    status,
    reviews,
    total,
    pages,
    page,
    pageSize,
    error,
    analytics,
    analyticsError,
    filters,
    pending,
    moderated,
    moderationError,
    failedWrite,
    setPage,
    setPageSize,
    setFilters,
    reload,
    moderate,
    create,
    dismiss,
  }
}

/** Whether a 200 body is actually the documented review shape. */
function isReview(value: unknown): value is Review {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const candidate = value as { rating?: unknown; source?: unknown; is_published?: unknown }
  return (
    typeof candidate.rating === 'string' &&
    typeof candidate.source === 'string' &&
    typeof candidate.is_published === 'boolean'
  )
}
