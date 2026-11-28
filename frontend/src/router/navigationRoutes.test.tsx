import { matchRoutes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { APP_ROUTES } from '@/router/AppRouter'
import { NAV_ITEMS } from '@/router/navigation'
import {
  BOOKING_DETAIL_PATTERN,
  bookingPath,
  GUEST_DETAIL_PATTERN,
  guestPath,
  ROOM_DETAIL_PATTERN,
  roomPath,
  ROUTES,
} from '@/router/routes'

/**
 * Every sidebar entry reaches a real page (A7).
 *
 * Until A7 the router generated a placeholder route for any navigation area not yet built, so a
 * sidebar entry could never point at nothing. Every area is built and that machinery is gone;
 * this holds the same guarantee directly. Each navigation target is resolved against the exact
 * route tree the application mounts (`APP_ROUTES`), with React Router's own matcher, and must
 * land on a route of its own -- not on the `*` fallback that renders "Page not found".
 */

/** The deepest route a path resolves to, or null when nothing matches. */
function leafFor(path: string) {
  const matches = matchRoutes(APP_ROUTES, path)
  return matches === null ? null : matches[matches.length - 1]!.route
}

describe('navigation and routes', () => {
  it('the test reads a real navigation and route tree', () => {
    // Guards everything below: an empty list would make every per-item check vacuous.
    expect(NAV_ITEMS.length).toBe(13)
    expect(leafFor('/no-such-page')?.path).toBe('*')
  })

  it.each(NAV_ITEMS.map((item) => [item.label, item.to] as const))(
    'the %s entry (%s) reaches its own route, not the not-found fallback',
    (_label, to) => {
      const leaf = leafFor(to)
      expect(leaf).not.toBeNull()
      expect(leaf!.path).not.toBe('*')
      // The dashboard is the shell's index route; every other area declares its own path.
      if (to === ROUTES.dashboard) {
        expect(leaf!.index).toBe(true)
      } else {
        expect(leaf!.path).toBe(to)
      }
    },
  )

  /*
   * The same guarantee as one test over every entry, under a fixed title: the per-entry tests
   * above say WHICH entry broke, this one is the named killer of the A7 mutations (the
   * mutation harness matches a killer by its literal title).
   */
  it('every sidebar entry reaches its own route, not the not-found fallback', () => {
    const unreachable = NAV_ITEMS.filter((item) => {
      const leaf = leafFor(item.to)
      if (leaf === null || leaf.path === '*') return true
      return item.to === ROUTES.dashboard ? leaf.index !== true : leaf.path !== item.to
    }).map((item) => `${item.label} (${item.to})`)
    expect(unreachable).toEqual([])
  })

  const DETAIL_PAGES = [
    ['a booking', bookingPath('e5b72bf8-11a1-456b-9755-5a5ac5219782'), BOOKING_DETAIL_PATTERN],
    ['a guest', guestPath('98c235eb-5ae6-4c75-a743-836e1a2b38da'), GUEST_DETAIL_PATTERN],
    ['a room', roomPath('DBL', '101'), ROOM_DETAIL_PATTERN],
  ] as const

  it.each(DETAIL_PAGES)('the detail page of %s keeps its own route', (_label, path, pattern) => {
    expect(path).not.toContain(':')
    expect(leafFor(path)?.path).toBe(pattern)
  })

  it('every detail page keeps its own route', () => {
    const lost = DETAIL_PAGES.filter(([, path, pattern]) => leafFor(path)?.path !== pattern)
    expect(lost.map(([label]) => label)).toEqual([])
  })

  it('sign-in stays outside the protected shell', () => {
    const matches = matchRoutes(APP_ROUTES, ROUTES.login)!
    expect(matches).toHaveLength(1)
    expect(matches[0]!.route.path).toBe(ROUTES.login)
  })
})
