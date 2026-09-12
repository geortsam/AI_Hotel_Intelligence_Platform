import { formatRating } from '@/features/dashboard/format'
import { sourceLabel } from '@/features/reviews/vocabulary'
import { formatCount, formatDate, UNAVAILABLE } from '@/lib/format'
import type { ReviewAnalyticsResponse } from '@/types/review'

import styles from './ReviewSummary.module.css'

export interface ReviewSummaryProps {
  readonly analytics: ReviewAnalyticsResponse
}

/**
 * The hotel's review statistics for the period, as PostgreSQL computed them.
 *
 * ## Every figure here came off the wire
 *
 * `review_count`, `published_count`, `average_rating_normalized`, the five distribution
 * buckets with their own bounds, and the per-source breakdown are all fields of
 * `GET /hotels/{h}/analytics/reviews`. This component prints them. It counts nothing,
 * averages nothing and buckets nothing -- which matters more here than on most screens,
 * because two rating scales are in play and an average taken over a page of mixed five- and
 * ten-point scores is not a rating, it is a category error.
 *
 * The one thing computed anywhere near this file is a **bar width**, and that is an axis,
 * not a statistic: each bar is drawn relative to the largest bucket, exactly as a chart's
 * y-axis is scaled to its tallest column. The count itself is always printed as the number
 * the server sent, so the figure never depends on the drawing.
 *
 * ## The average is shown on the five-point scale, and says so
 *
 * `average_rating_normalized` is a fraction in [0, 1] -- `AVG(rating / rating_scale)` -- and
 * a fraction is not how anyone reads a hotel rating. `formatRating` renders it as "3.9 / 5",
 * the same transform the dashboard has used since Stage 5.4, with the scale in the string so
 * it cannot be mistaken for a ten-point score. The value itself is the server's; only its
 * presentation is chosen here.
 *
 * ## Why the filters below do not change these numbers
 *
 * The analytics endpoint takes a date range and nothing else -- no source parameter, no
 * publication parameter. So a channel filter on the list cannot narrow this panel, and the
 * note says so rather than letting the two sections look connected in a way they are not.
 */
export function ReviewSummary({ analytics }: ReviewSummaryProps) {
  const { totals, rating_distribution: distribution, by_source: bySource } = analytics
  // The tallest bar, used only as the axis maximum. Never displayed, never compared.
  const axisMax = Math.max(1, ...distribution.map((bucket) => bucket.count))

  return (
    <div className={styles.summary}>
      <dl className={styles.figures}>
        <div className={styles.figure}>
          <dt className={styles.figureLabel}>Reviews</dt>
          <dd className={styles.figureValue}>{formatCount(totals.review_count)}</dd>
        </div>
        <div className={styles.figure}>
          <dt className={styles.figureLabel}>Published</dt>
          <dd className={styles.figureValue}>{formatCount(totals.published_count)}</dd>
        </div>
        <div className={styles.figure}>
          <dt className={styles.figureLabel}>Average rating</dt>
          <dd className={styles.figureValue}>
            {/*
              * Null is "undefined", not zero. A range with no reviews has no average, and
              * printing 0 / 5 there would assert that guests rated the property nothing.
              */}
            {totals.average_rating_normalized === null
              ? UNAVAILABLE
              : formatRating(totals.average_rating_normalized)}
          </dd>
        </div>
      </dl>

      <div className={styles.panels}>
        <section className={styles.panel} aria-labelledby="review-distribution">
          <h3 className={styles.panelTitle} id="review-distribution">
            Rating distribution
          </h3>
          <p className={styles.panelNote}>
            Five equal bands of the normalized rating (rating divided by its scale), so a
            five-point and a ten-point score fall in comparable bands. Bucketed by the server.
          </p>
          <table className={styles.table}>
            <caption className={styles.caption}>
              Reviews per band of the normalized rating, for {formatDate(analytics.range.date_from)}{' '}
              to {formatDate(analytics.range.date_to)}.
            </caption>
            <thead>
              <tr>
                <th scope="col">Band</th>
                <th scope="col">Range</th>
                <th scope="col" className={styles.numeric}>
                  Reviews
                </th>
              </tr>
            </thead>
            <tbody>
              {distribution.map((bucket) => (
                <tr key={bucket.bucket}>
                  <th scope="row" className={styles.rowHeader}>
                    Band {bucket.bucket}
                  </th>
                  <td className={styles.range}>
                    {bucket.lower_bound} – {bucket.upper_bound}
                  </td>
                  <td className={styles.numeric}>
                    <span className={styles.barCell}>
                      <span
                        className={styles.bar}
                        style={{ width: `${(bucket.count / axisMax) * 100}%` }}
                        aria-hidden="true"
                      />
                      <span className={styles.barCount}>{formatCount(bucket.count)}</span>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className={styles.panel} aria-labelledby="review-sources">
          <h3 className={styles.panelTitle} id="review-sources">
            By channel
          </h3>
          <p className={styles.panelNote}>
            Each channel&rsquo;s own count and average, grouped by the server. Channels with no
            reviews in this period are absent because the server did not report them.
          </p>
          {bySource.length === 0 ? (
            <p className={styles.empty} role="status">
              No channel reported reviews in this period.
            </p>
          ) : (
            <table className={styles.table}>
              <caption className={styles.caption}>
                Reviews per channel for {formatDate(analytics.range.date_from)} to{' '}
                {formatDate(analytics.range.date_to)}.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Channel</th>
                  <th scope="col" className={styles.numeric}>
                    Reviews
                  </th>
                  <th scope="col" className={styles.numeric}>
                    Published
                  </th>
                  <th scope="col" className={styles.numeric}>
                    Average
                  </th>
                </tr>
              </thead>
              <tbody>
                {bySource.map((entry) => (
                  <tr key={entry.source}>
                    <th scope="row" className={styles.rowHeader}>
                      {sourceLabel(entry.source)}
                    </th>
                    <td className={styles.numeric}>{formatCount(entry.review_count)}</td>
                    <td className={styles.numeric}>{formatCount(entry.published_count)}</td>
                    <td className={styles.numeric}>
                      {entry.average_rating_normalized === null
                        ? UNAVAILABLE
                        : formatRating(entry.average_rating_normalized)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </div>
  )
}
