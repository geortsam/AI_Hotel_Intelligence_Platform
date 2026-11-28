import { API_BASE_URL, API_PREFIX } from '@/config/env'
import { ApiError, isErrorResponse } from '@/services/api/ApiError'

/**
 * The one place the frontend talks HTTP.
 *
 * Everything that would otherwise be repeated at each call site lives here exactly once:
 * where the API is, the `/api/v1` prefix, JSON encoding and decoding, the `Accept` and
 * `Content-Type` headers, and what a non-2xx response means. A component that needs data
 * calls a service; a service calls this; nothing else constructs a `fetch`.
 *
 * **No endpoint is defined here.** This is transport, and the domain calls that use it --
 * hotels, bookings, availability -- arrive with the features that need them. Guessing at
 * them now would mean inventing contracts the backend has not been read for.
 */

/**
 * How the client reaches the session, and what it does when the session is rejected.
 *
 * Injected rather than imported. If this module reached into the session store directly,
 * transport would depend on React state and the session store would depend on transport --
 * a cycle, and one that makes the client impossible to test without mounting a provider.
 * The session registers itself here at start-up instead, so this file keeps knowing only
 * about HTTP.
 *
 * ## The cookie session (R1.1)
 *
 * The credentials are `HttpOnly` cookies the browser attaches itself (`credentials:
 * 'include'`); this file never holds one. What it holds is the session's **CSRF token**, which
 * it sends as `X-CSRF-Token` on every request that changes something -- a request without it
 * is refused, so another site riding the cookies gets nowhere.
 */
export interface AuthBridge {
  /** The session's CSRF token, or null when this page knows of no session. */
  readonly getToken: () => string | null
  /** Keep a CSRF token the server has just issued: a new session, or a renewed one. */
  readonly adoptToken?: (csrfToken: string) => void
  /**
   * Called once when the session is rejected and could not be renewed.
   *
   * The session uses it to clear itself. A 401 is first answered by **one** refresh of the
   * session and one retry of the request; only if that fails does this run, so an expired
   * access cookie never signs anyone out while the session itself is alive.
   */
  readonly onUnauthenticated: () => void
}

const noBridge: AuthBridge = { getToken: () => null, onUnauthenticated: () => {} }

let bridge: AuthBridge = noBridge

/** Wires the session into the client. Called once, at start-up. */
export function setAuthBridge(next: AuthBridge): void {
  bridge = next
}

/** Restores the unauthenticated default. For tests, so one does not leak into the next. */
export function resetAuthBridge(): void {
  bridge = noBridge
}

/** The HTTP verbs the API uses. Restated as a union so a typo is a compile error. */
export type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'

/** The header the CSRF token travels in, both ways. */
export const CSRF_HEADER = 'X-CSRF-Token'

/** The route that renews a cookie session. */
const REFRESH_PATH = '/auth/session/refresh'

/**
 * Query parameters. `undefined` values are dropped rather than sent as "undefined".
 *
 * An **array** value is serialised as the same key repeated -- `rooms=DLX:2&rooms=STD:1` --
 * because that is what FastAPI reads back into a `list[str]`, and it is the exact shape the
 * availability endpoint's mixed request takes. Joining an array into one comma-separated
 * value would send a single parameter the server cannot parse.
 */
export type QueryParams = Readonly<
  Record<string, string | number | boolean | readonly string[] | undefined>
>

