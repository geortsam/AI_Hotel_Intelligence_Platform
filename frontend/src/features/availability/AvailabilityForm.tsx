import { useId, useState, type FormEvent } from 'react'
import { Plus, X } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import {
  MAX_ROOMS_REQUIRED,
  MAX_ROOM_TYPE_DEMANDS,
  MAX_STAY_NIGHTS,
  type AvailabilitySearch,
} from '@/services/availability/availabilityService'
import type { RoomType } from '@/types/room'

import styles from './AvailabilityForm.module.css'

export interface AvailabilityFormProps {
  readonly types: readonly RoomType[]
  /** Today in the hotel's own zone, used only as the default check-in. */
  readonly today: string
  readonly onSearch: (search: AvailabilitySearch) => void
  readonly busy: boolean
  /** Clears the previous failure as soon as the form is touched. */
  readonly onDirty?: () => void
}

type Mode = 'single' | 'mixed'

interface DemandDraft {
  code: string
  count: string
}

/**
 * The availability search form.
 *
 * ## Two modes, because the backend has two request forms and refuses their mixture
 *
 * `rooms` names types and counts; `room_type_code` and `rooms_required` are the single-type
 * way of saying a similar thing. Sending both is a **422** -- verified for each combination
 * -- so the form offers a choice rather than a set of fields that can contradict each other.
 * The refused request is not merely discouraged here; it cannot be expressed.
 *
 * ## What is validated here, and what is deliberately left to the server
 *
 * Mirrored, so a refusal lands on the field that caused it: `check_out` later than
 * `check_in`, a stay within {@link MAX_STAY_NIGHTS} nights, `rooms_required` and each mixed
 * count within 1..{@link MAX_ROOMS_REQUIRED}, at most {@link MAX_ROOM_TYPE_DEMANDS} types, no
 * type named twice.
 *
 * **Not** mirrored: whether any rooms are free. That is the question being asked, and the
 * only thing that can answer it is the server -- there is no client-side count, no date
 * arithmetic against bookings, and no inference from a room's status.
 *
 * The night count is computed from two `YYYY-MM-DD` strings purely to check the 366-night
 * bound before sending. It is a length, not an availability judgement.
 *
 * ## Codes are sent exactly as the catalogue gave them
 *
 * `room_type_code=dlx` is a **404** while `rooms=dlx:1` succeeds -- the mixed parser
 * upper-cases and the single filter does not. Since every code here comes from a `<select>`
 * populated by the catalogue, both forms send a value the server already knows.
 */
