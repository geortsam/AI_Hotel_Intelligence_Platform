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
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption className={styles.caption}>{caption}</caption>
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
                <span className={styles.code}>{row.category_code}</span>
                {row.flag ? <span className={styles.tag}>{flagLabel}</span> : null}
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
    </div>
  )
}
