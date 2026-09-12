import { useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import type { Guest, GuestCreateRequest, GuestUpdateRequest } from '@/types/guest'

import styles from './GuestForm.module.css'

export interface GuestFormProps {
  /** The guest being edited, or absent when creating one. */
  readonly guest?: Guest
  readonly onCreate?: (payload: GuestCreateRequest) => void
  readonly onUpdate?: (payload: GuestUpdateRequest) => void
  readonly onCancel: () => void
  readonly busy: boolean
  /** Clears the previous refusal as soon as a new attempt starts. */
  readonly onDirty?: () => void
}

/** The editable shape, all as strings, because that is what an input holds. */
interface Draft {
  first_name: string
  last_name: string
  email: string
  phone: string
  country_code: string
  preferred_language: string
  date_of_birth: string
  notes: string
  marketing_opt_in: boolean
}

function draftFrom(guest: Guest | undefined): Draft {
  return {
    first_name: guest?.first_name ?? '',
    last_name: guest?.last_name ?? '',
    email: guest?.email ?? '',
    phone: guest?.phone ?? '',
    country_code: guest?.country_code ?? '',
    preferred_language: guest?.preferred_language ?? '',
    date_of_birth: guest?.date_of_birth ?? '',
    notes: guest?.notes ?? '',
    marketing_opt_in: guest?.marketing_opt_in ?? false,
  }
}

/**
 * Creating a guest, or editing one.
 *
 * One component for both, because `GuestCreate` and `GuestUpdate` carry **the same fields** --
 * the only difference is that create requires the two names and update requires nothing. A
 * second form would be the same nine inputs with a different submit handler, and the two
 * would drift.
 *
 * ## Only the fields the client may write
 *
 * Nine, and they are exactly the schema's. `public_id` and `hotel_public_id` are absent
 * because they are the URL identity and server-assigned -- sending either is a 422, verified.
 * `created_at` and `updated_at` are absent for the same reason.
 *
 * ## What is NOT validated here, and why that is deliberate
 *
 * The backend checks: names 1..100 characters, email 3..254 characters, `country_code`
 * `^[A-Z]{2}$`, `preferred_language` `^[a-z]{2}$`. Those are mirrored below so a refusal
 * arrives at the field that caused it rather than as a banner.
 *
 * It does **not** check that an email looks like an email. `"not-an-email"` was accepted with
 * a 201, verified live -- there is no format rule in the schema and no CHECK on the column.
 * So this form does not impose one either: a client-side rule stricter than the server's
 * would refuse data the system is willing to hold, and would do it in the one place where a
 * receptionist cannot override it. The field is `type="email"` for the keyboard and autofill
 * it buys on a phone, and the form is `noValidate`, so the browser does not enforce a rule
 * the platform has not adopted.
 *
 * Nor is a date of birth bounded: `2199-01-01` was accepted. Left to the server for the same
 * reason.
 *
 * ## Case is the server's to fix
 *
 * `gb` is stored as `GB` and `EN` as `en` -- the schema normalises both before the CHECK
 * sees them, verified live. So the codes are sent as typed. Doing it here as well would be a
 * second implementation of a rule that already has one.
 *
 * ## An empty optional means "clear it", and only when editing
 *
 * On update, a field emptied by the operator is sent as an **explicit null**, which is how
 * the API clears a column -- an omitted field is left untouched, and those are different
 * requests. On create there is nothing to clear, so an empty field is simply omitted.
 */
export function GuestForm({
  guest,
  onCreate,
  onUpdate,
  onCancel,
  busy,
  onDirty,
}: GuestFormProps) {
  const firstId = useId()
  const lastId = useId()
  const emailId = useId()
  const phoneId = useId()
  const countryId = useId()
  const languageId = useId()
  const dobId = useId()
  const notesId = useId()
  const optInId = useId()
  const errorId = useId()

  const editing = guest !== undefined
  const [draft, setDraft] = useState<Draft>(() => draftFrom(guest))
  const [problem, setProblem] = useState<string | null>(null)

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }))
    setProblem(null)
    onDirty?.()
  }

  const validate = (): string | null => {
    // Mirrors `NameField`: 1..100 after the server's own whitespace strip.
    if (draft.first_name.trim() === '' || draft.last_name.trim() === '') {
      return 'A guest needs both a first and a last name.'
    }
    if (draft.first_name.trim().length > 100 || draft.last_name.trim().length > 100) {
      return 'A name can be at most 100 characters.'
    }
    // Mirrors `EmailField`: a length bound, and nothing about format. See the docstring.
    const email = draft.email.trim()
    if (email !== '' && (email.length < 3 || email.length > 254)) {
      return 'An email address is between 3 and 254 characters.'
    }
    if (draft.phone.trim().length > 50) {
      return 'A phone number can be at most 50 characters.'
    }
    const country = draft.country_code.trim()
    if (country !== '' && !/^[A-Za-z]{2}$/.test(country)) {
      return 'A country is a two-letter code, such as GR or GB.'
    }
    const language = draft.preferred_language.trim()
    if (language !== '' && !/^[A-Za-z]{2}$/.test(language)) {
      return 'A language is a two-letter code, such as en or el.'
    }
    return null
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (busy) {
      return
    }
    const failure = validate()
    if (failure !== null) {
      setProblem(failure)
      return
    }
    setProblem(null)

    if (editing) {
      // Emptied optionals become an explicit null: that is how the API clears a column.
      onUpdate?.({
        first_name: draft.first_name.trim(),
        last_name: draft.last_name.trim(),
        email: draft.email.trim() === '' ? null : draft.email.trim(),
        phone: draft.phone.trim() === '' ? null : draft.phone.trim(),
        country_code: draft.country_code.trim() === '' ? null : draft.country_code.trim(),
        preferred_language:
          draft.preferred_language.trim() === '' ? null : draft.preferred_language.trim(),
        date_of_birth: draft.date_of_birth === '' ? null : draft.date_of_birth,
        notes: draft.notes.trim() === '' ? null : draft.notes.trim(),
        marketing_opt_in: draft.marketing_opt_in,
      })
      return
    }

    // Creating: an empty optional is omitted rather than sent as null. There is nothing to
    // clear on a row that does not exist yet, and `extra="forbid"` rewards sending less.
    onCreate?.({
      first_name: draft.first_name.trim(),
      last_name: draft.last_name.trim(),
      ...(draft.email.trim() !== '' ? { email: draft.email.trim() } : {}),
      ...(draft.phone.trim() !== '' ? { phone: draft.phone.trim() } : {}),
      ...(draft.country_code.trim() !== '' ? { country_code: draft.country_code.trim() } : {}),
      ...(draft.preferred_language.trim() !== ''
        ? { preferred_language: draft.preferred_language.trim() }
        : {}),
      ...(draft.date_of_birth !== '' ? { date_of_birth: draft.date_of_birth } : {}),
      ...(draft.notes.trim() !== '' ? { notes: draft.notes.trim() } : {}),
      marketing_opt_in: draft.marketing_opt_in,
    })
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      <p className={styles.intro}>
        {editing
          ? 'Changes are saved to this property’s own guest record. The same person at another property is a separate record.'
          : 'Creates a guest record for this property. A guest belongs to one hotel; the same person elsewhere is a separate record.'}
      </p>

      <div className={styles.grid}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={firstId}>
            First name
          </label>
          <input
            id={firstId}
            className={styles.input}
            type="text"
            autoComplete="off"
            maxLength={100}
            value={draft.first_name}
            disabled={busy}
            required
            aria-describedby={problem ? errorId : undefined}
            aria-invalid={problem !== null || undefined}
            onChange={(event) => {
              set('first_name', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={lastId}>
            Last name
          </label>
          <input
            id={lastId}
            className={styles.input}
            type="text"
            autoComplete="off"
            maxLength={100}
            value={draft.last_name}
            disabled={busy}
            required
            aria-describedby={problem ? errorId : undefined}
            aria-invalid={problem !== null || undefined}
            onChange={(event) => {
              set('last_name', event.target.value)
            }}
          />
          <span className={styles.hint}>The list is ordered by surname, then forename.</span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={emailId}>
            Email (optional)
          </label>
          <input
            id={emailId}
            className={styles.input}
            type="email"
            autoComplete="off"
            maxLength={254}
            value={draft.email}
            disabled={busy}
            onChange={(event) => {
              set('email', event.target.value)
            }}
          />
          <span className={styles.hint}>
            At most one guest at this property may use an address. Many have none, which is
            allowed.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={phoneId}>
            Phone (optional)
          </label>
          <input
            id={phoneId}
            className={styles.input}
            type="tel"
            autoComplete="off"
            maxLength={50}
            value={draft.phone}
            disabled={busy}
            onChange={(event) => {
              set('phone', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={countryId}>
            Country (optional)
          </label>
          <input
            id={countryId}
            className={`${styles.input} ${styles.short}`}
            type="text"
            autoComplete="off"
            maxLength={2}
            placeholder="GR"
            value={draft.country_code}
            disabled={busy}
            onChange={(event) => {
              set('country_code', event.target.value)
            }}
          />
          <span className={styles.hint}>Two letters. Stored upper-case by the server.</span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={languageId}>
            Preferred language (optional)
          </label>
          <input
            id={languageId}
            className={`${styles.input} ${styles.short}`}
            type="text"
            autoComplete="off"
            maxLength={2}
            placeholder="en"
            value={draft.preferred_language}
            disabled={busy}
            onChange={(event) => {
              set('preferred_language', event.target.value)
            }}
          />
          <span className={styles.hint}>Two letters. Stored lower-case by the server.</span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={dobId}>
            Date of birth (optional)
          </label>
          <input
            id={dobId}
            className={styles.input}
            type="date"
            value={draft.date_of_birth}
            disabled={busy}
            onChange={(event) => {
              set('date_of_birth', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <span className={styles.label}>Marketing</span>
          <label className={styles.checkbox} htmlFor={optInId}>
            <input
              id={optInId}
              type="checkbox"
              checked={draft.marketing_opt_in}
              disabled={busy}
              onChange={(event) => {
                set('marketing_opt_in', event.target.checked)
              }}
            />
            This guest has consented to marketing
          </label>
          <span className={styles.hint}>
            Off unless the guest said otherwise &mdash; the column defaults to no consent.
          </span>
        </div>

        <div className={`${styles.field} ${styles.wide}`}>
          <label className={styles.label} htmlFor={notesId}>
            Notes (optional)
          </label>
          <textarea
            id={notesId}
            className={styles.textarea}
            rows={3}
            value={draft.notes}
            disabled={busy}
            onChange={(event) => {
              set('notes', event.target.value)
            }}
          />
          <span className={styles.hint}>
            Visible to anyone with access to this property&rsquo;s guest records.
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
          {busy ? 'Saving…' : editing ? 'Save changes' : 'Create guest'}
        </Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
