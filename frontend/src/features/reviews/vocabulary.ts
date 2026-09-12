import type { BadgeTone } from '@/components/ui/Badge'
import type { ReviewSource } from '@/types/review'

/**
 * The words this application puts on review concepts, and the one rule about ratings.
 *
 * Nothing here decides anything: no label gates a control, chooses an endpoint or changes a
 * figure. A source code with no entry falls back to the code itself, because inventing a
 * friendly name for a channel added to the backend after this file was written would be
 * showing a label the server never said.
 */

/** Every value of `ck_reviews_source_valid`, in the order the schema lists them. */
export const REVIEW_SOURCES: readonly ReviewSource[] = [
  'direct',
  'booking_com',
  'tripadvisor',
  'google',
  'expedia',
  'airbnb',
  'other',
]

const SOURCE_LABELS: Readonly<Record<ReviewSource, string>> = {
  direct: 'Direct',
  booking_com: 'Booking.com',
  tripadvisor: 'Tripadvisor',
  google: 'Google',
  expedia: 'Expedia',
  airbnb: 'Airbnb',
  other: 'Other',
}

/**
 * The channel's name, as a word.
 *
 * Takes a plain `string`, not `ReviewSource`, and that is deliberate: the analytics
 * breakdown groups by whatever codes are in the table, so a source added to the backend
 * later arrives here as a value this union does not contain. Falling back to the code keeps
 * the page rendering something true instead of `undefined` -- or crashing on a lookup.
 */
export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source as ReviewSource] ?? source
}

/**
 * The tone for a source badge.
 *
 * Every source is neutral. Colouring channels would imply a ranking between them that the
 * schema does not have -- and the label already carries the meaning, which is the rule the
 * `Badge` component was written around.
 */
export const SOURCE_TONE: BadgeTone = 'neutral'

/** How a review's publication state reads, and how it is coloured. */
export function publicationPresentation(isPublished: boolean): {
  label: string
  tone: BadgeTone
} {
  // "Hidden", not "Rejected" or "Unapproved": `is_published` is one boolean column, and the
  // API has no notion of a review having been judged. Naming it as a moderation verdict
  // would be inventing a workflow the backend does not implement.
  return isPublished
    ? { label: 'Published', tone: 'success' }
    : { label: 'Hidden', tone: 'warning' }
}

/**
 * A review's rating, in its own words.
 *
 * ## Why no conversion happens here
 *
 * `rating_scale` is 5 or 10, and **both are present in a single list** -- Booking.com scores
 * out of ten, Tripadvisor out of five, and the seeded data really does mix them. So a rating
 * is shown on the scale the source used and never on any other. `9.00/10` is rendered as
 * "9 / 10", not as "4.5 / 5", because the second is a number the guest did not give.
 *
 * `rating_normalized` exists and is `rating / rating_scale`, computed by PostgreSQL. It is
 * the only figure comparable across scales, which is why the **aggregate** uses it -- but
 * putting it on an individual review would replace the guest's score with a fraction.
 *
 * ## Why there are no stars
 *
 * A row of five stars beside a ten-point score is a conversion drawn rather than written,
 * and a viewer reads the picture before the number. With two scales interleaved there is no
 * star count that is honest for both, so the rating is text. The scale is part of that text,
 * never implied by the shape of a control.
 *
 * The string is not parsed: `rating` arrives as a decimal string and the trailing zeros are
 * trimmed by string operations, so no monetary-style value passes through a float.
 */
export function formatReviewRating(rating: string, scale: number): string {
  return `${trimTrailingZeros(rating)} / ${scale}`
}

/**
 * The same rating for a screen reader, spelled out.
 *
 * "4 out of 5" rather than "4 / 5", because a slash is read as "slash" or skipped entirely,
 * and a rating whose scale is inaudible is exactly the ambiguity this module exists to
 * prevent.
 */
export function describeReviewRating(rating: string, scale: number): string {
  return `Rated ${trimTrailingZeros(rating)} out of ${scale}`
}

/**
 * `4.00` -> `4`, `4.50` -> `4.5`, `10.00` -> `10`.
 *
 * Purely textual: the string is cut, never converted to a number and back. `NUMERIC(4,2)`
 * always arrives with two decimal places, and printing "4.00 / 5" reads like a measurement
 * to two decimals when the guest picked a whole star.
 */
function trimTrailingZeros(value: string): string {
  if (!value.includes('.')) {
    return value
  }
  return value.replace(/0+$/, '').replace(/\.$/, '')
}
