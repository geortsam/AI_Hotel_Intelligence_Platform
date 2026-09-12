import { useState, type ReactNode } from 'react'
import { AlertTriangle, Building2, CalendarOff, WifiOff } from 'lucide-react'

import { Badge } from '@/components/ui/Badge'
import { Card, CardBody, CardHeader } from '@/components/ui/Card'
import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { describeFailure } from '@/features/dashboard/failures'
import {
  bucketFor,
  computeDelta,
  currenciesIn,
  formatBucket,
  formatCount,
  formatMoney,
  formatMoneyCompact,
  formatRating,
  formatRatioAsPercent,
} from '@/features/dashboard/format'
import { KpiCard } from '@/features/dashboard/KpiCard'
import {
  DEFAULT_PERIOD,
  describeRange,
  periodOption,
  type PeriodId,
} from '@/features/dashboard/period'
import { PeriodSelector } from '@/features/dashboard/PeriodSelector'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { TrendChart, type TrendPoint } from '@/features/dashboard/TrendChart'
import { useDashboardData } from '@/features/dashboard/useDashboardData'
import { toAnalyticsRange } from '@/services/analytics/analyticsService'
import { useHotelContext } from '@/session/HotelProvider'
import type { DailySeriesResponse, OverviewResponse } from '@/types/analytics'
import type { Hotel } from '@/types/hotel'

import styles from './DashboardPage.module.css'

/**
 * The hotel intelligence dashboard.
 *
 * Every figure on this page came from `GET /hotels/{id}/analytics/...` and none was computed
 * here. Occupancy, ADR and RevPAR are the backend's own definitions -- transcribed from
 * `daily_hotel_metrics`'s generated columns, `NULLIF` guards included -- so the dashboard and
 * any report built later cannot disagree about what ADR means. The one derived number is the
 * period-over-period change, and it comes from two authoritative values for two adjacent,
 * equal-length windows; see `computeDelta` and `previousRange`.
 *
 * Three things this page refuses to do, each easy to do by accident:
 *
 * * **It never shows a number the API did not send.** A metric the backend called undefined
 *   -- `adr: null`, or an absent `room_revenue` bucket -- renders as a dash with a reason,
 *   never as `0.00`.
 * * **It never merges currencies.** There is no FX rate anywhere in this system. Money is
 *   reported per currency, and when a range holds more than one the page says so and lists
 *   them separately rather than producing a total that is not money.
 * * **It never fills a failure with zeroes.** A failed request replaces the figures with an
 *   explanation, because a grid of zeroes is indistinguishable from a hotel that sold
 *   nothing.
 */
export function DashboardPage() {
  const [period, setPeriod] = useState<PeriodId>(DEFAULT_PERIOD)
  const hotelContext = useHotelContext()
  const hotel = hotelContext.selected
  const { status, data, error, range, retry } = useDashboardData(hotel, period)

  const selectedPeriod = periodOption(period)

  /* --- no hotel to report on ----------------------------------------------------------- */

  if (hotelContext.status === 'empty') {
    return (
      <Shell period={period} onPeriodChange={setPeriod} hotel={null} rangeLabel={null} disabled>
        <StateMessage
          icon={Building2}
          tone="status"
          title="No hotel is linked to your account"
          detail="Analytics are reported per property, and this account is not yet a member of one. An owner or manager can add you to a hotel from its member list."
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
    // `idle` or `loading`: the membership query has not answered yet. The shell and the
    // period control are already on screen; only the figures are pending.
    return (
      <Shell period={period} onPeriodChange={setPeriod} hotel={null} rangeLabel={null} disabled>
        <LoadingBoard />
      </Shell>
    )
  }

  const rangeLabel = range ? describeRange(range) : null

  /* --- the hotel is known -------------------------------------------------------------- */

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
        <LoadingBoard />
      </Shell>
    )
  }

  return (
    <Shell
      period={period}
      onPeriodChange={setPeriod}
      hotel={hotel}
      /*
       * Labelled from the range the RESPONSE echoes, not the one that was requested.
       *
       * The two are the same once data arrives, but not in the frame between clicking a new
       * period and the fetch effect running: React re-renders with the new selection while
       * the previous period's figures are still on screen, and labelling from the request
       * put "12 Aug – 10 Sept" above seven days of data. Brief, but it is a dashboard
       * describing its own numbers wrongly, which is the one thing it must never do. The API
       * echoes `range` so a response is self-describing; this is what that is for.
       */
      rangeLabel={describeRange(toAnalyticsRange(data.overview.range))}
    >
      <Board
        hotel={hotel}
        overview={data.overview}
        previous={data.previous}
        daily={data.daily}
        periodLabel={selectedPeriod.label}
      />
    </Shell>
  )
}

