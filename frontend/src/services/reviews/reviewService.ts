import { api } from '@/services/api/client'
import type { Page } from '@/types/api'
import type {
  Review,
  ReviewAnalyticsResponse,
  ReviewCreateRequest,
  ReviewModerationRequest,
  ReviewSource,
} from '@/types/review'

/**
 * The review calls this application makes, and the complete set the contract offers.
 *
 * ## Two URLs, and the second one is a booking
 *
 * * `GET /hotels/{h}/reviews` -- the hotel's reviews, newest first.
 * * `GET|POST|PATCH /hotels/{h}/bookings/{b}/review` -- the review **of one stay**.
 *
 * That is the whole surface. There is no `/reviews/{id}`, because `reviews` has no
 * `public_id`; `GET /hotels/{h}/reviews/1` is a 404, verified. And there is **no DELETE** on
 * either URL -- 405 on both, verified -- because `is_published` is the column the schema
 * provides for taking a review down. `PATCH {is_published: false}` is the withdrawal, and it
 * is the reason nothing here wraps a delete.
 *
 * `POST`/`PATCH`/`DELETE` against the collection are all 405 as well, so a review cannot be
 * created except through the booking that occasioned it.
 *
 * ## Statistics come from analytics, never from a page of rows
 *
 * `Page<Review>` carries the server's `total` -- a **row count**. Every other figure this
 * application shows about reviews comes from `analytics/reviews`, which aggregates in
 * PostgreSQL. Nothing averages, counts or buckets a rating in the browser.
 *
 * ## Authorization, verified live
 *
 * Reading requires **membership**; `POST` and `PATCH` require **`HotelRole.STAFF`**,
 * declared on the router itself.
 */

/** From `app.services.review`. */
export const DEFAULT_PAGE_SIZE = 20
/** The router's own `le=100`. Asking for 101 is a 422, verified. */
export const MAX_PAGE_SIZE = 100

/**
 * The filters the review list actually supports.
 *
 * Exactly these. There is no search, no rating filter, no date range and no sort, and the
 * backend **ignores an unrecognised query parameter silently** -- `?rating=5` returned all 55
 * rows with a 200 -- so an invented filter would not fail loudly, it would appear to work.
 */
export interface ReviewQuery {
  readonly page: number
  readonly pageSize: number
  /** One channel. An unknown value is a **422**, not an empty page: it is a closed Literal. */
  readonly source?: ReviewSource
  /** `true` for published, `false` for hidden, omitted for both. */
  readonly isPublished?: boolean
}

/** The inclusive range the aggregate covers. Both bounds required; neither has a default. */
export interface ReviewAnalyticsRange {
  readonly dateFrom: string
  readonly dateTo: string
}

export const reviewService = {
  /**
   * One page of the hotel's reviews, **newest first**.
   *
   * The ordering matches `ix_reviews_hotel_id_review_date` and is not configurable.
   *
   * Throws `ApiError`: **404** for an unknown hotel or one the caller cannot reach --
   * distinct from a hotel with no reviews, which is a 200 with an empty page; **422** for a
   * source outside the vocabulary or a `page_size` above 100.
   */
  list(hotelPublicId: string, query: ReviewQuery, signal?: AbortSignal): Promise<Page<Review>> {
    return api.get<Page<Review>>(`/hotels/${hotelPublicId}/reviews`, {
      query: {
        page: query.page,
        page_size: query.pageSize,
        // Omitted rather than sent empty: `source=` is not the same request as no `source`,
        // and `is_published` has three meaningful states, of which one is "don't filter".
        ...(query.source ? { source: query.source } : {}),
        ...(query.isPublished === undefined ? {} : { is_published: query.isPublished }),
      },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * The review of one stay.
   *
   * Throws `ApiError` with **404** in two distinct situations the messages tell apart but
   * the status does not: the booking is unknown to this hotel, or the booking exists and
   * simply has no review. Both verified live. The caller treats them the same, because the
   * status is all the client is entitled to act on.
   */
  getForBooking(
    hotelPublicId: string,
    bookingPublicId: string,
    signal?: AbortSignal,
  ): Promise<Review> {
    return api.get<Review>(`/hotels/${hotelPublicId}/bookings/${bookingPublicId}/review`, {
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Record the review of a stay.
   *
   * Throws `ApiError`: **409** when the stay already has one -- `uq_reviews_booking_id`
   * decides that, not a prior lookup, verified; **403** without the staff role; **404** for
   * an unknown hotel or booking; **422** for a rating above its scale, more than two decimal
   * places, a `rating_scale` other than 5 or 10, a malformed `language`, or any field the
   * schema does not have.
   */
  create(
    hotelPublicId: string,
    bookingPublicId: string,
    payload: ReviewCreateRequest,
    signal?: AbortSignal,
  ): Promise<Review> {
    return api.post<Review>(`/hotels/${hotelPublicId}/bookings/${bookingPublicId}/review`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Publish, hide, or mark a review answered.
   *
   * The only mutation the API offers on an existing review, and it reaches two columns:
   * `is_published` and `responded_at`. Sending `rating`, `body`, `title` or
   * `reviewer_name` is a **422** -- verified for `rating` and `body` -- because the guest's
   * account of their stay is not editable through this API.
   *
   * An omitted field is left untouched; an **explicit null** clears the column, which is how
   * a response is retracted. An empty payload is accepted and changes nothing (200).
   */
  moderate(
    hotelPublicId: string,
    bookingPublicId: string,
    payload: ReviewModerationRequest,
    signal?: AbortSignal,
  ): Promise<Review> {
    return api.patch<Review>(`/hotels/${hotelPublicId}/bookings/${bookingPublicId}/review`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * The hotel's review statistics for a range, as PostgreSQL computed them.
   *
   * **This is where every review figure on the screen comes from**: the count, the published
   * count, the average of `rating_normalized`, the five-bucket distribution and the per-source
   * breakdown. Nothing in this application derives any of them.
   *
   * Throws `ApiError`: 422 for a missing bound or a reversed range; 404 for a hotel the
   * caller cannot reach.
   */
  getAnalytics(
    hotelPublicId: string,
    { dateFrom, dateTo }: ReviewAnalyticsRange,
    signal?: AbortSignal,
  ): Promise<ReviewAnalyticsResponse> {
    return api.get<ReviewAnalyticsResponse>(`/hotels/${hotelPublicId}/analytics/reviews`, {
      query: { date_from: dateFrom, date_to: dateTo },
      ...(signal ? { signal } : {}),
    })
  },
} as const
