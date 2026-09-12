import { useState, type ReactNode } from 'react'
import { AlertTriangle, FileX2, Plus, WifiOff } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import { ApiError } from '@/services/api/ApiError'
import { Pagination } from '@/features/bookings/Pagination'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { CategoryTotals } from '@/features/finance/CategoryTotals'
import { ExpenseForm } from '@/features/finance/ExpenseForm'
import { LedgerCards } from '@/features/finance/LedgerCards'
import { LedgerFilters } from '@/features/finance/LedgerFilters'
import { LedgerTable } from '@/features/finance/LedgerTable'
import { RevenueForm } from '@/features/finance/RevenueForm'
import { LEDGER_VOCABULARY, type LedgerKind } from '@/features/finance/vocabulary'
import { hasActiveFilters, useLedger, type LedgerState } from '@/features/finance/useLedger'
import { formatCount, formatDate, formatMoney } from '@/lib/format'
import { useIsCompact } from '@/lib/useIsCompact'
import { describeFailure } from '@/services/api/failures'
import {
  financeService,
  type BreakdownRange,
  type RevenueQuery,
} from '@/services/finance/financeService'
import type {
  ExpenseBucket,
  ExpenseCategory,
  ExpenseCreateRequest,
  ExpenseEntry,
  RevenueBucket,
  RevenueCategory,
  RevenueCreateRequest,
  RevenueEntry,
} from '@/types/finance'

import styles from './LedgerSection.module.css'

/**
 * One journal, end to end: the server's totals, the filtered page, and the posting form.
 *
 * ## Why the sources are module constants
 *
 * `useLedger` keys its effects on the source object. Building one inside the component would
 * make a new identity on every render and refetch the journal in a loop. They are frozen
 * here, once, and the methods are wrapped in arrows so nothing depends on `this`.
 *
 * ## Why the two kinds are two components
 *
 * They share the chrome and almost nothing else: the columns differ, the totals carry a
 * different flag, the forms have different fields, and only revenue has a booking. The
 * alternative -- one component holding a union of entry types -- buys shorter code with
 * casts at every point the union has to be narrowed, on a screen where getting a field wrong
 * means showing the wrong money. The shared parts are `LedgerChrome`, which both wrap.
 */

const REVENUE_SOURCE = {
  list: (hotel: string, query: RevenueQuery, signal?: AbortSignal) =>
    financeService.listRevenue(hotel, query, signal),
  create: (hotel: string, payload: RevenueCreateRequest, signal?: AbortSignal) =>
    financeService.createRevenue(hotel, payload, signal),
  breakdown: (hotel: string, range: BreakdownRange, signal?: AbortSignal) =>
    financeService.getRevenueBreakdown(hotel, range, signal),
  categories: (signal?: AbortSignal) => financeService.listRevenueCategories(signal),
} as const

const EXPENSE_SOURCE = {
  list: (hotel: string, query: RevenueQuery, signal?: AbortSignal) =>
    financeService.listExpenses(hotel, query, signal),
  create: (hotel: string, payload: ExpenseCreateRequest, signal?: AbortSignal) =>
    financeService.createExpense(hotel, payload, signal),
  breakdown: (hotel: string, range: BreakdownRange, signal?: AbortSignal) =>
    financeService.getExpenseBreakdown(hotel, range, signal),
  categories: (signal?: AbortSignal) => financeService.listExpenseCategories(signal),
} as const

/** Every option is inside the routers' own `ge=1, le=100`, so none can produce a 422. */
const PAGE_SIZE_OPTIONS = [20, 50, 100] as const

export interface LedgerSectionProps {
  readonly hotelPublicId: string | null
  readonly range: BreakdownRange
  readonly timeZone: string
  readonly currency: string
  /** Today in the hotel's own zone, as the posting form's default date. */
  readonly today: string
}

export function RevenueSection(props: LedgerSectionProps) {
  const ledger = useLedger<RevenueEntry, RevenueBucket, RevenueCreateRequest, RevenueCategory>({
    hotelPublicId: props.hotelPublicId,
    range: props.range,
    source: REVENUE_SOURCE,
    entryDate: (entry) => entry.revenue_date,
    postedMessage: 'Revenue line posted.',
  })

  return (
    <LedgerChrome
      kind="revenue"
      ledger={ledger}
      range={props.range}
      postedSummary={
        ledger.posted === null
          ? null
          : `${formatMoney(ledger.posted.entry.amount, ledger.posted.entry.currency)} of ${
              ledger.posted.entry.category_code
            } dated ${formatDate(ledger.posted.entry.revenue_date)}`
      }
      totals={
        ledger.breakdown === null ? null : (
          <CategoryTotals
            kind="revenue"
            buckets={ledger.breakdown.categories}
            range={ledger.breakdown.range}
          />
        )
      }
      rows={(compact) =>
        compact ? (
          <LedgerCards kind="revenue" entries={ledger.entries} timeZone={props.timeZone} />
        ) : (
          <LedgerTable kind="revenue" entries={ledger.entries} timeZone={props.timeZone} />
        )
      }
      form={(close) => (
        <RevenueForm
          categories={ledger.categories}
          categoriesUnavailable={ledger.categoriesUnavailable}
          defaultCurrency={props.currency}
          today={props.today}
          busy={ledger.pending}
          onDirty={ledger.dismiss}
          onCancel={close}
          onSubmit={(payload) => {
            void ledger.post(payload).then((accepted) => {
              if (accepted) {
                close()
              }
            })
          }}
        />
      )}
    />
  )
}

