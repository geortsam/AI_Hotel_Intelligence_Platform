/**
 * Conditional updates (Issue H6).
 *
 * Five PATCH routes -- hotel, guest, room type, room and booking -- take an optional
 * `If-Match` naming the version of the record the client edited. The version is the record's
 * own `updated_at`, exactly as the API returned it, quoted as a strong entity-tag. If the record
 * has been written since, the server refuses with **412** and `error.code` {@link STALE_UPDATE},
 * and nothing is saved.
 *
 * The client never compares timestamps itself and never rewrites the string: the server
 * compares by instant, and the string it sent back is already a valid token.
 */

/** `error.code` on the 412 a conditional update gets when its version is no longer current. */
export const STALE_UPDATE = 'STALE_UPDATE'

/** The request header that makes an update conditional on `updatedAt`. */
export function ifMatch(updatedAt: string): Readonly<Record<string, string>> {
  return { 'If-Match': `"${updatedAt}"` }
}

/**
 * The entries of `after` whose value differs from `before`, by strict equality.
 *
 * Both sides must have been built by the same normaliser -- a form's draft-to-payload function
 * applied to the record as loaded, and to the draft as edited -- so that an untouched field
 * compares equal however the API spells it, and an intentional clearing (`null`) or a flipped
 * boolean compares unequal. Only what changed is sent: an update can then never restore a
 * value someone else wrote to a field this operator did not touch.
 */
export function changedFields<T extends object>(before: T, after: T): Partial<T> {
  const changes: Partial<T> = {}
  for (const key of Object.keys(after) as (keyof T)[]) {
    if (after[key] !== before[key]) {
      changes[key] = after[key]
    }
  }
  return changes
}
