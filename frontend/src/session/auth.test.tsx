import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { RouteObject } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AppShell } from '@/components/layout/AppShell'
import { LoginPage } from '@/pages/LoginPage'
import { ProtectedRoute } from '@/router/ProtectedRoute'
import { ROUTES } from '@/router/routes'
import {
  clearStoredToken,
  installFetchStub,
  renderWithAuth,
  seedStoredToken,
  TEST_TOKEN,
  TEST_USER,
  type FetchStub,
} from '@/test/harness'

/**
 * Authentication behaviour, end to end through the real router, provider and API client.
 *
 * Only `fetch` is stubbed. Everything between a keystroke on the sign-in form and the
 * outgoing HTTP request is the code that ships -- including the `Authorization` header, the
 * error envelope parsing and the redirect logic, all of which a service-level mock would
 * have skipped.
 */

let fetchStub: FetchStub

/** The route tree under test: one public sign-in route, everything else guarded. */
const routes: RouteObject[] = [
  { path: ROUTES.login, element: <LoginPage /> },
  {
    element: <ProtectedRoute />,
    children: [
      {
        path: ROUTES.dashboard,
        element: <AppShell />,
        children: [
          { index: true, element: <p>dashboard content</p> },
          { path: 'analytics', element: <p>analytics content</p> },
          { path: '*', element: <p>not found content</p> },
        ],
      },
    ],
  },
]

const CREDENTIALS = { email: 'reception@example.test', password: 'not-a-real-password' }

function stubLoginSuccess() {
  fetchStub.on('POST', '/auth/login', {
    body: { access_token: TEST_TOKEN, token_type: 'bearer', expires_in: 1800 },
  })
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
}

/** Fill and submit the sign-in form. */
async function signIn(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Email address'), CREDENTIALS.email)
  await user.type(screen.getByLabelText('Password'), CREDENTIALS.password)
  await user.click(screen.getByRole('button', { name: 'Sign in' }))
}

beforeEach(() => {
  fetchStub = installFetchStub()
  clearStoredToken()
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
  vi.restoreAllMocks()
})

// ======================================================================================
// Protected routes
// ======================================================================================

describe('protected routes', () => {
  it('sends an unauthenticated visitor to sign in', async () => {
    renderWithAuth(routes, { initialEntries: [ROUTES.dashboard] })

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText('dashboard content')).not.toBeInTheDocument()
  })

  it('sends an unauthenticated deep link to sign in too', async () => {
    renderWithAuth(routes, { initialEntries: ['/analytics'] })

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText('analytics content')).not.toBeInTheDocument()
  })

  it('lets an authenticated user through', async () => {
    fetchStub.on('GET', '/auth/me', { body: TEST_USER })
    seedStoredToken()

    renderWithAuth(routes, { initialEntries: [ROUTES.dashboard] })

    expect(await screen.findByText('dashboard content')).toBeInTheDocument()
  })

  it('does not flash protected content while the session is being restored', async () => {
    // The failure this guards against: rendering the dashboard for one frame before
    // /auth/me answers, which leaks the previous user's screen on a shared machine.
    fetchStub.on('GET', '/auth/me', { body: TEST_USER })
    seedStoredToken()

    renderWithAuth(routes, { initialEntries: [ROUTES.dashboard] })

    expect(screen.getByRole('status')).toHaveTextContent('Restoring your session')
    expect(screen.queryByText('dashboard content')).not.toBeInTheDocument()
    expect(await screen.findByText('dashboard content')).toBeInTheDocument()
  })

  it('keeps the 404 behaviour for an authenticated user', async () => {
    fetchStub.on('GET', '/auth/me', { body: TEST_USER })
    seedStoredToken()

    renderWithAuth(routes, { initialEntries: ['/no-such-page'] })

    expect(await screen.findByText('not found content')).toBeInTheDocument()
  })

  it('sends an unauthenticated visitor to an unknown URL to sign in, not to a 404', async () => {
    // A 404 for an unauthenticated caller would say which paths exist.
    renderWithAuth(routes, { initialEntries: ['/no-such-page'] })

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText('not found content')).not.toBeInTheDocument()
  })
})

