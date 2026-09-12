/**
 * The three shared catalogues and the platform audit trail, transcribed from the backend.
 *
 * These resources belong to **no hotel**. `amenities`, `revenue_categories` and
 * `expense_categories` have no `hotel_id` column and their codes are unique across the whole
 * installation, so "sea view" and "F&B" mean the same thing for every property. That is the
 * schema's shape, not a convention, and it is why the routes sit outside the hotel segment.
 *
 * Reading them needs only a session -- every hotel must be able to reference the vocabulary
 * it is required to use. **Writing them needs a platform administrator grant**, which is a
 * different authority from any hotel role and is not ranked against one: a platform
 * administrator is not a very senior owner, and an owner is not a junior platform
 * administrator.
 *
 * The response types for the two category catalogues are **not restated here**. Stage 5.8
 * transcribed them for the ledger screens and they are the same rows; re-declaring them
 * would create a second definition to keep in step with the first.
 */

/**
 * `GET|POST|PATCH /amenities[/{code}]` response. Mirrors `AmenityResponse`.
 *
 * Exactly the three business columns the table has. **No timestamps**: unlike every other
 * resource in this application `amenities` does not use the timestamp mixin and the table has
 * no `created_at` or `updated_at`, so none is typed. `amenities.id` is a sequential BIGINT
 * and never leaves the server.
 */
export interface Amenity {
  /** Globally unique, upper-case, and the URL identity. */
  readonly code: string
  readonly name: string
  /** A free-text grouping, e.g. "Bathroom". Optional in the schema and often absent. */
  readonly category: string | null
}

/**
 * `POST /amenities` body. Mirrors `AmenityCreate`.
 *
 * `code` is upper-cased by the schema before the unique constraint sees it -- the constraint
 * is case-sensitive, so without that `wifi` and `WIFI` would be two entries for one concept.
 */
export interface AmenityCreateRequest {
  readonly code: string
  readonly name: string
  readonly category?: string | null
}

/**
 * `PATCH /amenities/{code}` body. Mirrors `AmenityUpdate`.
 *
 * `code` is absent: it is the URL identity, exactly as a hotel's slug and a room type's code
 * are, and every room-type association is keyed on the row it names.
 */
export interface AmenityUpdateRequest {
  readonly name?: string
  readonly category?: string | null
}

/** `POST /revenue-categories` body. Mirrors `RevenueCategoryCreate`. */
export interface RevenueCategoryCreateRequest {
  readonly code: string
  readonly name: string
  readonly is_room_revenue?: boolean
  readonly is_active?: boolean
}

/**
 * `PATCH /revenue-categories/{code}` body. Mirrors `RevenueCategoryUpdate`.
 *
 * No `code`. A category is retired with `is_active: false`, never renamed or deleted once
 * anything references it.
 */
export interface RevenueCategoryUpdateRequest {
  readonly name?: string
  readonly is_room_revenue?: boolean
  readonly is_active?: boolean
}

/** `POST /expense-categories` body. Mirrors `ExpenseCategoryCreate`. */
export interface ExpenseCategoryCreateRequest {
  readonly code: string
  readonly name: string
  readonly is_fixed_cost?: boolean
  readonly is_active?: boolean
}

/** `PATCH /expense-categories/{code}` body. Mirrors `ExpenseCategoryUpdate`. */
export interface ExpenseCategoryUpdateRequest {
  readonly name?: string
  readonly is_fixed_cost?: boolean
  readonly is_active?: boolean
}

/**
 * `GET /platform/audit-events` response row. Mirrors `PlatformAuditEventResponse`.
 *
 * **There is no hotel field, and its absence is the type stating the scope.** Every row this
 * can describe has `hotel_id IS NULL`: a password change, or an edit to one of the three
 * catalogues every property shares. A nullable hotel field here would invite a reader to
 * wonder which rows have one.
 *
 * The trail is **append-only**. No endpoint writes, edits or deletes an event -- which is
 * the property that makes it worth reading at all.
 */
export interface PlatformAuditEvent {
  readonly public_id: string
  /** ISO 8601. Stamped by the database, never by the caller. */
  readonly occurred_at: string
  /** From the closed audit vocabulary, e.g. `amenity.created`. */
  readonly action: string
  readonly resource_type: string
  /** Which one: a public identifier, or a natural key such as a catalogue code. */
  readonly resource_reference: string
  readonly actor_public_id: string | null
  readonly actor_email: string | null
  /** The `X-Request-ID` of the request that caused the change. */
  readonly request_id: string | null
  /**
   * A small object describing the change.
   *
   * The backend guarantees it is never a request body and never a credential, a token, a card
   * fragment or an internal key. It is rendered as text regardless.
   */
  readonly details: Readonly<Record<string, unknown>>
}

/**
 * The audit actions that belong to no property, from `PLATFORM_AUDIT_ACTIONS`.
 *
 * Ten values, and the filter offers exactly these. The backend validates `action` against the
 * *whole* closed vocabulary and answers an empty page for a hotel-only action rather than an
 * error -- so offering `booking.created` here would be a filter that silently always returns
 * nothing, which is worse than not offering it.
 */
export const PLATFORM_AUDIT_ACTIONS: readonly string[] = [
  'auth.password_changed',
  'amenity.created',
  'amenity.updated',
  'amenity.deleted',
  'revenue_category.created',
  'revenue_category.updated',
  'revenue_category.deleted',
  'expense_category.created',
  'expense_category.updated',
  'expense_category.deleted',
]

/** The resource types those actions are about, from `AuditResourceType`. */
export const PLATFORM_AUDIT_RESOURCE_TYPES: readonly string[] = [
  'user',
  'amenity',
  'revenue_category',
  'expense_category',
]

/** Which catalogue a screen is looking at. Not a backend concept; a UI selector. */
export type CatalogueKind = 'amenities' | 'revenue-categories' | 'expense-categories'
