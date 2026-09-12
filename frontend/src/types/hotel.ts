import type { DecimalString } from '@/types/analytics'

/**
 * The hotel contract, transcribed from `app.schemas.hotel.HotelResponse`.
 *
 * Stage 5.4 typed only the fields the shell and the dashboard read, on the grounds that
 * adding the rest would declare an interest that stage did not have. Stage 5.13 has it: the
 * hotel management screen shows and edits the whole record, so the remaining columns are
 * typed here now rather than in a second, competing definition of the same response.
 *
 * Note what is **not** present and cannot be: `id`. The backend's own docstring is explicit
 * that the internal BIGINT is never exposed, "a sequential integer key leaks how many
 * hotels exist". `public_id` is the only identifier that crosses the wire, and it is the one
 * every hotel-scoped URL is keyed by.
 */
export interface Hotel {
  /** The UUID every hotel-scoped route is addressed by. */
  readonly public_id: string
  /** Stable URL identity. Set once at creation and never changed. */
  readonly slug: string
  readonly name: string
  readonly city: string
  readonly country_code: string
  /**
   * IANA zone, e.g. `Europe/Athens`.
   *
   * The reason this field is here at all. Analytics ranges are hotel-local calendar dates,
   * so "today" must be resolved in this zone -- not in the browser's. A receptionist in
   * Athens and a regional manager in London must see the same figures for the same day.
   */
  readonly timezone: string
  /** ISO 4217 code. The hotel's *configured* currency, which is not necessarily the only
   * one its analytics report: revenue and expenses each carry their own. */
  readonly currency: string
  readonly star_rating: number | null
  readonly is_active: boolean

  /* The rest of `HotelResponse`, added in Stage 5.13. */
  readonly address_line1: string
  readonly address_line2: string | null
  readonly region: string | null
  readonly postal_code: string | null
  /** `NUMERIC(9,6)`, so a string on the wire. Displayed, never used in arithmetic. */
  readonly latitude: DecimalString | null
  readonly longitude: DecimalString | null
  readonly email: string | null
  readonly phone: string | null
  readonly website: string | null
  readonly created_at: string
  readonly updated_at: string
}

/**
 * `POST /hotels` -- the `HotelCreate` payload.
 *
 * Required: `slug`, `name`, `address_line1`, `city`, `country_code`, `currency`. `timezone`
 * defaults to `UTC` in the schema. Everything else is optional.
 *
 * **`slug` is settable here and nowhere else.** It is the hotel's stable URL identity, and
 * the update schema omits it deliberately -- sending it to `PATCH` is a 422, verified. It
 * must be lower-case: `^[a-z0-9]+(-[a-z0-9]+)*$`, and `S513-Bad` is a 422.
 *
 * **`is_active` is absent**, and that is not an oversight: it is update-only, and sending it
 * at creation is a 422, verified. A hotel is created active.
 *
 * `country_code` and `currency` are upper-cased by the schema, so `gr` and `eur` are both
 * accepted and stored canonically -- verified live.
 *
 * Creating a hotel also makes the caller its **owner**, in the same transaction. Any
 * authenticated user may do it.
 */
export interface HotelCreateRequest {
  readonly slug: string
  readonly name: string
  readonly address_line1: string
  readonly city: string
  readonly country_code: string
  readonly currency: string
  readonly timezone?: string
  readonly address_line2?: string | null
  readonly region?: string | null
  readonly postal_code?: string | null
  readonly latitude?: DecimalString | null
  readonly longitude?: DecimalString | null
  readonly email?: string | null
  readonly phone?: string | null
  readonly website?: string | null
  readonly star_rating?: number | null
}

/**
 * `PATCH /hotels/{public_id}` -- the `HotelUpdate` payload.
 *
 * Every field optional. An omitted field is left untouched; an explicit null clears a
 * nullable column. An empty payload is a 200 that changes nothing. All verified.
 *
 * **`slug` is absent and `is_active` is present** -- the mirror image of create, and both
 * are 422s the other way round.
 *
 * Requires the **owner** role on the hotel, which is a higher bar than the manager role that
 * room types need.
 */
export interface HotelUpdateRequest {
  readonly name?: string
  readonly address_line1?: string
  readonly address_line2?: string | null
  readonly city?: string
  readonly region?: string | null
  readonly postal_code?: string | null
  readonly country_code?: string
  readonly latitude?: DecimalString | null
  readonly longitude?: DecimalString | null
  readonly email?: string | null
  readonly phone?: string | null
  readonly website?: string | null
  readonly timezone?: string
  readonly currency?: string
  readonly star_rating?: number | null
  readonly is_active?: boolean
}
