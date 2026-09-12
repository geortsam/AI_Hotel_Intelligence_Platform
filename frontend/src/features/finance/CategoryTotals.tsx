import { formatCount, formatDate, formatMoney } from '@/lib/format'
import type { ExpenseBucket, RevenueBucket } from '@/types/finance'
import type { DateRange } from '@/types/analytics'

import styles from './CategoryTotals.module.css'

export type CategoryTotalsProps =
  | {
      readonly kind: 'revenue'
      readonly buckets: readonly RevenueBucket[]
      readonly range: DateRange
    }
  | {
      readonly kind: 'expenses'
      readonly buckets: readonly ExpenseBucket[]
      readonly range: DateRange
    }

/**
 * The server's totals for the period, one row per category and currency.
 *
 * ## Every figure here was computed by PostgreSQL
 *
 * `GET /hotels/{h}/analytics/revenue-by-category` and its expense counterpart group by
 * `(category_code, currency)` and return an amount, a tax amount and an entry count per
 * bucket. This component prints those. It adds nothing, subtracts nothing, and converts
 * nothing.
 *
 * ## Why there is no grand total
 *
 * The endpoint reports no overall figure, and the two ways to produce one are both wrong.
 * Summing across currencies produces a number that is not money -- there is no FX rate
 * anywhere in this system and there is no correct one to invent. Summing within a currency
 * would be this browser stating a total that no part of the platform has computed or
 * checked, on a screen whose entire purpose is that the database is the financial authority.
 *
 * So the buckets are shown as the buckets they are, and the currency is a column rather than
 * a footnote, because a category with lines in EUR and USD genuinely is two answers.
 *
 * ## Why the journal's filters do not narrow these
 *
 * The aggregation endpoint accepts a date range and nothing else -- no category parameter,
 * no booking parameter. A category filter on the journal below therefore cannot narrow the
 * totals, and the note in the header says so rather than letting the two sections look
 * connected in a way they are not.
 */
export function CategoryTotals(props: CategoryTotalsProps) {
  const { kind, range, buckets } = props
  const flagLabel = kind === 'revenue' ? 'Room revenue' : 'Fixed cost'

  if (buckets.length === 0) {
    return (
      <p className={styles.empty} role="status">
        The server reported no {kind === 'revenue' ? 'revenue' : 'expense'} lines between{' '}
        {formatDate(range.date_from)} and {formatDate(range.date_to)}. That is a total of zero
        lines, not a total of zero money &mdash; no amount is shown because none was reported.
      </p>
    )
  }

  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption className={styles.caption}>
          Totals computed by the server for {formatDate(range.date_from)} to{' '}
          {formatDate(range.date_to)}, grouped by category and currency.
        </caption>
        <thead>
          <tr>
            <th scope="col">Category</th>
            <th scope="col">Currency</th>
            <th scope="col" className={styles.numeric}>
              Amount
            </th>
            <th scope="col" className={styles.numeric}>
              Tax
            </th>
            <th scope="col" className={styles.numeric}>
              Lines
            </th>
            <th scope="col">{flagLabel}</th>
          </tr>
        </thead>
        <tbody>
          {props.kind === 'revenue'
            ? props.buckets.map((bucket) => (
                <Row
                  /* `(category_code, currency)` is the grouping key the server used, so it is
                   * unique in this response by construction. */
                  key={`${bucket.category_code}-${bucket.currency}`}
                  bucket={bucket}
                  flag={bucket.is_room_revenue}
                />
              ))
            : props.buckets.map((bucket) => (
                <Row
                  key={`${bucket.category_code}-${bucket.currency}`}
                  bucket={bucket}
                  flag={bucket.is_fixed_cost}
                />
              ))}
        </tbody>
      </table>
    </div>
  )
}

function Row({
  bucket,
  flag,
}: {
  bucket: RevenueBucket | ExpenseBucket
  flag: boolean
}) {
  return (
    <tr>
      <th scope="row" className={styles.rowHeader}>
        <span className={styles.code}>{bucket.category_code}</span>
      </th>
      <td>{bucket.currency}</td>
      <td className={styles.numeric}>{formatMoney(bucket.amount, bucket.currency)}</td>
      <td className={styles.numeric}>{formatMoney(bucket.tax_amount, bucket.currency)}</td>
      <td className={styles.numeric}>{formatCount(bucket.entry_count)}</td>
      <td>{flag ? 'Yes' : 'No'}</td>
    </tr>
  )
}
