import { useState, type ReactNode } from 'react'
import { AlertTriangle, Building2, WifiOff } from 'lucide-react'

import { Card, CardBody, CardHeader } from '@/components/ui/Card'
import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { BreakdownTable, type BreakdownRow } from '@/features/analytics/BreakdownTable'
import { DemandForecastPanel } from '@/features/analytics/DemandForecastPanel'
import { countPoints, occupancyPoints } from '@/features/analytics/series'
import { useAnalyticsReport } from '@/features/analytics/useAnalyticsReport'
import { describeFailure } from '@/features/dashboard/failures'
import {
  currenciesIn,
  bucketFor,
  formatCount,
  formatMoneyCompact,
  formatRatioAsPercent,
} from '@/features/dashboard/format'
import { KpiCard } from '@/features/dashboard/KpiCard'
import { describeRange, periodOption, type PeriodId } from '@/features/dashboard/period'
import { PeriodSelector } from '@/features/dashboard/PeriodSelector'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { TrendChart, type TrendPoint } from '@/features/dashboard/TrendChart'
import { useHotelContext } from '@/session/HotelProvider'
import type { Hotel } from '@/types/hotel'

import styles from './AnalyticsPage.module.css'

/**
 * Period reporting: what the property did over a chosen window, and one estimate of what it
 * will do next.
 *
 * ## How this differs from the dashboard
 *
 * The dashboard answers *how are we doing right now* — a short window, a comparison against the
 * previous one, two trend lines. This screen answers *what happened over a period, and where did
 * the money come from and go*. It defaults to ninety days rather than seven, it breaks revenue
 * and expenses down by category, it charts the booking activity the dashboard has no room for,
 * and it is the only screen in the application that asks the trained demand model anything.
 *
 * ## Every figure is the server's
 *
 * Occupancy, ADR and RevPAR are the backend's own definitions, read from
 * `analytics/overview` and transcribed — `NULLIF` guards included, so an undefined ADR arrives
 * as `null` and renders as a dash rather than as `0.00`. The category tables print decimal
 * strings the server grouped; nothing here parses, sums, converts or rounds a money value.
 * There is no total row because the server sends no total, and adding EUR to USD would not
 * produce money.
 *
 * ## The one modelled number is fenced off
 *
 * `DemandForecastPanel` is visually and structurally separate from every recorded figure, says
 * "modelled estimate" in words, carries the model's version and its own `production_ready:
 * false`, and draws no confidence band — the served model is a point forecaster and a band
 * would be invented. See that component for the full reasoning.
 */
