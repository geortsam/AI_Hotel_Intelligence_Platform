import { useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import { RECURRENCE_INTERVALS, recurrenceLabel } from '@/features/finance/vocabulary'
import {
  isNegativeMoneyString,
  isNonNegativeMoneyString,
  isSignedMoneyString,
  toDecimalString,
} from '@/lib/decimal'
import { formatDate, formatMoney } from '@/lib/format'
import type { ExpenseCategory, ExpenseCreateRequest, RecurrenceInterval } from '@/types/finance'

import styles from './LedgerForms.module.css'

export interface ExpenseFormProps {
  readonly categories: readonly ExpenseCategory[]
  readonly defaultCurrency: string
  readonly today: string
  readonly onSubmit: (payload: ExpenseCreateRequest) => void
  readonly onCancel: () => void
  readonly busy: boolean
  readonly onDirty?: () => void
  /** Whether the shared catalogue could not be read. See `RevenueFormProps`. */
  readonly categoriesUnavailable?: boolean
}

/**
 * Posting one expense line.
 *
 * The revenue form's docstring covers what the two share: the two-step confirmation, because
 * the journal is append-only and a posted line cannot be withdrawn; the signed amount, which
 * is how a credit note is recorded; the non-negative tax; the editable currency; and the fact
 * that no figure here is ever a JavaScript number.
 *
 * Two things are specific to expenses.
 *
 * **There is no booking field, and there is no booking column.** `expenses` has no
 * `booking_id` at all, and `ExpenseCreate` has no such key -- sending one is a 422 from
 * `extra="forbid"`, verified live. So the form does not offer it, and this is an absence in
 * the schema rather than a decision taken here.
 *
 * **Recurring and its interval are bound together.** The schema's `model_validator` requires
 * `is_recurring` to be true exactly when `recurrence_interval` is present: each without the
 * other is a 422, verified in both directions. The control is therefore a single choice --
 * ticking the box reveals the interval, and clearing it removes both from the payload -- so
 * the invalid pair cannot be expressed by the interface at all.
 *
 * Recording a recurrence does **not** schedule anything. The backend has no job that posts
 * next month's line; the flag records that this cost repeats. The hint on the control says
 * so, because a checkbox called "recurring" that quietly did nothing would be the more
 * expensive misunderstanding.
 */
export function ExpenseForm({
  categories,
  defaultCurrency,
  today,
  onSubmit,
  onCancel,
  busy,
  onDirty,
  categoriesUnavailable = false,
}: ExpenseFormProps) {
  const categoryId = useId()
  const dateId = useId()
  const amountId = useId()
  const currencyId = useId()
  const taxId = useId()
  const vendorId = useId()
  const invoiceId = useId()
  const descriptionId = useId()
  const recurringId = useId()
  const intervalId = useId()
  const errorId = useId()

  const [categoryCode, setCategoryCode] = useState('')
  const [date, setDate] = useState(today)
  const [amount, setAmount] = useState('')
  const [currency, setCurrency] = useState(defaultCurrency)
  const [tax, setTax] = useState('')
  const [vendor, setVendor] = useState('')
  const [invoice, setInvoice] = useState('')
  const [description, setDescription] = useState('')
  const [recurring, setRecurring] = useState(false)
  const [interval, setInterval] = useState<RecurrenceInterval>('monthly')
  const [problem, setProblem] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)

  const touched = () => {
    setProblem(null)
    onDirty?.()
  }

  const review = (event: FormEvent) => {
    event.preventDefault()
    if (busy) {
      return
    }
    if (categoryCode === '') {
      setProblem('Choose the category this expense belongs to.')
      return
    }
    if (date === '') {
      setProblem('Enter the date this expense belongs to.')
      return
    }
    if (!isSignedMoneyString(amount)) {
      setProblem(
        'Enter an amount with at most two decimal places. A leading minus posts a credit note.',
      )
      return
    }
    if (tax.trim() !== '' && !isNonNegativeMoneyString(tax)) {
      setProblem('Tax cannot be negative, and takes at most two decimal places.')
      return
    }
    if (!/^[A-Za-z]{3}$/.test(currency.trim())) {
      setProblem('Enter a three-letter currency code, such as EUR.')
      return
    }
    setProblem(null)
    setConfirming(true)
  }

  const payload = (): ExpenseCreateRequest => ({
    category_code: categoryCode,
    expense_date: date,
    amount: toDecimalString(amount),
    currency: currency.trim().toUpperCase(),
    ...(tax.trim() !== '' ? { tax_amount: toDecimalString(tax) } : {}),
    ...(vendor.trim() !== '' ? { vendor: vendor.trim() } : {}),
    ...(invoice.trim() !== '' ? { invoice_reference: invoice.trim() } : {}),
    ...(description.trim() !== '' ? { description: description.trim() } : {}),
    // Both keys or neither: the server's validator rejects each on its own.
    ...(recurring ? { is_recurring: true, recurrence_interval: interval } : {}),
  })

  if (confirming) {
    const credit = isNegativeMoneyString(amount)
    return (
      <div className={styles.form}>
        <div className={styles.confirm} role="group" aria-label="Confirm expense line">
          <p className={styles.confirmQuestion}>
            Post{' '}
            <strong>{formatMoney(toDecimalString(amount), currency.trim().toUpperCase())}</strong>{' '}
            of {categoryCode} expense dated {formatDate(date)}
            {credit ? ' as a credit note' : ''}
            {recurring ? `, marked ${recurrenceLabel(interval).toLowerCase()}` : ''}?
          </p>
          <p className={styles.confirmNote}>
            The journal is append-only. Once posted, this line cannot be edited or deleted
            &mdash; a mistake is corrected by posting a negative line beside it.
          </p>
          <div className={styles.actions}>
            <Button
              autoFocus
              variant="primary"
              size="sm"
              disabled={busy}
              onClick={() => {
                onSubmit(payload())
              }}
            >
              {busy ? 'Posting…' : 'Yes, post this line'}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={busy}
              onClick={() => {
                setConfirming(false)
              }}
            >
              Back
            </Button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <form className={styles.form} onSubmit={review} noValidate>
      <p className={styles.intro}>
        Records an expense in this property&rsquo;s journal. Posting a line writes it to the
        ledger and pays nobody.
      </p>

      <div className={styles.grid}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={categoryId}>
            Category
          </label>
          {categoriesUnavailable ? (
            <input
              id={categoryId}
              className={styles.input}
              type="text"
              autoComplete="off"
              spellCheck={false}
              placeholder="Category code"
              value={categoryCode}
              disabled={busy}
              required
              onChange={(event) => {
                setCategoryCode(event.target.value)
                touched()
              }}
            />
          ) : (
            <select
              id={categoryId}
              className={styles.select}
              value={categoryCode}
              disabled={busy}
              required
              onChange={(event) => {
                setCategoryCode(event.target.value)
                touched()
              }}
            >
              <option value="">Choose a category</option>
              {categories.map((category) => (
                <option key={category.code} value={category.code}>
                  {category.name} ({category.code})
                </option>
              ))}
            </select>
          )}
          <span className={styles.hint}>
            {categoriesUnavailable
              ? 'The shared catalogue could not be read, so the code is typed here. The server checks it and refuses an unknown one.'
              : 'From the platform’s shared catalogue. Categories are managed by a platform administrator, not per property.'}
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={dateId}>
            Expense date
          </label>
          <input
            id={dateId}
            className={styles.input}
            type="date"
            value={date}
            disabled={busy}
            required
            onChange={(event) => {
              setDate(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            The day the cost belongs to, in this property&rsquo;s own calendar.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={amountId}>
            Amount
          </label>
          <input
            id={amountId}
            className={styles.input}
            type="text"
            inputMode="decimal"
            autoComplete="off"
            placeholder="0.00"
            value={amount}
            disabled={busy}
            required
            aria-describedby={problem ? errorId : undefined}
            aria-invalid={problem !== null || undefined}
            onChange={(event) => {
              setAmount(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            A leading minus posts a credit note against an earlier line.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={currencyId}>
            Currency
          </label>
          <input
            id={currencyId}
            className={`${styles.input} ${styles.currency}`}
            type="text"
            autoComplete="off"
            maxLength={3}
            value={currency}
            disabled={busy}
            required
            onChange={(event) => {
              setCurrency(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            Defaults to the property&rsquo;s currency. A supplier invoiced in another is
            recorded in that one and totalled separately.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={taxId}>
            Tax (optional)
          </label>
          <input
            id={taxId}
            className={styles.input}
            type="text"
            inputMode="decimal"
            autoComplete="off"
            placeholder="0.00"
            value={tax}
            disabled={busy}
            onChange={(event) => {
              setTax(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>Never negative. Left blank, the server records zero.</span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={vendorId}>
            Vendor (optional)
          </label>
          <input
            id={vendorId}
            className={styles.input}
            type="text"
            autoComplete="off"
            maxLength={200}
            value={vendor}
            disabled={busy}
            onChange={(event) => {
              setVendor(event.target.value)
              touched()
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={invoiceId}>
            Invoice reference (optional)
          </label>
          <input
            id={invoiceId}
            className={styles.input}
            type="text"
            autoComplete="off"
            maxLength={200}
            value={invoice}
            disabled={busy}
            onChange={(event) => {
              setInvoice(event.target.value)
              touched()
            }}
          />
        </div>

        <div className={styles.field}>
          <span className={styles.label}>Recurrence</span>
          <label className={styles.checkbox} htmlFor={recurringId}>
            <input
              id={recurringId}
              type="checkbox"
              checked={recurring}
              disabled={busy}
              onChange={(event) => {
                setRecurring(event.target.checked)
                touched()
              }}
            />
            This cost repeats
          </label>
          {recurring ? (
            <>
              <label className={styles.label} htmlFor={intervalId}>
                Interval
              </label>
              <select
                id={intervalId}
                className={styles.select}
                value={interval}
                disabled={busy}
                onChange={(event) => {
                  setInterval(event.target.value as RecurrenceInterval)
                  touched()
                }}
              >
                {RECURRENCE_INTERVALS.map((option) => (
                  <option key={option} value={option}>
                    {recurrenceLabel(option)}
                  </option>
                ))}
              </select>
            </>
          ) : null}
          <span className={styles.hint}>
            Records that the cost repeats. It does not schedule anything &mdash; the next
            period&rsquo;s line is posted when it is incurred.
          </span>
        </div>

        <div className={`${styles.field} ${styles.wide}`}>
          <label className={styles.label} htmlFor={descriptionId}>
            Description (optional)
          </label>
          <textarea
            id={descriptionId}
            className={styles.textarea}
            rows={2}
            value={description}
            disabled={busy}
            onChange={(event) => {
              setDescription(event.target.value)
              touched()
            }}
          />
        </div>
      </div>

      {problem ? (
        <p className={styles.error} id={errorId} role="alert">
          {problem}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button type="submit" variant="primary" size="sm" disabled={busy}>
          Review line
        </Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
