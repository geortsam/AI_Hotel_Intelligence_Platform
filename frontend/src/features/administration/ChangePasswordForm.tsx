import { useId, useRef, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import { ApiError } from '@/services/api/ApiError'
import { authService } from '@/services/auth/authService'
import { useAuth } from '@/session/AuthProvider'
import { MIN_PASSWORD_LENGTH } from '@/types/auth'

import styles from './AdministrationForms.module.css'

type Outcome =
  | { readonly kind: 'idle' }
  | { readonly kind: 'done' }
  | { readonly kind: 'problem'; readonly message: string }

/**
 * Changing your own password.
 *
 * ## Two things about this endpoint make it unlike every other write in the application
 *
 * 1. **It revokes every token issued before it, including the one that authorised the
 *    request**, and returns the replacement. So the response is adopted through
 *    `AuthProvider.adoptToken` before anything else happens. Without that the very next
 *    request 401s and the user is signed out by the act of securing their account.
 * 2. **A wrong *current* password is a 401**, exactly as it is at sign-in, while the bearer
 *    token stays perfectly valid. The service sends it with `credentialInBody` so the shared
 *    401 handling does not read that as expiry -- otherwise a typo would end the session.
 *    Both behaviours were verified against the live API.
 *
 * ## Whose password changes is decided by the token
 *
 * There is no `email` or `public_id` in the body, so this cannot be aimed at another
 * account. **Nothing in this application can reset somebody else's password**, and no
 * endpoint exists to: an administrator who needs to help a locked-out colleague has no
 * mechanism here, which is a real gap and is recorded as one rather than papered over.
 *
 * ## The confirmation field is this form's own
 *
 * The API takes two fields, not three. The third exists because a mistyped new password that
 * is never revealed is unrecoverable -- there is no reset endpoint to fall back on.
 */
export function ChangePasswordForm() {
  const currentId = useId()
  const nextId = useId()
  const confirmId = useId()
  const errorId = useId()

  const { adoptToken } = useAuth()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [outcome, setOutcome] = useState<Outcome>({ kind: 'idle' })
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)

  const touched = () => {
    setOutcome((previous) => (previous.kind === 'idle' ? previous : { kind: 'idle' }))
  }

  const fail = (message: string) => {
    setOutcome({ kind: 'problem', message })
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (inFlight.current) {
      return
    }
    if (current === '') {
      fail('Enter your current password.')
      return
    }
    if (next.length < MIN_PASSWORD_LENGTH) {
      fail(`A new password is at least ${MIN_PASSWORD_LENGTH} characters.`)
      return
    }
    if (next !== confirm) {
      fail('The two new passwords do not match.')
      return
    }
    if (next === current) {
      fail('The new password is the same as the current one.')
      return
    }

    inFlight.current = true
    setBusy(true)
    setOutcome({ kind: 'idle' })
    try {
      const token = await authService.changePassword({
        current_password: current,
        new_password: next,
      })
      // Before anything else: every earlier token, including the one that made this call, is
      // now revoked.
      adoptToken(token.access_token)
      setCurrent('')
      setNext('')
      setConfirm('')
      setOutcome({ kind: 'done' })
    } catch (cause: unknown) {
      fail(describe(cause))
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  return (
    <form className={styles.form} onSubmit={(event) => void submit(event)} noValidate>
      <p className={styles.intro}>
        Changes the password of the account you are signed in as. Every other session signed
        in as this account is ended; this one continues.
      </p>

      <div className={styles.grid}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={currentId}>
            Current password
          </label>
          <input
            id={currentId}
            className={styles.input}
            type="password"
            autoComplete="current-password"
            maxLength={256}
            value={current}
            disabled={busy}
            required
            aria-describedby={outcome.kind === 'problem' ? errorId : undefined}
            aria-invalid={outcome.kind === 'problem' || undefined}
            onChange={(event) => {
              setCurrent(event.target.value)
              touched()
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={nextId}>
            New password
          </label>
          <input
            id={nextId}
            className={styles.input}
            type="password"
            autoComplete="new-password"
            maxLength={256}
            value={next}
            disabled={busy}
            required
            onChange={(event) => {
              setNext(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            At least {MIN_PASSWORD_LENGTH} characters. Length is what protects a password;
            there is no composition rule.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={confirmId}>
            Confirm new password
          </label>
          <input
            id={confirmId}
            className={styles.input}
            type="password"
            autoComplete="new-password"
            maxLength={256}
            value={confirm}
            disabled={busy}
            required
            onChange={(event) => {
              setConfirm(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            Typed twice because nothing can recover a mistyped one &mdash; the API has no
            password-reset endpoint.
          </span>
        </div>
      </div>

      {outcome.kind === 'problem' ? (
        <p className={styles.error} id={errorId} role="alert">
          {outcome.message}
        </p>
      ) : null}
      {outcome.kind === 'done' ? (
        <p className={styles.success} role="status">
          Password changed. This session continues with a new token; any other session signed
          in as this account has been ended.
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button type="submit" variant="primary" size="sm" disabled={busy}>
          {busy ? 'Changing…' : 'Change password'}
        </Button>
      </div>
    </form>
  )
}

/**
 * What a refusal says, written here rather than taken from the server.
 *
 * The 401 is the interesting one: it means the *current password* was wrong, not that the
 * session died, and the copy has to say so or the reader will think they have been signed
 * out. The backend's own message is login's -- "Invalid email or password" -- which would be
 * actively confusing on a screen with no email field on it.
 */
function describe(cause: unknown): string {
  if (!(cause instanceof ApiError)) {
    return 'The password could not be changed.'
  }
  if (cause.isUnauthenticated) {
    return 'That is not your current password. Nothing was changed.'
  }
  if (cause.isRateLimited) {
    return cause.retryAfterSeconds === null
      ? 'Too many attempts. Wait a moment and try again.'
      : `Too many attempts. Try again in ${cause.retryAfterSeconds} second${
          cause.retryAfterSeconds === 1 ? '' : 's'
        }.`
  }
  if (cause.status === 422) {
    return `The new password was rejected. It must be at least ${MIN_PASSWORD_LENGTH} characters.`
  }
  if (cause.code === ApiError.NETWORK_CODE) {
    return 'The server could not be reached. Nothing was changed.'
  }
  return 'The password could not be changed. Nothing was changed.'
}
