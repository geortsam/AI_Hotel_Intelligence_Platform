import type { ErrorDetail, ErrorResponse } from '@/types/api'

/**
 * Every failure the API client can produce, as one type.
 *
 * Callers get a single thing to catch whether the request was refused by the backend, never
 * reached it, or came back as something that was not JSON. Without that, each call site
 * ends up re-deciding what a failure looks like, which is how inconsistent error handling
 * spreads through a UI.
 *
 * `status` is 0 when no HTTP response was received at all -- a DNS failure, a refused
 * connection, an aborted request. That is deliberately distinguishable from any real status
 * code, so "the server said no" and "there was no server" never get confused.
 */
export class ApiError extends Error {
  /** HTTP status, or 0 when the request never produced a response. */
  readonly status: number
  /** The backend's stable error code, or one of the synthetic codes below. */
  readonly code: string
  /** Field-level detail; empty unless the backend sent validation errors. */
  readonly details: readonly ErrorDetail[]
  /**
   * Seconds to wait, from the `Retry-After` header on a 429.
   *
   * `null` when the server sent none. The distinction matters: the backend computes this
   * from the window it actually counted against, so it is the honest wait. Inventing a
   * fallback number would show the user a countdown that is not the server's.
   */
  readonly retryAfterSeconds: number | null

  constructor(
    status: number,
    code: string,
    message: string,
    details: readonly ErrorDetail[] = [],
    retryAfterSeconds: number | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
    this.retryAfterSeconds = retryAfterSeconds
  }

  /** The request never reached the backend. */
  static readonly NETWORK_CODE = 'NETWORK_ERROR'
  /** A response arrived but was not the documented error envelope. */
  static readonly MALFORMED_CODE = 'MALFORMED_RESPONSE'

  /* The backend's own codes, transcribed from app.core.errors. Named so a caller matches on
   * a constant rather than retyping the string at each call site. */
  /** Login was refused. Identical for an unknown address, a wrong password, a disabled account. */
  static readonly AUTHENTICATION_FAILED = 'AUTHENTICATION_FAILED'
  /** A bearer token was missing, malformed, expired or revoked. */
  static readonly INVALID_TOKEN = 'INVALID_TOKEN'
  /** Authenticated, but the role is too low. */
  static readonly FORBIDDEN = 'FORBIDDEN'
  /** Too many requests from this client address. Carries {@link retryAfterSeconds}. */
  static readonly RATE_LIMITED = 'RATE_LIMITED'

  /** True when the server rejected the caller's identity: a 401 in any of its forms. */
  get isUnauthenticated(): boolean {
    return this.status === 401
  }

  /** True when the caller is known but not permitted. */
  get isForbidden(): boolean {
    return this.status === 403
  }

  /** True when the caller is being rate limited. */
  get isRateLimited(): boolean {
    return this.status === 429
  }

  /** True for 4xx: the request itself was refused and repeating it unchanged will fail again. */
  get isClientError(): boolean {
    return this.status >= 400 && this.status < 500
  }

  /** True for 5xx: the backend failed, and the same request might succeed later. */
  get isServerError(): boolean {
    return this.status >= 500
  }
}

/** Whether an unknown value is the backend's documented error envelope. */
export function isErrorResponse(value: unknown): value is ErrorResponse {
  if (typeof value !== 'object' || value === null || !('error' in value)) {
    return false
  }
  const { error } = value as { error: unknown }
  return (
    typeof error === 'object' &&
    error !== null &&
    typeof (error as { code?: unknown }).code === 'string' &&
    typeof (error as { message?: unknown }).message === 'string'
  )
}
