import { useId, type ReactNode } from 'react'

import { useHorizontalOverflow } from '@/lib/useHorizontalOverflow'

import styles from './BreakdownTable.module.css'

/**
 * One row exactly as the server grouped it. `amount` stays a string the whole way.
 *
 * The backend returns `NUMERIC(14,2)` as a decimal string precisely so a 64-bit binary float
 * never touches it. This component displays that string and never parses it.
 */
export interface BreakdownRow {
  readonly category_code: string
  readonly currency: string
  /** The server's decimal string, e.g. `"22701.00"`. Never converted. */
  readonly amount: string
  readonly tax_amount: string
  readonly entry_count: number
  /** `is_room_revenue` for revenue, `is_fixed_cost` for expenses. Rendered as a tag. */
  readonly flag: boolean
}

export interface BreakdownTableProps {
  readonly caption: string
  readonly rows: readonly BreakdownRow[]
  /** What `flag` means here, e.g. `Room revenue` or `Fixed cost`. */
  readonly flagLabel: string
  readonly emptyMessage: string
}

/**
 * Revenue or expenses, by category and currency, as a table.
 *
 * ## Why there is no total row
 *
 * Because the server does not send one, and this component will not compute one. The two
 * breakdown endpoints group by category **and currency** and stop there — the same reasoning
 * the financials screen states in as many words: adding EUR to USD would not produce money,
 * and this application holds no exchange rates. A "Total" row would either be wrong across
 * currencies or would silently pick one, and both are worse than its absence.
 *
 * Rows arrive grouped by currency from the server and are rendered in the order given. Sorting
 * them here would be a second opinion about a question the server already answered.
 *
 * ## Why a real table
 *
 * Category, currency and amount are three dimensions of one record, which is what a table is
 * for. A `<caption>` names the period so the figures are never read without their window, and
 * amounts are right-aligned through a class rather than an inline style so the column scans.
 *
 * ## Why it fits its card, and scrolls only when it cannot
 *
 * Until 2026-11-27 the expenses table was wider than its card at 1440px and its last column
 * sat outside the box (the A9 audit's recorded defect). Two constraints combined. Every cell
 * was `nowrap`, so a long code with its "Fixed cost" tag made the category column alone
 * about 175px wide. And the page's breakdown grid put the two cards side by side from 340px
 * each, a minimum unrelated to what the tables need. Now the tag may wrap under its code, so
 * the column can narrow to its longest word, and the page sets the cards side by side only
 * when both tables fit (see `AnalyticsPage.module.css`). Every figure stays on one line.
 *
 * Where even the narrowest table is wider than the screen, as on a phone, it scrolls inside
 * its box. While it does, the box is a focusable region named by the caption, so a keyboard
 * can reach it and scroll it with the arrow keys (`useHorizontalOverflow`).
 */
export function BreakdownTable({ caption, rows, flagLabel, emptyMessage }: BreakdownTableProps) {
  if (rows.length === 0) {
    return (
      <p className={styles.empty} role="status">
        {emptyMessage}
      </p>
    )
  }

  return (
    <ScrollBox>
      {(captionId) => (
        <table className={styles.table}>
          <caption id={captionId} className={styles.caption}>
            {caption}
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
                Entries
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.category_code}-${row.currency}`}>
                <th scope="row" className={styles.category}>
                  <span className={styles.categoryLabel}>
                    <span className={styles.code}>{row.category_code}</span>
                    {row.flag ? <span className={styles.tag}>{flagLabel}</span> : null}
                  </span>
                </th>
                <td>{row.currency}</td>
                <td className={styles.numeric}>
                  {row.currency} {row.amount}
                </td>
                <td className={styles.numeric}>{row.tax_amount}</td>
                <td className={styles.numeric}>{row.entry_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </ScrollBox>
  )
}

/**
 * The box the table scrolls inside. Rendered only with rows, so its overflow is measured from
 * the moment it mounts. While the table is wider than the box, the box is a focusable region
 * labelled by the table's caption; otherwise it is a plain container with no tab stop.
 */
function ScrollBox({ children }: { readonly children: (captionId: string) => ReactNode }) {
  const captionId = useId()
  const [ref, overflowing] = useHorizontalOverflow<HTMLDivElement>()
  return (
    <div
      ref={ref}
      className={styles.scroll}
      {...(overflowing ? { tabIndex: 0, role: 'region', 'aria-labelledby': captionId } : {})}
    >
      {children(captionId)}
    </div>
  )
}
