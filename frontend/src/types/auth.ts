/**
 * The authentication contract, transcribed from the backend.
 *
 * Every field below was read from `app/schemas/auth.py` and `app/api/v1/endpoints/auth.py`.
 * Nothing is inferred, and nothing the backend does not return appears here -- notably there
 * is no refresh token, because the API issues none.
 */

/** `POST /auth/login` request body. Mirrors `LoginRequest`. */
export interface LoginRequest {
  /** The account's email address. The backend calls this field `email`, not `username`. */
  readonly email: string
  readonly password: string
}

/**
 * `POST /auth/login` response. Mirrors `TokenResponse`.
 *
 * `token_type` is the RFC 6750 scheme and is always `"bearer"`; it is typed as `string`
 * because that is what the API declares, and pinning a literal here would make a future
 * scheme change a type error in the wrong place.
 *
 * There is deliberately no `refresh_token`: the backend has no refresh endpoint, so a
 * session ends when the token expires and the user signs in again.
 */
export interface TokenResponse {
  readonly access_token: string
  readonly token_type: string
  /** Lifetime in seconds. 1800 at the time of writing -- read, never assumed. */
  readonly expires_in: number
}

/**
 * `GET /auth/me` response. Mirrors `UserResponse`.
 *
 * Note what is absent: no role, no hotel membership, no permissions. The backend documents
 * this endpoint as returning "who the caller is -- not what they may access", so the
 * frontend must not infer authorization from it.
 */
export interface AuthenticatedUser {
  readonly public_id: string
  readonly email: string
  readonly full_name: string
  readonly is_active: boolean
  /** ISO 8601 timestamp. */
  readonly created_at: string
  /** ISO 8601 timestamp, or null when the account has never signed in. */
  readonly last_login_at: string | null
}

/**
 * `POST /auth/change-password` body. Mirrors `PasswordChangeRequest`.
 *
 * The current password is required as well as a valid token: a token alone is not authority
 * to replace the credential it was minted from. `new_password` carries the same twelve-
 * character floor registration uses -- shorter is a 422 naming the field, verified.
 *
 * Whose password changes is decided by the **token**, never by the body: there is no
 * `email` or `public_id` here, so this endpoint cannot be aimed at another account.
 */
export interface PasswordChangeRequest {
  readonly current_password: string
  readonly new_password: string
}

/** The backend's floor, from `PasswordField`. Checked here so a short one is not a round trip. */
export const MIN_PASSWORD_LENGTH = 12
/** The backend's ceiling, from the same annotation. Argon2id hashes whatever it is given. */
export const MAX_PASSWORD_LENGTH = 256
