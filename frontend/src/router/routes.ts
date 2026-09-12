/**
 * Every route path the application has, named once.
 *
 * Links and navigation read from here rather than repeating string literals, so a path can
 * be changed in one place and a typo in a destination is a compile error instead of a blank
 * screen at runtime.
 *
 * **These paths all resolve.** Stage 5.2 renders the shell around them and each one is an
 * honest "not built yet" page naming the stage that will fill it -- not a fake screen, and
 * not a dead link. The distinction matters: a link that 404s looks broken, and a link
 * showing invented data looks finished. Neither is true, so the page says which it is.
 */
export const ROUTES = {
  /**
   * The sign-in screen. The only PUBLIC route.
   *
   * Everything else sits behind `ProtectedRoute`. Keeping the exception explicit here means
   * "which routes are public" is answerable by reading one file.
   */
  login: '/login',

  /** Overview. The application's landing route. */
  dashboard: '/',

  /* Operations */
  bookings: '/bookings',
  guests: '/guests',
  rooms: '/rooms',
  availability: '/availability',

  /* Finance */
  financials: '/financials',

  /* Intelligence */
  analytics: '/analytics',
  reviews: '/reviews',
  /*
   * Flat, like every other page. The API path is hotel-scoped
   * (`/hotels/{id}/intelligence/...`) because that is where tenant isolation is established;
   * the frontend takes its hotel from the shared picker in the shell, so switching property
   * re-reads this screen exactly as it re-reads every other one.
   */
  intelligence: '/intelligence',

  /* Administration */
  property: '/property',
  administration: '/administration',
  /*
   * Platform, not hotel. The catalogues behind this route have no `hotel_id` and their codes
   * are unique across the installation, so the path carries no hotel segment -- the URL says
   * the same thing the schema does.
   */
  platform: '/platform',
} as const

/** A path this application knows how to render. */
export type AppRoute = (typeof ROUTES)[keyof typeof ROUTES]

/**
 * The route pattern for one booking, as React Router matches it.
 *
 * Kept beside `ROUTES` rather than inside it, because `AppRoute` is the set of *navigable*
 * paths -- the ones the sidebar can point at -- and a pattern with a parameter is not one of
 * those. Putting it in the object would make `AppRoute` include the literal
 * `'/bookings/:bookingPublicId'`, a path that can never be visited.
 */
export const BOOKING_DETAIL_PATTERN = '/bookings/:bookingPublicId'

/**
 * The URL for one booking.
 *
 * **The parameter is the booking's `public_id`, a UUID, and no other identifier exists to
 * use.** The backend never sends an internal key -- `BookingResponse`'s own docstring says
 * "No internal key appears in it" -- so there is no numeric id available to leak here even by
 * accident. It is encoded rather than interpolated raw: the value comes from a server
 * response today, but a path built by bare concatenation is the kind of thing that quietly
 * stops being safe the first time someone passes it something from elsewhere.
 */
export function bookingPath(bookingPublicId: string): string {
  return `${ROUTES.bookings}/${encodeURIComponent(bookingPublicId)}`
}

/**
 * The route pattern for one guest, as React Router matches it.
 *
 * Kept beside `ROUTES` rather than inside it for the same reason as the booking pattern: a
 * path with a parameter is not navigable, and putting it in the object would make
 * `AppRoute` include a literal nobody can visit.
 */
export const GUEST_DETAIL_PATTERN = '/guests/:guestPublicId'

/**
 * The URL for one guest.
 *
 * **The parameter is the guest's `public_id`, a UUID.** `GuestResponse` carries no internal
 * key -- the schema's own docstring says the internal BIGINT never leaves the database -- so
 * there is no numeric id available to put here even by accident. Encoded rather than
 * interpolated raw, for the reason `bookingPath` gives.
 */
export function guestPath(guestPublicId: string): string {
  return `${ROUTES.guests}/${encodeURIComponent(guestPublicId)}`
}

/**
 * The route pattern for one room, as React Router matches it.
 *
 * **Two parameters, because a room needs both to be found.** The backend resolves a room
 * against the whole chain -- hotel, then room type, then number -- and the same number under
 * a different type is a 404, verified. A URL carrying only the number could not address it.
 */
export const ROOM_DETAIL_PATTERN = '/rooms/:roomTypeCode/:roomNumber'

/**
 * The URL for one room.
 *
 * Both segments are public identifiers: a room type's `code` and a room's `room_number`.
 * `RoomResponse` carries no internal key -- not `rooms.id`, not `room_type_id` -- so there is
 * no numeric id available to place here even by accident. Both are encoded: a room number may
 * legitimately contain `.` and `-`, and building a path by bare concatenation is the kind of
 * thing that stops being safe the first time someone passes it something from elsewhere.
 */
export function roomPath(roomTypeCode: string, roomNumber: string): string {
  return `${ROUTES.rooms}/${encodeURIComponent(roomTypeCode)}/${encodeURIComponent(roomNumber)}`
}
