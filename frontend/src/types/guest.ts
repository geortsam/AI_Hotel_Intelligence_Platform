/**
 * The guest contract, transcribed from `app.schemas.guest.GuestResponse`.
 *
 * Every field the endpoint returns is typed, including the ones this application chooses not
 * to display. Typing them is not the same as showing them: an accurate type is how a reader
 * can tell that `date_of_birth` and `notes` are *omitted deliberately* rather than forgotten.
 * See `GuestSection` for which are rendered and why.
 *
 * `guests.id` and `hotel_id` are absent because the backend never sends them.
 */
export interface Guest {
  readonly hotel_public_id: string
  readonly public_id: string
  readonly first_name: string
  readonly last_name: string
  /** Personal data. Masked wherever it is shown; never placed in a URL or a log. */
  readonly email: string | null
  /** Personal data. Masked wherever it is shown. */
  readonly phone: string | null
  readonly country_code: string | null
  readonly preferred_language: string | null
  /**
   * Personal data with no operational use on a booking screen.
   *
   * Typed so the contract is complete, and **not rendered**: a date of birth identifies a
   * person far beyond confirming which booking is theirs, and a screen read over the desk by
   * whoever is standing at it is the wrong place for it.
   */
  readonly date_of_birth: string | null
  readonly marketing_opt_in: boolean
  /** Free text about the guest. Not rendered here, for the reason above. */
  readonly notes: string | null
  readonly created_at: string
  readonly updated_at: string
}

/**
 * `POST /hotels/{h}/guests` -- the `GuestCreate` payload.
 *
 * Required: `first_name` and `last_name`, each 1..100 characters. Everything else is
 * optional, and `marketing_opt_in` defaults to `false` both in the schema and in the column.
 *
 * Absent by design, and each for a schema reason: `hotel_public_id` is in the URL, and
 * `public_id` is assigned by the database -- sending either is a 422, verified.
 *
 * ## What the backend does NOT check
 *
 * `email` is a length-bounded string, 3..254 characters, and **nothing more**. There is no
 * format validation in the schema and no CHECK on the column: `"not-an-email"` was accepted
 * with a 201, verified live. This application therefore does not invent one either -- a
 * client-side rule stricter than the server's would refuse data the system is willing to
 * hold, and a hotel really does end up with `front.desk+walkin` style addresses.
 *
 * `date_of_birth` is equally unconstrained: `2199-01-01` was accepted with a 201.
 */
export interface GuestCreateRequest {
  readonly first_name: string
  readonly last_name: string
  readonly email?: string | null
  readonly phone?: string | null
  /** Two letters. Sent as typed: the server upper-cases `gb` to `GB` itself. */
  readonly country_code?: string | null
  /** ISO-639-1. Sent as typed: the server lower-cases `EN` to `en` itself. */
  readonly preferred_language?: string | null
  readonly date_of_birth?: string | null
  readonly marketing_opt_in?: boolean
  readonly notes?: string | null
}

/**
 * `PATCH /hotels/{h}/guests/{g}` -- the `GuestUpdate` payload.
 *
 * Every field optional. An **omitted** field is left untouched; an **explicit null** clears
 * the column. The two are genuinely different requests and both are used: clearing an email
 * is `{"email": null}`, and leaving it alone is sending nothing at all.
 *
 * `public_id` is absent because it is the URL identity -- server-assigned and immutable.
 */
export interface GuestUpdateRequest {
  readonly first_name?: string
  readonly last_name?: string
  readonly email?: string | null
  readonly phone?: string | null
  readonly country_code?: string | null
  readonly preferred_language?: string | null
  readonly date_of_birth?: string | null
  readonly marketing_opt_in?: boolean
  readonly notes?: string | null
}
