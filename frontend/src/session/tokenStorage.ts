/**
 * Where the session's **CSRF token** lives, and the one place that decision is made (R1.1).
 *
 * ## No credential lives here any more
 *
 * Until R1.1 this file held the bearer access token in `sessionStorage`, where any script on
 * the origin -- an XSS payload included -- could read it and take the session elsewhere. The
 * backend now signs the browser in with `POST /auth/session`, which keeps the access and
 * refresh tokens in `HttpOnly; Secure; SameSite=Strict` cookies that script cannot read at
 * all. The browser attaches them; this application never sees them.
 *
 * What remains for script is the CSRF token, which the page must echo in `X-CSRF-Token` on
 * every request that changes something. It is not a credential: it proves only that a request
 * came from this page, and is useless without the cookies, which never leave the browser's
 * jar. An XSS payload can still act *in* the page while it runs -- no storage choice changes
 * that -- but it can no longer carry the session away.
 *
 * ## Where it is read from
 *
 * The backend sets it in a readable cookie, `__Host-ahip_csrf`, which survives a new tab, and
 * also returns it in the body and the `X-CSRF-Token` response header of every response that
 * starts or renews a session. The cookie is read first; `sessionStorage` is the fallback for
 * an environment that cannot hold a `__Host-` cookie, such as a test DOM on plain HTTP.
 */

/** The readable cookie the backend sets beside the `HttpOnly` ones. */
export const CSRF_COOKIE = '__Host-ahip_csrf'

/**
 * The storage key.
 *
 * Prefixed so it is identifiable in devtools and cannot collide with another application on
 * the same origin during development.
 */
const TOKEN_KEY = 'ahip.csrf_token'

/**
 * Web Storage throws rather than returning null in several real situations: Safari in
 * private mode historically, and any browser configured to block site data. A sign-in
 * screen that crashes because storage is unavailable is worse than one that works for the
 * current page load, so every access is guarded.
 */
function safeStorage(): Storage | null {
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

function fromCookie(): string | null {
  try {
    for (const part of document.cookie.split(';')) {
      const [name, ...value] = part.trim().split('=')
      if (name === CSRF_COOKIE && value.length > 0) {
        const token = value.join('=')
        return token === '' ? null : token
      }
    }
  } catch {
    /* No document, or cookies blocked: fall back to storage. */
  }
  return null
}

/** The session's CSRF token, or null when there is no session this page knows of. */
export function readToken(): string | null {
  const cookie = fromCookie()
  if (cookie !== null) {
    return cookie
  }
  try {
    return safeStorage()?.getItem(TOKEN_KEY) ?? null
  } catch {
    return null
  }
}

/** Keep the CSRF token for this tab. Silently does nothing when storage is unavailable. */
export function writeToken(token: string): void {
  try {
    safeStorage()?.setItem(TOKEN_KEY, token)
  } catch {
    /* Storage is full or blocked. The in-memory session still works for this page load. */
  }
}

/** Forget the CSRF token. Called on sign-out and whenever the session is found to have ended. */
export function clearToken(): void {
  try {
    safeStorage()?.removeItem(TOKEN_KEY)
  } catch {
    /* Nothing to do: if it cannot be removed it could not have been written. */
  }
}
