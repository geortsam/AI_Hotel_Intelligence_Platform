import { api } from '@/services/api/client'
import type { Page } from '@/types/api'
import type { ExpenseCategory, RevenueCategory } from '@/types/finance'
import type {
  Amenity,
  AmenityCreateRequest,
  AmenityUpdateRequest,
  ExpenseCategoryCreateRequest,
  ExpenseCategoryUpdateRequest,
  PlatformAuditEvent,
  RevenueCategoryCreateRequest,
  RevenueCategoryUpdateRequest,
} from '@/types/platform'

/**
 * The catalogues every property shares, and the audit trail of changes to them.
 *
 * ## Why this is a separate module from `financeService`
 *
 * Stage 5.8's `financeService.listRevenueCategories` reads the catalogue with
 * `is_active=true`, because a retired category must not be offered for a new posting. That is
 * a *consumer* of the catalogue; this is the surface that maintains it, and it must be able
 * to see retired entries in order to reactivate them. Extending the locked finance service
 * with six mutations it has no business owning would blur which stage owns what, so the
 * maintenance verbs live here and `financeService` is untouched.
 *
 * ## The authorization is split down the middle, and not by hotel role
 *
 * **Every read requires only a session.** Every hotel must be able to reference the
 * vocabulary it is required to use, so `get_current_user` alone guards the GETs.
 *
 * **Every write requires a platform administrator grant** -- `require_platform_admin`,
 * declared on each writing route. That authority is held in `platform_admins`, read from the
 * database on every request, and is **deliberately not ranked against any hotel role**: an
 * owner is not a junior platform administrator. The frontend is told neither, so it offers
 * the documented control and renders the server's 403. Verified live: an authenticated owner
 * of two hotels is refused every write here with 403, and an anonymous caller gets 401.
 *
 * ## The two list surfaces differ, and the difference is real
 *
 * Both category routes accept **`is_active`**; the amenity route does **not**, because
 * `amenities` has no such column. Checked rather than assumed, because the backend ignores an
 * unrecognised query parameter silently -- `?is_active=true` and `?search=wifi` on
 * `/amenities` both returned the unfiltered page with a 200.
 *
 * ## Codes are the identity, and the server canonicalises them
 *
 * None of these tables has a `public_id`; a globally unique `code` is the identity and the
 * URL segment. The schema upper-cases it on the way in, so `wifi` and `WIFI` cannot become
 * two entries for one concept.
 */

/** From `app.services.amenity` and `app.services.finance` -- the same values in both. */
export const DEFAULT_PAGE_SIZE = 20
/** Each router's own `le=100`. Asking for 101 is a 422, verified. */
export const MAX_PAGE_SIZE = 100

interface PageQuery {
  readonly page: number
  readonly pageSize: number
}

/** The category lists take this; the amenity list does not. */
interface CategoryQuery extends PageQuery {
  /** Omitted entirely when undefined: `is_active=` is not the same request as no filter. */
  readonly isActive?: boolean
}