// ======================================================================================
// Signing in
// ======================================================================================

describe('signing in', () => {
  it('authenticates and lands on the dashboard', async () => {
    stubLoginSuccess()
    const user = userEvent.setup()
    renderWithAuth(routes, { initialEntries: [ROUTES.login] })
    await screen.findByRole('heading', { name: 'Sign in' })

    await signIn(user)

    expect(await screen.findByText('dashboard content')).toBeInTheDocument()
  })

  it('returns the user to the page they originally asked for', async () => {
    stubLoginSuccess()
    const user = userEvent.setup()
    // Start at a protected deep link; the guard redirects and remembers where.
    renderWithAuth(routes, { initialEntries: ['/analytics'] })
    await screen.findByRole('heading', { name: 'Sign in' })

    await signIn(user)

    expect(await screen.findByText('analytics content')).toBeInTheDocument()
  })

  it('sends the credentials to the login endpoint and nothing else', async () => {
    stubLoginSuccess()
    const user = userEvent.setup()
    renderWithAuth(routes, { initialEntries: [ROUTES.login] })
    await screen.findByRole('heading', { name: 'Sign in' })

    await signIn(user)
    await screen.findByText('dashboard content')

    const loginCall = fetchStub.calls.find((call) => call.url.includes('/auth/login'))
    expect(loginCall?.method).toBe('POST')
    expect(JSON.parse(loginCall?.body ?? '{}')).toEqual(CREDENTIALS)
    // Signing in must not carry a bearer token: a stale one is meaningless here, and a 401
    // from a wrong password would otherwise be read as "your session expired".
    expect(loginCall?.authorization).toBeNull()
  })

  it('attaches the bearer token to authenticated requests', async () => {
    stubLoginSuccess()
    const user = userEvent.setup()
    renderWithAuth(routes, { initialEntries: [ROUTES.login] })
    await screen.findByRole('heading', { name: 'Sign in' })

    await signIn(user)
    await screen.findByText('dashboard content')

    const meCall = fetchStub.calls.find((call) => call.url.includes('/auth/me'))
    expect(meCall?.authorization).toBe(`Bearer ${TEST_TOKEN}`)
  })

  it('shows a loading state and blocks resubmission while the request is in flight', async () => {
    // A deferred response, so the in-flight window can actually be observed. Without this
    // the request resolves before any assertion runs and the "loading state" is untested.
    let resolveLogin: ((value: { body: unknown }) => void) | undefined
    const pending = new Promise<{ body: unknown }>((resolve) => {
      resolveLogin = resolve
    })
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/login')) {
        const result = await pending
        return new Response(JSON.stringify(result.body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify(TEST_USER), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }) as unknown as typeof fetch

    const user = userEvent.setup()
    renderWithAuth(routes, { initialEntries: [ROUTES.login] })
    await screen.findByRole('heading', { name: 'Sign in' })

    await user.type(screen.getByLabelText('Email address'), CREDENTIALS.email)
    await user.type(screen.getByLabelText('Password'), CREDENTIALS.password)
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    // Mid-flight: the button says so, and both it and the fields are disabled so the
    // request cannot be fired twice by an impatient double-click.
    const submitting = await screen.findByRole('button', { name: 'Signing in…' })
    expect(submitting).toBeDisabled()
    expect(screen.getByLabelText('Email address')).toBeDisabled()
    expect(screen.getByLabelText('Password')).toBeDisabled()

    resolveLogin?.({
      body: { access_token: TEST_TOKEN, token_type: 'bearer', expires_in: 1800 },
    })

    expect(await screen.findByText('dashboard content')).toBeInTheDocument()
  })
})

// ======================================================================================
// Failures
// ======================================================================================

