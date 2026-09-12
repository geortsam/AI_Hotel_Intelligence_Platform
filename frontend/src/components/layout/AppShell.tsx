import { useCallback, useEffect, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'

import { findNavItem, NAV_SECTIONS } from '@/router/navigation'

import { Header } from './Header'
import { Sidebar } from './Sidebar'
import styles from './AppShell.module.css'

/**
 * The section a route belongs to, for the header's breadcrumb line.
 *
 * Resolved through `findNavItem` rather than by comparing paths again here, so a nested
 * route like `/bookings/{id}` inherits its parent's section. Two independent path
 * comparisons is how the title and the breadcrumb end up disagreeing about which area the
 * user is in.
 */
function sectionLabelFor(pathname: string): string | undefined {
  const item = findNavItem(pathname)
  if (!item) {
    return undefined
  }
  return NAV_SECTIONS.find((section) => section.items.includes(item))?.label ?? undefined
}

/**
 * The application shell: sidebar, header, and the routed content between them.
 *
 * The drawer's open state lives here as local state rather than in a store. It is
 * genuinely local -- nothing outside this subtree needs to know whether the mobile
 * navigation is showing -- and a global store for it would be shared mutable state that
 * every later feature could reach into for no reason.
 *
 * Three behaviours make the drawer usable rather than merely present:
 *
 * 1. **It closes on navigation.** Following a link on a phone should reveal the page, not
 *    leave the menu covering it.
 * 2. **Escape closes it**, which is what the platform conventions lead people to try.
 * 3. **The toggle reports its state** through `aria-expanded`, so the control is meaningful
 *    without sight of the drawer.
 */
export function AppShell() {
  const [isSidebarOpen, setSidebarOpen] = useState(false)
  const { pathname } = useLocation()

  const closeSidebar = useCallback(() => {
    setSidebarOpen(false)
  }, [])

  const toggleSidebar = useCallback(() => {
    setSidebarOpen((open) => !open)
  }, [])

  // Close on navigation. Keyed on pathname so it fires for any route change, including
  // one triggered from somewhere other than the sidebar.
  useEffect(() => {
    setSidebarOpen(false)
  }, [pathname])

  useEffect(() => {
    if (!isSidebarOpen) {
      return
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setSidebarOpen(false)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [isSidebarOpen])

  const navItem = findNavItem(pathname)
  const title = navItem?.label ?? 'Not found'
  const breadcrumb = sectionLabelFor(pathname)

  return (
    <div className={styles.shell}>
      <a className={styles.skipLink} href="#main-content">
        Skip to content
      </a>

      <Sidebar isOpen={isSidebarOpen} onNavigate={closeSidebar} />

      {/*
       * The dimmed area behind the drawer. A real <button> so it is a genuine control
       * rather than a click-handling <div>, but removed from the accessibility tree and
       * the tab order: it duplicates the toggle exactly, and a keyboard user already has
       * two better ways out (Escape, and the toggle itself). Exposing it would put a
       * second identically-named "Close navigation" control in the tab order for no gain.
       */}
      {isSidebarOpen ? (
        <button
          type="button"
          className={styles.backdrop}
          tabIndex={-1}
          aria-hidden="true"
          onClick={closeSidebar}
        />
      ) : null}

      <div className={styles.header}>
        <Header
          title={title}
          breadcrumb={breadcrumb}
          isSidebarOpen={isSidebarOpen}
          onToggleSidebar={toggleSidebar}
        />
      </div>

      <main className={styles.main} id="main-content">
        <Outlet />
      </main>
    </div>
  )
}