export const platformService = {
  /* --- amenities ------------------------------------------------------------------------ */

  /**
   * One page of the global amenity catalogue, ordered by code.
   *
   * Takes a page and nothing else. Throws `ApiError` only for transport and auth failures --
   * an empty catalogue is a 200 with an empty page, which is the demo installation's actual
   * state.
   */
  listAmenities(query: PageQuery, signal?: AbortSignal): Promise<Page<Amenity>> {
    return api.get<Page<Amenity>>('/amenities', {
      query: { page: query.page, page_size: query.pageSize },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Create an amenity.
   *
   * Throws `ApiError`: **403** without the platform grant; **409** when the code is taken;
   * **422** for a code outside `^[A-Z0-9][A-Z0-9_-]*$`, a name over 100 characters, or any
   * field the schema lacks.
   */
  createAmenity(payload: AmenityCreateRequest, signal?: AbortSignal): Promise<Amenity> {
    return api.post<Amenity>('/amenities', { body: payload, ...(signal ? { signal } : {}) })
  },

  /** Update an amenity. `code` is immutable and absent from the body. */
  updateAmenity(
    code: string,
    payload: AmenityUpdateRequest,
    signal?: AbortSignal,
  ): Promise<Amenity> {
    return api.patch<Amenity>(`/amenities/${encodeURIComponent(code)}`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Delete an amenity. 204, with no body.
   *
   * Throws `ApiError`: **409** while the amenity is still assigned to any room type -- the
   * database's RESTRICT policy is honoured rather than worked around; **404** for an unknown
   * code; **403** without the grant.
   */
  deleteAmenity(code: string, signal?: AbortSignal): Promise<void> {
    return api.delete<void>(`/amenities/${encodeURIComponent(code)}`, {
      ...(signal ? { signal } : {}),
    })
  },

  /* --- revenue categories --------------------------------------------------------------- */

  /**
   * One page of the revenue category catalogue, alphabetical by code.
   *
   * `is_active` is a real filter here, unlike on amenities: verified live, `true` returned
   * all four seeded categories and `false` returned none.
   */
  listRevenueCategories(
    query: CategoryQuery,
    signal?: AbortSignal,
  ): Promise<Page<RevenueCategory>> {
    return api.get<Page<RevenueCategory>>('/revenue-categories', {
      query: {
        page: query.page,
        page_size: query.pageSize,
        ...(query.isActive === undefined ? {} : { is_active: query.isActive }),
      },
      ...(signal ? { signal } : {}),
    })
  },

  createRevenueCategory(
    payload: RevenueCategoryCreateRequest,
    signal?: AbortSignal,
  ): Promise<RevenueCategory> {
    return api.post<RevenueCategory>('/revenue-categories', {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  updateRevenueCategory(
    code: string,
    payload: RevenueCategoryUpdateRequest,
    signal?: AbortSignal,
  ): Promise<RevenueCategory> {
    return api.patch<RevenueCategory>(`/revenue-categories/${encodeURIComponent(code)}`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Delete an unused revenue category. 204.
   *
   * Throws `ApiError`: **409** when revenue entries still reference it -- the backend's own
   * guidance is to deactivate it instead, and that is what the UI offers.
   */
  deleteRevenueCategory(code: string, signal?: AbortSignal): Promise<void> {
    return api.delete<void>(`/revenue-categories/${encodeURIComponent(code)}`, {
      ...(signal ? { signal } : {}),
    })
  },

  /* --- expense categories --------------------------------------------------------------- */

  listExpenseCategories(
    query: CategoryQuery,
    signal?: AbortSignal,
  ): Promise<Page<ExpenseCategory>> {
    return api.get<Page<ExpenseCategory>>('/expense-categories', {
      query: {
        page: query.page,
        page_size: query.pageSize,
        ...(query.isActive === undefined ? {} : { is_active: query.isActive }),
      },
      ...(signal ? { signal } : {}),
    })
  },

  createExpenseCategory(
    payload: ExpenseCategoryCreateRequest,
    signal?: AbortSignal,
  ): Promise<ExpenseCategory> {
    return api.post<ExpenseCategory>('/expense-categories', {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  updateExpenseCategory(
    code: string,
    payload: ExpenseCategoryUpdateRequest,
    signal?: AbortSignal,
  ): Promise<ExpenseCategory> {
    return api.patch<ExpenseCategory>(`/expense-categories/${encodeURIComponent(code)}`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  deleteExpenseCategory(code: string, signal?: AbortSignal): Promise<void> {
    return api.delete<void>(`/expense-categories/${encodeURIComponent(code)}`, {
      ...(signal ? { signal } : {}),
    })
  },

  /* --- the platform audit trail --------------------------------------------------------- */

  /**
   * One page of platform-scoped audit history, newest first.
   *
   * **Read-only, and append-only on the server**: no endpoint writes, edits or deletes an
   * event. Every row has `hotel_id IS NULL` -- a password change, or a catalogue mutation.
   *
   * Requires the platform grant, so this is the one *read* on this screen that can answer
   * **403**; an anonymous caller gets 401 instead, because the dependency chain authenticates
   * before it authorises. Both verified live.
   *
   * An unknown `actor_public_id` and a hotel-only `action` each return an empty page rather
   * than an error, so neither filter can be used to discover what exists.
   */
  listPlatformAuditEvents(
    query: {
      readonly page: number
      readonly pageSize: number
      readonly action?: string
      readonly resourceType?: string
    },
    signal?: AbortSignal,
  ): Promise<Page<PlatformAuditEvent>> {
    return api.get<Page<PlatformAuditEvent>>('/platform/audit-events', {
      query: {
        page: query.page,
        page_size: query.pageSize,
        ...(query.action ? { action: query.action } : {}),
        ...(query.resourceType ? { resource_type: query.resourceType } : {}),
      },
      ...(signal ? { signal } : {}),
    })
  },
} as const