describe('failed sign-in', () => {
  async function attemptWith(response: Parameters<FetchStub['on']>[2]) {
    fetchStub.on('POST', '/auth/login', response)
    const user = userEvent.setup()
    renderWithAuth(routes, { initialEntries: [ROUTES.login] })
    await screen.findByRole('heading', { name: 'Sign in' })
    await signIn(user)
    return user
  }

  it('reports invalid credentials in the backend’s own words', async () => {
    await attemptWith({
      status: 401,
      body: {
        error: {
          code: 'AUTHENTICATION_FAILED',
          message: 'Invalid email or password.',
          details: [],
        },
      },
    })

    // Reused verbatim: the backend returns one message for an unknown address, a wrong
    // password and a disabled account, and rephrasing risks reintroducing the distinction.
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid email or password.')
    expect(screen.queryByText('dashboard content')).not.toBeInTheDocument()
  })

  it('announces the failure to assistive technology', async () => {
    await attemptWith({
      status: 401,
      body: {
        error: { code: 'AUTHENTICATION_FAILED', message: 'Invalid email or password.', details: [] },
      },
    })

    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })

  it('clears the password field after a rejection', async () => {
    // A shared terminal must not keep the previous attempt in the field.
    await attemptWith({
      status: 401,
      body: {
        error: { code: 'AUTHENTICATION_FAILED', message: 'Invalid email or password.', details: [] },
      },
    })

    await screen.findByRole('alert')
    expect(screen.getByLabelText('Password')).toHaveValue('')
    // The email survives, so a retry is one field, not two.
    expect(screen.getByLabelText('Email address')).toHaveValue(CREDENTIALS.email)
  })

  it('uses the server’s Retry-After on a rate limit', async () => {
    await attemptWith({
      status: 429,
      headers: { 'Retry-After': '42' },
      body: {
        error: {
          code: 'RATE_LIMITED',
          message: 'Too many requests. Please wait before trying again.',
          details: [],
        },
      },
    })

    expect(await screen.findByRole('alert')).toHaveTextContent('42 seconds')
  })

  it('does not invent a wait when the server sends no Retry-After', async () => {
    await attemptWith({
      status: 429,
      body: {
        error: {
          code: 'RATE_LIMITED',
          message: 'Too many requests. Please wait before trying again.',
          details: [],
        },
      },
    })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Too many sign-in attempts')
    expect(alert.textContent).not.toMatch(/\d+ seconds?/)
  })

  it('explains a network failure without blaming the credentials', async () => {
    await attemptWith({ networkError: true })

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the server')
  })

  it('does not surface server internals on a 500', async () => {
    await attemptWith({
      status: 500,
      body: {
        error: {
          code: 'INTERNAL_ERROR',
          message: 'relation "users" does not exist: psycopg.errors.UndefinedTable',
          details: [],
        },
      },
    })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The server could not complete the request')
    for (const leak of ['psycopg', 'relation', 'users', 'UndefinedTable']) {
      expect(alert.textContent).not.toContain(leak)
    }
  })

  it('handles a malformed response without crashing', async () => {
    await attemptWith({ status: 200, body: { unexpected: 'shape' } })

    // A 200 whose body is not a TokenResponse: the token is undefined, /auth/me is then
    // rejected, and the half-session is discarded rather than leaving the UI signed in.
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByText('dashboard content')).not.toBeInTheDocument()
  })

  it('validates empty fields without contacting the server', async () => {
    const user = userEvent.setup()
    renderWithAuth(routes, { initialEntries: [ROUTES.login] })
    await screen.findByRole('heading', { name: 'Sign in' })

    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(screen.getByText('Enter your email address.')).toBeInTheDocument()
    expect(screen.getByText('Enter your password.')).toBeInTheDocument()
    expect(fetchStub.calls).toHaveLength(0)
  })
})

// ======================================================================================
// Session restoration and expiry
// ======================================================================================