export function ExpenseSection(props: LedgerSectionProps) {
  const ledger = useLedger<ExpenseEntry, ExpenseBucket, ExpenseCreateRequest, ExpenseCategory>({
    hotelPublicId: props.hotelPublicId,
    range: props.range,
    source: EXPENSE_SOURCE,
    entryDate: (entry) => entry.expense_date,
    postedMessage: 'Expense line posted.',
  })

  return (
    <LedgerChrome
      kind="expenses"
      ledger={ledger}
      range={props.range}
      postedSummary={
        ledger.posted === null
          ? null
          : `${formatMoney(ledger.posted.entry.amount, ledger.posted.entry.currency)} of ${
              ledger.posted.entry.category_code
            } dated ${formatDate(ledger.posted.entry.expense_date)}`
      }
      totals={
        ledger.breakdown === null ? null : (
          <CategoryTotals
            kind="expenses"
            buckets={ledger.breakdown.categories}
            range={ledger.breakdown.range}
          />
        )
      }
      rows={(compact) =>
        compact ? (
          <LedgerCards kind="expenses" entries={ledger.entries} timeZone={props.timeZone} />
        ) : (
          <LedgerTable kind="expenses" entries={ledger.entries} timeZone={props.timeZone} />
        )
      }
      form={(close) => (
        <ExpenseForm
          categories={ledger.categories}
          categoriesUnavailable={ledger.categoriesUnavailable}
          defaultCurrency={props.currency}
          today={props.today}
          busy={ledger.pending}
          onDirty={ledger.dismiss}
          onCancel={close}
          onSubmit={(payload) => {
            void ledger.post(payload).then((accepted) => {
              if (accepted) {
                close()
              }
            })
          }}
        />
      )}
    />
  )
}

/** What a 404 means on each request this section makes. Never the backend's own words. */
const LEDGER_FAILURE_COPY = {
  notFound: {
    title: 'Nothing to show for that filter',
    detail:
      'The property, the category or the booking asked for could not be found. A category that has been retired, or an identifier typed from memory, will read this way — check the filter and try again.',
    canRetry: false,
  },
  serverFault: {
    title: 'The ledger is temporarily unavailable',
    detail: 'The journal could not be read. Nothing has been changed by this.',
    canRetry: true,
  },
} as const

const POSTING_FAILURE_COPY = {
  forbidden: {
    title: 'Nothing was posted',
    detail:
      'Posting to this journal needs the staff role on this property, which this account does not have. Reading it does not — so the lines above are complete, and nothing was written.',
    canRetry: false,
  },
  notFound: {
    title: 'Nothing was posted',
    detail:
      'The category or the booking named on the line could not be found, so the line was not recorded. Check both and post it again.',
    canRetry: false,
  },
  serverFault: {
    title: 'Nothing was posted',
    detail:
      'The line could not be recorded. Nothing was written to the journal, so posting it again will not create a duplicate of a line that got through.',
    canRetry: true,
  },
} as const

const TOTALS_FAILURE_COPY = {
  notFound: {
    title: 'Totals unavailable',
    detail: 'The property asked for could not be found.',
    canRetry: false,
  },
  serverFault: {
    title: 'Totals are temporarily unavailable',
    detail:
      'The server could not total this period. The journal below is unaffected — but it is a page of lines, not a total, and nothing here adds it up.',
    canRetry: true,
  },
} as const

interface LedgerChromeProps<TEntry, TBucket, TCreate, TCategory> {
  readonly kind: LedgerKind
  readonly ledger: LedgerState<TEntry, TBucket, TCreate, TCategory>
  readonly range: BreakdownRange
  /** The posted line restated from the server's own response, for the confirmation. */
  readonly postedSummary: string | null
  readonly totals: ReactNode
  readonly rows: (compact: boolean) => ReactNode
  readonly form: (close: () => void) => ReactNode
}

/**
 * Everything the two journals render identically.
 *
 * The order on screen is the order of the questions: what did this period come to, what
 * exactly is in it, and then -- last, behind a button -- add to it. A form sitting open above
 * a financial record invites posting before reading.
 */
