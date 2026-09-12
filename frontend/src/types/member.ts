/**
 * Hotel membership, transcribed from `app/schemas/membership.py` and `app/models/enums.py`.
 *
 * A membership joins a **global** account to **one** property. That shape decides the types:
 * a member is addressed by the *user's* `public_id` (identity is global, so the member and
 * the account are the same person), and the hotel comes from the URL and never from a body
 * -- `MemberCreate` rejects `hotel_public_id` as an extra field, verified live.
 *
 * No internal identifier appears here: not `users.id`, not `user_hotels.id`, not `hotel_id`.
 * A membership has no public id of its own, so there is none to type.
 */

/**
 * What a member may do at one hotel.
 *
 * Totally ordered -- viewer < staff < manager < owner -- which is what makes the backend's
 * authorization a rank comparison rather than a permission matrix. The four values are the
 * ones `HotelRole` declares; the server answers 422 listing exactly these for anything else,
 * verified.
 */
export type HotelRole = 'viewer' | 'staff' | 'manager' | 'owner'

/** In rank order, lowest first. The order the UI offers them in. */
export const HOTEL_ROLES: readonly HotelRole[] = ['viewer', 'staff', 'manager', 'owner']

/**
 * What each role means, written from the backend's own authorization requirements rather
 * than invented: these are the roles the routes across the API actually demand.
 */
export const ROLE_DESCRIPTIONS: Readonly<Record<HotelRole, string>> = {
  viewer: 'Can read this property’s data. Cannot change anything.',
  staff: 'Can record day-to-day work — bookings, guests, payments.',
  manager: 'Everything staff can do, plus room types, rooms and the member list.',
  owner: 'Full control, including the property’s own record and who may reach it.',
}

/** `GET|POST|PATCH /hotels/{id}/members[/{user}]` response. Mirrors `MemberResponse`. */
export interface Member {
  /** The member's ACCOUNT public id. The identifier the PATCH and DELETE routes take. */
  readonly user_public_id: string
  readonly email: string
  readonly full_name: string
  /**
   * Whether the **account** is enabled — not a membership flag.
   *
   * The backend is explicit about this: a disabled account keeps its memberships and simply
   * cannot authenticate. Rendering it as though it described the membership would tell an
   * administrator that access had been revoked when it had not.
   */
  readonly is_active: boolean
  readonly role: HotelRole
  /** ISO 8601 timestamp. When the membership was created, not when the account was. */
  readonly joined_at: string
}

/**
 * `POST /hotels/{id}/members` body. Mirrors `MemberCreate`.
 *
 * Names an existing account **by email**, because that is the identifier a human actually
 * has: the API offers no user-search endpoint (`GET /users` is a 404, verified). An address
 * with no account behind it is a 404, and this never creates one.
 */
export interface MemberCreateRequest {
  readonly email: string
  readonly role: HotelRole
}

/**
 * `PATCH /hotels/{id}/members/{user}` body. Mirrors `MemberUpdate`.
 *
 * `role` is **required**, not optional: a membership has exactly one mutable field, so a
 * partial update would be a request that changes nothing. An empty body is a 422, verified.
 */
export interface MemberUpdateRequest {
  readonly role: HotelRole
}

/** Whether an unknown string is one of the four roles. */
export function isHotelRole(value: unknown): value is HotelRole {
  return typeof value === 'string' && (HOTEL_ROLES as readonly string[]).includes(value)
}