describe('session restoration', () => {
  it('restores a stored session on reload', async () => {
    fetchStub.on('GET', '/auth/me', { body: TEST_USER })
    seedStoredToken()

    renderWithAuth(routes, { initialEntries: [ROUTES.dashboard] })

    expect(await screen.findByText('dashboard content')).toBeInTheDocument()
    expect(fetchStub.calls[0]?.authorization).toBe(`Bearer ${TEST_TOKEN}`)
  })

  it('discards a token the server rejects, and explains why', async () => {
    fetchStub.on('GET', '/auth/me', {
      status: 401,
      body: { error: { code: 'INVALID_TOKEN', message: 'Not authenticated.', details: [] } },
    })
    seedStoredToken()

    renderWithAuth(routes, { initialEntries: [ROUTES.dashboard] })

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByText('Your session has ended. Please sign in again.')).toBeInTheDocument()
    expect(window.sessionStorage.getItem('ahip.access_token')).toBeNull()
  })

  it('does not retry a rejected token', async () => {
    // No refresh endpoint exists, so a retry would replay the same rejected credential
    // forever. Exactly one attempt is made.
    fetchStub.on('GET', '/auth/me', {
      status: 401,
      body: { error: { code: 'INVALID_TOKEN', message: 'Not authenticated.', details: [] } },
    })
    seedStoredToken()

    renderWithAuth(routes, { initialEntries: [ROUTES.dashboard] })
    await screen.findByRole('heading', { name: 'Sign in' })

    expect(fetchStub.calls.filter((call) => call.url.includes('/auth/me'))).toHaveLength(1)
  })

  it('falls back to sign-in when the server is unreachable during restore', async () => {
    fetchStub.on('GET', '/auth/me', { networkError: true })
    seedStoredToken()

    renderWithAuth(routes, { initialEntries: [ROUTES.dashboard] })

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    // Not announced as an expiry: the token may be perfectly good, the network was not.
    expect(
      screen.queryByText('Your session has ended. Please sign in again.'),
    ).not.toBeInTheDocument()
  })

  it('goes straight to sign-in when nothing is stored', async () => {
    renderWithAuth(routes, { initialEntries: [ROUTES.dashboard] })

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(fetchStub.calls).toHaveLength(0)
  })
})

// ======================================================================================
// Signing out
// ======================================================================================

