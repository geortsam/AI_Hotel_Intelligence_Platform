import { useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import { methodLabel, PAYMENT_METHODS, POSTABLE_STATUSES, statusPresentation } from '@/features/payments/vocabulary'
import { isPositiveMoneyString, toDecimalString } from '@/lib/decimal'
import type { ChargeRequest, PaymentMethod, PaymentStatus } from '@/types/payment'

import styles from './PaymentForms.module.css'

export interface ChargeFormProps {
  /** The booking's currency. Fixed, not chosen — see the component docstring. */
  readonly currency: string
  readonly onSubmit: (payload: ChargeRequest) => void
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
 * Recording a charge against a booking.
 *
 * ## The currency is fixed to the booking's, and that is a safety decision
 *
 * `ChargeCreate` requires a currency and the server neither derives nor validates it: a EUR
 * booking will happily accept a USD charge. What happens next is the problem.
 * `ReconciliationService` refuses to aggregate across currencies -- rightly, since summing
 * them produces a number that is not money -- so **one foreign charge makes that booking's
 * financial summary a permanent 409**. Verified live: posting USD 10.00 against a EUR booking
 * turned reconciliation into "This booking cannot be reconciled".
 *
 * So the field is shown and sent, but not editable. The API's wider capability is real and
 * is named in the report; offering it here would be offering an operator a one-click way to
 * break the booking's accounts with no warning and no undo.
 *
 * ## The amount never becomes a number
 *
 * It is validated as a string against the column's shape and sent as a string, which the
 * request schema accepts. `parseFloat` appears nowhere. See `@/lib/decimal`.
 *
 * ## The transaction reference is the idempotency key
 *
 * With a provider, it is half of a partial unique index, and a repeat is a 409 rather than a
 * second charge. It is sent **exactly as typed** -- not upper-cased, not trimmed of internal
 * spaces, not normalised in any way -- because it is the processor's identifier and this
 * application does not own its format.
 */
export function ChargeForm({ currency, onSubmit, onCancel, busy, onDirty }: ChargeFormProps) {
  const amountId = useId()
  const methodId = useId()
  const statusId = useId()
  const paidAtId = useId()
  const providerId = useId()
  const referenceId = useId()
  const cardId = useId()
  const errorId = useId()

  const [amount, setAmount] = useState('')
  const [method, setMethod] = useState<PaymentMethod>('card')
  const [status, setStatus] = useState<PaymentStatus>('captured')
  const [paidAt, setPaidAt] = useState(() => new Date().toISOString().slice(0, 16))
  const [provider, setProvider] = useState('')
  const [reference, setReference] = useState('')
  const [cardLastFour, setCardLastFour] = useState('')
  const [problem, setProblem] = useState<string | null>(null)

  // `ck_payments_captured_has_paid_at`. Caught here so the field can be shown, not to
  // second-guess the server -- it applies the same rule and returns a 422 naming the field.
  const needsPaidAt = status === 'captured'

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    if (busy) {
      return
    }

    if (!isPositiveMoneyString(amount)) {
      setProblem('Enter an amount greater than zero, with at most two decimal places.')
      return
    }
    if (needsPaidAt && paidAt.trim() === '') {
      setProblem('A captured payment needs the time the money was taken.')
      return
    }
    if (cardLastFour !== '' && !/^\d{4}$/.test(cardLastFour)) {
      setProblem('Card digits must be exactly the last four.')
      return
    }
    setProblem(null)

    onSubmit({
      amount: toDecimalString(amount),
      currency,
      method,
      status,
      // Sent only when the field means something. An omitted optional is not the same as an
      // empty string, and `extra="forbid"` makes a wrong shape a 422 rather than a shrug.
      ...(needsPaidAt ? { paid_at: new Date(paidAt).toISOString() } : {}),
      ...(provider.trim() !== '' ? { provider: provider.trim() } : {}),
      ...(reference !== '' ? { transaction_reference: reference } : {}),
      ...(cardLastFour !== '' ? { card_last_four: cardLastFour } : {}),
    })
  }

  return (
    <form className={styles.form} onSubmit={handleSubmit} noValidate>
      <p className={styles.intro}>
        Records a payment already taken. This platform processes nothing &mdash; posting a
        charge writes it to the booking&rsquo;s ledger and moves no money.
      </p>

      <div className={styles.grid}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={amountId}>
            Amount ({currency})
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
            In {currency}, the booking&rsquo;s currency. Postings in another currency would
            stop this booking being reconciled.
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

        <div className={styles.field}>
          <label className={styles.label} htmlFor={statusId}>
            Status
          </label>
          <select
            id={statusId}
            className={styles.select}
            value={status}
            disabled={busy}
            onChange={(event) => {
              setStatus(event.target.value as PaymentStatus)
            }}
          >
            {POSTABLE_STATUSES.map((option) => (
              <option key={option} value={option}>
                {statusPresentation(option).label}
              </option>
            ))}
          </select>
        </div>

        {needsPaidAt ? (
          <div className={styles.field}>
            <label className={styles.label} htmlFor={paidAtId}>
              Taken at
            </label>
            <input
              id={paidAtId}
              className={styles.input}
              type="datetime-local"
              value={paidAt}
              disabled={busy}
              required
              onChange={(event) => {
                setPaidAt(event.target.value)
              }}
            />
          </div>
        ) : null}

        <div className={styles.field}>
          <label className={styles.label} htmlFor={providerId}>
            Provider (optional)
          </label>
          <input
            id={providerId}
            className={styles.input}
            type="text"
            autoComplete="off"
            maxLength={50}
            value={provider}
            disabled={busy}
            onChange={(event) => {
              setProvider(event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={referenceId}>
            Transaction reference (optional)
          </label>
          <input
            id={referenceId}
            className={styles.input}
            type="text"
            autoComplete="off"
            maxLength={100}
            value={reference}
            disabled={busy}
            onChange={(event) => {
              setReference(event.target.value)
            }}
          />
          <span className={styles.hint}>
            With a provider, this prevents the same payment being recorded twice. Sent exactly
            as typed.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={cardId}>
            Card last four (optional)
          </label>
          <input
            id={cardId}
            className={styles.input}
            type="text"
            inputMode="numeric"
            autoComplete="off"
            maxLength={4}
            value={cardLastFour}
            disabled={busy}
            onChange={(event) => {
              setCardLastFour(event.target.value)
            }}
          />
          <span className={styles.hint}>
            The last four digits only. Nothing else about a card is stored, and there is no
            field for a full number.
          </span>
        </div>
      </div>

      {problem ? (
        <p className={styles.error} id={errorId} role="alert">
          {problem}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button type="submit" variant="primary" size="sm" disabled={busy}>
          {busy ? 'Recording…' : 'Record charge'}
        </Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
