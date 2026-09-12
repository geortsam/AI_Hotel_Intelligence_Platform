import { ApiError } from '@/services/api/ApiError'

/**
 * What a failed request says to the person looking at it.
 *
 * **Nothing here ever renders `error.message`.** The backend's own messages are written to
 * be safe -- it is disciplined about never leaking SQL, constraint names or stack traces --
 * but "the server says it is safe" is not the property to depend on when the alternative
 * costs one `switch`. Anything that reached the browser through a proxy, a gateway or a body
 * that was not JSON has not been through that discipline at all, and a 500 rendered verbatim
 * is precisely how a table name ends up on a receptionist's screen.
 *
 * So every branch returns copy written here, chosen by status. The `ApiError` still carries
 * the original for a developer reading the console; the DOM only ever gets these strings.
 *
 * Written for the dashboard in Stage 5.4 and moved here in Stage 5.5, when bookings needed
 * the same handling for the same nine statuses. Only the wording that is genuinely
 * per-resource -- what a 404 means -- is overridable; the rest is deliberately identical
 * everywhere, because a user should not have to learn a second vocabulary of failures per
 * screen.
 */
export interface FailureNotice {
  readonly title: string
  readonly detail: string
  /** Whether retrying the same request could plausibly succeed. */
  readonly canRetry: boolean
}

export interface FailureCopyOverrides {
  /**
   * What a 404 means for this resource.
   *
   * Worth overriding because 404 is the one status whose meaning is specific: for analytics
   * it is the hotel, for a booking it is the booking. It is also the status this backend
   * uses to hide things -- a booking belonging to another hotel answers 404 with the same
   * message as one that does not exist -- so the copy must cover "gone" and "not yours"
   * without claiming to know which.
   */
  readonly notFound?: FailureNotice
  /** What a 409 means, where the operation can actually produce one. */
  readonly conflict?: FailureNotice
  /**
   * What a 403 means for this operation.
   *
   * Worth overriding on anything that WRITES. The default copy says the account may not
   * *view* this information, which is right for a list and wrong for a posting: a member who
   * lacks `HotelRole.STAFF` can read the ledger perfectly well and was refused the write.
   * Telling them they cannot see what is on screen in front of them is both untrue and
   * unactionable.
   */
  readonly forbidden?: FailureNotice
  /**
   * The catch-all for 5xx and anything unrecognised.
   *
   * Overridable only so a screen can name what is unavailable -- "Analytics are temporarily
   * unavailable" reads better than "This information is". It must never describe the fault
   * itself; nothing about a server fault is the reader's business.
   */
  readonly serverFault?: FailureNotice
}

export function describeFailure(
  error: ApiError,
  overrides: FailureCopyOverrides = {},
): FailureNotice {
  if (error.isUnauthenticated) {
    // The API client's bridge has already ended the session, and the route guard is about to
    // redirect. This is what shows in the frame before that happens.
    return {
      title: 'Your session has ended',
      detail: 'Please sign in again to continue.',
      canRetry: false,
    }
  }

  if (error.isForbidden) {
    return (
      overrides.forbidden ?? {
        title: 'You do not have access to this data',
        detail: 'Your account does not have permission to view this information for this hotel.',
        canRetry: false,
      }
    )
  }

  if (error.status === 404) {
    return (
      overrides.notFound ?? {
        title: 'Not available',
        detail:
          'This record could not be found, or your access to it has been removed. Try returning to the list.',
        canRetry: false,
      }
    )
  }

  if (error.status === 409) {
    return (
      overrides.conflict ?? {
        title: 'That change conflicts with the current state',
        detail: 'The record changed since this page was loaded. Reload and try again.',
        canRetry: true,
      }
    )
  }

  if (error.isRateLimited) {
    const wait =
      error.retryAfterSeconds === null
        ? 'Please wait a moment before trying again.'
        : `Please try again in ${error.retryAfterSeconds} second${error.retryAfterSeconds === 1 ? '' : 's'}.`
    return { title: 'Too many requests', detail: wait, canRetry: true }
  }

  if (error.status === 422) {
    // Reachable only if the UI built a request the API rejects. Nothing the interface offers
    // can do that, so this is a bug report rather than a user error, and the copy does not
    // blame the reader for it.
    return {
      title: 'That request could not be processed',
      detail: 'The details sent were not valid. Adjust your selection and try again.',
      canRetry: false,
    }
  }

  if (error.code === ApiError.NETWORK_CODE) {
    return {
      title: 'Could not reach the server',
      detail: 'Check your connection and try again.',
      canRetry: true,
    }
  }

  if (error.code === ApiError.MALFORMED_CODE) {
    return {
      title: 'Unexpected response',
      detail: 'The server returned something this page could not read. Try again shortly.',
      canRetry: true,
    }
  }

  // Everything else, 5xx included. A server fault might clear on its own, so retrying is
  // offered; nothing about the fault itself is described, because nothing about it is the
  // reader's business.
  return (
    overrides.serverFault ?? {
      title: 'This information is temporarily unavailable',
      detail: 'The data could not be loaded. Try again shortly.',
      canRetry: true,
    }
  )
}
