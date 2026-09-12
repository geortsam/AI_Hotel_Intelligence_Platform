import { api } from '@/services/api/client'
import type { Page } from '@/types/api'
import type { Member, MemberCreateRequest, MemberUpdateRequest } from '@/types/member'

/**
 * Who may reach a hotel, and with what role.
 *
 * ## The routes are nested, and there is deliberately no flat one
 *
 * Every path is `/hotels/{hotel_public_id}/members`. The backend documents why: a flat
 * `GET /users/{id}/memberships` would tell one property's administrator which *other*
 * properties a colleague works at -- a question no hotel is entitled to ask -- and it would
 * sit outside the resolver that makes the 404 wall work. Confirmed live: that path is a 404,
 * and a structural test on the backend asserts it never appears.
 *
 * ## The authorization is asymmetric, and both halves are on the router
 *
 * **Listing needs `MANAGER`**; adding, changing a role and removing all need **`OWNER`**.
 * Unlike the hotel routes of Stage 5.13, these are declared with `require_role` on the
 * routes themselves. Running a property day to day means knowing who has access to it;
 * changing who has access is the owner's decision.
 *
 * ## A member is named by the ACCOUNT's public id
 *
 * A membership has no public id of its own -- `UNIQUE (user_id, hotel_id)` is its identity --
 * so `PATCH` and `DELETE` take the user's `public_id`, which the listing returns. No
 * internal BIGINT appears in any URL or response.
 *
 * ## The list takes a page and nothing else
 *
 * `page` and `page_size` (default 20, max 100), ordered by email ascending. No search, no
 * role filter, no sort -- and checked rather than assumed, because the backend **ignores an
 * unrecognised query parameter silently**: `?search=ops`, `?role=viewer` and `?sort=role`
 * each returned the full list with a 200.
 */

/** From `app.services.membership`. */
export const DEFAULT_PAGE_SIZE = 20
/** The router's own `le=100`. Asking for 101 is a 422, verified. */
export const MAX_PAGE_SIZE = 100

export const memberService = {
  /**
   * One page of a hotel's members, ordered by email.
   *
   * Requires **manager**. Throws `ApiError`: **403** for a member of this hotel holding too
   * low a role; **404** for an unknown hotel *or* one the caller is not a member of at all --
   * one answer for both, so the endpoint cannot be used to discover which properties exist.
   */
  list(
    hotelPublicId: string,
    { page, pageSize }: { page: number; pageSize: number },
    signal?: AbortSignal,
  ): Promise<Page<Member>> {
    return api.get<Page<Member>>(`/hotels/${hotelPublicId}/members`, {
      query: { page, page_size: pageSize },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Grant an existing account a role at this hotel.
   *
   * **This never creates an account.** Identity is global; a second row for the same person
   * would mean two logins and two password hashes for one human. The address is matched
   * case-insensitively -- an upper-case address resolved to the same account, verified -- and
   * the response carries the stored form.
   *
   * Requires **owner**. Throws `ApiError`: **404** when no account exists for that address,
   * with a message that says the person must register first; **409** when they are already a
   * member -- not a 404, because the caller can already see that person in their own listing;
   * **422** for a malformed address, a role outside the four, or any extra field --
   * `hotel_public_id` included, since the hotel comes from the URL.
   */
  add(
    hotelPublicId: string,
    payload: MemberCreateRequest,
    signal?: AbortSignal,
  ): Promise<Member> {
    return api.post<Member>(`/hotels/${hotelPublicId}/members`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Change what a member may do here.
   *
   * `role` is required; an empty body is a 422. Reasserting the role somebody already holds
   * is accepted and changes nothing -- the backend does not even write an audit row for it.
   *
   * Requires **owner**. Throws `ApiError`: **409** when the change would demote the hotel's
   * **last owner**, the one rule that keeps a property administrable; **404** when the
   * account is not a member here (the same answer as an account that does not exist, so this
   * hotel's administrator cannot probe the user table); **403** without the owner role.
   */
  changeRole(
    hotelPublicId: string,
    userPublicId: string,
    payload: MemberUpdateRequest,
    signal?: AbortSignal,
  ): Promise<Member> {
    return api.patch<Member>(`/hotels/${hotelPublicId}/members/${userPublicId}`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Revoke a member's access. 204, with no body.
   *
   * Deleting the membership *is* the revocation: `user_hotels` has no `is_active` column,
   * deliberately, so "no longer has access" has exactly one representation. **The account
   * itself is untouched** -- and cannot be touched, since the API has no endpoint that
   * deletes a user.
   *
   * Requires **owner**. Throws `ApiError`: **409** when it would remove the hotel's last
   * owner -- which applies whether the caller is removing themselves or somebody else;
   * **404** for an account that is not a member here; **403** without the owner role.
   */
  remove(hotelPublicId: string, userPublicId: string, signal?: AbortSignal): Promise<void> {
    return api.delete<void>(`/hotels/${hotelPublicId}/members/${userPublicId}`, {
      ...(signal ? { signal } : {}),
    })
  },
} as const
