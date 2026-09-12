import { createBrowserRouter, RouterProvider, type RouteObject } from 'react-router-dom'

import { AppShell } from '@/components/layout/AppShell'
import { BookingDetailPage } from '@/pages/BookingDetailPage'
import { AvailabilityPage } from '@/pages/AvailabilityPage'
import { BookingsPage } from '@/pages/BookingsPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { FinancialsPage } from '@/pages/FinancialsPage'
import { GuestDetailPage } from '@/pages/GuestDetailPage'
import { GuestsPage } from '@/pages/GuestsPage'
import { LoginPage } from '@/pages/LoginPage'
import { NotFoundPage, RouteErrorPage } from '@/pages/NotFoundPage'
import { PlaceholderPage } from '@/pages/PlaceholderPage'
import { AdministrationPage } from '@/pages/AdministrationPage'
import { IntelligencePage } from '@/pages/IntelligencePage'
import { PlatformPage } from '@/pages/PlatformPage'
import { PropertyPage } from '@/pages/PropertyPage'
import { RoomDetailPage } from '@/pages/RoomDetailPage'
import { RoomsPage } from '@/pages/RoomsPage'
import { ReviewsPage } from '@/pages/ReviewsPage'
import { NAV_ITEMS } from '@/router/navigation'
import { ProtectedRoute } from '@/router/ProtectedRoute'
import {
  BOOKING_DETAIL_PATTERN,
  GUEST_DETAIL_PATTERN,
  ROOM_DETAIL_PATTERN,
  ROUTES,
} from '@/router/routes'

/**
 * What each not-yet-built area will contain, in the platform's own terms.
 *
 * Bookings is absent because Stage 5.5 built it, Financials because Stage 5.8 did, Reviews
 * because Stage 5.9 did, Guests because Stage 5.10 did, Rooms because Stage 5.11 did and
 * Availability because Stage 5.12 did and Property because Stage 5.13 did; their entries
 * were removed rather than left as data nothing reads.
 *
 * Kept beside the routes rather than in the navigation data because it describes a PAGE,
 * not a menu entry. Every string here describes capability the backend already has -- the
 * work outstanding is the interface, not the domain.
 */
const AREA_DESCRIPTIONS: Readonly<Record<string, string>> = {
  [ROUTES.analytics]:
    'Occupancy, ADR and RevPAR reporting over a chosen period, plus revenue and expense breakdowns.',
}

/**
 * The route tree, and the only place it is assembled.
 *
 * Two branches, and the split is the security boundary:
 *
 * * `/login` is public. It must be, or an unauthenticated user would be redirected to a
 *   page that redirects them — the classic loop.
 * * everything else sits under `ProtectedRoute`, which renders the shell only once a
 *   session exists. Adding a feature area cannot accidentally expose it, because there is
 *   no route position outside the guard for it to land in.
 *
 * The feature routes are still generated from `NAV_ITEMS`, so a sidebar entry cannot point
 * at a route that does not exist, and a route cannot exist with no way to reach it.
 *
 * `*` stays a child of the shell, so an unknown URL renders the not-found page with the
 * navigation still around it — and, being inside the guard, an unauthenticated visitor to a
 * nonsense URL is sent to sign in rather than shown a 404 that leaks which paths exist.
 */
/**
 * Areas still awaiting their stage.
 *
 * `dashboard` (Stage 5.4), `bookings` (Stage 5.5), `financials` (Stage 5.8), `reviews`
 * (Stage 5.9), `guests` (Stage 5.10), `rooms` (Stage 5.11) and `availability` (Stage 5.12)
 * and `property` (Stage 5.13) are excluded because they are built: leaving any of them in
 * would render a "not built yet" page over a working feature. The filter is the single place
 * that knows which areas are real, so adding one is a one-line change here rather than a
 * route that has to be remembered.
 */
const BUILT_AREAS: readonly string[] = [
  ROUTES.dashboard,
  ROUTES.bookings,
  ROUTES.financials,
  ROUTES.reviews,
  ROUTES.guests,
  ROUTES.rooms,
  ROUTES.availability,
  ROUTES.property,
  ROUTES.administration,
  ROUTES.platform,
  ROUTES.intelligence,
]

