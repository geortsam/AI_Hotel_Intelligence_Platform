import { api } from '@/services/api/client'
import type { Page } from '@/types/api'
import type {
  ExpenseBreakdownResponse,
  ExpenseCategory,
  ExpenseCreateRequest,
  ExpenseEntry,
  RevenueBreakdownResponse,
  RevenueCategory,
  RevenueCreateRequest,
  RevenueEntry,
} from '@/types/finance'

/**
 * The finance calls this application makes, and the complete set the contract offers.
 *
 * ## Two journals, POST and GET, and nothing else
 *
 * `/hotels/{h}/revenue` and `/hotels/{h}/expenses` are append-only. Their routers expose a
 * create and a list; there is no PATCH, no DELETE and **no single-entry URL**, because
 * neither table has a `public_id` to put in one. Verified live: PATCH and DELETE on both
 * collections answer **405**, and `GET .../revenue/1` is a 404 rather than a row.
 *
 * Nothing here wraps a verb that does not exist. The absence is the reason the UI offers no
 * edit and no delete control -- not a decision this layer took, a capability the API does not
 * have.
 *
 * ## Totals come from the analytics breakdowns, never from the journal
 *
 * The list endpoints return `Page<T>` with the server's own `total` -- a **row count**, not a
 * sum of money. The only monetary totals this application shows come from
 * `analytics/revenue-by-category` and `analytics/expenses-by-category`, which aggregate in
 * PostgreSQL, grouped by `(category_code, currency)`. Both require an explicit range.
 *
 * ## Authorization, verified live
 *
 * Reading either journal requires **membership**; posting to either requires
 * **`HotelRole.STAFF`**, declared on the routers. The two category catalogues sit outside the
 * hotel segment and are readable by any authenticated user; writing one needs a platform
 * grant, so a hotel owner receives 403 -- which is why no catalogue management is wrapped.
 */

/** From the routers' own `page_size` default. */
export const DEFAULT_PAGE_SIZE = 20
/** The routers' `le=100`. Asking for more is a 422. */
export const MAX_PAGE_SIZE = 100
/**
 * What the category routes are asked for in one page.
 *
 * The same `MAX_PAGE_SIZE = 100` as every other list -- the category routers import the very
 * constant `app/services/finance.py` defines. Asking for 200 is a **422**, which is how this
 * was found: not by reading the source, which is what produced the wrong guess, but by
 * watching the real request fail in a browser and leave the category control empty.
 *
 * Both seeded catalogues hold fewer than ten codes, so one page is the whole catalogue. If a
 * deployment ever exceeded a hundred, the controls would show the first hundred by code --
 * and the forms fall back to a typed code, which the server validates either way.
 */
export const CATEGORY_PAGE_SIZE = 100

/**
 * The filters the revenue list actually supports.
 *
 * Exactly these. An unsupported query parameter is **silently ignored** -- verified: adding
 * `currency=USD` returned every row -- so a filter this application does not send is a filter
 * that does not exist, and offering one in the UI would be showing unfiltered data under a
 * filtered heading.
 */
export interface RevenueQuery {
  readonly page: number
  readonly pageSize: number
  readonly categoryCode?: string
  /** `YYYY-MM-DD`, inclusive. */
  readonly dateFrom?: string
  /** `YYYY-MM-DD`, inclusive. */
  readonly dateTo?: string
  /** A booking's UUID. Revenue only -- expenses have no booking column. */
  readonly bookingPublicId?: string
}

/** The expense list's filters. The same, minus the booking. */
export type ExpenseQuery = Omit<RevenueQuery, 'bookingPublicId'>

/** The inclusive range a breakdown covers. Both bounds required; neither has a default. */
export interface BreakdownRange {
  readonly dateFrom: string
  readonly dateTo: string
}

function listQuery(query: RevenueQuery): Record<string, string | number> {
  return {
    page: query.page,
    page_size: query.pageSize,
    // An absent filter is omitted rather than sent empty: `category_code=` is not the same
    // request as no `category_code`, and the second is the one that means "all".
    ...(query.categoryCode ? { category_code: query.categoryCode } : {}),
    ...(query.dateFrom ? { date_from: query.dateFrom } : {}),
    ...(query.dateTo ? { date_to: query.dateTo } : {}),
    ...(query.bookingPublicId ? { booking_public_id: query.bookingPublicId } : {}),
  }
}

