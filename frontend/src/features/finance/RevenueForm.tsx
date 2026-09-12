import { useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import {
  isNegativeMoneyString,
  isNonNegativeMoneyString,
  isSignedMoneyString,
  toDecimalString,
} from '@/lib/decimal'
import { formatDate, formatMoney } from '@/lib/format'
import type { RevenueCategory, RevenueCreateRequest } from '@/types/finance'

import styles from './LedgerForms.module.css'

export interface RevenueFormProps {
  readonly categories: readonly RevenueCategory[]
  /** The hotel's configured currency. The default, not a constraint -- see the docstring. */
  readonly defaultCurrency: string
  /** Today in the hotel's own zone, so the default date is the property's today. */
  readonly today: string
  readonly onSubmit: (payload: RevenueCreateRequest) => void
  readonly onCancel: () => void
  readonly busy: boolean
  /** Clears the previous refusal as soon as a new attempt starts. */
  readonly onDirty?: () => void
  /**
   * Whether the shared catalogue could not be read.
   *
   * When it could not, the select is replaced by a plain text field for the code. The server
   * validates it either way -- an unknown code is a 404 -- so a posting stays possible
   * instead of the form presenting an empty dropdown and no way forward.
   */
  readonly categoriesUnavailable?: boolean
}

/**
 * Posting one revenue line.
 *
 * ## Two steps, because this cannot be undone
 *
 * The journal is append-only: there is no PATCH, no DELETE and no identifier to aim either
 * at. A line posted by mistake stays in the record for ever and is corrected by posting a
 * second, negative line beside it. That makes the confirmation step worth the click -- it is
 * the last moment at which nothing has happened. The confirmation restates **only what was
 * typed**; it computes no total and previews no new balance.
 *
 * ## The amount may be negative, and that is the correction mechanism
 *
 * `revenue.amount` carries no positivity constraint, deliberately -- the schema's own reason
 * is that a compensating line keeps the journal additive. So a leading `-` is accepted, and
 * the confirmation names it as a correction so nobody posts one by slipping on the key.
 * `tax_amount` is the opposite: `ck_revenue_tax_amount_non_negative` refuses a negative with
 * a 422, so it is checked at the field that produced it.
 *
 * Neither figure ever becomes a number. Both are validated as strings and sent as strings,
 * which the request schema accepts. See `@/lib/decimal`.
 *
 * ## The currency is editable, unlike a payment's
 *
 * The charge form fixes a payment's currency to the booking's, because a foreign payment
 * makes that booking permanently unreconcilable. Nothing of the kind happens here: the
 * schema explicitly does not tie a ledger line's currency to the hotel's, and the aggregation
 * endpoint groups by `(category_code, currency)`, so a USD line beside EUR lines produces a
 * second bucket rather than a broken total. Verified live: a USD line on a EUR property was
 * accepted, and the breakdown reported EUR and USD separately.
 *
 * The typed code is upper-cased before sending. That is a canonicalisation of an ISO 4217
 * code, not a rewrite of a value this application does not own -- unlike a processor's
 * transaction reference, which the payment form sends exactly as typed.
 *
 * ## The booking is a pasted identifier
 *
 * For the reason given in `LedgerFilters`: no endpoint lists the bookings worth attaching
 * revenue to, and populating a dropdown would mean fetching the property's entire booking
 * history. An unknown identifier is a 404 from the server, rendered as a refusal.
 */
export function RevenueForm({
  categories,
  defaultCurrency,
  today,
  onSubmit,
  onCancel,
  busy,
  onDirty,
  categoriesUnavailable = false,
}: RevenueFormProps) {
  const categoryId = useId()
  const dateId = useId()
  const amountId = useId()
  const currencyId = useId()
  const taxId = useId()
  const bookingId = useId()
  const referenceId = useId()
  const descriptionId = useId()
  const errorId = useId()

  const [categoryCode, setCategoryCode] = useState('')
  const [date, setDate] = useState(today)
  const [amount, setAmount] = useState('')
  const [currency, setCurrency] = useState(defaultCurrency)
  const [tax, setTax] = useState('')
  const [booking, setBooking] = useState('')
  const [reference, setReference] = useState('')
  const [description, setDescription] = useState('')
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
      setProblem('Choose the category this revenue belongs to.')
      return
    }
    if (date === '') {
      setProblem('Enter the date this revenue belongs to.')
      return
    }
    if (!isSignedMoneyString(amount)) {
      setProblem(
        'Enter an amount with at most two decimal places. A leading minus posts a correction.',
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

  const payload = (): RevenueCreateRequest => ({
    category_code: categoryCode,
    revenue_date: date,
    amount: toDecimalString(amount),
    currency: currency.trim().toUpperCase(),
    // Sent only when the field means something. An omitted optional is not an empty string,
    // and `extra="forbid"` makes a wrong shape a 422 rather than a shrug.
    ...(tax.trim() !== '' ? { tax_amount: toDecimalString(tax) } : {}),
    ...(booking.trim() !== '' ? { booking_public_id: booking.trim() } : {}),
    ...(reference.trim() !== '' ? { reference: reference.trim() } : {}),
    ...(description.trim() !== '' ? { description: description.trim() } : {}),
  })

  if (confirming) {
    const correction = isNegativeMoneyString(amount)
    return (
      <div className={styles.form}>
        <div className={styles.confirm} role="group" aria-label="Confirm revenue line">
          <p className={styles.confirmQuestion}>
            Post{' '}
            <strong>{formatMoney(toDecimalString(amount), currency.trim().toUpperCase())}</strong>{' '}
            of {categoryCode} revenue dated {formatDate(date)}
            {correction ? ' as a correction' : ''}?
          </p>
          <p className={styles.confirmNote}>
            The journal is append-only. Once posted, this line cannot be edited or deleted
            &mdash; a mistake is corrected by posting a negative line beside it.
          </p>
          <div className={styles.actions}>
            <Button
              /* Focus lands on the confirming action. `autoFocus` rather than a ref: the
               * design system's Button is a plain function component. */
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
        Records revenue in this property&rsquo;s journal. Posting a line writes it to the
        ledger and moves no money.
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
            Revenue date
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
            The day the revenue belongs to, in this property&rsquo;s own calendar &mdash; not
            necessarily today.
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
            A leading minus posts a correction against an earlier line, which is how a
            mistake is fixed here.
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
            Defaults to the property&rsquo;s currency. A line in another currency is
            permitted and is totalled in its own bucket, never converted.
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
          <label className={styles.label} htmlFor={bookingId}>
            Booking (optional)
          </label>
          <input
            id={bookingId}
            className={styles.input}
            type="text"
            autoComplete="off"
            spellCheck={false}
            placeholder="Booking identifier"
            value={booking}
            disabled={busy}
            onChange={(event) => {
              setBooking(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            The identifier from a booking&rsquo;s own page, when this revenue was earned
            against one.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={referenceId}>
            Reference (optional)
          </label>
          <input
            id={referenceId}
            className={styles.input}
            type="text"
            autoComplete="off"
            maxLength={200}
            value={reference}
            disabled={busy}
            onChange={(event) => {
              setReference(event.target.value)
              touched()
            }}
          />
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