export function AvailabilityForm({
  types,
  today,
  onSearch,
  busy,
  onDirty,
}: AvailabilityFormProps) {
  const modeName = useId()
  const checkInId = useId()
  const checkOutId = useId()
  const typeId = useId()
  const guestsId = useId()
  const roomsRequiredId = useId()
  const errorId = useId()

  const [mode, setMode] = useState<Mode>('single')
  const [checkIn, setCheckIn] = useState(today)
  const [checkOut, setCheckOut] = useState(() => addDays(today, 1))
  const [roomTypeCode, setRoomTypeCode] = useState('')
  const [guests, setGuests] = useState('')
  const [roomsRequired, setRoomsRequired] = useState('1')
  const [demands, setDemands] = useState<DemandDraft[]>([{ code: '', count: '1' }])
  const [problem, setProblem] = useState<string | null>(null)

  const touched = () => {
    setProblem(null)
    onDirty?.()
  }

  const validate = (): string | null => {
    if (checkIn === '' || checkOut === '') {
      return 'Enter both a check-in and a check-out date.'
    }
    // `YYYY-MM-DD` sorts lexically, so this needs no date parsing.
    if (checkOut <= checkIn) {
      return 'Check-out must be later than check-in. The departure day itself is not a night.'
    }
    const nights = nightsBetween(checkIn, checkOut)
    if (nights > MAX_STAY_NIGHTS) {
      return `A search may span at most ${MAX_STAY_NIGHTS} nights; this one spans ${nights}.`
    }
    if (guests.trim() !== '' && !/^[1-9]\d{0,2}$/.test(guests.trim())) {
      return 'Guests must be a whole number of at least 1.'
    }

    if (mode === 'single') {
      const required = Number(roomsRequired)
      if (!/^\d+$/.test(roomsRequired) || required < 1 || required > MAX_ROOMS_REQUIRED) {
        return `Rooms required must be between 1 and ${MAX_ROOMS_REQUIRED}.`
      }
      return null
    }

    const filled = demands.filter((demand) => demand.code !== '')
    if (filled.length === 0) {
      return 'Name at least one room type and how many of it you need.'
    }
    if (filled.length > MAX_ROOM_TYPE_DEMANDS) {
      return `A search may name at most ${MAX_ROOM_TYPE_DEMANDS} room types.`
    }
    const seen = new Set<string>()
    for (const demand of filled) {
      if (seen.has(demand.code)) {
        return 'Each room type may be named only once.'
      }
      seen.add(demand.code)
      const count = Number(demand.count)
      if (!/^\d+$/.test(demand.count) || count < 1 || count > MAX_ROOMS_REQUIRED) {
        return `Each room count must be between 1 and ${MAX_ROOMS_REQUIRED}.`
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

    if (mode === 'mixed') {
      onSearch({
        kind: 'mixed',
        checkIn,
        checkOut,
        demands: demands
          .filter((demand) => demand.code !== '')
          .map((demand) => ({ code: demand.code, count: Number(demand.count) })),
      })
      return
    }

    onSearch({
      kind: 'single',
      checkIn,
      checkOut,
      ...(roomTypeCode !== '' ? { roomTypeCode } : {}),
      ...(guests.trim() !== '' ? { guests: Number(guests.trim()) } : {}),
      roomsRequired: Number(roomsRequired),
    })
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      {/*
        * Native radios, restyled -- the same choice the period selector makes: the arrow-key
        * contract, the group's name and "1 of 2" all come free with the real element.
        */}
      <fieldset className={styles.modes}>
        <legend className={styles.legend}>What are you looking for</legend>
        <div className={styles.modeOptions}>
          {(
            [
              ['single', 'Rooms of one type'],
              ['mixed', 'Several types at once'],
            ] as const
          ).map(([value, label]) => (
            <label key={value} className={styles.modeOption}>
              <input
                type="radio"
                className={styles.modeInput}
                name={modeName}
                value={value}
                checked={mode === value}
                disabled={busy}
                onChange={() => {
                  setMode(value)
                  touched()
                }}
              />
              <span className={styles.modeLabel}>{label}</span>
            </label>
          ))}
        </div>
        <p className={styles.modeNote}>
          The two cannot be combined &mdash; naming types and counts already says how many of
          each, so the server refuses a request that does both.
        </p>
      </fieldset>

      <div className={styles.grid}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={checkInId}>
            Check-in
          </label>
          <input
            id={checkInId}
            className={styles.input}
            type="date"
            value={checkIn}
            disabled={busy}
            required
            aria-describedby={problem ? errorId : undefined}
            onChange={(event) => {
              setCheckIn(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>The first night of the stay, included.</span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={checkOutId}>
            Check-out
          </label>
          <input
            id={checkOutId}
            className={styles.input}
            type="date"
            value={checkOut}
            disabled={busy}
            required
            aria-describedby={problem ? errorId : undefined}
            onChange={(event) => {
              setCheckOut(event.target.value)
              touched()
            }}
          />
          <span className={styles.hint}>
            The departure day, <strong>not</strong> a night. A stay ending on the 14th leaves
            the 14th free for someone else.
          </span>
        </div>

        {mode === 'single' ? (
          <>
            <div className={styles.field}>
              <label className={styles.label} htmlFor={typeId}>
                Room type
              </label>
              <select
                id={typeId}
                className={styles.select}
                value={roomTypeCode}
                disabled={busy}
                onChange={(event) => {
                  setRoomTypeCode(event.target.value)
                  touched()
                }}
              >
                <option value="">Any room type</option>
                {types.map((type) => (
                  <option key={type.code} value={type.code}>
                    {type.name} ({type.code})
                  </option>
                ))}
              </select>
            </div>

            <div className={styles.field}>
              <label className={styles.label} htmlFor={roomsRequiredId}>
                Rooms required
              </label>
              <input
                id={roomsRequiredId}
                className={`${styles.input} ${styles.short}`}
                type="number"
                inputMode="numeric"
                min={1}
                max={MAX_ROOMS_REQUIRED}
                step={1}
                value={roomsRequired}
                disabled={busy}
                onChange={(event) => {
                  setRoomsRequired(event.target.value)
                  touched()
                }}
              />
              <span className={styles.hint}>
                Rooms, not guests &mdash; and all of one type. Types that cannot supply this
                many for the whole stay are left out of the answer.
              </span>
            </div>

            <div className={styles.field}>
              <label className={styles.label} htmlFor={guestsId}>
                Guests per room (optional)
              </label>
              <input
                id={guestsId}
                className={`${styles.input} ${styles.short}`}
                type="number"
                inputMode="numeric"
                min={1}
                step={1}
                value={guests}
                disabled={busy}
                onChange={(event) => {
                  setGuests(event.target.value)
                  touched()
                }}
              />
              <span className={styles.hint}>
                Keeps only types that can seat this many. Left blank, occupancy is ignored.
              </span>
            </div>
          </>
        ) : (
          <div className={`${styles.field} ${styles.wide}`}>
            <span className={styles.label}>Room types and counts</span>
            <ul className={styles.demands}>
              {demands.map((demand, index) => (
                <li key={index} className={styles.demand}>
                  <label className={styles.demandLabel} htmlFor={`${typeId}-${index}`}>
                    Room type {index + 1}
                  </label>
                  <select
                    id={`${typeId}-${index}`}
                    className={styles.select}
                    value={demand.code}
                    disabled={busy}
                    onChange={(event) => {
                      setDemands((current) =>
                        current.map((entry, i) =>
                          i === index ? { ...entry, code: event.target.value } : entry,
                        ),
                      )
                      touched()
                    }}
                  >
                    <option value="">Choose a type</option>
                    {types.map((type) => (
                      <option key={type.code} value={type.code}>
                        {type.name} ({type.code})
                      </option>
                    ))}
                  </select>

                  <label className={styles.demandLabel} htmlFor={`${roomsRequiredId}-${index}`}>
                    How many
                  </label>
                  <input
                    id={`${roomsRequiredId}-${index}`}
                    className={`${styles.input} ${styles.short}`}
                    type="number"
                    inputMode="numeric"
                    min={1}
                    max={MAX_ROOMS_REQUIRED}
                    step={1}
                    value={demand.count}
                    disabled={busy}
                    onChange={(event) => {
                      setDemands((current) =>
                        current.map((entry, i) =>
                          i === index ? { ...entry, count: event.target.value } : entry,
                        ),
                      )
                      touched()
                    }}
                  />

                  {demands.length > 1 ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busy}
                      onClick={() => {
                        setDemands((current) => current.filter((_, i) => i !== index))
                        touched()
                      }}
                    >
                      <X size={15} aria-hidden="true" />
                      Remove room type {index + 1}
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>

            {demands.length < MAX_ROOM_TYPE_DEMANDS ? (
              <div>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={busy}
                  onClick={() => {
                    setDemands((current) => [...current, { code: '', count: '1' }])
                    touched()
                  }}
                >
                  <Plus size={15} aria-hidden="true" />
                  Add another room type
                </Button>
              </div>
            ) : null}

            <span className={styles.hint}>
              Each type is answered on its own, and the whole request is met only when every
              one of them can supply its share. At most {MAX_ROOM_TYPE_DEMANDS} types.
            </span>
          </div>
        )}
      </div>

      {problem ? (
        <p className={styles.error} id={errorId} role="alert">
          {problem}
        </p>
      ) : null}

      <div className={styles.actions}>
        <Button type="submit" variant="primary" size="sm" disabled={busy}>
          {busy ? 'Searching…' : 'Search availability'}
        </Button>
      </div>
    </form>
  )
}

/**
 * Nights between two `YYYY-MM-DD` dates, for the 366-night bound only.
 *
 * A length, not an availability judgement: it decides whether the request is worth sending,
 * never what the answer is. Both ends are parsed as UTC so the subtraction cannot be moved a
 * day by the browser's zone -- the same reasoning `formatDate` uses.
 */
function nightsBetween(from: string, to: string): number {
  const start = Date.parse(`${from}T00:00:00Z`)
  const end = Date.parse(`${to}T00:00:00Z`)
  if (Number.isNaN(start) || Number.isNaN(end)) {
    return 0
  }
  return Math.round((end - start) / 86_400_000)
}

/** The day after a `YYYY-MM-DD` date, for the default check-out. Computed in UTC. */
function addDays(date: string, days: number): string {
  const parsed = Date.parse(`${date}T00:00:00Z`)
  if (Number.isNaN(parsed)) {
    return date
  }
  return new Date(parsed + days * 86_400_000).toISOString().slice(0, 10)
}
