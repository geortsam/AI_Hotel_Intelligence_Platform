import { useId, useMemo, useState, type ReactNode } from 'react'
import { Building2, WifiOff } from 'lucide-react'

import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import {
  DEFAULT_PERIOD,
  describeRange,
  rangeFor,
  todayInZone,
  type PeriodId,
} from '@/features/dashboard/period'
import { PeriodSelector } from '@/features/dashboard/PeriodSelector'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { ExpenseSection, RevenueSection } from '@/features/finance/LedgerSection'
import { LEDGER_VOCABULARY, type LedgerKind } from '@/features/finance/vocabulary'
import { useHotelContext } from '@/session/HotelProvider'

import styles from './FinancialsPage.module.css'

const LEDGER_KINDS: readonly LedgerKind[] = ['revenue', 'expenses']

/**
 * Financials &mdash; the revenue and expense journals for one property.
 *
 * ## The backend is the financial authority, and this page is a reader of it
 *
 * Every figure on this screen was computed by PostgreSQL. The per-category totals come from
 * `analytics/revenue-by-category` and `analytics/expenses-by-category`; the line counts come
 * from the list endpoints' own `total`; the amounts on each row are the strings the server
 * sent. Nothing on this page is added, subtracted, converted or averaged in the browser.
 *
 * That includes the figures a financial screen is most expected to show and this one does
 * not: **profit**, a **net position**, a **grand total** and any **cross-currency figure**.
 * No endpoint reports them. Revenue and expenses are aggregated separately, per currency, and
 * there is no FX rate anywhere in the platform -- so a "profit" here would be this browser
 * inventing the most consequential number on the page. The one thing worse than a missing
 * figure on an accounts screen is a plausible wrong one.
 *
 * ## One journal at a time, deliberately
 *
 * The two sections are mounted one at a time rather than both at once. Each costs three
 * requests -- its page, its totals, its catalogue -- and rendering both would double that to
 * show a panel nobody is looking at.
 *
 * ## The period drives both the totals and the journal
 *
 * One control, one range, sent to both endpoints. The alternative -- a period for the totals
 * and separate date fields for the journal -- lets the two drift apart, and a total labelled
 * one range over rows from another is the kind of screen someone acts on.
 *
 * Dates are resolved in the **hotel's** zone, never the browser's, for the reason the
 * dashboard states: a receptionist in Athens and a manager in London must see the same day.
 */
export function FinancialsPage() {
  const hotelContext = useHotelContext()
  const hotel = hotelContext.selected

  const [period, setPeriod] = useState<PeriodId>(DEFAULT_PERIOD)
  const [kind, setKind] = useState<LedgerKind>('revenue')
  const kindName = useId()

  const timeZone = hotel?.timezone ?? 'UTC'
  // Recomputed only when the period or the property changes -- not on every render, which
  // would make a new `range` object and refetch the journal from `useLedger`'s effect.
  const range = useMemo(() => rangeFor(period, timeZone), [period, timeZone])
  const today = useMemo(() => todayInZone(timeZone), [timeZone])

  if (hotelContext.status === 'empty') {
    return (
      <Frame>
        <StateMessage
          icon={Building2}
          tone="status"
          title="No hotel is linked to your account"
          detail="Financial records are held per property, and this account is not yet a member of one. An owner or manager can add you to a hotel from its member list."
        />
      </Frame>
    )
  }

  if (hotelContext.status === 'error') {
    return (
      <Frame>
        <StateMessage
          icon={WifiOff}
          tone="alert"
          title="Could not load your hotels"
          detail="The list of properties you have access to could not be retrieved. Check your connection and try again."
          onRetry={hotelContext.retry}
        />
      </Frame>
    )
  }

  return (
    <Frame>
      <div className={styles.controls}>
        {/*
         * Native radios, restyled -- the same choice `PeriodSelector` makes and for the same
         * reason: the arrow-key contract, the group's accessible name and the announcement of
         * which option is current all come free with the real element. A pair of buttons with
         * `aria-pressed` would need every one of those written by hand.
         */}
        <fieldset className={styles.kinds}>
          <legend className={styles.legend}>Ledger</legend>
          <div className={styles.kindOptions}>
            {LEDGER_KINDS.map((option) => (
              <label key={option} className={styles.kindOption}>
                <input
                  type="radio"
                  className={styles.kindInput}
                  name={kindName}
                  value={option}
                  checked={kind === option}
                  onChange={() => {
                    setKind(option)
                  }}
                />
                <span className={styles.kindLabel}>{LEDGER_VOCABULARY[option].title}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <PeriodSelector value={period} onChange={setPeriod} />
      </div>

      <p className={styles.range} role="status">
        Showing {describeRange(range)}, in {hotel?.name ?? 'this property'}&rsquo;s own calendar
        ({timeZone}).
      </p>

      {/*
       * Keyed by kind so switching ledgers REMOUNTS rather than reuses. Without the key the
       * two sections would share position in the tree, and React would keep the previous
       * journal's rows on screen while the new one loaded -- expense lines under a heading
       * saying Revenue, for one frame.
       */}
      {kind === 'revenue' ? (
        <RevenueSection
          key="revenue"
          hotelPublicId={hotel?.public_id ?? null}
          range={range}
          timeZone={timeZone}
          currency={hotel?.currency ?? ''}
          today={today}
        />
      ) : (
        <ExpenseSection
          key="expenses"
          hotelPublicId={hotel?.public_id ?? null}
          range={range}
          timeZone={timeZone}
          currency={hotel?.currency ?? ''}
          today={today}
        />
      )}
    </Frame>
  )
}

/** The page heading, shared by every state so the frame never jumps. */
function Frame({ children }: { children: ReactNode }) {
  return (
    <PageContainer>
      <SectionHeader
        as="h1"
        title="Financials"
        description="The revenue and expense journals for this property. Both are append-only: a posted line is corrected by posting another, never by editing or deleting one."
      />
      {children}
    </PageContainer>
  )
}