/* -------------------------------------------------------------------------------------- */

interface ShellProps {
  readonly period: PeriodId
  readonly onPeriodChange: (period: PeriodId) => void
  readonly hotel: Hotel | null
  readonly rangeLabel: string | null
  readonly disabled?: boolean
  readonly children: ReactNode
}

/**
 * The page frame: heading, period control, and the range actually requested.
 *
 * Held constant across every state, so switching period or hitting a failure does not move
 * the controls under the pointer. The range line is what makes a figure here checkable --
 * "58.0%" means nothing without "4 – 10 Sep 2026" -- and the hotel's time zone is named
 * because that is the calendar these dates belong to, not the reader's.
 */
function Shell({ period, onPeriodChange, hotel, rangeLabel, disabled, children }: ShellProps) {
  return (
    <PageContainer>
      <SectionHeader
        as="h2"
        title="Overview"
        description={
          hotel
            ? `Performance for ${hotel.name}, measured in the property's own calendar (${hotel.timezone}).`
            : 'Property performance at a glance.'
        }
        actions={
          <PeriodSelector value={period} onChange={onPeriodChange} disabled={disabled ?? false} />
        }
      />

      {rangeLabel ? (
        <p className={styles.rangeLine}>
          Reporting on <strong>{rangeLabel}</strong>, both dates included.
        </p>
      ) : null}

      {children}
    </PageContainer>
  )
}

/** Skeleton tiles the size of the real ones, so nothing shifts when the figures land. */
function LoadingBoard() {
  return (
    <>
      <dl className={styles.grid} aria-busy="true" aria-label="Loading key figures">
        {['Occupancy', 'ADR', 'RevPAR', 'Room revenue'].map((label) => (
          <KpiCard key={label} label={label} value={null} isLoading />
        ))}
      </dl>
      <Card className={styles.chartCard}>
        <p className={styles.loadingNote} role="status">
          Loading analytics…
        </p>
      </Card>
    </>
  )
}

interface BoardProps {
  readonly hotel: Hotel
  readonly overview: OverviewResponse
  readonly previous: OverviewResponse | null
  readonly daily: DailySeriesResponse
  readonly periodLabel: string
}

/** True when the range genuinely holds nothing, as distinct from failing to load. */
function isEmptyPeriod(overview: OverviewResponse): boolean {
  return (
    overview.occupancy.occupied_room_nights === 0 &&
    overview.room_revenue.length === 0 &&
    overview.other_revenue.length === 0 &&
    overview.total_expenses.length === 0 &&
    overview.bookings_by_stay.total === 0 &&
    overview.reviews.review_count === 0
  )
}

