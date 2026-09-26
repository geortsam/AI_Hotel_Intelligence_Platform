import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * Which routes are loaded on demand, asserted against the router's own source.
 *
 * Route splitting is the kind of improvement that decays silently. Re-adding one ordinary
 * `import { ReviewsPage } from '@/pages/ReviewsPage'` pulls that page and its whole feature
 * back into the initial chunk, and nothing fails: the application still works, the suite is
 * still green, and the only symptom is a bundle that grew. This states the arrangement so
 * that undoing it has to be a decision somebody takes here.
 *
 * Read from disk rather than imported, because what is being asserted is the SHAPE of the
 * module -- `lazy(() => import(...))` versus a static import -- which is invisible once the
 * module has been evaluated.
 */

const ROOT = process.cwd()
const SOURCE = readFileSync(join(ROOT, 'src', 'router', 'AppRouter.tsx'), 'utf8')

/** Deferred: every screen reachable only after navigating to it. */
const ON_DEMAND = [
  'AnalyticsPage',
  'AdministrationPage',
  'AvailabilityPage',
  'BookingDetailPage',
  'BookingsPage',
  'CopilotPage',
  'FinancialsPage',
  'GuestDetailPage',
  'GuestsPage',
  'IntelligencePage',
  'PlatformPage',
  'PropertyPage',
  'ReviewsPage',
  'RoomDetailPage',
  'RoomsPage',
]

/**
 * Eager, and each for a reason. Login is the unauthenticated entry point and Dashboard is the
 * index route -- deferring either adds a round trip to the first screen anyone sees. The
 * error and not-found screens must not need a fetch to explain that a fetch failed.
 */
const EAGER = ['DashboardPage', 'LoginPage', 'NotFoundPage', 'RouteErrorPage', 'PlaceholderPage']

/** The exact statements, written out: two of these five share one import. */
const EAGER_IMPORTS = [
  "import { DashboardPage } from '@/pages/DashboardPage'",
  "import { LoginPage } from '@/pages/LoginPage'",
  "import { NotFoundPage, RouteErrorPage } from '@/pages/NotFoundPage'",
  "import { PlaceholderPage } from '@/pages/PlaceholderPage'",
]

describe('route splitting', () => {
  it('reads a router that actually declares these routes', () => {
    // Guards every assertion below: a scan of the wrong file would otherwise pass silently.
    expect(SOURCE).toContain('createBrowserRouter')
    expect(SOURCE.length).toBeGreaterThan(1000)
  })

  it.each(ON_DEMAND)('loads %s on demand', (page) => {
    expect(SOURCE).toContain(`const ${page} = lazy(() =>`)
    expect(SOURCE).toContain(`import('@/pages/${page}')`)
    // The static form would defeat it, whatever else the file says.
    expect(SOURCE).not.toContain(`import { ${page} } from '@/pages/${page}'`)
  })

  it.each(ON_DEMAND)('renders %s inside a loading boundary', (page) => {
    expect(SOURCE).toContain(`element: onDemand(<${page} />)`)
  })

  it('keeps the boundary in one place rather than per route', () => {
    expect(SOURCE).toContain('<Suspense fallback={<RouteLoading />}>')
    expect(SOURCE.match(/<Suspense/g) ?? []).toHaveLength(1)
  })

  it.each(EAGER_IMPORTS)('keeps `%s` a static import', (statement) => {
    expect(SOURCE).toContain(statement)
  })

  it.each(EAGER)('does not defer %s', (page) => {
    expect(SOURCE).not.toContain(`const ${page} = lazy(`)
  })
})
