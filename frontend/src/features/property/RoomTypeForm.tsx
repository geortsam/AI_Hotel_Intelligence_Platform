import { useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import type { RoomType } from '@/types/room'
import type { RoomTypeCreateRequest, RoomTypeUpdateRequest } from '@/types/roomType'

import styles from './PropertyForms.module.css'

export interface RoomTypeFormProps {
  /** The room type being edited, or absent when creating one. */
  readonly roomType?: RoomType
  /** The property's currency, used as the default for a new type. */
  readonly defaultCurrency: string
  readonly onCreate?: (payload: RoomTypeCreateRequest) => void
  readonly onUpdate?: (payload: RoomTypeUpdateRequest) => void
  readonly onCancel: () => void
  readonly busy: boolean
  readonly onDirty?: () => void
}

interface Draft {
  code: string
  name: string
  description: string
  max_occupancy: string
  standard_occupancy: string
  bed_count: string
  bed_configuration: string
  size_sqm: string
  base_price: string
  currency: string
  is_active: boolean
}

function draftFrom(roomType: RoomType | undefined, defaultCurrency: string): Draft {
  return {
    code: roomType?.code ?? '',
    name: roomType?.name ?? '',
    description: roomType?.description ?? '',
    max_occupancy: roomType === undefined ? '2' : String(roomType.max_occupancy),
    standard_occupancy: roomType === undefined ? '2' : String(roomType.standard_occupancy),
    bed_count: roomType === undefined ? '1' : String(roomType.bed_count),
    bed_configuration: roomType?.bed_configuration ?? '',
    size_sqm: roomType?.size_sqm ?? '',
    base_price: roomType?.base_price ?? '',
    currency: roomType?.currency ?? defaultCurrency,
    is_active: roomType?.is_active ?? true,
  }
}

/**
 * Creating a room type, or editing one.
 *
 * One component for both, because the two schemas carry the same fields apart from three
 * differences the form encodes directly:
 *
 * * **`code` is settable at creation and immutable afterwards** -- it is the URL identity,
 *   exactly as a hotel's slug is, and sending it to `PATCH` is a 422. Shown read-only when
 *   editing rather than hidden.
 * * **`is_active` is update-only.** Sending it at creation is a 422; a type is created
 *   active.
 * * Everything else is required on create and optional on update.
 *
 * ## The price is a string from the field to the column
 *
 * `base_price` is `NUMERIC(14,2)` and arrives as a JSON string; it is validated as a string
 * against the column's shape and sent as one. `parseFloat` appears nowhere near it -- the
 * same rule the ledger screens follow, for the same reason.
 *
 * ## The occupancy rule is checked here, and again by the server for a reason
 *
 * `standard_occupancy` may not exceed `max_occupancy`. Both are on this form, so the check
 * is mirrored and lands on the field. Note that the server enforces it twice with different
 * statuses: a **422** when both values are sent, and a **409** when only one is and the
 * comparison is against the stored value. Because this form always sends both on an update,
 * a refusal from it arrives as the 422 -- but the 409 path is real and is handled by the
 * page.
 */
export function RoomTypeForm({
  roomType,
  defaultCurrency,
  onCreate,
  onUpdate,
  onCancel,
  busy,
  onDirty,
}: RoomTypeFormProps) {
  const codeId = useId()
  const nameId = useId()
  const descriptionId = useId()
  const maxId = useId()
  const standardId = useId()
  const bedsId = useId()
  const bedConfigId = useId()
  const sizeId = useId()
  const priceId = useId()
  const currencyId = useId()
  const activeId = useId()
  const errorId = useId()

  const editing = roomType !== undefined
  const [draft, setDraft] = useState<Draft>(() => draftFrom(roomType, defaultCurrency))
  const [problem, setProblem] = useState<string | null>(null)

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }))
    setProblem(null)
    onDirty?.()
  }

  const validate = (): string | null => {
    if (!editing) {
      const code = draft.code.trim()
      if (code === '') {
        return 'A room type needs a code.'
      }
      if (code.length > 20 || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(code)) {
        return 'A code starts with a letter or digit, may then contain letters, digits, dashes and underscores, and is at most 20 characters.'
      }
    }
    if (draft.name.trim() === '') {
      return 'A room type needs a name.'
    }
    for (const [label, value] of [
      ['Maximum occupancy', draft.max_occupancy],
      ['Standard occupancy', draft.standard_occupancy],
      ['Bed count', draft.bed_count],
    ] as const) {
      if (!/^\d{1,2}$/.test(value) || Number(value) < 1 || Number(value) > 99) {
        return `${label} is a whole number between 1 and 99.`
      }
    }
    if (Number(draft.standard_occupancy) > Number(draft.max_occupancy)) {
      return 'Standard occupancy cannot exceed maximum occupancy.'
    }
    // `NUMERIC(14,2)`, zero or more. Checked as a string; never parsed into a float.
    if (!/^(0|[1-9]\d{0,11})(\.\d{1,2})?$/.test(draft.base_price.trim())) {
      return 'A base price is zero or more, with at most two decimal places.'
    }
    if (draft.size_sqm.trim() !== '') {
      if (!/^(0|[1-9]\d{0,3})(\.\d{1,2})?$/.test(draft.size_sqm.trim())) {
        return 'A size is a number with at most two decimal places.'
      }
      if (/^0(\.0{1,2})?$/.test(draft.size_sqm.trim())) {
        return 'A size must be greater than zero, or blank.'
      }
    }
    if (!/^[A-Za-z]{3}$/.test(draft.currency.trim())) {
      return 'A currency is a three-letter code, such as EUR.'
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

    const shared = {
      name: draft.name.trim(),
      max_occupancy: Number(draft.max_occupancy),
      standard_occupancy: Number(draft.standard_occupancy),
      bed_count: Number(draft.bed_count),
      // A decimal string, sent as typed. See the docstring.
      base_price: draft.base_price.trim(),
      currency: draft.currency.trim(),
    }

    if (editing) {
      onUpdate?.({
        ...shared,
        description: draft.description.trim() === '' ? null : draft.description.trim(),
        bed_configuration:
          draft.bed_configuration.trim() === '' ? null : draft.bed_configuration.trim(),
        size_sqm: draft.size_sqm.trim() === '' ? null : draft.size_sqm.trim(),
        is_active: draft.is_active,
      })
      return
    }

    onCreate?.({
      ...shared,
      // Sent as typed: the schema upper-cases it.
      code: draft.code.trim(),
      ...(draft.description.trim() !== '' ? { description: draft.description.trim() } : {}),
      ...(draft.bed_configuration.trim() !== ''
        ? { bed_configuration: draft.bed_configuration.trim() }
        : {}),
      ...(draft.size_sqm.trim() !== '' ? { size_sqm: draft.size_sqm.trim() } : {}),
    })
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      <p className={styles.intro}>
        {editing
          ? 'A room type’s code is its address and cannot be changed. Everything else can.'
          : 'Creates a room type for this property. Its code becomes part of every room’s address, so it cannot be changed afterwards.'}
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
            maxLength={20}
            placeholder="DLX"
            value={draft.code}
            disabled={busy || editing}
            readOnly={editing}
            required={!editing}
            aria-describedby={problem ? errorId : undefined}
            aria-invalid={problem !== null || undefined}
            onChange={(event) => {
              set('code', event.target.value)
            }}
          />
          <span className={styles.hint}>
            {editing
              ? 'Part of every room’s address. Changing it is not something the API allows.'
              : 'Unique within this property. Stored upper-case.'}
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
            maxLength={150}
            value={draft.name}
            disabled={busy}
            required
            onChange={(event) => {
              set('name', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={maxId}>
            Maximum occupancy
          </label>
          <input
            id={maxId}
            className={`${styles.input} ${styles.short}`}
            type="number"
            inputMode="numeric"
            min={1}
            max={99}
            step={1}
            value={draft.max_occupancy}
            disabled={busy}
            required
            onChange={(event) => {
              set('max_occupancy', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={standardId}>
            Standard occupancy
          </label>
          <input
            id={standardId}
            className={`${styles.input} ${styles.short}`}
            type="number"
            inputMode="numeric"
            min={1}
            max={99}
            step={1}
            value={draft.standard_occupancy}
            disabled={busy}
            required
            onChange={(event) => {
              set('standard_occupancy', event.target.value)
            }}
          />
          <span className={styles.hint}>Never more than the maximum.</span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={bedsId}>
            Bed count
          </label>
          <input
            id={bedsId}
            className={`${styles.input} ${styles.short}`}
            type="number"
            inputMode="numeric"
            min={1}
            max={99}
            step={1}
            value={draft.bed_count}
            disabled={busy}
            required
            onChange={(event) => {
              set('bed_count', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={bedConfigId}>
            Bed configuration (optional)
          </label>
          <input
            id={bedConfigId}
            className={styles.input}
            type="text"
            maxLength={100}
            placeholder="1 king + 1 sofa bed"
            value={draft.bed_configuration}
            disabled={busy}
            onChange={(event) => {
              set('bed_configuration', event.target.value)
            }}
          />
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={priceId}>
            Base price
          </label>
          <input
            id={priceId}
            className={styles.input}
            type="text"
            inputMode="decimal"
            autoComplete="off"
            placeholder="0.00"
            value={draft.base_price}
            disabled={busy}
            required
            onChange={(event) => {
              set('base_price', event.target.value)
            }}
          />
          <span className={styles.hint}>
            The type&rsquo;s list price. Nothing on this screen prices a stay &mdash; a
            booking&rsquo;s nightly rates are its own.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={currencyId}>
            Currency
          </label>
          <input
            id={currencyId}
            className={`${styles.input} ${styles.short}`}
            type="text"
            maxLength={3}
            value={draft.currency}
            disabled={busy}
            required
            onChange={(event) => {
              set('currency', event.target.value)
            }}
          />
          <span className={styles.hint}>Defaults to the property&rsquo;s currency.</span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={sizeId}>
            Size in m² (optional)
          </label>
          <input
            id={sizeId}
            className={`${styles.input} ${styles.short}`}
            type="text"
            inputMode="decimal"
            autoComplete="off"
            value={draft.size_sqm}
            disabled={busy}
            onChange={(event) => {
              set('size_sqm', event.target.value)
            }}
          />
        </div>

        {editing ? (
          <div className={styles.field}>
            <span className={styles.label}>Status</span>
            <label className={styles.checkbox} htmlFor={activeId}>
              <input
                id={activeId}
                type="checkbox"
                checked={draft.is_active}
                disabled={busy}
                onChange={(event) => {
                  set('is_active', event.target.checked)
                }}
              />
              This room type is bookable
            </label>
            <span className={styles.hint}>
              An inactive type is excluded from availability. Its rooms keep their records.
            </span>
          </div>
        ) : null}

        <div className={`${styles.field} ${styles.wide}`}>
          <label className={styles.label} htmlFor={descriptionId}>
            Description (optional)
          </label>
          <textarea
            id={descriptionId}
            className={styles.textarea}
            rows={3}
            value={draft.description}
            disabled={busy}
            onChange={(event) => {
              set('description', event.target.value)
            }}
          />
        </div>
      </div>

      {problem ? (
        <p className={styles.error} id={errorId} role="alert">
          {problem}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button type="submit" variant="primary" size="sm" disabled={busy}>
          {busy ? 'Saving…' : editing ? 'Save room type' : 'Create room type'}
        </Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
