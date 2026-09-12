import { LogOut, Menu } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import { useAuth } from '@/session/AuthProvider'

import { HotelContextChip } from './HotelContext'
import styles from './Header.module.css'

export interface HeaderProps {
  /** The current area's name, shown as the page title. */
  readonly title: string
  /** Context above the title, e.g. the section a page sits in. */
  readonly breadcrumb?: string
  readonly isSidebarOpen: boolean
  readonly onToggleSidebar: () => void
}

/**
 * The top bar.
 *
 * It reads the session from context rather than taking the user as a prop. That is the
 * point of the seam: the shell does not thread identity down through three components, and
 * no part of the tree keeps its own copy to go stale. When the user menu arrives it changes
 * this file and nothing above it.
 *
 * The hotel context is no longer a placeholder. `GET /auth/me` still returns identity only
 * -- "who the caller is, not what they may access" -- so it remains the wrong place to learn
 * which properties are reachable. `GET /hotels` is the right one: the backend filters it to
 * the caller's own memberships, so the chip renders a server-side answer rather than a list
 * this application assembled. See `HotelContext`.
 *
 * The menu button carries `aria-expanded` and `aria-controls`, and is hidden by CSS at
 * desktop width where the sidebar is permanent and a toggle would be meaningless.
 */
export function Header({ title, breadcrumb, isSidebarOpen, onToggleSidebar }: HeaderProps) {
  const { user, logout } = useAuth()

  return (
    <header className={styles.header}>
      <Button
        className={styles.menuButton}
        variant="ghost"
        size="sm"
        iconOnly
        aria-label={isSidebarOpen ? 'Close navigation' : 'Open navigation'}
        aria-expanded={isSidebarOpen}
        aria-controls="app-sidebar"
        onClick={onToggleSidebar}
      >
        <Menu size={18} aria-hidden="true" />
      </Button>

      <div className={styles.titleGroup}>
        {breadcrumb ? <span className={styles.breadcrumb}>{breadcrumb}</span> : null}
        <h1 className={styles.title}>{title}</h1>
      </div>

      <div className={styles.actions}>
        <HotelContextChip />

        {user ? (
          <div className={styles.account}>
            {/*
             * The full name, not the email. Both identify the user, but a name is what a
             * colleague looking over the shoulder needs, and an email address on screen in
             * a shared back office is more personal data than the header requires.
             */}
            <span className={styles.accountName}>{user.full_name}</span>
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              aria-label={`Sign out ${user.full_name}`}
              onClick={logout}
            >
              <LogOut size={16} aria-hidden="true" />
            </Button>
          </div>
        ) : null}
      </div>
    </header>
  )
}