export function AnalyticsPage() {
  /* Ninety days by default: this is a reporting screen, and a category breakdown over seven
     days is mostly empty for a property that invoices monthly. */
  const [period, setPeriod] = useState<PeriodId>('last90')
  const hotelContext = useHotelContext()
  const hotel = hotelContext.selected
  const { status, data, error, range, retry } = useAnalyticsReport(hotel, period)

  const rangeLabel = range ? describeRange(range) : null

  if (hotelContext.status === 'empty') {
    return (
      <Shell period={period} onPeriodChange={setPeriod} hotel={null} rangeLabel={null} disabled>
        <StateMessage
          icon={Building2}
          tone="status"
          title="No hotel is linked to your account"
          detail="Reporting is per property, and this account is not yet a member of one. An owner or manager can add you to a hotel from its member list."
        />
      </Shell>
    )
  }

  if (hotelContext.status === 'error') {
    return (
      <Shell period={period} onPeriodChange={setPeriod} hotel={null} rangeLabel={null} disabled>
        <StateMessage
          icon={WifiOff}
          tone="alert"
          title="Could not load your hotels"
          detail="The list of properties you have access to could not be retrieved. Check your connection and try again."
          onRetry={hotelContext.retry}
        />
      </Shell>
    )
  }

  if (hotel === null) {
    return (
      <Shell period={period} onPeriodChange={setPeriod} hotel={null} rangeLabel={null} disabled>
        <LoadingReport />
      </Shell>
    )
  }

  if (status === 'error' && error !== null) {
    const notice = describeFailure(error)
    return (
      <Shell period={period} onPeriodChange={setPeriod} hotel={hotel} rangeLabel={rangeLabel}>
        <StateMessage
          icon={AlertTriangle}
          tone="alert"
          title={notice.title}
          detail={notice.detail}
          {...(notice.canRetry ? { onRetry: retry } : {})}
        />
      </Shell>
    )
  }

  if (status !== 'ready' || data === null) {
    return (
      <Shell period={period} onPeriodChange={setPeriod} hotel={hotel} rangeLabel={rangeLabel}>
        <LoadingReport />
      </Shell>
    )
  }

  const { overview, daily, revenue, expenses, forecast } = data
  const { occupancy } = overview

  /* The reporting currency: the first the server listed. Where a period holds more than one,
     the server says so with `is_multi_currency` and the page says so too rather than choosing
     silently. */
  const currencies = currenciesIn(overview.room_revenue)
  const primary = currencies[0] ?? null
  const roomRevenue = primary ? bucketFor(overview.room_revenue, primary) : null

  /* Built in `series.ts`, which is the only place this feature converts anything and explains
     why that is safe for a ratio and would not be for money. */
  const occupancySeries: TrendPoint[] = occupancyPoints(daily.days)
  const arrivalSeries: TrendPoint[] = countPoints(daily.days, 'arrivals')
  const bookingSeries: TrendPoint[] = countPoints(daily.days, 'bookings_created')

  const revenueRows: BreakdownRow[] = revenue.categories.map((row) => ({
    category_code: row.category_code,
    currency: row.currency,
    amount: row.amount,
    tax_amount: row.tax_amount,
    entry_count: row.entry_count,
    flag: row.is_room_revenue,
  }))

  const expenseRows: BreakdownRow[] = expenses.categories.map((row) => ({
    category_code: row.category_code,
    currency: row.currency,
    amount: row.amount,
    tax_amount: row.tax_amount,
    entry_count: row.entry_count,
    flag: row.is_fixed_cost,
  }))

  return (
    <Shell period={period} onPeriodChange={setPeriod} hotel={hotel} rangeLabel={rangeLabel}>
      {overview.is_multi_currency ? (
        <p className={styles.currencyNotice} role="status">
          This period holds entries in more than one currency. Headline figures below are shown in{' '}
          {primary ?? 'the first currency reported'}; each currency is listed separately in the
          breakdowns. Nothing is converted — the platform holds no exchange rates.
        </p>
      ) : null}

      <section aria-label="Performance">
        <SectionHeader title="Performance" />
        <div className={styles.kpiGrid}>
          <KpiCard
            label="Occupancy"
            value={formatRatioAsPercent(occupancy.occupancy_rate)}
            unavailableReason="No room nights were available in this period."
            caption={`${formatCount(occupancy.occupied_room_nights)} of ${formatCount(occupancy.available_room_nights)} room nights`}
          />
          <KpiCard
            label="ADR"
            value={
              roomRevenue && roomRevenue.adr !== null
                ? formatMoneyCompact(roomRevenue.adr, roomRevenue.currency)
                : null
            }
            unavailableReason="No room nights were sold in this period."
            caption="Average daily rate over nights sold"
          />
          <KpiCard
            label="RevPAR"
            value={
              roomRevenue && roomRevenue.revpar !== null
                ? formatMoneyCompact(roomRevenue.revpar, roomRevenue.currency)
                : null
            }
            unavailableReason="No room nights were available in this period."
            caption="Revenue per available room night"
          />
          <KpiCard
            label="Room revenue"
            value={
              roomRevenue
                ? formatMoneyCompact(roomRevenue.room_revenue, roomRevenue.currency)
                : null
            }
            unavailableReason="No room revenue was recorded in this period."
            caption="From nightly rates on occupied nights"
          />
        </div>
      </section>

      <section aria-label="Activity">
        <SectionHeader
          title="Activity"
          description="Recorded per day, from the server's own series."
        />
        <div className={styles.chartGrid}>
          <Card>
            <CardHeader title="Occupancy" />
            <CardBody>
              <TrendChart
                metric="Occupancy"
                points={occupancySeries}
                formatValue={(value) => `${Math.round(value)}%`}
                unit="%"
                emptyMessage="No occupancy was recorded in this period."
              />
            </CardBody>
          </Card>
          <Card>
            <CardHeader title="Arrivals" />
            <CardBody>
              <TrendChart
                metric="Arrivals"
                points={arrivalSeries}
                formatValue={(value) => String(value)}
                unit="arrivals"
                emptyMessage="No arrivals were recorded in this period."
              />
            </CardBody>
          </Card>
          <Card>
            <CardHeader title="Bookings created" />
            <CardBody>
              <TrendChart
                metric="Bookings created"
                points={bookingSeries}
                formatValue={(value) => String(value)}
                unit="bookings"
                emptyMessage="No bookings were created in this period."
              />
            </CardBody>
          </Card>
        </div>
      </section>

      <section aria-label="Revenue and expenses">
        <SectionHeader
          title="Revenue and expenses"
          description="Grouped by category and currency by the server. No overall total is shown: adding currencies together would not produce money."
        />
        <div className={styles.breakdownGrid}>
          <Card>
            <CardHeader title="Revenue by category" />
            <CardBody>
              <BreakdownTable
                caption={`Ledger revenue, ${rangeLabel ?? 'the selected period'}`}
                rows={revenueRows}
                flagLabel="Room revenue"
                emptyMessage="No revenue was posted in this period."
              />
            </CardBody>
          </Card>
          <Card>
            <CardHeader title="Expenses by category" />
            <CardBody>
              <BreakdownTable
                caption={`Ledger expenses, ${rangeLabel ?? 'the selected period'}`}
                rows={expenseRows}
                flagLabel="Fixed cost"
                emptyMessage="No expenses were posted in this period."
              />
            </CardBody>
          </Card>
        </div>
      </section>

      <section aria-label="Demand estimate">
        <SectionHeader
          title="Demand estimate"
          description="Produced by the trained model. Everything above is recorded; this is not."
        />
        <DemandForecastPanel outcome={forecast} isLoading={false} />
      </section>
    </Shell>
  )
}

