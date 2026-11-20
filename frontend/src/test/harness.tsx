import { render, type RenderResult } from '@testing-library/react'
import type { ReactNode } from 'react'
import { createMemoryRouter, RouterProvider, type RouteObject } from 'react-router-dom'
import { vi } from 'vitest'

import { AuthProvider } from '@/session/AuthProvider'
import { HotelProvider } from '@/session/HotelProvider'

/**
 * Test seams for authentication.
 *
 * The mock sits at `fetch` -- the real network boundary -- rather than at the auth service
 * or the API client. Stubbing the service would mean the URL, the method, the
 * `Authorization` header and the error translation all go untested, which is most of what
 * this stage actually builds.
 */

export interface StubResponse {
  readonly status?: number
  readonly body?: unknown
  readonly headers?: Readonly<Record<string, string>>
  /** Rejects the fetch, as a DNS failure or refused connection would. */
  readonly networkError?: boolean
}

export type StubHandler = (request: { url: string; init: RequestInit }) => StubResponse

export interface RecordedCall {
  readonly method: string
  readonly url: string
  /** Always null since R1.1: the browser's credentials are cookies, never a header. */
  readonly authorization: string | null
  /** The `X-CSRF-Token` the request carried, if any (R1.1). */
  readonly csrf: string | null
  /** The request's `credentials` mode; `include` sends the session's cookies (R1.1). */
  readonly credentials: string | null
  /** The `If-Match` precondition, if the request carried one (Issue H6). */
  readonly ifMatch: string | null
  readonly body: string | null
}

export interface FetchStub {
  /**
   * Route by method and a path fragment, e.g. `'POST', '/auth/login'`. A fragment starting with
   * `=` matches one path exactly instead, query ignored — `'=/api/v1/'` is the API root and
   * nothing under it, which no substring can say.
   */
  on(method: string, pathFragment: string, response: StubResponse | StubHandler): void
  /** Every request that was issued, in order, with its Authorization header. */
  readonly calls: RecordedCall[]
  restore(): void
}

/**
 * Replace `globalThis.fetch` with a router over stubbed responses.
 *
 * An unmatched request throws rather than returning a default. A test that quietly received
 * `{}` for a call it never configured would pass for the wrong reason, and the failure it
 * was written to catch would be invisible.
 */
export function installFetchStub(): FetchStub {
  const handlers = new Map<string, StubResponse | StubHandler>()
  const calls: RecordedCall[] = []
  // R1.1. Two session routes the client reaches on its own: a renewal after a 401, and the
  // sign-out request. By default the session cannot be renewed -- so a 401 still ends it, as
  // every test written before cookie sessions expects -- and signing out succeeds. Registered
  // first, so a test's own `/auth/session` stub never shadows the renewal.
  handlers.set('POST /auth/session/refresh', {
    status: 401,
    body: { error: { code: 'INVALID_TOKEN', message: 'Not authenticated.', details: [] } },
  })
  handlers.set('POST /auth/session/end', { status: 204 })
  const original = globalThis.fetch

  const stub = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === 'string' ? input : input.toString()
    const method = (init.method ?? 'GET').toUpperCase()

    const headers = (init.headers ?? {}) as Record<string, string>
    calls.push({
      method,
      url,
      authorization: headers.Authorization ?? null,
      csrf: headers['X-CSRF-Token'] ?? null,
      credentials: init.credentials ?? null,
      ifMatch: headers['If-Match'] ?? null,
      body: typeof init.body === 'string' ? init.body : null,
    })

    const key = [...handlers.keys()].find((candidate) => {
      const [handlerMethod, fragment] = candidate.split(' ')
      if (handlerMethod !== method) {
        return false
      }
      const wanted = fragment ?? ''
      return wanted.startsWith('=')
        ? new URL(url, 'http://localhost').pathname === wanted.slice(1)
        : url.includes(wanted)
    })
    if (key === undefined) {
      throw new Error(`No stub configured for ${method} ${url}`)
    }

    const configured = handlers.get(key)!
    const result =
      typeof configured === 'function' ? configured({ url, init }) : configured

    if (result.networkError === true) {
      throw new TypeError('Failed to fetch')
    }

    const status = result.status ?? 200
    const payload = result.body === undefined ? '' : JSON.stringify(result.body)
    return new Response(payload === '' ? null : payload, {
      status,
      headers: { 'Content-Type': 'application/json', ...(result.headers ?? {}) },
    })
  })

  globalThis.fetch = stub as unknown as typeof fetch

  return {
    on(method, pathFragment, response) {
      handlers.set(`${method.toUpperCase()} ${pathFragment}`, response)
    },
    calls,
    restore() {
      globalThis.fetch = original
    },
  }
}

/** A `UserResponse` shaped exactly like the backend's, for stubbing `/auth/me`. */
export const TEST_USER = {
  public_id: '2f1c9a10-0c1e-4d5f-9a4b-6e7c8d9e0f11',
  email: 'reception@example.test',
  full_name: 'Reception Desk',
  is_active: true,
  created_at: '2026-01-01T09:00:00Z',
  last_login_at: '2026-09-09T08:30:00Z',
} as const

/**
 * A CSRF-token-shaped string that is not a real one (R1.1).
 *
 * Since cookie sessions the browser's credentials are `HttpOnly` cookies this application
 * never sees; what a returning browser has is the session's CSRF token. Obviously synthetic,
 * and no real token is ever committed.
 */
export const TEST_TOKEN = 'csrf-token-test-only'

/** Seed a known session, as a returning user's browser would have: its CSRF token. */
export function seedStoredToken(token: string = TEST_TOKEN): void {
  window.sessionStorage.setItem('ahip.csrf_token', token)
}

export function clearStoredToken(): void {
  window.sessionStorage.clear()
}

/**
 * A `HotelResponse` shaped exactly like the backend's, for stubbing `GET /hotels`.
 *
 * The values are the ones the live backend returned during Stage 5.4 verification, so a
 * test and the browser are looking at the same shape -- `timezone` included, since that is
 * what the period logic resolves "today" against.
 */
export const TEST_HOTEL = {
  public_id: 'ceaac449-3ce6-4835-aae8-8ff64712dee2',
  slug: 'meridian-harbour',
  name: 'Meridian Harbour Hotel',
  city: 'Piraeus',
  country_code: 'GR',
  timezone: 'Europe/Athens',
  currency: 'EUR',
  star_rating: 4,
  is_active: true,
} as const

/** One page of hotels, in the backend's `Page` envelope. */
export function hotelPage(items: readonly unknown[] = [TEST_HOTEL], total = items.length) {
  return { items, total, page: 1, page_size: 25, pages: total === 0 ? 0 : 1 }
}

/**
 * Render inside the session and hotel providers, and a memory router.
 *
 * Both providers, because the application mounts both and a page that reads hotel context
 * would otherwise throw. The hotel provider issues `GET /hotels` as soon as a session
 * exists, so a test using this must stub that route -- which is deliberate: a silent default
 * would let a test pass without the request the real application depends on.
 */
export function renderWithAuth(
  routes: RouteObject[],
  { initialEntries = ['/'] }: { initialEntries?: string[] } = {},
): RenderResult {
  const router = createMemoryRouter(routes, { initialEntries })
  return render(
    <AuthProvider>
      <HotelProvider>
        <RouterProvider router={router} />
      </HotelProvider>
    </AuthProvider>,
  )
}

/** Render arbitrary UI inside both providers, with no router. */
export function renderInAuth(ui: ReactNode): RenderResult {
  return render(
    <AuthProvider>
      <HotelProvider>{ui}</HotelProvider>
    </AuthProvider>,
  )
}
