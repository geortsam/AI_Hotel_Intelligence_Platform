import { Link } from 'react-router-dom'

import { recurrenceLabel } from '@/features/finance/vocabulary'
import { isNegativeMoneyString } from '@/lib/decimal'
import { formatDate, formatDateTime, formatMoney } from '@/lib/format'
import { bookingPath } from '@/router/routes'
import type { ExpenseEntry, RevenueEntry } from '@/types/finance'

import styles from './LedgerCards.module.css'

export type LedgerCardsProps =
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
 * The journal below 768px, as cards.
 *
 * The same rows, not a subset: a nine-column table on a phone is either a horizontal scroll
 * nobody finds or six columns silently dropped, and on a financial record the dropped column
 * is the one that mattered. Every field the table shows appears here, as a labelled pair,
 * with the fields absent from the record simply left out rather than printed as an em dash --
 * a list of dashes is what makes a small screen unreadable.
 *
 * A `<ul>` rather than a stack of `<div>`s, so the count is announced and each line is one
 * item. Keys are the index within the page, for the reason in `LedgerTable`: a ledger row has
 * no identifier and two identical rows are legitimate.
 */
export function LedgerCards(props: LedgerCardsProps) {
  const { kind, timeZone } = props

  return (
    <ul className={styles.list}>
      {kind === 'revenue'
        ? props.entries.map((entry, index) => (
            <li key={index} className={styles.card}>
              <Header
                date={formatDate(entry.revenue_date)}
                code={entry.category_code}
                amount={entry.amount}
                currency={entry.currency}
              />
              <dl className={styles.fields}>
                <Field label="Tax">{formatMoney(entry.tax_amount, entry.currency)}</Field>
                {entry.booking_public_id !== null ? (
                  <Field label="Booking">
                    <Link className={styles.link} to={bookingPath(entry.booking_public_id)}>
                      View booking
                    </Link>
                  </Field>
                ) : null}
                {entry.reference !== null ? (
                  <Field label="Reference">{entry.reference}</Field>
                ) : null}
                {entry.description !== null ? (
                  <Field label="Description">{entry.description}</Field>
                ) : null}
                <Field label="Posted">{formatDateTime(entry.created_at, timeZone)}</Field>
              </dl>
            </li>
          ))
        : props.entries.map((entry, index) => (
            <li key={index} className={styles.card}>
              <Header
                date={formatDate(entry.expense_date)}
                code={entry.category_code}
                amount={entry.amount}
                currency={entry.currency}
              />
              <dl className={styles.fields}>
                <Field label="Tax">{formatMoney(entry.tax_amount, entry.currency)}</Field>
                {entry.vendor !== null ? <Field label="Vendor">{entry.vendor}</Field> : null}
                {entry.is_recurring && entry.recurrence_interval !== null ? (
                  <Field label="Recurring">{recurrenceLabel(entry.recurrence_interval)}</Field>
                ) : null}
                {entry.invoice_reference !== null ? (
                  <Field label="Invoice reference">{entry.invoice_reference}</Field>
                ) : null}
                {entry.description !== null ? (
                  <Field label="Description">{entry.description}</Field>
                ) : null}
                <Field label="Posted">{formatDateTime(entry.created_at, timeZone)}</Field>
              </dl>
            </li>
          ))}
    </ul>
  )
}

function Header({
  date,
  code,
  amount,
  currency,
}: {
  date: string
  code: string
  amount: string
  currency: string
}) {
  const correction = isNegativeMoneyString(amount)
  return (
    <div className={styles.header}>
      <div>
        <p className={styles.date}>{date}</p>
        <span className={styles.code}>{code}</span>
      </div>
      <p className={`${styles.amount} ${correction ? styles.correction : ''}`}>
        {formatMoney(amount, currency)}
        {correction ? <span className={styles.correctionTag}>Correction</span> : null}
      </p>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className={styles.field}>
      <dt className={styles.label}>{label}</dt>
      <dd className={styles.value}>{children}</dd>
    </div>
  )
}