function Board({ hotel, overview, previous, daily, periodLabel }: BoardProps) {
  /* The day count comes from the response too, for the same reason the range label does:
   * it describes the figures being shown rather than the selection that asked for them. */
  const periodDays = overview.range.days

  /*
   * Which currency the headline tiles are in.
   *
   * The hotel's configured currency when the range actually contains it, otherwise the first
   * the backend returned -- its bucket lists are sorted by code, so "first" is deterministic
   * rather than arbitrary. When the range is empty there is no currency in the data at all,
   * and the hotel's own is used purely to label a set of dashes.
   */
  const currencies = currenciesIn(
    overview.room_revenue,
    overview.other_revenue,
    overview.total_expenses,
    overview.net_operating_result,
  )
  const currency = currencies.includes(hotel.currency)
    ? hotel.currency
    : (currencies[0] ?? hotel.currency)

  const room = bucketFor(overview.room_revenue, currency)
  const previousRoom = previous ? bucketFor(previous.room_revenue, currency) : null

  const deltaLabel = `vs previous ${periodDays === 1 ? 'day' : `${periodDays} days`}`
  const empty = isEmptyPeriod(overview)

  const occupancyTrend: TrendPoint[] = daily.days.map((day) => ({
    date: day.date,
    value: day.occupancy_rate === null ? null : Number(day.occupancy_rate) * 100,
  }))

  const revenueTrend: TrendPoint[] = daily.days.map((day) => {
    const bucket = bucketFor(day.room_revenue, currency)
    // A day with no occupied nights has no bucket at all. For revenue that is a true zero --
    // no nights were sold, so none was earned -- unlike ADR, which is genuinely undefined.
    // Plotting it as 0 is therefore reporting a measurement, not inventing one.
    return { date: day.date, value: bucket === null ? 0 : Number(bucket.room_revenue) }
  })

  return (
    <>
      {empty ? (
        <div className={styles.notice}>
          <StateMessage
            icon={CalendarOff}
            tone="status"
            title={`No activity in the ${periodLabel.toLowerCase()}`}
            detail="This hotel recorded no room nights, ledger entries or reviews in this period. The figures below are measured, not missing."
          />
        </div>
      ) : null}

      {overview.is_multi_currency ? (
        <p className={styles.currencyNotice} role="status">
          <Badge tone="warning">Multiple currencies</Badge>
          <span>
            This period holds entries in {currencies.join(', ')}. Headline figures are shown in{' '}
            {currency}; each currency is listed separately below. Nothing is converted &mdash; the
            platform holds no exchange rates.
          </span>
        </p>
      ) : null}

      <h3 className={styles.sectionTitle}>Performance</h3>
      <dl className={styles.grid}>
        <KpiCard
          label="Occupancy"
          value={formatRatioAsPercent(overview.occupancy.occupancy_rate)}
          caption={`${formatCount(overview.occupancy.occupied_room_nights)} of ${formatCount(
            overview.occupancy.available_room_nights,
          )} room nights`}
          unavailableReason="The hotel has no active rooms, so occupancy is undefined."
          delta={computeDelta(
            overview.occupancy.occupancy_rate,
            previous?.occupancy.occupancy_rate ?? null,
          )}
          deltaLabel={deltaLabel}
        />
        <KpiCard
          label="ADR"
          value={room?.adr ? formatMoney(room.adr, currency) : null}
          caption={`Average daily rate over ${formatCount(
            overview.occupancy.room_nights_sold,
          )} nights sold`}
          unavailableReason="No room nights were sold in this period, so the average rate is undefined."
          delta={computeDelta(room?.adr ?? null, previousRoom?.adr ?? null)}
          deltaLabel={deltaLabel}
        />
        <KpiCard
          label="RevPAR"
          value={room?.revpar ? formatMoney(room.revpar, currency) : null}
          caption={`Revenue per available room night (${formatCount(
            overview.occupancy.available_room_nights,
          )} available)`}
          unavailableReason="The hotel has no active rooms, so RevPAR is undefined."
          delta={computeDelta(room?.revpar ?? null, previousRoom?.revpar ?? null)}
          deltaLabel={deltaLabel}
        />
        <KpiCard
          label="Room revenue"
          value={room ? formatMoneyCompact(room.room_revenue, currency) : null}
          caption="From nightly rates on occupied nights"
          unavailableReason="No room nights were occupied in this period."
          delta={computeDelta(room?.room_revenue ?? null, previousRoom?.room_revenue ?? null)}
          deltaLabel={deltaLabel}
        />
      </dl>

      <h3 className={styles.sectionTitle}>Revenue and result</h3>
      <dl className={styles.grid}>
        <KpiCard
          label="Other revenue"
          value={
            bucketFor(overview.other_revenue, currency)
              ? formatBucket(overview.other_revenue, currency)
              : null
          }
          caption="Ledger revenue outside room charges"
          unavailableReason="No non-room revenue was recorded in this period."
        />
        <KpiCard
          label="Expenses"
          value={
            bucketFor(overview.total_expenses, currency)
              ? formatBucket(overview.total_expenses, currency)
              : null
          }
          caption="Ledger expenses recorded in this period"
          unavailableReason="No expenses were recorded in this period."
        />
        <KpiCard
          label="Net operating result"
          value={
            bucketFor(overview.net_operating_result, currency)
              ? formatBucket(overview.net_operating_result, currency)
              : null
          }
          caption="Room and other revenue, less expenses, within this currency"
          unavailableReason="There is nothing in this currency to net off."
        />
        <KpiCard
          label="Guest rating"
          value={
            overview.reviews.average_rating_normalized === null
              ? null
              : formatRating(overview.reviews.average_rating_normalized)
          }
          caption={`${formatCount(overview.reviews.review_count)} review${
            overview.reviews.review_count === 1 ? '' : 's'
          }, ${formatCount(overview.reviews.published_count)} published`}
          unavailableReason="No reviews were dated in this period."
        />
      </dl>

      <div className={styles.charts}>
        <Card padded={false}>
          <CardHeader title="Occupancy trend" />
          <CardBody>
            <TrendChart
              metric="Occupancy"
              points={occupancyTrend}
              unit="percent"
              formatValue={(value) => `${value.toFixed(0)}%`}
              emptyMessage="Occupancy could not be calculated for any day in this period, because the hotel has no active rooms."
            />
          </CardBody>
        </Card>

        <Card padded={false}>
          <CardHeader title={`Room revenue trend (${currency})`} />
          <CardBody>
            <TrendChart
              metric="Room revenue"
              points={revenueTrend}
              unit={currency}
              formatValue={(value) =>
                new Intl.NumberFormat('en-GB', {
                  notation: 'compact',
                  maximumSignificantDigits: 3,
                }).format(value)
              }
              emptyMessage={`No room revenue in ${currency} was recorded on any day in this period.`}
            />
          </CardBody>
        </Card>
      </div>

      <div className={styles.charts}>
        <Card padded={false}>
          <CardHeader title="Stay activity" />
          <CardBody>
            <dl className={styles.facts}>
              <dt>Arrivals</dt>
              <dd>{formatCount(overview.stay_flow.arrivals)}</dd>
              <dt>Departures</dt>
              <dd>{formatCount(overview.stay_flow.departures)}</dd>
              <dt>Cancellations</dt>
              <dd>{formatCount(overview.stay_flow.cancellations)}</dd>
              <dt>Bookings staying</dt>
              <dd>{formatCount(overview.bookings_by_stay.total)}</dd>
              <dt>Bookings taken</dt>
              <dd>{formatCount(overview.bookings_created.total)}</dd>
              <dt>Room nights sold</dt>
              <dd>{formatCount(overview.occupancy.room_nights_sold)}</dd>
            </dl>
            <p className={styles.footnote}>
              &ldquo;Staying&rdquo; counts bookings whose stay overlaps this period;
              &ldquo;taken&rdquo; counts bookings created in it. Neither answers the
              other&rsquo;s question.
            </p>
          </CardBody>
        </Card>

        <Card padded={false}>
          <CardHeader title="How these figures are measured" />
          <CardBody>
            <ul className={styles.notes}>
              <li>
                Occupancy is occupied room nights divided by available room nights.
                Complimentary nights count as occupied; ADR excludes them.
              </li>
              <li>
                Available room nights use the hotel&rsquo;s{' '}
                <strong>
                  {overview.occupancy.available_room_nights_basis === 'current_active_rooms'
                    ? 'current active room count'
                    : overview.occupancy.available_room_nights_basis}
                </strong>
                . The database keeps no history of room activation, so capacity for a past
                period is exact only if the inventory has not changed since.
              </li>
              <li>
                Room revenue comes from the nightly rates stored on each stay night, not from
                the ledger. A ledger entry posted to a room-revenue category is reported
                separately by the API and is not added here.
              </li>
              <li>
                Comparisons are against the{' '}
                {periodDays === 1 ? 'previous day' : `previous ${periodDays} days`}, an
                equal-length window ending the day before this one starts. No comparison is
                shown where either period&rsquo;s figure is undefined.
              </li>
            </ul>
          </CardBody>
        </Card>
      </div>

      {overview.is_multi_currency ? (
        <Card padded={false}>
          <CardHeader title="By currency" />
          <CardBody>
            <div className={styles.tableScroll}>
              <table className={styles.currencyTable}>
                <caption className={styles.tableCaption}>
                  Each currency is reported on its own. No exchange rate is applied anywhere.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Currency</th>
                    <th scope="col">Room revenue</th>
                    <th scope="col">ADR</th>
                    <th scope="col">RevPAR</th>
                    <th scope="col">Other revenue</th>
                    <th scope="col">Expenses</th>
                    <th scope="col">Net</th>
                  </tr>
                </thead>
                <tbody>
                  {currencies.map((code) => {
                    const bucket = bucketFor(overview.room_revenue, code)
                    return (
                      <tr key={code}>
                        <th scope="row">{code}</th>
                        <td>{bucket ? formatMoney(bucket.room_revenue, code) : '—'}</td>
                        <td>{bucket?.adr ? formatMoney(bucket.adr, code) : '—'}</td>
                        <td>{bucket?.revpar ? formatMoney(bucket.revpar, code) : '—'}</td>
                        <td>{formatBucket(overview.other_revenue, code)}</td>
                        <td>{formatBucket(overview.total_expenses, code)}</td>
                        <td>{formatBucket(overview.net_operating_result, code)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </CardBody>
        </Card>
      ) : null}
    </>
  )
}
