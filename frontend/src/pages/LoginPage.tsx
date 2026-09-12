import { AlertCircle, Hotel } from 'lucide-react'
import { useId, useState, type FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'

import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { ROUTES } from '@/router/routes'
import { ApiError } from '@/services/api/ApiError'
import { useAuth } from '@/session/AuthProvider'

import styles from './LoginPage.module.css'

/**
 * Turn a failed sign-in into something a person can act on.
 *
 * The backend's messages are already written for display -- it returns one deliberately
 * uninformative sentence for every bad credential, so that the endpoint cannot be used to
 * discover which addresses have accounts. That wording is used as-is; rephrasing it risks
 * reintroducing the distinction it exists to hide.
 *
 * Everything else is mapped to something concrete, because a raw transport failure is not
 * an answer a user can do anything with.
 */
function describeFailure(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return 'Something went wrong while signing in. Please try again.'
  }

  if (error.isRateLimited) {
    const wait = error.retryAfterSeconds
    // The wait comes from the server's own window. No fallback number is invented: a made-up
    // countdown that disagrees with the limiter is worse than not showing one.
    return wait === null
      ? 'Too many sign-in attempts. Please wait before trying again.'
      : `Too many sign-in attempts. Please try again in ${wait} second${wait === 1 ? '' : 's'}.`
  }

  if (error.status === 0) {
    return 'Could not reach the server. Check your connection and try again.'
  }

  if (error.code === ApiError.MALFORMED_CODE || error.isServerError) {
    // Never surfaces the body: a 500 can carry detail that is not for a login screen.
    return 'The server could not complete the request. Please try again shortly.'
  }

  return error.message
}

/** Only a same-site path is an acceptable post-login destination. */
function safeRedirect(from: unknown): string {
  if (typeof from !== 'string') {
    return ROUTES.dashboard
  }
  // Must be a single leading slash. `//evil.example` and `https://evil.example` are both
  // absolute URLs a browser would follow off-site -- the open-redirect shape. Router state
  // is not attacker-addressable, so this is defence in depth rather than the only guard.
  if (!from.startsWith('/') || from.startsWith('//')) {
    return ROUTES.dashboard
  }
  if (from === ROUTES.login) {
    return ROUTES.dashboard
  }
  return from
}

/**
 * The sign-in screen.
 *
 * Uses the Stage 5.2 tokens, `Card` and `Button`. The inputs are native `<input>` elements
 * with real `<label>`s: a native password field gets the browser's reveal control, password
 * managers, and autofill for free, and no custom control could match that.
 *
 * **Nothing here logs, stores or displays the password.** It lives in component state for
 * as long as the form is mounted and goes no further than the request body.
 */
export function LoginPage() {
  const { status, login, expiryNotice } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()

  const emailId = useId()
  const passwordId = useId()
  const errorId = useId()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [fieldErrors, setFieldErrors] = useState<{ email?: string; password?: string }>({})
  const [formError, setFormError] = useState<string | null>(null)
  const [isSubmitting, setSubmitting] = useState(false)

  const destination = safeRedirect((location.state as { from?: unknown } | null)?.from)

  // Already signed in: never show the form. Covers a bookmarked /login and the moment right
  // after a successful submit.
  if (status === 'authenticated') {
    return <Navigate to={destination} replace />
  }

  const validate = (): boolean => {
    const next: { email?: string; password?: string } = {}
    if (email.trim() === '') {
      next.email = 'Enter your email address.'
    }
    if (password === '') {
      next.password = 'Enter your password.'
    }
    setFieldErrors(next)
    return Object.keys(next).length === 0
  }

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setFormError(null)

    // Emptiness only. The backend deliberately applies no length floor at login -- rejecting
    // a short password client-side would tell an attacker their guess was too short to be
    // this account's password, which is exactly the signal the API is built not to give.
    if (!validate()) {
      return
    }

    setSubmitting(true)
    try {
      await login({ email: email.trim(), password })
      navigate(destination, { replace: true })
    } catch (error: unknown) {
      setFormError(describeFailure(error))
      // The password is cleared on failure so a shared terminal does not keep it in a field
      // for the next person, and a retry is typed deliberately.
      setPassword('')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className={styles.screen}>
      <div className={styles.panel}>
        <div className={styles.brand}>
          <span className={styles.brandMark} aria-hidden="true">
            <Hotel size={18} strokeWidth={2.25} />
          </span>
          <span className={styles.brandText}>Hotel Intelligence</span>
        </div>

        <Card>
          <h1 className={styles.title}>Sign in</h1>
          <p className={styles.subtitle}>Use your staff account to continue.</p>

          <form className={styles.form} onSubmit={onSubmit} noValidate>
            {expiryNotice !== null && formError === null ? (
              <p className={styles.notice}>{expiryNotice}</p>
            ) : null}

            {/*
             * The failure is announced, not just shown. `role="alert"` makes a screen
             * reader read it when it appears, which is the difference between a sighted
             * user seeing "invalid credentials" and everyone else wondering why nothing
             * happened.
             */}
            {formError !== null ? (
              <p className={styles.formError} role="alert" id={errorId}>
                <AlertCircle className={styles.formErrorIcon} size={16} aria-hidden="true" />
                <span>{formError}</span>
              </p>
            ) : null}

            <div className={styles.field}>
              <label className={styles.label} htmlFor={emailId}>
                Email address
              </label>
              <input
                id={emailId}
                className={`${styles.input} ${fieldErrors.email ? styles.inputInvalid : ''}`}
                type="email"
                name="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                autoComplete="username"
                autoFocus
                required
                disabled={isSubmitting}
                aria-invalid={fieldErrors.email ? true : undefined}
                aria-describedby={fieldErrors.email ? `${emailId}-error` : undefined}
              />
              {fieldErrors.email ? (
                <span className={styles.fieldError} id={`${emailId}-error`}>
                  {fieldErrors.email}
                </span>
              ) : null}
            </div>

            <div className={styles.field}>
              <label className={styles.label} htmlFor={passwordId}>
                Password
              </label>
              <input
                id={passwordId}
                className={`${styles.input} ${fieldErrors.password ? styles.inputInvalid : ''}`}
                type="password"
                name="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                required
                disabled={isSubmitting}
                aria-invalid={fieldErrors.password ? true : undefined}
                aria-describedby={fieldErrors.password ? `${passwordId}-error` : undefined}
              />
              {fieldErrors.password ? (
                <span className={styles.fieldError} id={`${passwordId}-error`}>
                  {fieldErrors.password}
                </span>
              ) : null}
            </div>

            {/* A real submit button, so Enter in either field submits the form. */}
            <Button
              className={styles.submit}
              type="submit"
              variant="primary"
              size="lg"
              disabled={isSubmitting}
            >
              {isSubmitting ? 'Signing in…' : 'Sign in'}
            </Button>
          </form>
        </Card>

        <p className={styles.footnote}>
          Accounts are issued by your administrator.
        </p>
      </div>
    </div>
  )
}
