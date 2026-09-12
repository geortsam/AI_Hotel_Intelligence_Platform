import type { ApiError } from '@/services/api/ApiError'
import {
  describeFailure as describeApiFailure,
  type FailureNotice,
} from '@/services/api/failures'

/**
 * The analytics screens' failure copy.
 *
 * The nine-status decision moved to `@/services/api/failures` in Stage 5.5, when bookings
 * needed identical handling; this file now supplies only the two strings that are genuinely
 * about analytics. Everything else -- 401, 403, 429, 422, network, malformed, 5xx -- is
 * shared, deliberately, so a user does not have to learn a second vocabulary of failures per
 * screen.
 *
 * A note on 403 and 404, because they are not what one would guess. Analytics routes require
 * *membership* and no particular role, and a non-member is answered with **404 "Hotel not
 * found."** -- byte-identical to a hotel that does not exist. That is deliberate on the
 * backend's part: a 403 would confirm the property exists and turn the endpoint into an
 * existence oracle. So the 404 copy below covers both "gone" and "not yours" without
 * distinguishing them, and 403 is handled for completeness rather than because this endpoint
 * produces one.
 */
export type { FailureNotice }

export function describeFailure(error: ApiError): FailureNotice {
  return describeApiFailure(error, {
    notFound: {
      title: 'Hotel not available',
      detail:
        'This hotel could not be found, or your access to it has been removed. Try selecting another hotel.',
      canRetry: false,
    },
    serverFault: {
      title: 'Analytics are temporarily unavailable',
      detail: 'The data could not be loaded. Try again shortly.',
      canRetry: true,
    },
  })
}
