import { api } from '@/services/api/client'
import type {
  AuthenticatedUser,
  LoginRequest,
  PasswordChangeRequest,
  SessionResponse,
  TokenResponse,
} from '@/types/auth'

/**
 * The authentication calls this application makes.
 *
 * Two of them, because two is what Stage 5.3 needs and what the backend supports for a
 * sign-in flow. Transport, headers, base URL and error translation all stay in the API
 * client; this module only knows which paths exist and what they return.
 *
 * **Deliberately absent**, though the backend provides them:
 *
 * * `POST /auth/register` -- there is no sign-up screen in scope, and an unused method is
 *   dead code that still has to be maintained and reviewed.
 * `POST /auth/change-password` was added in Stage 5.14, with exactly the hazard this note
 * warned about: see its docstring below.
 *
 * Still deliberately absent:
 *
 * * `POST /auth/register` -- there is no sign-up screen in scope, and the member screen
 *   cannot create accounts either: `POST .../members` names an account that must already
 *   exist. An unused method is dead code that still has to be maintained and reviewed.
 *
 * **The browser signs in to a cookie session (R1.1)**: `POST /auth/session` sets `HttpOnly`
 * cookies this code never reads, and `POST /auth/session/end` ends the session on the server.
 * `POST /auth/login`, the bearer login, is for API clients and is not used here.
 */
export const authService = {
  /**
   * Sign in to a cookie session.
   *
   * `anonymous` matters. Without it the 401 that a wrong password produces would be read by
   * the client as "your session expired", clearing a session the user still legitimately had.
   *
   * Throws `ApiError` on rejection: 401 `AUTHENTICATION_FAILED` for any bad credential,
   * 429 `RATE_LIMITED` with `retryAfterSeconds` when the address has tried too often.
   */
  login(credentials: LoginRequest, signal?: AbortSignal): Promise<SessionResponse> {
    return api.post<SessionResponse>('/auth/session', {
      body: credentials,
      anonymous: true,
      ...(signal ? { signal } : {}),
    })
  },

  /**
   * End the cookie session: the server revokes it -- its access tokens stop at once -- and
   * clears its cookies. Idempotent: with no session it still answers 204.
   */
  logout(signal?: AbortSignal): Promise<void> {
    return api.post<void>('/auth/session/end', { anonymous: true, ...(signal ? { signal } : {}) })
  },

  /**
   * Who the current token belongs to.
   *
   * Also the session-restoration check: the backend re-reads the user from the database on
   * every call rather than trusting token claims, so this returns 401 for a token that is
   * expired, revoked by a password change, or belongs to an account since disabled. That
   * makes it the only honest way to find out whether a stored token still works — decoding
   * the JWT client-side would miss all three.
   */
  getCurrentUser(signal?: AbortSignal): Promise<AuthenticatedUser> {
    return api.get<AuthenticatedUser>('/auth/me', signal ? { signal } : {})
  },

  /**
   * Change the signed-in user's own password.
   *
   * **The returned token must be adopted.** The backend revokes every token issued before
   * the change -- including the one that authorised this request -- and the `TokenResponse`
   * here is the caller's way to continue without signing in again. `AuthProvider.adoptToken`
   * exists for this and nothing else.
   *
   * `credentialInBody` matters as much. This route answers **401** for a wrong *current*
   * password, exactly as login does, while the bearer token remains valid; without the flag
   * the client's shared 401 handling would read that as expiry and end the session over a
   * typo. Verified live: wrong current password -> 401 `AUTHENTICATION_FAILED`, token still
   * good afterwards.
   *
   * Throws `ApiError`: **401** for a wrong current password, **422** for a new password
   * under twelve characters or a missing field, **429** `RATE_LIMITED` with
   * `retryAfterSeconds` -- the route is rate-limited to bound how often one source can make
   * the server spend Argon2id's 64 MiB.
   */
  changePassword(payload: PasswordChangeRequest, signal?: AbortSignal): Promise<TokenResponse> {
    return api.post<TokenResponse>('/auth/change-password', {
      body: payload,
      credentialInBody: true,
      ...(signal ? { signal } : {}),
    })
  },
} as const
