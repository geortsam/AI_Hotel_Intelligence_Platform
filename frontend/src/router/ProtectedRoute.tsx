import { Navigate, Outlet, useLocation } from 'react-router-dom'

import { AuthLoading } from '@/components/layout/AuthLoading'
import { ROUTES } from '@/router/routes'
import { useAuth } from '@/session/AuthProvider'

/**
 * The gate in front of every application route.
 *
 * A layout route with no UI of its own: it renders the shell's children when there is a
 * session, the sign-in page's redirect when there is not, and a loading state while that is
 * still being decided.
 *
 * **The `restoring` branch is the important one.** Without it, a returning user with a valid
 * stored token would be redirected to sign-in on the first frame -- before `/auth/me` had
 * answered -- and then bounced back. That reads as a random logout, and it is the bug this
 * three-state design exists to prevent.
 *
 * `replace` keeps the redirect out of the history stack, so the browser's Back button does
 * not walk the user through a chain of bounces.
 */
export function ProtectedRoute() {
  const { status } = useAuth()
  const location = useLocation()

  if (status === 'restoring') {
    return <AuthLoading />
  }

  if (status === 'unauthenticated') {
    return (
      <Navigate
        to={ROUTES.login}
        replace
        /*
         * Where the user was headed, so signing in can return them there.
         *
         * Carried in router state rather than a `?next=` query parameter, deliberately.
         * A redirect target in the URL is attacker-supplied input: it invites an open
         * redirect, and it survives copy-paste into a chat window. Router state is not
         * addressable, so it cannot be crafted by a link. The sign-in page still validates
         * it before using it -- see `LoginPage`.
         */
        state={{ from: location.pathname + location.search }}
      />
    )
  }

  return <Outlet />
}
