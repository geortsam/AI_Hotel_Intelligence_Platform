import { Link } from 'react-router-dom'

import { recurrenceLabel } from '@/features/finance/vocabulary'
import { isNegativeMoneyString } from '@/lib/decimal'
import { formatDate, formatDateTime, formatMoney, UNAVAILABLE } from '@/lib/format'
import { bookingPath } from '@/router/routes'
import type { ExpenseEntry, RevenueEntry } from '@/types/finance'

import styles from './LedgerTable.module.css'

export type LedgerTableProps =
  | {
      readonly kind: 'revenue'
      readonly entries: readonly RevenueEntry[]
      readonly timeZone: string
    }
  | {
      readonly kind: 'expenses'
      readonly entries: readonly ExpenseEntry[]
      readonly timeZone: string
    }

/**
 * One page of a financial journal, as a table.
 *
 * ## No row is a link, and no row has an id
 *
 * Neither ledger table has a `public_id`, and there is no single-entry endpoint to navigate
 * to -- so there is nothing for a row to link to and no detail page to build. A revenue line
 * that names a booking links to **that booking**, which is a real route.
 *
 * The same absence is why React keys are the row's index within the page. Two byte-identical
 * lines are a legitimate state here -- the same 20.00 bar tab twice is two sales -- so a key
 * built from the row's contents would collide, and a synthesised id would assert an identity
 * the record does not have. The list is fully replaced on every fetch and never reordered
 * locally, which is the case an index key is safe in.
 *
 * ## Every figure is the server's, printed
 *
 * There is no total row, no running balance and no subtotal. The amounts on one page are one
 * page of many; adding them would produce a partial sum that looks like a whole one. Totals
 * live in `CategoryTotals`, where PostgreSQL computed them.
 *
 * A **negative amount is a correction**, not an error, and is marked as such -- the schema
 * has no positivity constraint precisely so a mistake can be reversed by a compensating line.
 * The mark is presentation: `isNegativeMoneyString` reads the leading `-` off the string and
 * performs no arithmetic.
 *
 * ## Currency travels with every amount
 *
 * Each row prints its own currency code because a hotel's journal may hold several -- the
 * schema explicitly does not tie a line's currency to the property's. Nothing is converted
 * and nothing is normalised.
 */
export function LedgerTable(props: LedgerTableProps) {
  const { kind, timeZone } = props

  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption className={styles.caption}>
          {kind === 'revenue'
            ? 'Revenue lines, newest first. Amounts are shown in the currency each line was posted in.'
            : 'Expense lines, newest first. Amounts are shown in the currency each line was posted in.'}
        </caption>
        <thead>
          <tr>
            <th scope="col">{kind === 'revenue' ? 'Revenue date' : 'Expense date'}</th>
            <th scope="col">Category</th>
            <th scope="col" className={styles.numeric}>
              Amount
            </th>
            <th scope="col" className={styles.numeric}>
              Tax
            </th>
            {kind === 'revenue' ? (
              <th scope="col">Booking</th>
            ) : (
              <>
                <th scope="col">Vendor</th>
                <th scope="col">Recurring</th>
              </>
            )}
            <th scope="col">{kind === 'revenue' ? 'Reference' : 'Invoice reference'}</th>
            <th scope="col">Description</th>
            <th scope="col">Posted</th>
          </tr>
        </thead>
        <tbody>
          {kind === 'revenue'
            ? props.entries.map((entry, index) => (
                <tr key={index}>
                  <td>{formatDate(entry.revenue_date)}</td>
                  <td>
                    <span className={styles.code}>{entry.category_code}</span>
                  </td>
                  <Amount amount={entry.amount} currency={entry.currency} />
                  <td className={styles.numeric}>
                    {formatMoney(entry.tax_amount, entry.currency)}
                  </td>
                  <td>
                    {entry.booking_public_id === null ? (
                      <span className={styles.absent}>{UNAVAILABLE}</span>
                    ) : (
                      <Link className={styles.link} to={bookingPath(entry.booking_public_id)}>
                        View booking
                      </Link>
                    )}
                  </td>
                  <td>{entry.reference ?? <span className={styles.absent}>{UNAVAILABLE}</span>}</td>
                  <td className={styles.description}>
                    {entry.description ?? <span className={styles.absent}>{UNAVAILABLE}</span>}
                  </td>
                  <td className={styles.posted}>{formatDateTime(entry.created_at, timeZone)}</td>
                </tr>
              ))
            : props.entries.map((entry, index) => (
                <tr key={index}>
                  <td>{formatDate(entry.expense_date)}</td>
                  <td>
                    <span className={styles.code}>{entry.category_code}</span>
                  </td>
                  <Amount amount={entry.amount} currency={entry.currency} />
                  <td className={styles.numeric}>
                    {formatMoney(entry.tax_amount, entry.currency)}
                  </td>
                  <td>{entry.vendor ?? <span className={styles.absent}>{UNAVAILABLE}</span>}</td>
                  <td>
                    {entry.is_recurring && entry.recurrence_interval !== null
                      ? recurrenceLabel(entry.recurrence_interval)
                      : /* Not "No": the column answers "on what cycle", and a one-off
                         * expense has none. */
                        <span className={styles.absent}>{UNAVAILABLE}</span>}
                  </td>
                  <td>
                    {entry.invoice_reference ?? <span className={styles.absent}>{UNAVAILABLE}</span>}
                  </td>
                  <td className={styles.description}>
                    {entry.description ?? <span className={styles.absent}>{UNAVAILABLE}</span>}
                  </td>
                  <td className={styles.posted}>{formatDateTime(entry.created_at, timeZone)}</td>
                </tr>
              ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * One amount cell.
 *
 * A negative figure carries a visible label as well as its minus sign, because a minus in a
 * column of money is easy to miss and the difference between a 150.00 sale and a 150.00
 * reversal is the whole meaning of the row.
 */
function Amount({ amount, currency }: { amount: string; currency: string }) {
  const correction = isNegativeMoneyString(amount)
  return (
    <td className={`${styles.numeric} ${correction ? styles.correction : ''}`}>
      {formatMoney(amount, currency)}
      {correction ? <span className={styles.correctionTag}>Correction</span> : null}
    </td>
  )
}