function LedgerChrome<TEntry, TBucket, TCreate, TCategory>({
  kind,
  ledger,
  range,
  postedSummary,
  totals,
  rows,
  form,
}: LedgerChromeProps<TEntry, TBucket, TCreate, TCategory>) {
  const [open, setOpen] = useState(false)
  const isCompact = useIsCompact()
  const vocabulary = LEDGER_VOCABULARY[kind]

  const failure = ledger.error === null ? null : describeFailure(ledger.error, LEDGER_FAILURE_COPY)
  const postFailure =
    ledger.postError === null ? null : describeFailure(ledger.postError, POSTING_FAILURE_COPY)
  const totalsFailure =
    ledger.breakdownError === null
      ? null
      : describeFailure(ledger.breakdownError, TOTALS_FAILURE_COPY)

  const close = () => {
    setOpen(false)
  }

  return (
    <div className={styles.section}>
      {/* --- the server's totals ---------------------------------------------------------- */}

      <section className={styles.block} aria-labelledby={`${kind}-totals`}>
        <h3 className={styles.blockTitle} id={`${kind}-totals`}>
          Totals for this period
        </h3>
        <p className={styles.blockNote}>
          Computed by the server, grouped by category and currency. The journal&rsquo;s filters
          below do not narrow them &mdash; the totals endpoint accepts a date range and nothing
          else &mdash; and no overall figure is shown, because none is reported and adding
          currencies together would not produce money.
        </p>
        {totalsFailure ? (
          <div className={styles.failure} role="alert">
            <AlertTriangle size={16} aria-hidden="true" />
            <div>
              <p className={styles.failureTitle}>{totalsFailure.title}</p>
              <p className={styles.failureDetail}>{totalsFailure.detail}</p>
            </div>
          </div>
        ) : (
          totals
        )}
      </section>

      {/* --- the journal ------------------------------------------------------------------ */}

      <section className={styles.block} aria-labelledby={`${kind}-journal`}>
        <div className={styles.blockHeader}>
          <div>
            <h3 className={styles.blockTitle} id={`${kind}-journal`}>
              {vocabulary.title} journal
            </h3>
            <p className={styles.blockNote}>{vocabulary.summary}</p>
          </div>
          {open ? null : (
            <Button
              variant="primary"
              size="sm"
              onClick={() => {
                ledger.dismiss()
                setOpen(true)
              }}
            >
              <Plus size={16} aria-hidden="true" />
              Post {vocabulary.lineNoun}
            </Button>
          )}
        </div>

        {ledger.posted ? (
          <p className={styles.success} role="status">
            {ledger.posted.message} The server recorded {postedSummary}.
            {ledger.posted.outsideRange
              ? ' It falls outside the period shown, so it does not appear in the journal below.'
              : ''}
          </p>
        ) : null}

        {postFailure ? (
          <div className={styles.failure} role="alert">
            <AlertTriangle size={16} aria-hidden="true" />
            <div>
              <p className={styles.failureTitle}>{postFailure.title}</p>
              <p className={styles.failureDetail}>{postFailure.detail}</p>
            </div>
          </div>
        ) : null}

        {open ? <div className={styles.formPanel}>{form(close)}</div> : null}

        <LedgerFilters
          kind={kind}
          categories={ledger.categories as readonly { code: string; name: string }[]}
          categoriesUnavailable={ledger.categoriesUnavailable}
          value={ledger.filters}
          onChange={ledger.setFilters}
          disabled={ledger.status === 'loading'}
        />

        {failure ? (
          <StateMessage
            icon={ledger.error?.code === ApiError.NETWORK_CODE ? WifiOff : AlertTriangle}
            tone="alert"
            title={failure.title}
            detail={failure.detail}
            {...(failure.canRetry ? { onRetry: ledger.reload } : {})}
          />
        ) : ledger.status === 'loading' ? (
          <p className={styles.loading} role="status" aria-busy="true">
            Loading the {vocabulary.title.toLowerCase()} journal&hellip;
          </p>
        ) : ledger.entries.length === 0 ? (
          <StateMessage
            icon={FileX2}
            tone="status"
            title={
              hasActiveFilters(ledger.filters)
                ? 'No lines match those filters'
                : `No ${vocabulary.title.toLowerCase()} was posted in this period`
            }
            detail={
              hasActiveFilters(ledger.filters)
                ? `Between ${formatDate(range.dateFrom)} and ${formatDate(range.dateTo)}, no line matches. The server searched the whole period, not just this page.`
                : `The server reported no lines between ${formatDate(range.dateFrom)} and ${formatDate(range.dateTo)}. That is a period with no entries, not a period totalling zero.`
            }
          />
        ) : (
          <>
            <p className={styles.count} role="status">
              {formatCount(ledger.total)} {ledger.total === 1 ? 'line' : 'lines'} in this period
              {hasActiveFilters(ledger.filters) ? ', matching the filters' : ''}. Counted by the
              server across every page.
            </p>
            {rows(isCompact)}
            <Pagination
              page={ledger.page}
              pageSize={ledger.pageSize}
              total={ledger.total}
              pages={ledger.pages}
              onPageChange={ledger.setPage}
              onPageSizeChange={ledger.setPageSize}
              pageSizeOptions={PAGE_SIZE_OPTIONS}
              itemNoun={{ singular: 'line', plural: 'lines' }}
              /* No `disabled`: this branch renders only once the page is `ready`, so the
               * loading state cannot be reached here -- TypeScript narrows it away. */
            />
          </>
        )}
      </section>
    </div>
  )
}
