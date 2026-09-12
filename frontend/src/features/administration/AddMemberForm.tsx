import { useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import { HOTEL_ROLES, ROLE_DESCRIPTIONS, type HotelRole } from '@/types/member'
import type { MemberCreateRequest } from '@/types/member'

import styles from './AdministrationForms.module.css'

export interface AddMemberFormProps {
  readonly onAdd: (payload: MemberCreateRequest) => void
  readonly onCancel: () => void
  readonly busy: boolean
  readonly onDirty?: () => void
}

/**
 * Granting an existing account access to this property.
 *
 * ## It names a person by email, and cannot create them
 *
 * `MemberCreate` takes an email because that is the identifier a human actually has: the API
 * has **no user-search endpoint** (`GET /users` is a 404, verified), so there is nothing to
 * pick from. An address with no account behind it is a 404, and the page says what to do
 * about it -- the person registers, then they can be added. This form never creates an
 * account, because identity is global and a second row for one human would mean two
 * passwords and two audit trails.
 *
 * ## The role is required, and there are exactly four
 *
 * No default is pre-selected beyond the lowest one. `viewer` is the safe floor: granting
 * more access than intended by leaving a field alone is the kind of mistake a membership
 * screen should make hard, and the four values are the ones `HotelRole` declares -- anything
 * else is a 422 that lists them.
 *
 * ## The address is not normalised here
 *
 * The backend lower-cases it before matching, and an upper-case address resolved to the same
 * account in live verification. Trimming whitespace is this form's business; case is the
 * server's.
 */
export function AddMemberForm({ onAdd, onCancel, busy, onDirty }: AddMemberFormProps) {
  const emailId = useId()
  const roleId = useId()
  const errorId = useId()
  const hintId = useId()

  const [email, setEmail] = useState('')
  const [role, setRole] = useState<HotelRole>('viewer')
  const [problem, setProblem] = useState<string | null>(null)

  const touched = () => {
    setProblem(null)
    onDirty?.()
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (busy) {
      return
    }
    const address = email.trim()
    // Mirrors the backend's `EmailField` pattern rather than inventing a stricter one: a
    // form that refuses an address the API would accept is a bug the user cannot work around.
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(address) || address.length > 254) {
      setProblem('Enter the email address of an existing account.')
      return
    }
    setProblem(null)
    onAdd({ email: address, role })
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      <p className={styles.intro}>
        Grants an account that <strong>already exists</strong> access to this property. Adding
        a member needs the owner role. This cannot create an account &mdash; if the person has
        none, they register first.
      </p>

      <div className={styles.grid}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={emailId}>
            Email address
          </label>
          <input
            id={emailId}
            className={styles.input}
            type="email"
            autoComplete="off"
            spellCheck={false}
            maxLength={254}
            placeholder="colleague@example.com"
            value={email}
            disabled={busy}
            required
            aria-describedby={problem ? errorId : undefined}
            aria-invalid={problem !== null || undefined}
            onChange={(event) => {
              setEmail(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            The address on their existing account. Matched without regard to case.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={roleId}>
            Role
          </label>
          <select
            id={roleId}
            className={styles.select}
            value={role}
            disabled={busy}
            aria-describedby={hintId}
            onChange={(event) => {
              setRole(event.target.value as HotelRole)
              touched()
            }}
          >
            {HOTEL_ROLES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <span className={styles.hint} id={hintId}>
            {ROLE_DESCRIPTIONS[role]}
          </span>
        </div>
      </div>

      {problem ? (
        <p className={styles.error} id={errorId} role="alert">
          {problem}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button type="submit" variant="primary" size="sm" disabled={busy}>
          {busy ? 'Adding…' : 'Add member'}
        </Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
