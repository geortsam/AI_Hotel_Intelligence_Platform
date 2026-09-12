import { useId } from 'react'
import { Search, X } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import {
  EMPTY_FILTERS,
  hasActiveFilters,
  type BookingFilterState,
  type StatusFilter,
} from '@/features/bookings/filters'
import { BOOKING_STATUSES, statusPresentation } from '@/features/bookings/vocabulary'
import { formatCount } from '@/lib/format'

import styles from './BookingFilters.module.css'

export interface BookingFiltersProps {
  readonly value: BookingFilterState
  readonly onChange: (next: BookingFilterState) => void
  /** How many rows the page holds, before filtering. */
  readonly pageCount: number
  /** How many survive the filters. */
  readonly matchCount: number
  /** Total across every page, from the backend. Used to state the filter's scope honestly. */
  readonly totalCount: number
  readonly disabled?: boolean
}

/**
 * The filter bar, and an explicit statement of what it can and cannot reach.
 *
 * **The scope line is not decoration.** These filters narrow the rows already fetched,
 * because the API offers no server-side filtering at all -- `page` and `page_size` are its
 * only query parameters. An operator who types a reference held on another page and is told
 * "no bookings" has been misled by the interface, so the bar says which bookings it is
 * looking at, every time, in the same place as the controls themselves.
 *
 * `role="search"` gives the region a landmark, and the live count means a screen-reader user
 * learns the result without hunting for the table.
 */
export function BookingFilters({
  value,
  onChange,
  pageCount,
  matchCount,
  totalCount,
  disabled = false,
}: BookingFiltersProps) {
  const searchId = useId()
  const statusId = useId()
  const active = hasActiveFilters(value)

  return (
    // `role="search"` rather than the <search> element: React 18's JSX typings do not
    // declare it, and the role is what actually creates the landmark.
    <div role="search" className={styles.bar} aria-label="Filter bookings on this page">
      <div className={styles.controls}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={searchId}>
            Search this page
          </label>
          <div className={styles.searchWrap}>
            <Search className={styles.searchIcon} size={15} aria-hidden="true" />
            <input
              id={searchId}
              className={styles.input}
              type="search"
              inputMode="search"
              placeholder="Reference, room or occupant"
              value={value.search}
              disabled={disabled}
              onChange={(event) => {
                onChange({ ...value, search: event.target.value })
              }}
            />
          </div>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={statusId}>
            Status
          </label>
          <select
            id={statusId}
            className={styles.select}
            value={value.status}
            disabled={disabled}
            onChange={(event) => {
              onChange({ ...value, status: event.target.value as StatusFilter })
            }}
          >
            <option value="all">All statuses</option>
            {BOOKING_STATUSES.map((status) => (
              <option key={status} value={status}>
                {statusPresentation(status).label}
              </option>
            ))}
          </select>
        </div>

        {active ? (
          <Button
            className={styles.clear}
            variant="ghost"
            size="sm"
            disabled={disabled}
            onClick={() => {
              onChange(EMPTY_FILTERS)
            }}
          >
            <X size={14} aria-hidden="true" />
            Clear
          </Button>
        ) : null}
      </div>

      {/*
       * The scope statement. `role="status"` so a change in the count is announced rather
       * than only shown, and `aria-live="polite"` so it waits for a pause instead of
       * interrupting someone mid-word while they type.
       */}
      <p className={styles.scope} role="status" aria-live="polite">
        {active
          ? `${formatCount(matchCount)} of ${formatCount(pageCount)} on this page match.`
          : `Showing ${formatCount(pageCount)} of ${formatCount(totalCount)} bookings.`}{' '}
        <span className={styles.scopeNote}>
          Filters apply to this page only &mdash; the API provides no server-side search.
        </span>
      </p>
    </div>
  )
}
