import { api } from '@/services/api/client'
import type { Page } from '@/types/api'
import type { Hotel, HotelCreateRequest, HotelUpdateRequest } from '@/types/hotel'

/**
 * How the frontend learns which hotels it may show.
 *
 * `GET /hotels` is **not** "every hotel". The backend filters it to the caller's own
 * memberships, and its docstring gives the reason: listing the whole portfolio would
 * disclose names, addresses and public ids to anyone who can register an account, handing
 * over the identifiers every other endpoint is keyed by. The `total` is filtered by the same
 * predicate as the page, so the count cannot contradict the rows.
 *
 * That makes this the hotel-context mechanism the platform actually has. It is a real
 * membership query answered by the server, not a selector invented on the client: the
 * frontend cannot widen it, because the filtering happens in the database and the
 * authorization decision is re-made on every hotel-scoped request afterwards. Choosing a
 * hotel here grants nothing -- it only decides which of the hotels the server already
 * returned is being viewed.
 *
 * ## The rest of the surface, added in Stage 5.13
 *
 * `GET`, `POST`, `PATCH` and `DELETE` on a hotel all exist. Their authorization is unusual
 * for this codebase in that **none of it is declared on the router** -- there is no
 * `require_role` on any hotel route. It lives in `HotelService`, and it differs per verb:
 *
 * * **list** -- membership-filtered, as above;
 * * **read** -- membership on that hotel;
 * * **create** -- any authenticated user, and the caller becomes the hotel's **owner** in the
 *   same transaction;
 * * **update** and **delete** -- **owner**, a higher bar than the manager role that room
 *   types require.
 *
 * Read from the service rather than inferred from the router, because reading the router
 * alone would have suggested these endpoints were unauthenticated.
 *
 * `PATCH`/`DELETE` on the collection and `PUT` on a hotel are all **405**, verified.
 */

/**
 * The page size used to resolve hotel context.
 *
 * The backend's `MAX_PAGE_SIZE` is 100 and its default is 20. Twenty-five is chosen for a
 * different reason than either: it is comfortably more properties than one staff account is
 * plausibly a member of, so a single request answers the question. When `total` exceeds what
 * came back, the UI says so rather than pretending the list is complete -- see
 * `HotelProvider`. Paging a selector is a Stage 5.10 problem, not this stage's.
 */
export const HOTEL_PAGE_SIZE = 25

export const hotelService = {
  /**
   * One page of the hotels the signed-in user is a member of.
   *
   * Throws `ApiError`; 401 when the token is rejected. There is no 403 path: a user with no
   * memberships gets an empty page, not a refusal.
   */
  listMine(signal?: AbortSignal): Promise<Page<Hotel>> {
    return api.get<Page<Hotel>>('/hotels', {
      query: { page: 1, page_size: HOTEL_PAGE_SIZE },
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * One hotel by public identifier.
   *
   * Throws `ApiError`: **404** for an unknown hotel *or* one the caller is not a member of
   * -- the same answer for both, so the endpoint cannot be used to discover which properties
   * exist; **422** for a malformed UUID, rejected before any lookup.
   */
  get(publicId: string, signal?: AbortSignal): Promise<Hotel> {
    return api.get<Hotel>(`/hotels/${publicId}`, { ...(signal ? { signal } : {}) })
  },

  /**
   * Create a hotel. The caller becomes its owner.
   *
   * Throws `ApiError`: **409** when the slug is taken -- checked before the insert for a
   * clear message, with the unique constraint still the authority; **422** for a slug that
   * is not lower-case kebab, a missing required field, a `star_rating` outside 1..5, or any
   * field the schema lacks -- `is_active` and `public_id` included.
   */
  create(payload: HotelCreateRequest, signal?: AbortSignal): Promise<Hotel> {
    return api.post<Hotel>('/hotels', { body: payload, ...(signal ? { signal } : {}) })
  },

  /**
   * Update a hotel. Only the fields present in the body are written.
   *
   * Throws `ApiError`: **403** without the owner role; **404** for an unknown hotel; **422**
   * for `slug`, which is immutable, or any other field the schema lacks.
   */
  update(publicId: string, payload: HotelUpdateRequest, signal?: AbortSignal): Promise<Hotel> {
    return api.patch<Hotel>(`/hotels/${publicId}`, {
      body: payload,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * Delete a hotel. 204, with no body.
   *
   * Throws `ApiError`: **409** when anything still references it -- room types, rooms,
   * guests, bookings, revenue, expenses are all `ON DELETE RESTRICT`, and no cascade is
   * performed, so a property with any operating history cannot be removed. Verified live
   * against a seeded hotel. Also **403** without the owner role and **404** for an unknown
   * hotel.
   *
   * The hotel's own memberships are deleted first, in the same transaction -- otherwise
   * their RESTRICT would make every hotel permanently undeletable, including one just
   * created.
   */
  remove(publicId: string, signal?: AbortSignal): Promise<void> {
    return api.delete<void>(`/hotels/${publicId}`, { ...(signal ? { signal } : {}) })
  },
} as const