const featureRoutes: RouteObject[] = NAV_ITEMS.filter(
  (item) => !BUILT_AREAS.includes(item.to),
).map(
  (item) => ({
    path: item.to,
    element: (
      <PlaceholderPage
        title={item.label}
        description={AREA_DESCRIPTIONS[item.to] ?? ''}
        plannedStage={item.plannedStage}
      />
    ),
  }),
)

const router = createBrowserRouter([
  {
    path: ROUTES.login,
    element: <LoginPage />,
    errorElement: <RouteErrorPage />,
  },
  {
    element: <ProtectedRoute />,
    errorElement: <RouteErrorPage />,
    children: [
      {
        path: ROUTES.dashboard,
        element: <AppShell />,
        children: [
          { index: true, element: <DashboardPage /> },
          /*
           * Bookings, and one booking.
           *
           * The detail route is a SIBLING of the list rather than a child of it. Nesting it
           * would keep the list mounted underneath -- and keep its request in flight -- while
           * a single booking is being read, which is a page of rows nobody is looking at.
           * Both sit inside `AppShell`, so a booking's own page keeps the sidebar, the
           * header and the breadcrumb.
           */
          { path: ROUTES.bookings, element: <BookingsPage /> },
          { path: BOOKING_DETAIL_PATTERN, element: <BookingDetailPage /> },
          /*
           * The revenue and expense journals. A sibling of the booking routes rather than a
           * child of anything: the ledgers are hotel-wide, and a revenue line's link to a
           * booking is a cross-reference, not a containment.
           */
          { path: ROUTES.financials, element: <FinancialsPage /> },
          /*
           * Guest reviews. Hotel-scoped like everything else under the shell -- there is no
           * flat `/reviews` on the backend, and a review's own address runs through the
           * booking it reviews rather than through this route.
           */
          { path: ROUTES.reviews, element: <ReviewsPage /> },
          /*
           * Guests, and one guest. Siblings rather than parent and child, for the reason the
           * booking routes give: nesting would keep the list mounted and its request in
           * flight underneath a single record nobody is looking at.
           */
          { path: ROUTES.guests, element: <GuestsPage /> },
          { path: GUEST_DETAIL_PATTERN, element: <GuestDetailPage /> },
          /*
           * Rooms, and one room. The detail pattern carries TWO segments -- the room type's
           * code and the room number -- because the backend resolves a room against both,
           * and the same number under another type is a different room.
           */
          { path: ROUTES.rooms, element: <RoomsPage /> },
          { path: ROOM_DETAIL_PATTERN, element: <RoomDetailPage /> },
          /*
           * Availability search. A sibling of Rooms rather than a child: it is a question
           * about inventory over dates, not a view of one room.
           */
          { path: ROUTES.availability, element: <AvailabilityPage /> },
          /*
           * The property's own record and its room types. Under Administration rather than
           * Operations: this is configuration, not the day's work.
           */
          { path: ROUTES.property, element: <PropertyPage /> },
          /*
           * Your own account, and who may reach the selected property. The two subjects sit
           * on one page because they are the two halves of one question -- who you are, and
           * what that lets you do here -- and the backend keeps them apart in exactly the
           * same way: identity is global, membership is per hotel.
           */
          { path: ROUTES.administration, element: <AdministrationPage /> },
          /*
           * The shared catalogues and the platform audit trail. A sibling of Administration
           * rather than a child: it is not a deeper view of one hotel's settings, it is the
           * set of things no hotel owns.
           */
          { path: ROUTES.platform, element: <PlatformPage /> },
          { path: ROUTES.intelligence, element: <IntelligencePage /> },
          ...featureRoutes,
          { path: '*', element: <NotFoundPage /> },
        ],
      },
    ],
  },
])

/** Mounts the router. Rendered by `App`, below the session provider. */
export function AppRouter() {
  return <RouterProvider router={router} />
}
