import { Hotel } from 'lucide-react'
import { NavLink, Link } from 'react-router-dom'

import { NAV_SECTIONS } from '@/router/navigation'
import { ROUTES } from '@/router/routes'

import styles from './Sidebar.module.css'

export interface SidebarProps {
  /** Whether the mobile drawer is showing. Ignored at desktop width, where it is permanent. */
  readonly isOpen: boolean
  /** Called when a navigation choice should dismiss the drawer. */
  readonly onNavigate: () => void
}

/**
 * The primary navigation.
 *
 * One component for both presentations. At desktop width the stylesheet makes it a
 * permanent column; below that it is an off-canvas drawer. Rendering two different trees
 * would mean maintaining the navigation twice and letting them drift.
 *
 * **Hiding the closed drawer is CSS's job, not React's.** The off-canvas state uses
 * `visibility: hidden`, which removes the element from the accessibility tree AND from the
 * tab order -- a transform alone would leave a keyboard user tabbing into links they cannot
 * see. An `aria-hidden` attribute driven by `isOpen` was tried and is wrong: `isOpen` is
 * false at desktop width too, where the sidebar is permanently visible, so it hid the whole
 * primary navigation from screen readers on the very screens that always show it. The
 * media query already knows which presentation is active; React does not, and should not
 * have to.
 *
 * `NavLink` supplies the active state from the router, so the highlight cannot disagree
 * with the URL. `end` is set on the dashboard because it is `/`, which would otherwise
 * match every route beneath it.
 */
export function Sidebar({ isOpen, onNavigate }: SidebarProps) {
  return (
    <aside id="app-sidebar" className={`${styles.sidebar} ${isOpen ? styles.open : ''}`}>
      <Link className={styles.brand} to={ROUTES.dashboard} onClick={onNavigate}>
        <span className={styles.brandMark} aria-hidden="true">
          <Hotel size={15} strokeWidth={2.25} />
        </span>
        <span className={styles.brandText}>Hotel Intelligence</span>
      </Link>

      <nav className={styles.nav} aria-label="Primary">
        {NAV_SECTIONS.map((section, index) => (
          <div className={styles.section} key={section.label ?? `section-${index}`}>
            {section.label ? (
              <div className={styles.sectionLabel} id={`nav-section-${index}`}>
                {section.label}
              </div>
            ) : null}
            <ul
              className={styles.list}
              aria-labelledby={section.label ? `nav-section-${index}` : undefined}
            >
              {section.items.map((item) => {
                const Icon = item.icon
                return (
                  <li key={item.to}>
                    <NavLink
                      to={item.to}
                      end={item.to === ROUTES.dashboard}
                      onClick={onNavigate}
                      className={({ isActive }) =>
                        `${styles.link} ${isActive ? styles.linkActive : ''}`
                      }
                    >
                      <Icon className={styles.linkIcon} size={17} aria-hidden="true" />
                      {item.label}
                    </NavLink>
                  </li>
                )
              })}
            </ul>
          </div>
        ))}
      </nav>

      {/*
       * The product name, and deliberately nothing else.
       *
       * This read "Foundation build - Stage 5.2" from Stage 5.2 until Stage 6.11, by which
       * point it was wrong by roughly thirty stages and appeared on every screen. A build or
       * stage marker here has to be updated by hand every time either moves, which is the
       * same as saying it will be stale; the version the running system reports is the
       * backend's, available from the API, and this footer is not where a reader should go
       * looking for it.
       */}
      <div className={styles.footer}>AI Hotel Intelligence Platform</div>
    </aside>
  )
}
