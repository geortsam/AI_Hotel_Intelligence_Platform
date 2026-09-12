import { useEffect, useId, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/Button'
import { NO_FILTERS, hasActiveFilters, type LedgerFilters as FilterState } from '@/features/finance/useLedger'

import styles from './LedgerFilters.module.css'

export interface LedgerFiltersProps {
  readonly kind: 'revenue' | 'expenses'
  /** The active catalogue, for the category control. Codes are the server's. */
  readonly categories: readonly { readonly code: string; readonly name: string }[]
  /** Whether the catalogue could not be read, so no category can be offered. */
  readonly categoriesUnavailable?: boolean
  readonly value: FilterState
  readonly onChange: (filters: FilterState) => void
  readonly disabled?: boolean
}

/**
 * The journal's filters &mdash; and only the ones the endpoint actually has.
 *
 * ## Why there is no free-text search and no currency filter
 *
 * The list endpoints accept `category_code`, `date_from`, `date_to`, `page`, `page_size`, and
 * -- on revenue only -- `booking_public_id`. Nothing else. And the backend **ignores an
 * unrecognised query parameter silently**: `?currency=USD` returned all 124 rows with a 200,
 * which on screen is indistinguishable from a filter that matched everything.
 *
 * That is the reason this component is small. A search box that filtered only the twenty rows
 * already fetched would be worse here than on the bookings list, because a journal is read to
 * answer "did we post this or not", and a false "no lines" is an answer someone would act on.
 * The date range is set by the period control above, which drives the same request.
 *
 * ## The booking filter is a pasted identifier, deliberately
 *
 * There is no endpoint that lists the bookings with revenue against them, and building a
 * dropdown would mean fetching every booking in the property to populate it -- dozens of
 * requests to fill a control, growing with the hotel. So the field takes the public id a
 * person already has in front of them, from the booking page they came from.
 *
 * An unknown id is a **404, not an empty list**, and that distinction is preserved all the
 * way to the screen: the page says the booking was not found rather than that it has no
 * revenue, because those are different facts and only one of them is about money.
 *
 * ## Submitted, not applied per keystroke
 *
 * A `<form>` with an explicit apply. Filtering on every keystroke would fire a request per
 * character of a 36-character UUID, and the intermediate ones are all 404s.
 */
export function LedgerFilters({
  kind,
  categories,
  categoriesUnavailable = false,
  value,
  onChange,
  disabled = false,
}: LedgerFiltersProps) {
  const categoryId = useId()
  const bookingId = useId()

  // Local draft, so typing does not refetch. Resynchronised when the parent resets the
  // filters -- which it does on a hotel change, and the draft must not survive that.
  const [draft, setDraft] = useState<FilterState>(value)
  useEffect(() => {
    setDraft(value)
  }, [value])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    onChange(draft)
  }

  return (
    <form className={styles.form} onSubmit={submit}>
      <div className={styles.controls}>
        <div className={styles.field}>
          {/*
            * "Filter by category", not "Category".
            *
            * The posting form below has a `Category` select too, and while the form is open
            * both are on the page at once. Two controls with the same accessible name are
            * indistinguishable to anyone navigating by label -- and choosing the wrong one
            * here means filtering the journal when you meant to categorise a line you are
            * about to post. Found by a test that could not tell them apart either.
            */}
          <label className={styles.label} htmlFor={categoryId}>
            Filter by category
          </label>
          <select
            id={categoryId}
            className={styles.select}
            value={draft.categoryCode}
            disabled={disabled}
            onChange={(event) => {
              setDraft({ ...draft, categoryCode: event.target.value })
            }}
          >
            <option value="">All categories</option>
            {categories.map((category) => (
              <option key={category.code} value={category.code}>
                {category.name} ({category.code})
              </option>
            ))}
          </select>
          {categoriesUnavailable ? (
            <span className={styles.hint}>
              The category list could not be read, so only a date range narrows the journal.
              Each line still shows the category the server recorded.
            </span>
          ) : null}
        </div>

        {kind === 'revenue' ? (
          <div className={styles.field}>
            <label className={styles.label} htmlFor={bookingId}>
              Filter by booking
            </label>
            <input
              id={bookingId}
              className={styles.input}
              type="text"
              inputMode="text"
              autoComplete="off"
              spellCheck={false}
              placeholder="Booking identifier"
              value={draft.bookingPublicId}
              disabled={disabled}
              onChange={(event) => {
                setDraft({ ...draft, bookingPublicId: event.target.value })
              }}
            />
            <span className={styles.hint}>
              The identifier from a booking&rsquo;s own page. Expenses carry no booking link,
              so this filter exists only here.
            </span>
          </div>
        ) : null}

        <div className={styles.actions}>
          <Button type="submit" variant="secondary" size="sm" disabled={disabled}>
            Apply filters
          </Button>
          {hasActiveFilters(value) ? (
            <Button
              variant="ghost"
              size="sm"
              disabled={disabled}
              onClick={() => {
                setDraft(NO_FILTERS)
                onChange(NO_FILTERS)
              }}
            >
              Clear
            </Button>
          ) : null}
        </div>
      </div>
    </form>
  )
}
