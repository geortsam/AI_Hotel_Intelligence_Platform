import { AppRouter } from '@/router/AppRouter'
import { AuthProvider } from '@/session/AuthProvider'
import { HotelProvider } from '@/session/HotelProvider'

/**
 * The application root.
 *
 * The session provider wraps the router, not the other way round: route guards and the
 * sign-in page both read the session, so it has to exist above them. It mounts nothing
 * else -- layout belongs to `AppShell`, routes to `AppRouter`, configuration to
 * `config/env`.
 *
 * The hotel context sits **between** the two, and the nesting is load-bearing in both
 * directions. It reads the session, so it must be inside `AuthProvider`; the header and the
 * dashboard both read it, so it must be outside the router. It issues no request until a
 * session exists and drops what it holds when one ends, so a signed-out browser is never
 * left holding a list of properties.
 */
export default function App() {
  return (
    <AuthProvider>
      <HotelProvider>
        <AppRouter />
      </HotelProvider>
    </AuthProvider>
  )
}
