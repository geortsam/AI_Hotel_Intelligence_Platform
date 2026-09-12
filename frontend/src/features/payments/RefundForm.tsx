import { useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import { methodLabel, PAYMENT_METHODS } from '@/features/payments/vocabulary'
import { compareDecimalStrings, isPositiveMoneyString, toDecimalString } from '@/lib/decimal'
import { formatDateTime, formatMoney } from '@/lib/format'
import type { Payment, PaymentMethod, RefundRequest } from '@/types/payment'

import styles from './PaymentForms.module.css'

export interface RefundFormProps {
  /** The charge being reversed. Its currency and id come from here, never from a field. */
  readonly parent: Payment
  readonly timeZone: string
  readonly onSubmit: (payload: RefundRequest) => void
  readonly onCancel: () => void
  readonly busy: boolean
  /**
   * Called as soon as the operator starts a new attempt.
   *
   * Clears the previous posting failure. Without it a refusal banner from the LAST attempt
   * sits above the form while a new amount is being typed -- found in the browser, where an
   * over-cap refusal and a fresh local validation error appeared as two alerts at once, the
   * first describing something the operator was no longer doing. On a screen about money,
   * a stale "nothing was recorded" is worse than no message.
   */
  readonly onDirty?: () => void
}

/**
 * Reversing a charge.
 *
 * ## No remaining-refundable figure is shown, and that is the point
 *
 * The server computes `refundable = parent.amount - already_refunded` **under the parent's
 * row lock**, with the already-refunded sum excluding voided rows. That figure is exposed by
 * no endpoint: there is no field for it on a payment, no preview route, and no reconciliation
 * entry for it. It surfaces only inside the 409 that refuses an over-refund.
 *
 * This form could subtract the refunds it can see on the current page and show a number. It
 * does not, for two reasons. The list is paginated, so the refunds it can see may not be all
 * of them. And even complete, the figure would be a snapshot taken outside the lock, wrong
 * the instant a colleague refunds concurrently -- which is exactly the race the lock exists
 * to close, verified live: two 80.00 refunds against a 100.00 charge produced one 201 and one
 * 409 naming the 20.00 that remained.
 *
 * So what is shown is what the API actually states: the parent charge's own amount, its
 * currency, and when it was recorded. The server decides the rest, and its refusal is
 * rendered.
 *
 * ## Two steps, because this moves money
 *
 * Filling the form does not post it. A confirmation step restates the figure and the charge
 * being reversed, and only that step submits. The confirmation shows **only values already
 * on screen** -- no computed remainder, no projected new balance.
 */
export function RefundForm({ parent, timeZone, onSubmit, onCancel, busy, onDirty }: RefundFormProps) {
  const amountId = useId()
  const methodId = useId()
  const errorId = useId()

  const [amount, setAmount] = useState('')
  const [method, setMethod] = useState<PaymentMethod>(parent.method)
  const [problem, setProblem] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)

  const review = (event: FormEvent) => {
    event.preventDefault()
    if (busy) {
      return
    }
    if (!isPositiveMoneyString(amount)) {
      setProblem('Enter an amount greater than zero, with at most two decimal places.')
      return
    }
    // The only local check on the figure, and it is a bound rather than a calculation: a
    // refund can never exceed the charge it reverses. What remains *after other refunds* is
    // the server's to decide, under its lock. Compared as strings -- see `@/lib/decimal`.
    if (compareDecimalStrings(amount, parent.amount) > 0) {
      setProblem(
        `A refund cannot exceed the charge it reverses (${formatMoney(parent.amount, parent.currency)}).`,
      )
      return
    }
    setProblem(null)
    setConfirming(true)
  }

  if (confirming) {
    return (
      <div className={styles.form}>
        <div className={styles.confirm} role="group" aria-label="Confirm refund">
          <p className={styles.confirmQuestion}>
            Record a refund of <strong>{formatMoney(toDecimalString(amount), parent.currency)}</strong>{' '}
            against the {formatMoney(parent.amount, parent.currency)} charge recorded{' '}
            {formatDateTime(parent.created_at, timeZone)}?
          </p>
          <p className={styles.confirmNote}>
            The server decides whether this much is still refundable and will refuse if it is
            not. Recording a refund writes it to the ledger; this platform moves no money.
          </p>
          <div className={styles.actions}>
            <Button
              /* Focus lands on the confirming action. `autoFocus` rather than a ref: the
               * design system's Button is a plain function component. */
              autoFocus
              variant="danger"
              size="sm"
              disabled={busy}
              onClick={() => {
                onSubmit({
                  amount: toDecimalString(amount),
                  // Both taken from the parent, never from a field: the server refuses any
                  // other currency, and the parent is named by the id it was returned with.
                  currency: parent.currency,
                  refunds_public_id: parent.public_id,
                  method,
                })
              }}
            >
              {busy ? 'Recording…' : 'Yes, record refund'}
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
        Reversing the {formatMoney(parent.amount, parent.currency)} charge recorded{' '}
        {formatDateTime(parent.created_at, timeZone)}. The refund is posted in{' '}
        {parent.currency}, the currency of the charge.
      </p>

      <div className={styles.grid}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={amountId}>
            Refund amount ({parent.currency})
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
              onDirty?.()
            }}
          />
          <span className={styles.hint}>
            How much of this charge is still refundable is decided by the server when the
            refund is posted. It is not shown here because no endpoint reports it.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={methodId}>
            Method
          </label>
          <select
            id={methodId}
            className={styles.select}
            value={method}
            disabled={busy}
            onChange={(event) => {
              setMethod(event.target.value as PaymentMethod)
            }}
          >
            {PAYMENT_METHODS.map((option) => (
              <option key={option} value={option}>
                {methodLabel(option)}
              </option>
            ))}
          </select>
        </div>
      </div>

      {problem ? (
        <p className={styles.error} id={errorId} role="alert">
          {problem}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button type="submit" variant="primary" size="sm" disabled={busy}>
          Review refund
        </Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
