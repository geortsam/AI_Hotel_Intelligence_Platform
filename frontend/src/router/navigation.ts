import {
  BarChart3,
  BedDouble,
  BotMessageSquare,
  CalendarRange,
  CalendarSearch,
  LayoutDashboard,
  Globe,
  Hotel,
  MessageSquareText,
  Users,
  Settings,
  Sparkles,
  Wallet,
  type LucideIcon,
} from 'lucide-react'

import { ROUTES, type AppRoute } from '@/router/routes'

export interface NavItem {
  readonly label: string
  readonly to: AppRoute
  readonly icon: LucideIcon
  /** Which stage builds this area. Shown on the placeholder page, not in the sidebar. */
  readonly plannedStage: string
}

export interface NavSection {
  /** Section heading. `null` for the first group, which needs no label above the first item. */
  readonly label: string | null
  readonly items: readonly NavItem[]
}

/**
 * The sidebar's contents, as data.
 *
 * A structure rather than markup so the sidebar renders it in a loop and the tests assert
 * against the same source the UI reads -- a hand-written list of links would let the two
 * disagree without anything failing.
 *
 * The grouping mirrors how a hotel actually divides the work: what is happening now
 * (Operations), what it earned (Finance), what it means (Intelligence), and who may change
 * it (Administration). Grouping by backend module instead would put availability next to
 * analytics because they happen to share a service.
 */
export const NAV_SECTIONS: readonly NavSection[] = [
  {
    label: null,
    items: [
      {
        label: 'Dashboard',
        to: ROUTES.dashboard,
        icon: LayoutDashboard,
        plannedStage: 'Stage 5.4',
      },
    ],
  },
  {
    label: 'Operations',
    items: [
      {
        label: 'Bookings',
        to: ROUTES.bookings,
        icon: CalendarRange,
        plannedStage: 'Stage 5.5',
      },
      {
        label: 'Guests',
        to: ROUTES.guests,
        icon: Users,
        plannedStage: 'Stage 5.10',
      },
      {
        /*
         * "Rooms", not "Rooms & Availability".
         *
         * Two reasons, and they agree. The area this label points at manages the physical
         * rooms of a property -- number, floor, status, service state. Availability search is
         * a different capability with its own endpoint, and naming it here promised something
         * the page does not do.
         *
         * It was also the longest label in the sidebar, and the shell's header renders the
         * current area's label on one line: at 375px "Operations / Rooms & Availability"
         * pushed the header to 432px and gave the whole page a horizontal scrollbar. Found by
         * measuring, not by reading.
         */
        label: 'Rooms',
        to: ROUTES.rooms,
        icon: BedDouble,
        plannedStage: 'Stage 5.11',
      },
      {
        /*
         * Separate from Rooms, because it answers a different question. Rooms manages what
         * the property HAS; this asks what it can SELL for a given stay -- a read-only search
         * whose answer comes from the database's exclusion constraint, not from the room
         * records. Stage 5.11 renamed the Rooms entry precisely so this could have its own.
         */
        label: 'Availability',
        to: ROUTES.availability,
        icon: CalendarSearch,
        plannedStage: 'Stage 5.12',
      },
    ],
  },
  {
    label: 'Finance',
    items: [
      {
        label: 'Financials',
        to: ROUTES.financials,
        icon: Wallet,
        plannedStage: 'Stage 5.7',
      },
    ],
  },
  {
    label: 'Intelligence',
    items: [
      {
        label: 'Analytics',
        to: ROUTES.analytics,
        icon: BarChart3,
        plannedStage: 'Stage 5.8',
      },
      {
        /*
         * Under Intelligence beside Analytics, not Administration: this is something an
         * operator reads to decide, not something they maintain.
         */
        label: 'Forecasting',
        to: ROUTES.intelligence,
        icon: Sparkles,
        plannedStage: 'Stage 5.16',
      },
      {
        label: 'Reviews',
        to: ROUTES.reviews,
        icon: MessageSquareText,
        plannedStage: 'Stage 5.9',
      },
      {
        /*
         * Last in Intelligence: questions answered from the figures the entries above show.
         * One item added to the existing group; the shell and the sections are unchanged.
         */
        label: 'Copilot',
        to: ROUTES.copilot,
        icon: BotMessageSquare,
        plannedStage: 'Stage 7.13',
      },
    ],
  },
  {
    label: 'Administration',
    items: [
      {
        /*
         * Separate from Administration, which still promises staff membership and the audit
         * trail -- neither of which this stage builds. Claiming that entry would have made
         * the placeholder's own description a lie about what is there.
         */
        label: 'Property',
        to: ROUTES.property,
        icon: Hotel,
        plannedStage: 'Stage 5.13',
      },
      {
        label: 'Administration',
        to: ROUTES.administration,
        icon: Settings,
        plannedStage: 'Stage 5.10',
      },
      {
        /*
         * Last in the group, and deliberately named for its scope rather than its contents.
         * "Catalogues" would have read as a sibling of Property; what distinguishes this
         * entry is that nothing behind it belongs to a hotel at all.
         */
        label: 'Platform',
        to: ROUTES.platform,
        icon: Globe,
        plannedStage: 'Stage 5.15',
      },
    ],
  },
]

/** Every navigable item, flattened. Used by the router and by the header's title lookup. */
export const NAV_ITEMS: readonly NavItem[] = NAV_SECTIONS.flatMap((section) => section.items)

/**
 * The nav item a pathname belongs to, or undefined when the URL matches no known area.
 *
 * Matches an exact path first, then the **longest** prefix. The prefix arm exists because
 * Stage 5.5 added `/bookings/:bookingPublicId`: an exact-only lookup returned nothing for a
 * booking's own page, so the shell headed it "Not found" and dropped the breadcrumb while
 * the page rendered perfectly well underneath.
 *
 * Two details keep the prefix arm from over-matching:
 *
 * * the boundary must be a `/`, so a future `/bookings-archive` is not swallowed by
 *   `/bookings`;
 * * the dashboard is excluded, because its path is `/` and would otherwise prefix-match
 *   every route in the application. `Sidebar` handles the same problem with `end` on the
 *   same item, for the same reason.
 *
 * Longest-first means a nested area would win over its parent if one is ever added.
 */
export function findNavItem(pathname: string): NavItem | undefined {
  const exact = NAV_ITEMS.find((item) => item.to === pathname)
  if (exact) {
    return exact
  }
  return [...NAV_ITEMS]
    .filter((item) => item.to !== ROUTES.dashboard)
    .sort((a, b) => b.to.length - a.to.length)
    .find((item) => pathname.startsWith(`${item.to}/`))
}