export interface RequestOptions {
  /** Appended to the URL, correctly encoded. */
  readonly query?: QueryParams
  /** Serialised as JSON. Omit for requests that carry no body. */
  readonly body?: unknown
  /** Merged over the defaults, so a caller can add headers without restating them. */
  readonly headers?: Readonly<Record<string, string>>
  /** Lets a caller cancel, e.g. when a component unmounts mid-request. */
  readonly signal?: AbortSignal
  /**
   * Does not treat a 401 as session expiry, and does not try to renew the session on one.
   *
   * For the endpoints that establish or end a session rather than consume one. The 401 from
   * a WRONG PASSWORD would otherwise be read as "your session expired" and clear a session
   * the user still had. The cookies still go, so a sign-in can set them and a sign-out can
   * clear them.
   */
  readonly anonymous?: boolean
  /**
   * Sends no cookies at all (`credentials: 'omit'`). For a public route that must not learn
   * whether, or as whom, the caller is signed in -- the API root's metadata.
   */
  readonly withoutCookies?: boolean
  /**
   * Says that a 401 from this route describes a credential in the **body**, not the token in
   * the header -- so the session must not be cleared.
   *
   * Added in Stage 5.14 for `POST /auth/change-password`, the one authenticated route that
   * takes a credential of its own. It answers **401 `AUTHENTICATION_FAILED`** for a wrong
   * *current* password while the bearer token is perfectly valid -- verified live. Without
   * this flag the shared 401 handling would end the session, signing a user out for a typo,
   * and doing it precisely while they were trying to secure their account.
   *
   * Distinct from `anonymous`: the header is still sent, because the route requires it. Only
   * the interpretation of the refusal changes.
   */
  readonly credentialInBody?: boolean
}

/** Build the absolute (or origin-relative) URL for one API path. */
function buildUrl(path: string, query?: QueryParams): string {
  const normalisedPath = path.startsWith('/') ? path : `/${path}`
  const url = `${API_BASE_URL}${API_PREFIX}${normalisedPath}`

  if (!query) {
    return url
  }
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined) {
      continue
    }
    if (Array.isArray(value)) {
      // Repeated key, not a joined string: see `QueryParams`. An empty array sends nothing,
      // which is the same as omitting the parameter.
      for (const entry of value) {
        search.append(key, entry)
      }
      continue
    }
    search.append(key, String(value))
  }
  const queryString = search.toString()
  return queryString ? `${url}?${queryString}` : url
}

/**
 * Turn a failed response into an `ApiError`, preferring what the backend actually said.
 *
 * The backend answers every failure with the same envelope, so its `code` and `message` are
 * used when they are there. When they are not -- a proxy returning HTML, a gateway timeout,
 * a body that is not JSON -- the status is still reported honestly rather than dressed up
 * as a backend error that never happened.
 */
function retryAfterOf(response: Response): number | null {
  const raw = response.headers.get('Retry-After')
  if (raw === null) {
    return null
  }
  // The backend always sends whole seconds. An HTTP-date is legal in the header but is not
  // something this API emits, so a non-numeric value is reported as absent rather than
  // guessed at.
  const seconds = Number.parseInt(raw, 10)
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null
}

async function toApiError(response: Response): Promise<ApiError> {
  const retryAfter = retryAfterOf(response)

  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    return new ApiError(
      response.status,
      ApiError.MALFORMED_CODE,
      `The server returned ${response.status} with a body that was not JSON.`,
      [],
      retryAfter,
    )
  }

  if (isErrorResponse(payload)) {
    const { code, message, details } = payload.error
    return new ApiError(response.status, code, message, details ?? [], retryAfter)
  }
  return new ApiError(
    response.status,
    ApiError.MALFORMED_CODE,
    `The server returned ${response.status} in an unrecognised format.`,
    [],
    retryAfter,
  )
}

/**
 * Issue one request and decode its response.
 *
 * `ResponseT` is the caller's claim about the response body; nothing here validates it at
 * runtime, which is the ordinary trade-off for typed clients and is why services -- not
 * components -- are the ones that state it.
 *
 * A 204, or any response with no body, resolves to `undefined`. Callers that expect nothing
 * back should ask for `request<void>`.
 */
