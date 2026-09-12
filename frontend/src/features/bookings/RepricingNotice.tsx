import { Badge } from '@/components/ui/Badge'
import { formatMoney } from '@/lib/format'
import type { StayRepricing } from '@/types/booking'

import styles from './StayForms.module.css'

export interface RepricingNoticeProps {
  readonly repricing: StayRepricing
}

/**
 * What a stay change did to the money, exactly as the server reported it.
 *
 * **Every figure here is read from the response. Nothing is computed.** Not the difference,
 * not the amount due, not the outstanding balance -- all seven come from `StayRepricing`,
 * which the backend derives from the two authoritative sums and the payment ledger. The
 * temptation is `new_total - previous_total`; the reason not to is that the server already
 * says it, and a second implementation would be a second opinion about money.
 *
 * **This is not a receipt, and it must not read like one.** The backend's own words:
 * `additional_amount_due` has not been charged and `refundable_amount` has not been
 * refunded. The platform owns no payment processor and settling either is a separate,
 * deliberate act, so the panel says so and offers no button that would imply otherwise.
 *
 * The `adjustment` field is the server's own summary of which of the three cases applies,
 * so the heading follows it rather than being inferred from the sign of a number. That
 * distinction is load-bearing: reducing a stay by EUR 360 against a booking that had paid
 * nothing came back `refundable_amount: "0.00"` with `adjustment: "none"` -- a cheaper stay
 * is not automatically a refund, and reading the sign would have said it was.
 */
export function RepricingNotice({ repricing }: RepricingNoticeProps) {
  const { currency } = repricing

  const heading =
    repricing.adjustment === 'amount_due'
      ? 'This change increased what is owed'
      : repricing.adjustment === 'refundable'
        ? 'This change made an amount refundable'
        : 'This change altered no balance'

  const tone =
    repricing.adjustment === 'amount_due'
      ? 'warning'
      : repricing.adjustment === 'refundable'
        ? 'info'
        : 'neutral'

  return (
    <div className={styles.repricing}>
      <div className={styles.repricingHead}>
        <h4 className={styles.repricingTitle}>{heading}</h4>
        <Badge tone={tone}>{currency}</Badge>
      </div>

      <dl className={styles.repricingFacts}>
        <div>
          <dt>Stay value before</dt>
          <dd>{formatMoney(repricing.previous_total, currency)}</dd>
        </div>
        <div>
          <dt>Stay value after</dt>
          <dd>{formatMoney(repricing.new_total, currency)}</dd>
        </div>
        <div>
          <dt>Difference</dt>
          <dd>{formatMoney(repricing.difference, currency)}</dd>
        </div>
        <div>
          <dt>Additional amount due</dt>
          <dd>{formatMoney(repricing.additional_amount_due, currency)}</dd>
        </div>
        <div>
          <dt>Refundable</dt>
          <dd>{formatMoney(repricing.refundable_amount, currency)}</dd>
        </div>
        <div>
          <dt>Outstanding afterwards</dt>
          <dd>{formatMoney(repricing.outstanding_after, currency)}</dd>
        </div>
      </dl>

      <p className={styles.repricingNote}>
        Nothing has been charged and nothing has been refunded. These figures describe the
        booking&rsquo;s position after the change; settling a difference is a separate action.
      </p>
    </div>
  )
}
