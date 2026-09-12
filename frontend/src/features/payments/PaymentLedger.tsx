import { ArrowDownLeft, ArrowUpRight, Undo2 } from 'lucide-react'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Skeleton } from '@/components/ui/Skeleton'
import { methodLabel, movedNoMoney, statusPresentation } from '@/features/payments/vocabulary'
import { formatDateTime, formatMoney, UNAVAILABLE } from '@/lib/format'
import { useIsCompact } from '@/lib/useIsCompact'
import type { Payment } from '@/types/payment'

import styles from './PaymentLedger.module.css'

export interface PaymentLedgerProps {
  readonly payments: readonly Payment[]
  readonly timeZone: string
  /** Called with the charge a refund should reverse. Absent when refunds are not offered. */
  readonly onRefund?: (payment: Payment) => void
  readonly busy?: boolean
  readonly isLoading?: boolean
}

/**
 * A booking's postings, exactly as the server ordered them.
 *
 * ## Nothing is totalled
 *
 * There is no footer row, no running balance and no subtotal. Those figures exist -- charged,
 * refunded, net paid, outstanding -- and every one of them belongs to reconciliation, which
 * the page shows separately from its own endpoint. Adding a column of numbers here would
 * produce a second answer to a question the server has already answered, and the two would
 * differ the moment a currency or a voided row entered the picture.
 *
 * ## Direction comes from `kind`, never from the sign
 *
 * `amount` is positive on both kinds; the schema puts direction in `kind`. So a refund is
 * shown with its own icon, its own word and a "less" prefix, and the figure itself is never
 * negated. A minus sign here would be this application reinterpreting the schema.
 *
 * ## A voided posting is marked, and cannot be refunded from here
 *
 * `failed` and `cancelled` moved no money: the backend refuses a refund against them and
 * excludes them from the already-refunded sum. They are labelled as such and their refund
 * control is withheld -- presentation, not enforcement, exactly as with the booking
 * lifecycle. The server still refuses.
 *
 * A refund cannot itself be refunded, so refunds carry no control either. Both refusals are
 * verified against the live API.
 */
export function PaymentLedger({
  payments,
  timeZone,
  onRefund,
  busy = false,
  isLoading = false,
}: PaymentLedgerProps) {
  const isCompact = useIsCompact()

  if (isLoading) {
    return (
      <div className={styles.loading} aria-busy="true" aria-label="Loading payments">
        {Array.from({ length: 3 }, (_, index) => (
          <Skeleton key={index} height="2.5rem" />
        ))}
      </div>
    )
  }

  /** Whether this row may be reversed. A judgement about *offering*, not about an amount. */
  const canRefund = (payment: Payment) =>
    onRefund !== undefined && payment.kind === 'charge' && !movedNoMoney(payment.status)

  if (isCompact) {
    return (
      <ul className={styles.cards}>
        {payments.map((payment) => (
          <li key={payment.public_id} className={styles.card}>
            <div className={styles.cardHead}>
              <KindLabel payment={payment} />
              <span className={styles.cardAmount}>
                {formatMoney(payment.amount, payment.currency)}
              </span>
            </div>
            <dl className={styles.cardFacts}>
              <div>
                <dt>Status</dt>
                <dd>
                  <StatusBadge payment={payment} />
                </dd>
              </div>
              <div>
                <dt>Method</dt>
                <dd>{methodLabel(payment.method)}</dd>
              </div>
              <div>
                <dt>Recorded</dt>
                <dd>{formatDateTime(payment.created_at, timeZone)}</dd>
              </div>
              <div>
                <dt>Reference</dt>
                <dd className={styles.mono}>
                  {payment.transaction_reference ?? <span className={styles.muted}>None</span>}
                </dd>
              </div>
            </dl>
            {canRefund(payment) ? (
              <Button
                variant="secondary"
                size="sm"
                disabled={busy}
                onClick={() => {
                  onRefund?.(payment)
                }}
              >
                <Undo2 size={14} aria-hidden="true" />
                Refund this charge
              </Button>
            ) : null}
          </li>
        ))}
      </ul>
    )
  }

  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption className={styles.caption}>
          Charges and refunds against this booking, oldest first. Amounts are positive on both
          kinds; the direction is the kind. No total is shown here — the financial summary
          above is the authoritative one.
        </caption>
        <thead>
          <tr>
            <th scope="col">Posting</th>
            <th scope="col" className={styles.numeric}>
              Amount
            </th>
            <th scope="col">Status</th>
            <th scope="col">Method</th>
            <th scope="col">Reference</th>
            <th scope="col">Recorded</th>
            <th scope="col">
              <span className={styles.srOnly}>Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {payments.map((payment) => (
            <tr key={payment.public_id} className={payment.kind === 'refund' ? styles.refundRow : undefined}>
              <th scope="row" className={styles.kindCell}>
                <KindLabel payment={payment} />
              </th>
              <td className={styles.numeric}>
                {payment.kind === 'refund' ? <span className={styles.less}>less </span> : null}
                {formatMoney(payment.amount, payment.currency)}
              </td>
              <td>
                <StatusBadge payment={payment} />
              </td>
              <td>{methodLabel(payment.method)}</td>
              <td className={styles.mono}>
                {payment.transaction_reference ?? <span className={styles.muted}>{UNAVAILABLE}</span>}
              </td>
              <td className={styles.recorded}>{formatDateTime(payment.created_at, timeZone)}</td>
              <td className={styles.actionsCell}>
                {canRefund(payment) ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={busy}
                    aria-label={`Refund the ${formatMoney(payment.amount, payment.currency)} charge`}
                    onClick={() => {
                      onRefund?.(payment)
                    }}
                  >
                    <Undo2 size={14} aria-hidden="true" />
                    Refund
                  </Button>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Charge or refund, said in a word and reinforced by an icon. */
function KindLabel({ payment }: { readonly payment: Payment }) {
  const Icon = payment.kind === 'refund' ? ArrowUpRight : ArrowDownLeft
  return (
    <span className={styles.kind}>
      <Icon size={14} aria-hidden="true" className={payment.kind === 'refund' ? styles.refundIcon : styles.chargeIcon} />
      {payment.kind === 'refund' ? 'Refund' : 'Charge'}
      {payment.card_last_four ? (
        <span className={styles.cardTail}>&middot;&middot;&middot;&middot; {payment.card_last_four}</span>
      ) : null}
    </span>
  )
}

/** The posting's status, plus a plain statement when it moved no money. */
function StatusBadge({ payment }: { readonly payment: Payment }) {
  const { label, tone, description } = statusPresentation(payment.status)
  return (
    <span className={styles.statusWrap}>
      <Badge tone={tone}>
        {label}
        <span className={styles.srOnly}>. {description}</span>
      </Badge>
      {movedNoMoney(payment.status) ? (
        <span className={styles.voided}>Moved no money</span>
      ) : null}
    </span>
  )
}