export async function request<ResponseT>(
  method: HttpMethod,
  path: string,
  options: RequestOptions = {},
): Promise<ResponseT> {
  const {
    query,
    body,
    headers,
    signal,
    anonymous = false,
    credentialInBody = false,
    withoutCookies = false,
  } = options

  const send = () => {
    // Read at send time, so a retry after a refresh carries the token the refresh adopted.
    const csrf = method === 'GET' ? null : bridge.getToken()
    const init: RequestInit = {
      method,
      // The session's cookies, attached by the browser -- on every request, so a sign-in, a
      // refresh and a sign-out can set and clear them, and every other call is authenticated.
      credentials: withoutCookies ? 'omit' : 'include',
      headers: {
        Accept: 'application/json',
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        // Attached here and nowhere else, so there is exactly one place the CSRF token
        // reaches the wire. No component or service builds this header.
        ...(csrf === null ? {} : { [CSRF_HEADER]: csrf }),
        ...headers,
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      ...(signal ? { signal } : {}),
    }
    return transmit(buildUrl(path, query), init)
  }

  let response = await send()

  if (!response.ok) {
    let error = await toApiError(response)
    if (error.isUnauthenticated && !anonymous && !credentialInBody) {
      // The access cookie was rejected -- most often simply expired. Renew the session once
      // and send this request once more; only a session that cannot be renewed has ended.
      if (bridge.getToken() !== null && (await refreshSession())) {
        response = await send()
        if (response.ok) {
          return decode<ResponseT>(response)
        }
        error = await toApiError(response)
      }
      if (error.isUnauthenticated) {
        bridge.onUnauthenticated()
      }
    }
    throw error
  }

  return decode<ResponseT>(response)
}

/** `fetch`, with a rejected promise turned into the network `ApiError`. */
async function transmit(url: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init)
  } catch (cause) {
    // A rejected fetch means no response existed: DNS, connection refused, CORS, or an
    // abort. Status 0 keeps that distinguishable from every real HTTP status.
    throw new ApiError(
      0,
      ApiError.NETWORK_CODE,
      cause instanceof Error ? cause.message : 'The request could not be sent.',
    )
  }
}

/** The one refresh in flight, shared by every request that met the same expired cookie. */
let refreshing: Promise<boolean> | null = null

/**
 * Renew the cookie session: `POST /auth/session/refresh` with the CSRF token. Resolves
 * whether the session lives on. Concurrent callers share one attempt, because a refresh token
 * works once -- a second, parallel use of it would read as a replay and end the session.
 */
export function refreshSession(): Promise<boolean> {
  if (refreshing !== null) {
    return refreshing
  }
  const csrf = bridge.getToken()
  if (csrf === null) {
    return Promise.resolve(false)
  }
  refreshing = transmit(buildUrl(REFRESH_PATH), {
    method: 'POST',
    credentials: 'include',
    headers: { Accept: 'application/json', [CSRF_HEADER]: csrf },
  })
    .then((response) => {
      if (!response.ok) {
        return false
      }
      adoptCsrfFrom(response)
      return true
    })
    .catch(() => false)
    .finally(() => {
      refreshing = null
    })
  return refreshing
}

/** Keep the CSRF token a response carries, when it carries one (a session was just set). */
function adoptCsrfFrom(response: Response): void {
  const issued = response.headers.get(CSRF_HEADER)
  if (issued !== null && issued !== '') {
    bridge.adoptToken?.(issued)
  }
}

/** A successful response's body: `undefined` for a 204 or an empty body, else its JSON. */
async function decode<ResponseT>(response: Response): Promise<ResponseT> {
  adoptCsrfFrom(response)

  if (response.status === 204 || response.headers.get('Content-Length') === '0') {
    return undefined as ResponseT
  }

  const text = await response.text()
  if (text === '') {
    return undefined as ResponseT
  }
  try {
    return JSON.parse(text) as ResponseT
  } catch {
    throw new ApiError(
      response.status,
      ApiError.MALFORMED_CODE,
      'The server returned a successful response whose body was not JSON.',
    )
  }
}

/**
 * The verbs, bound to {@link request}.
 *
 * Sugar with a purpose: a service reads `api.get<Hotel>('/hotels/1')` rather than repeating
 * the method string, and the method can no longer be passed in the wrong argument position.
 */
export const api = {
  get: <ResponseT>(path: string, options?: RequestOptions) =>
    request<ResponseT>('GET', path, options),
  post: <ResponseT>(path: string, options?: RequestOptions) =>
    request<ResponseT>('POST', path, options),
  patch: <ResponseT>(path: string, options?: RequestOptions) =>
    request<ResponseT>('PATCH', path, options),
  put: <ResponseT>(path: string, options?: RequestOptions) =>
    request<ResponseT>('PUT', path, options),
  delete: <ResponseT = void>(path: string, options?: RequestOptions) =>
    request<ResponseT>('DELETE', path, options),
} as const
