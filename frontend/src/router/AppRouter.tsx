import { lazy, Suspense, type ReactNode } from 'react'
import { createBrowserRouter, RouterProvider, type RouteObject } from 'react-router-dom'

import { AppShell } from '@/components/layout/AppShell'
import { RouteLoading } from '@/components/layout/RouteLoading'
import { DashboardPage } from '@/pages/DashboardPage'
import { LoginPage } from '@/pages/LoginPage'
import { NotFoundPage, RouteErrorPage } from '@/pages/NotFoundPage'
import { ProtectedRoute } from '@/router/ProtectedRoute'
import {
  BOOKING_DETAIL_PATTERN,
  GUEST_DETAIL_PATTERN,
  ROOM_DETAIL_PATTERN,
  ROUTES,
} from '@/router/routes'


/*
 * Loaded on demand: one chunk per screen, fetched the first time that screen is opened.
 *
 * Every page used to be imported here, so opening the dashboard also downloaded the ledger,
 * the audit trail, the forecast and every form behind them -- 315 kB of route code, half the
 * bundle, for screens most sessions never visit.
 *
 * The dashboard and the login page are deliberately NOT in this list. Login is the
 * unauthenticated entry point, and the dashboard is the index route: both are needed
 * immediately, and deferring them would add a round trip to the first screen anyone sees in
 * order to save a few kilobytes. `AppShell`, `NotFoundPage` and `RouteErrorPage` stay eager
 * too -- the shell wraps every route, and an error screen that has to fetch itself is the
 * one screen that must work when fetching is what failed.
 *
 * `lazy` wants a default export and these pages are named ones, so each loader renames it.
 * Written out rather than generated from a list: a helper taking a module path and an export
 * name would have to cast, and a page renamed without updating its string would then fail at
 * runtime instead of here, in `tsc`.
 */
const AdministrationPage = lazy(() =>
  import('@/pages/AdministrationPage').then((module) => ({ default: module.AdministrationPage })),
)
const AvailabilityPage = lazy(() =>
  import('@/pages/AvailabilityPage').then((module) => ({ default: module.AvailabilityPage })),
)
const BookingDetailPage = lazy(() =>
  import('@/pages/BookingDetailPage').then((module) => ({ default: module.BookingDetailPage })),
)
const BookingsPage = lazy(() =>
  import('@/pages/BookingsPage').then((module) => ({ default: module.BookingsPage })),
)
const FinancialsPage = lazy(() =>
  import('@/pages/FinancialsPage').then((module) => ({ default: module.FinancialsPage })),
)
const GuestDetailPage = lazy(() =>
  import('@/pages/GuestDetailPage').then((module) => ({ default: module.GuestDetailPage })),
)
const GuestsPage = lazy(() =>
  import('@/pages/GuestsPage').then((module) => ({ default: module.GuestsPage })),
)
const AnalyticsPage = lazy(() =>
  import('@/pages/AnalyticsPage').then((module) => ({ default: module.AnalyticsPage })),
)
const CopilotPage = lazy(() =>
  import('@/pages/CopilotPage').then((module) => ({ default: module.CopilotPage })),
)
const IntelligencePage = lazy(() =>
  import('@/pages/IntelligencePage').then((module) => ({ default: module.IntelligencePage })),
)
const PlatformPage = lazy(() =>
  import('@/pages/PlatformPage').then((module) => ({ default: module.PlatformPage })),
)
const PropertyPage = lazy(() =>
  import('@/pages/PropertyPage').then((module) => ({ default: module.PropertyPage })),
)
const ReviewsPage = lazy(() =>
  import('@/pages/ReviewsPage').then((module) => ({ default: module.ReviewsPage })),
)
const RoomDetailPage = lazy(() =>
  import('@/pages/RoomDetailPage').then((module) => ({ default: module.RoomDetailPage })),
)
const RoomsPage = lazy(() =>
  import('@/pages/RoomsPage').then((module) => ({ default: module.RoomsPage })),
)

/**
 * Wraps a route element in the boundary that shows while its chunk is in flight.
 *
 * Per route rather than once around the shell: the shell is the PARENT route element, so it
 * is already mounted and stays mounted -- the sidebar, the header and the breadcrumb for the
 * destination remain on screen, and only the page area is replaced while its code arrives.
 */