export const financeService = {
  /**
   * One page of the hotel's revenue journal, **newest first**.
   *
   * The ordering is the backend's (`revenue_date DESC, created_at DESC`) and is not
   * configurable.
   *
   * Throws `ApiError`. Two 404s are worth distinguishing from an empty result, because they
   * mean the filter itself was wrong rather than that nothing matched: an **unknown
   * `category_code`** and an **unknown `booking_public_id`** are both 404, while a filter
   * that simply matches nothing is a 200 with zero rows. Both verified live.
   */
  listRevenue(
    hotelPublicId: string,
    query: RevenueQuery,
    signal?: AbortSignal,
  ): Promise<Page<RevenueEntry>> {
    return api.get<Page<RevenueEntry>>(`/hotels/${hotelPublicId}/revenue`, {
      query: listQuery(query),
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Post one revenue line.
   *
   * Throws `ApiError`: **403** without the staff role, **404** for an unknown category or an
   * unknown booking, **422** for a negative `tax_amount`, more than two decimal places, or
   * any field the schema does not have.
   *
   * A **negative `amount` is accepted**, and that is the contract's way of correcting a
   * mistake. There is no 409 here: the journal has no uniqueness constraint, so posting the
   * same line twice creates two rows -- which is why nothing in this application retries a
   * failed write automatically.
   */
  createRevenue(
    hotelPublicId: string,
    payload: RevenueCreateRequest,
    signal?: AbortSignal,
  ): Promise<RevenueEntry> {
    return api.post<RevenueEntry>(`/hotels/${hotelPublicId}/revenue`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * One page of the expense journal, newest first. Same rules as revenue, minus the booking.
   *
   * The booking filter is stripped rather than merely absent from the type. `expenses` has
   * no `booking_id` column, so `booking_public_id` is not a filter that matches nothing --
   * it is a parameter the endpoint does not know, and unknown parameters are **ignored
   * silently**. Sending one would return the unfiltered journal under a filtered heading.
   */
  listExpenses(
    hotelPublicId: string,
    query: ExpenseQuery,
    signal?: AbortSignal,
  ): Promise<Page<ExpenseEntry>> {
    const { bookingPublicId: _ignored, ...rest } = query as RevenueQuery
    return api.get<Page<ExpenseEntry>>(`/hotels/${hotelPublicId}/expenses`, {
      query: listQuery(rest),
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Post one expense line.
   *
   * Throws `ApiError`: **403** without the staff role, **404** for an unknown category,
   * **422** for a negative tax, for `is_recurring` without an interval, for an interval
   * without `is_recurring` -- the schema's `model_validator` binds them both ways, verified
   * -- and for `booking_public_id`, which this endpoint has no field for.
   */
  createExpense(
    hotelPublicId: string,
    payload: ExpenseCreateRequest,
    signal?: AbortSignal,
  ): Promise<ExpenseEntry> {
    return api.post<ExpenseEntry>(`/hotels/${hotelPublicId}/expenses`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * The global revenue category catalogue, alphabetical by code.
   *
   * `is_active=true` because a retired category must not be offered for a new posting. The
   * *list* is not filtered by it: an old line in a since-retired category still has to be
   * displayed, and its code is shown as the server sent it.
   */
  listRevenueCategories(signal?: AbortSignal): Promise<Page<RevenueCategory>> {
    return api.get<Page<RevenueCategory>>('/revenue-categories', {
      query: { is_active: true, page: 1, page_size: CATEGORY_PAGE_SIZE },
      ...(signal ? { signal } : {}),
    })
  },

  /** The global expense category catalogue. */
  listExpenseCategories(signal?: AbortSignal): Promise<Page<ExpenseCategory>> {
    return api.get<Page<ExpenseCategory>>('/expense-categories', {
      query: { is_active: true, page: 1, page_size: CATEGORY_PAGE_SIZE },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Revenue totalled by category and currency, over an explicit range.
   *
   * **This is where every revenue figure on the screen comes from.** PostgreSQL performs the
   * aggregation; this application renders the buckets it is handed and adds nothing together.
   *
   * Throws `ApiError`: 422 for a missing bound, a reversed range or one longer than the
   * schema's maximum; 404 for a hotel the caller cannot reach.
   */
  getRevenueBreakdown(
    hotelPublicId: string,
    { dateFrom, dateTo }: BreakdownRange,
    signal?: AbortSignal,
  ): Promise<RevenueBreakdownResponse> {
    return api.get<RevenueBreakdownResponse>(
      `/hotels/${hotelPublicId}/analytics/revenue-by-category`,
      { query: { date_from: dateFrom, date_to: dateTo }, ...(signal ? { signal } : {}) },
    )
  },

  /** Expenses totalled by category and currency, over the same range. */
  getExpenseBreakdown(
    hotelPublicId: string,
    { dateFrom, dateTo }: BreakdownRange,
    signal?: AbortSignal,
  ): Promise<ExpenseBreakdownResponse> {
    return api.get<ExpenseBreakdownResponse>(
      `/hotels/${hotelPublicId}/analytics/expenses-by-category`,
      { query: { date_from: dateFrom, date_to: dateTo }, ...(signal ? { signal } : {}) },
    )
  },
} as const
