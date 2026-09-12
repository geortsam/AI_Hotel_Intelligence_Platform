/**
 * Where the access token lives, and the one place that decision is made.
 *
 * ## The security trade-off, stated plainly
 *
 * The backend authenticates with a bearer JWT and exposes no cookie-session endpoint. A
 * token the browser must attach to a header is a token JavaScript must be able to read, so
 * **any XSS vulnerability in this application can exfiltrate the session token.** No choice
 * of Web Storage changes that; `localStorage` and `sessionStorage` are equally readable by
 * script running on the origin. Claiming otherwise would be the dangerous kind of wrong.
 *
 * The genuine fix is an `HttpOnly; Secure; SameSite` cookie issued by the backend, which
 * script cannot read at all. That is a backend change and is not available today.
 *
 * ## Why `sessionStorage` rather than `localStorage`
 *
 * Given both are script-readable, the remaining lever is how long the token survives:
 *
 * * `localStorage` persists until explicitly cleared -- across restarts, indefinitely, on
 *   every tab. On a shared back-office machine that is a token sitting on disk after the
 *   shift ends.
 * * `sessionStorage` is scoped to the tab and cleared when it closes, while still surviving
 *   a reload, which is what session restoration needs.
 *
 * The access token lives 30 minutes, so persistence beyond the tab buys almost nothing and
 * costs a longer exposure window. `sessionStorage` it is.
 *
 * ## Replaceability
 *
 * Everything storage-specific is in this file, behind four functions. Moving to cookies
 * means reimplementing them (or deleting them and letting the browser attach the cookie),
 * not touching the provider, the client, or any component.
 */

/**
 * The storage key.
 *
 * Prefixed so it is identifiable in devtools and cannot collide with another application on
 * the same origin during development.
 */
const TOKEN_KEY = 'ahip.access_token'

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

/** The stored token, or null when there is none or storage is unreadable. */
export function readToken(): string | null {
  try {
    return safeStorage()?.getItem(TOKEN_KEY) ?? null
  } catch {
    return null
  }
}

/** Persist the token for this tab. Silently does nothing when storage is unavailable. */
export function writeToken(token: string): void {
  try {
    safeStorage()?.setItem(TOKEN_KEY, token)
  } catch {
    /* Storage is full or blocked. The in-memory session still works for this page load. */
  }
}

/** Remove the stored token. Called on sign-out and whenever the server rejects it. */
export function clearToken(): void {
  try {
    safeStorage()?.removeItem(TOKEN_KEY)
  } catch {
    /* Nothing to do: if it cannot be removed it could not have been written. */
  }
}
