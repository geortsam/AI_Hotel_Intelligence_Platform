import { useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import type { CatalogueRow } from '@/features/platform/useCatalogue'
import type { CatalogueKind } from '@/types/platform'

import styles from './PlatformForms.module.css'

export interface CatalogueFormProps {
  readonly kind: CatalogueKind
  /** The entry being edited, or absent when creating one. */
  readonly entry?: CatalogueRow
  readonly onSubmit: (payload: Record<string, unknown>) => void
  readonly onCancel: () => void
  readonly busy: boolean
  readonly onDirty?: () => void
}

/** The boolean each catalogue carries, and what it is called in the schema. */
const FLAG: Readonly<Record<CatalogueKind, { field: string; label: string; hint: string } | null>> =
  {
    amenities: null,
    'revenue-categories': {
      field: 'is_room_revenue',
      label: 'This is room revenue',
      hint: 'A flag, not a rule. Room revenue is held in booking room nights; this marks the categories a metrics job must exclude from other-revenue totals.',
    },
    'expense-categories': {
      field: 'is_fixed_cost',
      label: 'This is a fixed cost',
      hint: 'Marks an expense that does not vary with occupancy.',
    },
  }

const NOUN: Readonly<Record<CatalogueKind, string>> = {
  amenities: 'amenity',
  'revenue-categories': 'revenue category',
  'expense-categories': 'expense category',
}

/**
 * Creating an entry in a shared catalogue, or editing one.
 *
 * One component for all three and for both verbs, because the contract is the same shape
 * every time, with two differences the form encodes directly:
 *
 * * **`code` is settable at creation and immutable afterwards.** It is the URL identity and
 *   every association is keyed on the row it names, so the update schemas simply have no
 *   `code` field -- sending one is a 422. Shown read-only when editing rather than hidden, so
 *   somebody looking for "rename this" finds the answer instead of assuming it was forgotten.
 * * **`is_active` exists on the two category catalogues and not on amenities**, because
 *   `amenities` has no such column. Retiring is the documented alternative to deleting a
 *   category that anything references.
 *
 * ## The code is sent as typed
 *
 * Each schema upper-cases it before the unique constraint sees it -- the constraint is
 * case-sensitive, so `wifi` and `WIFI` would otherwise become two entries for one concept.
 * Normalising here as well would hide which side is responsible.
 */
export function CatalogueForm({
  kind,
  entry,
  onSubmit,
  onCancel,
  busy,
  onDirty,
}: CatalogueFormProps) {
  const codeId = useId()
  const nameId = useId()
  const groupId = useId()
  const flagId = useId()
  const activeId = useId()
  const errorId = useId()

  const editing = entry !== undefined
  const flag = FLAG[kind]
  const hasActive = kind !== 'amenities'

  const [code, setCode] = useState(entry?.code ?? '')
  const [name, setName] = useState(entry?.name ?? '')
  const [group, setGroup] = useState(entry?.category ?? '')
  const [flagValue, setFlagValue] = useState(entry?.flag ?? false)
  const [active, setActive] = useState(entry?.isActive ?? true)
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

    if (!editing) {
      const typed = code.trim()
      if (typed === '') {
        return setProblem(`An ${NOUN[kind]} needs a code.`)
      }
      // Mirrors the schema's own pattern rather than inventing a stricter one: a form that
      // refuses a code the API would accept is a bug the user cannot work around.
      if (typed.length > 50 || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(typed)) {
        return setProblem(
          'A code starts with a letter or digit, may then contain letters, digits, dashes and underscores, and is at most 50 characters.',
        )
      }
    }
    if (name.trim() === '') {
      return setProblem('A name is required.')
    }
    if (name.trim().length > 100) {
      return setProblem('A name is at most 100 characters.')
    }
    if (kind === 'amenities' && group.trim().length > 50) {
      return setProblem('A group is at most 50 characters.')
    }
    setProblem(null)

    if (kind === 'amenities') {
      const shared = {
        name: name.trim(),
        // An emptied optional becomes an explicit null: that is how the API clears a column.
        category: group.trim() === '' ? null : group.trim(),
      }
      onSubmit(editing ? shared : { ...shared, code: code.trim() })
      return
    }

    const shared = {
      name: name.trim(),
      [flag!.field]: flagValue,
      is_active: active,
    }
    onSubmit(editing ? shared : { ...shared, code: code.trim() })
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      <p className={styles.intro}>
        {editing
          ? `A ${NOUN[kind]}’s code is its identity across every property and cannot be changed. Everything else can.`
          : `Creates a ${NOUN[kind]} shared by every property. Its code is global and cannot be changed afterwards.`}{' '}
        Writing a shared catalogue needs a platform administrator grant.
      </p>

      <div className={styles.grid}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={codeId}>
            Code
          </label>
          <input
            id={codeId}
            className={`${styles.input} ${styles.short}`}
            type="text"
            autoComplete="off"
            spellCheck={false}
            maxLength={50}
            placeholder={kind === 'amenities' ? 'SEA_VIEW' : 'FNB'}
            value={code}
            disabled={busy || editing}
            readOnly={editing}
            required={!editing}
            aria-describedby={problem ? errorId : undefined}
            aria-invalid={problem !== null || undefined}
            onChange={(event) => {
              setCode(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            {editing
              ? 'The identity every property references. Changing it is not something the API allows.'
              : 'Unique across the whole installation. Stored upper-case.'}
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={nameId}>
            Name
          </label>
          <input
            id={nameId}
            className={styles.input}
            type="text"
            maxLength={100}
            value={name}
            disabled={busy}
            required
            onChange={(event) => {
              setName(event.target.value)
              touched()
            }}
          />
        </div>

        {kind === 'amenities' ? (
          <div className={styles.field}>
            <label className={styles.label} htmlFor={groupId}>
              Group (optional)
            </label>
            <input
              id={groupId}
              className={styles.input}
              type="text"
              maxLength={50}
              placeholder="Bathroom"
              value={group}
              disabled={busy}
              onChange={(event) => {
                setGroup(event.target.value)
                touched()
              }}
            />
            <span className={styles.hint}>
              Free text used to group amenities for display. The API does not filter by it.
            </span>
          </div>
        ) : null}

        {flag ? (
          <div className={`${styles.field} ${styles.wide}`}>
            <span className={styles.label}>Classification</span>
            <label className={styles.checkbox} htmlFor={flagId}>
              <input
                id={flagId}
                type="checkbox"
                checked={flagValue}
                disabled={busy}
                onChange={(event) => {
                  setFlagValue(event.target.checked)
                  touched()
                }}
              />
              {flag.label}
            </label>
            <span className={styles.hint}>{flag.hint}</span>
          </div>
        ) : null}

        {hasActive ? (
          <div className={`${styles.field} ${styles.wide}`}>
            <span className={styles.label}>Status</span>
            <label className={styles.checkbox} htmlFor={activeId}>
              <input
                id={activeId}
                type="checkbox"
                checked={active}
                disabled={busy}
                onChange={(event) => {
                  setActive(event.target.checked)
                  touched()
                }}
              />
              Available for new postings
            </label>
            <span className={styles.hint}>
              Retiring a category keeps every ledger line that already references it, and is
              the documented alternative to deleting one that anything uses.
            </span>
          </div>
        ) : null}
      </div>

      {problem ? (
        <p className={styles.error} id={errorId} role="alert">
          {problem}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button type="submit" variant="primary" size="sm" disabled={busy}>
          {busy ? 'Saving…' : editing ? 'Save entry' : 'Create entry'}
        </Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