describe('signing out', () => {
  async function signedIn() {
    fetchStub.on('GET', '/auth/me', { body: TEST_USER })
    seedStoredToken()
    renderWithAuth(routes, { initialEntries: [ROUTES.dashboard] })
    await screen.findByText('dashboard content')
    return userEvent.setup()
  }

  it('returns the user to sign-in', async () => {
    const user = await signedIn()

    await user.click(screen.getByRole('button', { name: `Sign out ${TEST_USER.full_name}` }))

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('clears the stored token', async () => {
    const user = await signedIn()

    await user.click(screen.getByRole('button', { name: `Sign out ${TEST_USER.full_name}` }))

    await waitFor(() => {
      expect(window.sessionStorage.getItem('ahip.access_token')).toBeNull()
    })
  })

  it('makes protected content immediately inaccessible', async () => {
    const user = await signedIn()

    await user.click(screen.getByRole('button', { name: `Sign out ${TEST_USER.full_name}` }))
    await screen.findByRole('heading', { name: 'Sign in' })

    expect(screen.queryByText('dashboard content')).not.toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Primary' })).not.toBeInTheDocument()
  })

  it('does not call a logout endpoint, because the backend has none', async () => {
    const user = await signedIn()
    const before = fetchStub.calls.length

    await user.click(screen.getByRole('button', { name: `Sign out ${TEST_USER.full_name}` }))
    await screen.findByRole('heading', { name: 'Sign in' })

    expect(fetchStub.calls).toHaveLength(before)
  })
})

// ======================================================================================
// 403 -- authenticated, but not permitted
// ======================================================================================

describe('a forbidden response', () => {
  it('does not end the session', async () => {
    // 403 means the token is fine and the ROLE is not. Clearing the session would sign a
    // user out for opening a page above their permissions, which is both wrong and
    // infuriating. Only a 401 ends a session.
    fetchStub.on('GET', '/auth/me', { body: TEST_USER })
    seedStoredToken()
    renderWithAuth(routes, { initialEntries: [ROUTES.dashboard] })
    await screen.findByText('dashboard content')

    fetchStub.on('GET', '/hotels', {
      status: 403,
      body: {
        error: {
          code: 'FORBIDDEN',
          message: 'You do not have permission to perform this operation.',
          details: [],
        },
      },
    })
    const { api } = await import('@/services/api/client')
    const { ApiError } = await import('@/services/api/ApiError')
    const failure = await api.get('/hotels').catch((error: unknown) => error)

    expect(failure).toBeInstanceOf(ApiError)
    expect((failure as InstanceType<typeof ApiError>).isForbidden).toBe(true)
    // Still signed in.
    expect(screen.getByText('dashboard content')).toBeInTheDocument()
    expect(window.sessionStorage.getItem('ahip.access_token')).toBe(TEST_TOKEN)
  })
})

// ======================================================================================
// Secrets
// ======================================================================================

describe('credential hygiene', () => {
  it('never writes the token or the password to the console', async () => {
    const spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((level) =>
      vi.spyOn(console, level).mockImplementation(() => {}),
    )
    stubLoginSuccess()
    const user = userEvent.setup()
    renderWithAuth(routes, { initialEntries: [ROUTES.login] })
    await screen.findByRole('heading', { name: 'Sign in' })

    await signIn(user)
    await screen.findByText('dashboard content')

    const written = spies.flatMap((spy) => spy.mock.calls.flat()).map(String).join(' ')
    expect(written).not.toContain(TEST_TOKEN)
    expect(written).not.toContain(CREDENTIALS.password)
    expect(written).not.toContain('Bearer')
  })

  it('never persists the password', async () => {
    stubLoginSuccess()
    const user = userEvent.setup()
    renderWithAuth(routes, { initialEntries: [ROUTES.login] })
    await screen.findByRole('heading', { name: 'Sign in' })

    await signIn(user)
    await screen.findByText('dashboard content')

    const stored = JSON.stringify({
      session: { ...window.sessionStorage },
      local: { ...window.localStorage },
    })
    expect(stored).not.toContain(CREDENTIALS.password)
  })

  it('keeps the token out of the URL', async () => {
    stubLoginSuccess()
    const user = userEvent.setup()
    renderWithAuth(routes, { initialEntries: [ROUTES.login] })
    await screen.findByRole('heading', { name: 'Sign in' })

    await signIn(user)
    await screen.findByText('dashboard content')

    for (const call of fetchStub.calls) {
      expect(call.url).not.toContain(TEST_TOKEN)
      expect(call.url).not.toContain('token=')
    }
  })

  it('never renders the token', async () => {
    fetchStub.on('GET', '/auth/me', { body: TEST_USER })
    seedStoredToken()

    const { container } = renderWithAuth(routes, { initialEntries: [ROUTES.dashboard] })
    await screen.findByText('dashboard content')

    expect(container.innerHTML).not.toContain(TEST_TOKEN)
  })
})

// ======================================================================================
// The form itself
// ======================================================================================

describe('the sign-in form', () => {
  beforeEach(async () => {
    renderWithAuth(routes, { initialEntries: [ROUTES.login] })
    await screen.findByRole('heading', { name: 'Sign in' })
  })

  it('labels both fields', () => {
    expect(screen.getByLabelText('Email address')).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
  })

  it('uses a real password input, so the browser masks it and offers a manager', () => {
    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'password')
    expect(screen.getByLabelText('Password')).toHaveAttribute('autocomplete', 'current-password')
  })

  it('uses a real submit button, so Enter submits', () => {
    const submit = screen.getByRole('button', { name: 'Sign in' })
    expect(submit.tagName).toBe('BUTTON')
    expect(submit).toHaveAttribute('type', 'submit')
  })

  it('focuses the email field on arrival, then tabs to password and submit', async () => {
    const user = userEvent.setup()

    // The form autofocuses its first field, so a user can start typing without reaching
    // for the mouse -- which is what a front-desk operator signing in at shift change
    // actually does.
    expect(screen.getByLabelText('Email address')).toHaveFocus()

    await user.tab()
    expect(screen.getByLabelText('Password')).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: 'Sign in' })).toHaveFocus()
  })

  it('marks an invalid field for assistive technology', async () => {
    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(screen.getByLabelText('Email address')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText('Email address')).toHaveAccessibleDescription(
      'Enter your email address.',
    )
  })
})
