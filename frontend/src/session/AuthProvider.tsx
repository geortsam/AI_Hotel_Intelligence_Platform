import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import { ApiError } from '@/services/api/ApiError'
import { resetAuthBridge, setAuthBridge } from '@/services/api/client'
import { authService } from '@/services/auth/authService'
import { clearToken, readToken, writeToken } from '@/session/tokenStorage'
import type { AuthenticatedUser, LoginRequest } from '@/types/auth'

/**
 * Where the session is in its lifecycle.
 *
 * `restoring` is a real state, not a loading flag bolted onto the other two. Without it the
 * application cannot tell "no session" from "we have not looked yet", and every protected
 * route would bounce a returning user to the sign-in page for one frame before restoring
 * them — which looks exactly like being signed out.
 */
export type AuthStatus = 'restoring' | 'authenticated' | 'unauthenticated'

export interface AuthContextValue {
  readonly status: AuthStatus
  /** The signed-in identity, or null in either non-authenticated state. */
  readonly user: AuthenticatedUser | null
  /**
   * Why the session ended, when it ended on its own.
   *
   * Set only when the server rejected a token mid-session; cleared on a fresh sign-in.
   * The sign-in page shows it so an expiry is explained rather than looking like a random
   * logout.
   */
  readonly expiryNotice: string | null
  /** Exchanges credentials for a session. Throws `ApiError`; the caller renders the failure. */
  readonly login: (credentials: LoginRequest) => Promise<void>
  /** Ends the session locally. See the note on the absent backend endpoint. */
  readonly logout: () => void
  /**
   * Replace the session's token with one the server has just issued.
   *
   * Added in Stage 5.14, and its only caller is a password change. That endpoint **revokes
   * every token minted before it, including the one that authorised the request**, and
   * returns the replacement -- so without adopting it the next call 401s and the user is
   * signed out by the act of securing their own account.
   *
   * Not a sign-in: it does not re-read the identity and does not touch `status`, because the
   * person is the same and was authenticated a moment ago.
   */
  readonly adoptToken: (accessToken: string) => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

const EXPIRY_NOTICE = 'Your session has ended. Please sign in again.'

/**
 * The session, and the only place it is held.
 *
 * Header, sidebar, pages and route guards all read from this context. Nothing copies the
 * token or the user into its own state, so there is no second copy to go stale — which is
 * the failure that makes an application show a signed-in header after the session died.
 *
 * ## Restoration
 *
 * On mount, if a token is stored, `GET /auth/me` decides whether it still works. The
 * backend re-reads the user from the database on every call, so this is the only honest
 * check: it catches an expired token, one revoked by a password change, and an account
 * since disabled. Decoding the JWT here would miss the last two and would mean trusting a
 * value the client cannot verify.
 *
 * ## Signing out
 *
 * **The backend has no logout endpoint**, and this stage does not invent one. Signing out
 * is therefore local: the token is dropped from storage and from memory. The honest
 * consequence is that a token already captured elsewhere stays valid for the remainder of
 * its 30 minutes; only a server-side revocation list could change that, and the API offers
 * none. (The one revocation the backend does have is `password_changed_at`, which a
 * password change triggers — not something sign-out can use.)
 */
export function AuthProvider({ children }: { readonly children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('restoring')
  const [user, setUser] = useState<AuthenticatedUser | null>(null)
  const [expiryNotice, setExpiryNotice] = useState<string | null>(null)

  /**
   * The token, held in a ref rather than state.
   *
   * The API client asks for it synchronously while building a request, and a ref is always
   * current at that moment. State would hand back the value from the last render, which
   * during sign-in is `null` — the request immediately after logging in would go out
   * unauthenticated.
   */
  const tokenRef = useRef<string | null>(null)

  const endSession = useCallback((notice: string | null) => {
    tokenRef.current = null
    clearToken()
    setUser(null)
    setExpiryNotice(notice)
    setStatus('unauthenticated')
  }, [])

  /*
   * Wire the client to this session.
   *
   * Registration and teardown are ONE effect, deliberately. An earlier version registered
   * during render and reset in a separate effect's cleanup, which broke under React's
   * StrictMode: the development double-invoke runs mount, cleanup, mount, so the cleanup
   * unregistered the bridge and nothing put it back. Every later request then went out with
   * no `Authorization` header -- login succeeded and the very next call returned 401.
   *
   * Pairing them means the second mount re-registers. This effect is declared before the
   * restoration effect below, and React runs effects in declaration order, so the bridge is
   * always in place before the first request is issued.
   *
   * `getToken` reads a ref, so the closure captured here never goes stale.
   */
  useEffect(() => {
    setAuthBridge({
      getToken: () => tokenRef.current,
      // Fires when the server rejects the token on an authenticated request. It clears the
      // session; it never retries, because there is no refresh token to retry with.
      onUnauthenticated: () => {
        if (tokenRef.current !== null) {
          endSession(EXPIRY_NOTICE)
        }
      },
    })
    return resetAuthBridge
  }, [endSession])

  // Restore on load.
  useEffect(() => {
    const stored = readToken()
    if (stored === null) {
      setStatus('unauthenticated')
      return
    }

    tokenRef.current = stored
    const controller = new AbortController()
    let cancelled = false

    authService
      .getCurrentUser(controller.signal)
      .then((restored) => {
        if (cancelled) {
          return
        }
        setUser(restored)
        setStatus('authenticated')
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return
        }
        // A rejected token is an ordinary outcome here, not an error worth announcing:
        // the user simply has to sign in. A network failure is different — the token may
        // be perfectly good — but with no way to reach the API there is nothing to restore
        // either, so both end at the sign-in page. Only a rejection explains itself.
        const rejected = error instanceof ApiError && error.isUnauthenticated
        endSession(rejected ? EXPIRY_NOTICE : null)
      })

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [endSession])

  const login = useCallback(async (credentials: LoginRequest) => {
    const token = await authService.login(credentials)

    // Set before fetching the identity: `/auth/me` is an authenticated call and reads the
    // token through the bridge.
    tokenRef.current = token.access_token
    writeToken(token.access_token)

    try {
      const authenticated = await authService.getCurrentUser()
      setUser(authenticated)
      setExpiryNotice(null)
      setStatus('authenticated')
    } catch (error: unknown) {
      // Credentials were accepted but the identity could not be read. Keeping a token we
      // cannot describe would leave the UI signed in as nobody, so the half-session is
      // discarded and the caller sees the failure.
      tokenRef.current = null
      clearToken()
      setStatus('unauthenticated')
      throw error
    }
  }, [])

  const logout = useCallback(() => {
    endSession(null)
  }, [endSession])

  const adoptToken = useCallback((accessToken: string) => {
    tokenRef.current = accessToken
    writeToken(accessToken)
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({ status, user, expiryNotice, login, logout, adoptToken }),
    [status, user, expiryNotice, login, logout, adoptToken],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

/**
 * Read the session.
 *
 * Throws outside a provider rather than returning a default. A silent default would let a
 * component render as though signed out while the real session was fine, and the bug would
 * surface as a mysterious redirect rather than as a stack trace pointing at the mistake.
 */
export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (context === null) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}
