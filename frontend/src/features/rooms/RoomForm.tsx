import { useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import {
  MAX_FLOOR,
  MIN_FLOOR,
  ROOM_NUMBER_PATTERN,
  ROOM_STATUSES,
  statusPresentation,
} from '@/features/rooms/vocabulary'
import type { Room, RoomCreateRequest, RoomStatus, RoomUpdateRequest } from '@/types/room'

import styles from './RoomForm.module.css'

export interface RoomFormProps {
  /** The room being edited, or absent when creating one. */
  readonly room?: Room
  /** The type the new room will belong to. Shown, not chosen: it comes from the URL. */
  readonly roomTypeCode: string
  readonly onCreate?: (payload: RoomCreateRequest) => void
  readonly onUpdate?: (payload: RoomUpdateRequest) => void
  readonly onCancel: () => void
  readonly busy: boolean
  readonly onDirty?: () => void
}

interface Draft {
  room_number: string
  floor: string
  status: RoomStatus
  notes: string
  is_active: boolean
}

function draftFrom(room: Room | undefined): Draft {
  return {
    room_number: room?.room_number ?? '',
    floor: room?.floor === null || room?.floor === undefined ? '' : String(room.floor),
    status: room?.status ?? 'available',
    notes: room?.notes ?? '',
    is_active: room?.is_active ?? true,
  }
}

/**
 * Creating a room, or editing one.
 *
 * ## The number can be set but never changed
 *
 * `RoomCreate` takes `room_number`; `RoomUpdate` does not -- sending it is a 422, verified.
 * It is the room's URL identity, exactly as a hotel's slug is. So the field is editable when
 * creating and shown read-only when editing, rather than being silently absent: an operator
 * looking for "rename this room" should find the answer on the screen.
 *
 * ## The room type is not a field either
 *
 * It comes from the path, and `PATCH` refuses `room_type_code` for the same reason -- moving
 * a room between types would change its parent URL. The type is displayed so the form says
 * what it is creating, and there is no control to change it.
 *
 * ## Why the duplicate-number warning mentions the whole hotel
 *
 * `UNIQUE (hotel_id, room_number)` is per **property**, not per type, so a number already
 * used under a *different* type is still a 409 -- verified live. The URL reads like a
 * namespace and is not one, and the hint says so before the refusal does.
 *
 * ## Validation mirrors the schema and stops there
 *
 * Number `^[A-Z0-9][A-Z0-9._-]*$` and at most 20 characters; floor between -10 and 200. Both
 * are checked case-insensitively here because the server upper-cases before validating, so
 * `12a` is as acceptable as `12A` and is stored as the latter. Nothing stricter is invented.
 */
export function RoomForm({
  room,
  roomTypeCode,
  onCreate,
  onUpdate,
  onCancel,
  busy,
  onDirty,
}: RoomFormProps) {
  const numberId = useId()
  const floorId = useId()
  const statusId = useId()
  const notesId = useId()
  const activeId = useId()
  const errorId = useId()

  const editing = room !== undefined
  const [draft, setDraft] = useState<Draft>(() => draftFrom(room))
  const [problem, setProblem] = useState<string | null>(null)

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }))
    setProblem(null)
    onDirty?.()
  }

  const validate = (): string | null => {
    if (!editing) {
      const number = draft.room_number.trim()
      if (number === '') {
        return 'A room needs a number.'
      }
      if (number.length > 20) {
        return 'A room number can be at most 20 characters.'
      }
      if (!ROOM_NUMBER_PATTERN.test(number)) {
        return 'A room number starts with a letter or digit and may then contain letters, digits, dots, dashes and underscores.'
      }
    }
    if (draft.floor.trim() !== '') {
      // `FloorField` is an integer between -10 and 200. Parsed here only to range-check a
      // whole number typed into a numeric field -- it is not money and not a measurement.
      if (!/^-?\d{1,3}$/.test(draft.floor.trim())) {
        return 'A floor is a whole number.'
      }
      const floor = Number(draft.floor.trim())
      if (floor < MIN_FLOOR || floor > MAX_FLOOR) {
        return `A floor is between ${MIN_FLOOR} and ${MAX_FLOOR}.`
      }
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

    const floor = draft.floor.trim() === '' ? null : Number(draft.floor.trim())

    if (editing) {
      onUpdate?.({
        floor,
        status: draft.status,
        notes: draft.notes.trim() === '' ? null : draft.notes.trim(),
        is_active: draft.is_active,
      })
      return
    }

    onCreate?.({
      // Sent as typed: the schema upper-cases it, and doing so here as well would be a
      // second implementation of one rule.
      room_number: draft.room_number.trim(),
      ...(floor === null ? {} : { floor }),
      status: draft.status,
      ...(draft.notes.trim() !== '' ? { notes: draft.notes.trim() } : {}),
      is_active: draft.is_active,
    })
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      <p className={styles.intro}>
        {editing
          ? 'A room’s number and its type are its address and cannot be changed here. Everything else can.'
          : `Creates a room of type ${roomTypeCode}. The type comes from the list you are viewing and is not editable on the room.`}
      </p>

      <div className={styles.grid}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={numberId}>
            Room number
          </label>
          <input
            id={numberId}
            className={`${styles.input} ${styles.short}`}
            type="text"
            autoComplete="off"
            spellCheck={false}
            maxLength={20}
            placeholder="101"
            value={draft.room_number}
            disabled={busy || editing}
            readOnly={editing}
            required={!editing}
            aria-describedby={problem ? errorId : undefined}
            aria-invalid={problem !== null || undefined}
            onChange={(event) => {
              set('room_number', event.target.value)
            }}
          />
          <span className={styles.hint}>
            {editing
              ? 'The room’s identity. Changing it is not something the API allows.'
              : 'Unique across the whole property, not just this type — a number used by another type is refused. Stored upper-case.'}
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={floorId}>
            Floor (optional)
          </label>
          <input
            id={floorId}
            className={`${styles.input} ${styles.short}`}
            type="number"
            inputMode="numeric"
            min={MIN_FLOOR}
            max={MAX_FLOOR}
            step={1}
            value={draft.floor}
            disabled={busy}
            onChange={(event) => {
              set('floor', event.target.value)
            }}
          />
          <span className={styles.hint}>
            Between {MIN_FLOOR} and {MAX_FLOOR}. Basements are negative.
          </span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={statusId}>
            Status
          </label>
          <select
            id={statusId}
            className={styles.select}
            value={draft.status}
            disabled={busy}
            onChange={(event) => {
              set('status', event.target.value as RoomStatus)
            }}
          >
            {ROOM_STATUSES.map((option) => (
              <option key={option} value={option}>
                {statusPresentation(option).label}
              </option>
            ))}
          </select>
          <span className={styles.hint}>
            The room&rsquo;s current operational state &mdash; not its availability over time,
            which reservations decide.
          </span>
        </div>

        <div className={styles.field}>
          <span className={styles.label}>Service</span>
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
            This room is in service
          </label>
          <span className={styles.hint}>
            Separate from the status: a room can be withdrawn from service whatever state it
            is in.
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
        </div>
      </div>

      {problem ? (
        <p className={styles.error} id={errorId} role="alert">
          {problem}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button type="submit" variant="primary" size="sm" disabled={busy}>
          {busy ? 'Saving…' : editing ? 'Save changes' : 'Create room'}
        </Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
