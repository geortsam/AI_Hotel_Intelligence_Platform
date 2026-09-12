import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { type RouteObject } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { AppShell } from '@/components/layout/AppShell'
import { NAV_ITEMS } from '@/router/navigation'
import { ROUTES } from '@/router/routes'
import {
  clearStoredToken,
  installFetchStub,
  renderWithAuth,
  seedStoredToken,
  TEST_USER,
  type FetchStub,
} from '@/test/harness'

/**
 * Shell behaviour, not shell appearance.
 *
 * These assert the things that would silently break and that a person would only notice by
 * clicking around on a phone: which nav item is marked current, whether the drawer closes
 * after navigating, and whether a closed drawer is still reachable by keyboard. Nothing
 * here snapshots markup -- a snapshot of the sidebar would fail on every styling change
 * and catch none of these.
 */

let fetchStub: FetchStub

beforeEach(() => {
  // The shell is only ever rendered for a signed-in user, so these tests supply one. The
  // session itself is exercised in `src/session/auth.test.tsx`.
  fetchStub = installFetchStub()
  fetchStub.on('GET', '/auth/me', { body: TEST_USER })
  seedStoredToken()
})

afterEach(() => {
  fetchStub.restore()
  clearStoredToken()
})

/** Renders the shell at a given URL, with a stub for each routed page. */
async function renderShellAt(initialPath: string) {
  const routes: RouteObject[] = [
    {
      path: ROUTES.dashboard,
      element: <AppShell />,
      children: [
        { index: true, element: <p>dashboard content</p> },
        ...NAV_ITEMS.filter((item) => item.to !== ROUTES.dashboard).map((item) => ({
          path: item.to,
          element: <p>{item.label} content</p>,
        })),
        { path: '*', element: <p>not found content</p> },
      ],
    },
  ]
  const result = renderWithAuth(routes, { initialEntries: [initialPath] })
  // Wait for session restoration to settle so assertions do not race the first render.
  await waitFor(() => {
    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
  })
  return result
}

describe('landmarks', () => {
  it('exposes a navigation landmark and a main landmark', async () => {
    await renderShellAt(ROUTES.dashboard)

    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
    expect(screen.getByRole('main')).toBeInTheDocument()
  })

  it('offers a skip link as the first focusable element', async () => {
    await renderShellAt(ROUTES.dashboard)
    const user = userEvent.setup()

    await user.tab()

    expect(screen.getByRole('link', { name: 'Skip to content' })).toHaveFocus()
  })

  it('points the skip link at the main landmark', async () => {
    await renderShellAt(ROUTES.dashboard)

    const skip = screen.getByRole('link', { name: 'Skip to content' })
    expect(skip).toHaveAttribute('href', '#main-content')
    expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content')
  })
})

describe('navigation', () => {
  it('renders every navigation item exactly once', async () => {
    await renderShellAt(ROUTES.dashboard)
    const nav = screen.getByRole('navigation', { name: 'Primary' })

    for (const item of NAV_ITEMS) {
      expect(within(nav).getByRole('link', { name: item.label })).toBeInTheDocument()
    }
    expect(within(nav).getAllByRole('link')).toHaveLength(NAV_ITEMS.length)
  })

  it.each(NAV_ITEMS.map((item) => [item.label, item.to] as const))(
    'marks %s as the current page when its route is active',
    async (label, to) => {
      await renderShellAt(to)
      const nav = screen.getByRole('navigation', { name: 'Primary' })

      expect(within(nav).getByRole('link', { name: label })).toHaveAttribute(
        'aria-current',
        'page',
      )
    },
  )

  it('marks exactly one item as current at a time', async () => {
    await renderShellAt(ROUTES.bookings)
    const nav = screen.getByRole('navigation', { name: 'Primary' })

    const current = within(nav)
      .getAllByRole('link')
      .filter((link) => link.getAttribute('aria-current') === 'page')

    expect(current).toHaveLength(1)
    expect(current[0]).toHaveAccessibleName('Bookings')
  })

  it('does not mark the dashboard current on a nested route', async () => {
    // `/` is a prefix of every path, so without `end` on the NavLink the dashboard would
    // stay highlighted everywhere.
    await renderShellAt(ROUTES.analytics)
    const nav = screen.getByRole('navigation', { name: 'Primary' })

    expect(within(nav).getByRole('link', { name: 'Dashboard' })).not.toHaveAttribute(
      'aria-current',
    )
  })
})

describe('the header', () => {
  it('titles the page after the active navigation area', async () => {
    await renderShellAt(ROUTES.financials)

    expect(screen.getByRole('heading', { level: 1, name: 'Financials' })).toBeInTheDocument()
  })

  it('shows the section a route belongs to', async () => {
    await renderShellAt(ROUTES.reviews)

    // Scoped to the header: the sidebar also renders "Intelligence" as a group label.
    expect(within(screen.getByRole('banner')).getByText('Intelligence')).toBeInTheDocument()
  })
})

describe('the mobile drawer', () => {
  it('starts closed, and says so on the toggle', async () => {
    await renderShellAt(ROUTES.dashboard)

    expect(screen.getByRole('button', { name: 'Open navigation' })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
  })

  it('never marks the sidebar aria-hidden, which would hide it on desktop too', async () => {
    // The drawer is hidden off-canvas by `visibility: hidden` in CSS, which the desktop
    // media query overrides. An aria-hidden driven by React state would also be true at
    // desktop width -- where the sidebar is permanently visible -- and would hide the
    // primary navigation from screen readers on exactly the screens that always show it.
    const { container } = await renderShellAt(ROUTES.dashboard)

    expect(container.querySelector('#app-sidebar')).not.toHaveAttribute('aria-hidden')
  })

  it('opens from the keyboard and exposes the state', async () => {
    await renderShellAt(ROUTES.dashboard)
    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Open navigation' }))

    expect(screen.getByRole('button', { name: 'Close navigation' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  })

  it('closes on Escape', async () => {
    await renderShellAt(ROUTES.dashboard)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))

    await user.keyboard('{Escape}')

    expect(screen.getByRole('button', { name: 'Open navigation' })).toBeInTheDocument()
  })

  it('closes when a destination is chosen', async () => {
    // On a phone the drawer covers the content, so following a link has to reveal the page
    // rather than leaving the menu on top of it.
    await renderShellAt(ROUTES.dashboard)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))

    await user.click(screen.getByRole('link', { name: 'Bookings' }))

    expect(screen.getByRole('button', { name: 'Open navigation' })).toBeInTheDocument()
    expect(screen.getByText('Bookings content')).toBeInTheDocument()
  })

  it('dismisses on a backdrop click without adding a duplicate control', async () => {
    const { container } = await renderShellAt(ROUTES.dashboard)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))

    // Exactly one "Close navigation" control is exposed -- the toggle. The backdrop is a
    // real <button> for pointer users but is aria-hidden and untabbable, because it
    // duplicates the toggle and Escape already serves the keyboard.
    expect(screen.getAllByRole('button', { name: 'Close navigation' })).toHaveLength(1)

    const backdrop = container.querySelector<HTMLButtonElement>('button[aria-hidden="true"]')
    expect(backdrop).not.toBeNull()
    expect(backdrop).toHaveAttribute('tabindex', '-1')

    await user.click(backdrop!)
    expect(screen.getByRole('button', { name: 'Open navigation' })).toBeInTheDocument()
  })
})

describe('unknown routes', () => {
  it('renders the not-found page inside the shell, keeping navigation available', async () => {
    await renderShellAt('/no-such-area')

    expect(screen.getByText('not found content')).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
  })
})