function onDemand(element: ReactNode): ReactNode {
  return <Suspense fallback={<RouteLoading />}>{element}</Suspense>
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
 * Every sidebar entry (`NAV_ITEMS`) has a real route here, and `navigationRoutes.test.tsx`
 * resolves each one against this tree to prove it. Until A7 that was enforced by generating
 * a placeholder page for any area not yet built; every area is built, so the placeholder
 * machinery -- which had produced no route since Stage 7.2 -- is gone and the test holds the
 * guarantee instead.
 *
 * `*` stays a child of the shell, so an unknown URL renders the not-found page with the
 * navigation still around it — and, being inside the guard, an unauthenticated visitor to a
 * nonsense URL is sent to sign in rather than shown a 404 that leaks which paths exist.
 *
 * Exported so that test can resolve against exactly the routes the application mounts.
 */
export const APP_ROUTES: RouteObject[] = [
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
          { path: ROUTES.bookings, element: onDemand(<BookingsPage />) },
          { path: BOOKING_DETAIL_PATTERN, element: onDemand(<BookingDetailPage />) },
          /*
           * The revenue and expense journals. A sibling of the booking routes rather than a
           * child of anything: the ledgers are hotel-wide, and a revenue line's link to a
           * booking is a cross-reference, not a containment.
           */
          { path: ROUTES.financials, element: onDemand(<FinancialsPage />) },
          /*
           * Guest reviews. Hotel-scoped like everything else under the shell -- there is no
           * flat `/reviews` on the backend, and a review's own address runs through the
           * booking it reviews rather than through this route.
           */
          { path: ROUTES.reviews, element: onDemand(<ReviewsPage />) },
          /*
           * Guests, and one guest. Siblings rather than parent and child, for the reason the
           * booking routes give: nesting would keep the list mounted and its request in
           * flight underneath a single record nobody is looking at.
           */
          { path: ROUTES.guests, element: onDemand(<GuestsPage />) },
          { path: GUEST_DETAIL_PATTERN, element: onDemand(<GuestDetailPage />) },
          /*
           * Rooms, and one room. The detail pattern carries TWO segments -- the room type's
           * code and the room number -- because the backend resolves a room against both,
           * and the same number under another type is a different room.
           */
          { path: ROUTES.rooms, element: onDemand(<RoomsPage />) },
          { path: ROOM_DETAIL_PATTERN, element: onDemand(<RoomDetailPage />) },
          /*
           * Availability search. A sibling of Rooms rather than a child: it is a question
           * about inventory over dates, not a view of one room.
           */
          { path: ROUTES.availability, element: onDemand(<AvailabilityPage />) },
          /*
           * The property's own record and its room types. Under Administration rather than
           * Operations: this is configuration, not the day's work.
           */
          { path: ROUTES.property, element: onDemand(<PropertyPage />) },
          /*
           * Your own account, and who may reach the selected property. The two subjects sit
           * on one page because they are the two halves of one question -- who you are, and
           * what that lets you do here -- and the backend keeps them apart in exactly the
           * same way: identity is global, membership is per hotel.
           */
          { path: ROUTES.administration, element: onDemand(<AdministrationPage />) },
          /*
           * The shared catalogues and the platform audit trail. A sibling of Administration
           * rather than a child: it is not a deeper view of one hotel's settings, it is the
           * set of things no hotel owns.
           */
          { path: ROUTES.platform, element: onDemand(<PlatformPage />) },
          /*
           * Period reporting. A sibling of Forecasting rather than a child: they answer
           * different questions over the same data -- what happened, and what the model
           * expects next -- and neither is a deeper view of the other.
           */
          { path: ROUTES.analytics, element: onDemand(<AnalyticsPage />) },
          { path: ROUTES.intelligence, element: onDemand(<IntelligencePage />) },
          /*
           * Stage 7.13. Questions about the selected hotel, answered by the copilot. A sibling
           * of Forecasting and Analytics: it reads the same data through the server's tools,
           * and is not a deeper view of either.
           */
          { path: ROUTES.copilot, element: onDemand(<CopilotPage />) },
          { path: '*', element: <NotFoundPage /> },
        ],
      },
    ],
  },
]

const router = createBrowserRouter(APP_ROUTES)

/** Mounts the router. Rendered by `App`, below the session provider. */
export function AppRouter() {
  return <RouterProvider router={router} />
}