interface ShellProps {
  readonly period: PeriodId
  readonly onPeriodChange: (period: PeriodId) => void
  readonly hotel: Hotel | null
  readonly rangeLabel: string | null
  readonly disabled?: boolean
  readonly children: ReactNode
}

/** The page frame, so every state renders the same header and control. */
function Shell({
  period,
  onPeriodChange,
  hotel,
  rangeLabel,
  disabled = false,
  children,
}: ShellProps) {
  const option = periodOption(period)
  return (
    <PageContainer>
      <div className={styles.header}>
        <div className={styles.headerText}>
          <h1 className={styles.title}>Analytics</h1>
          <p className={styles.subtitle}>
            {hotel
              ? `Reporting for ${hotel.name}, in the property's own calendar (${hotel.timezone}).`
              : 'Occupancy, ADR and RevPAR over a chosen period, with revenue and expense breakdowns.'}
          </p>
          {rangeLabel ? (
            <p className={styles.range}>
              {option.label} — <strong>{rangeLabel}</strong>, both dates included.
            </p>
          ) : null}
        </div>
        <PeriodSelector value={period} onChange={onPeriodChange} disabled={disabled} />
      </div>
      <div className={styles.sections}>{children}</div>
    </PageContainer>
  )
}

/** Pending state: the shape of the page, without inventing figures for it. */
function LoadingReport() {
  return (
    <div className={styles.loading} aria-busy="true" aria-live="polite">
      <p className={styles.loadingText}>Loading this period's figures…</p>
    </div>
  )
}
