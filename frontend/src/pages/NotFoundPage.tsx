import { isRouteErrorResponse, Link, useRouteError } from 'react-router-dom'

import { Card } from '@/components/ui/Card'
import { PageContainer } from '@/components/ui/PageContainer'
import { ROUTES } from '@/router/routes'

import styles from './PlaceholderPage.module.css'

interface ErrorStateProps {
  readonly heading: string
  readonly detail: string
}

/** The shared presentation. Says nothing about why it is being shown. */
function ErrorState({ heading, detail }: ErrorStateProps) {
  return (
    <PageContainer>
      <Card>
        <div className={styles.empty}>
          <p className={styles.headline}>{heading}</p>
          <p className={styles.detail}>{detail}</p>
          <p className={styles.detail}>
            <Link to={ROUTES.dashboard}>Return to the dashboard</Link>
          </p>
        </div>
      </Card>
    </PageContainer>
  )
}

/**
 * An address that matches no route.
 *
 * Rendered by the catch-all route, so it appears INSIDE the application shell -- the user
 * keeps the navigation and can go somewhere else, rather than landing on a bare page with
 * nothing but a link. It calls no router error hook, because in this position there is no
 * error: the URL is simply unknown.
 */
export function NotFoundPage() {
  return (
    <ErrorState
      heading="Page not found"
      detail="That address does not match any page in this application."
    />
  )
}

/**
 * A route that threw while rendering.
 *
 * Kept separate from {@link NotFoundPage} deliberately. Reporting a crash as "page not
 * found" would tell a user their URL was wrong when the application had in fact failed --
 * the kind of small lie that costs an afternoon of debugging.
 *
 * The thrown value is never displayed: it can carry internal detail, and a UI is the wrong
 * place to surface it.
 */
export function RouteErrorPage() {
  const error = useRouteError()

  if (isRouteErrorResponse(error) && error.status === 404) {
    return (
      <ErrorState
        heading="Page not found"
        detail="That address does not match any page in this application."
      />
    )
  }

  return (
    <ErrorState
      heading="Something went wrong"
      detail="The page could not be displayed. Try again, or return to the dashboard."
    />
  )
}
