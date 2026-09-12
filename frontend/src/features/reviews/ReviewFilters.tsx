import { useId } from 'react'

import { Button } from '@/components/ui/Button'
import { REVIEW_SOURCES, sourceLabel } from '@/features/reviews/vocabulary'
import {
  hasActiveReviewFilters,
  NO_REVIEW_FILTERS,
  type ReviewFilters as FilterState,
} from '@/features/reviews/useReviews'
import type { ReviewSource } from '@/types/review'

import styles from './ReviewFilters.module.css'

export interface ReviewFiltersProps {
  readonly value: FilterState
  readonly onChange: (filters: FilterState) => void
  readonly disabled?: boolean
}

/**
 * The list's filters &mdash; and only the two the endpoint has.
 *
 * `GET /hotels/{h}/reviews` accepts `source`, `is_published`, `page` and `page_size`. There
 * is no search, no rating filter, no date range and no sort. And the backend **ignores an
 * unrecognised query parameter silently**: `?rating=5` returned all 55 rows with a 200,
 * which on screen is indistinguishable from a filter that matched everything. So a control
 * this component does not offer is a capability the API does not have, not an omission.
 *
 * Filtering client-side instead was the other option and is worse here than anywhere: the
 * list is eleven pages at the default size, and a rating filter applied to the twenty rows
 * on screen would answer "no reviews below three stars" while eight of them sat on page 4.
 *
 * Both controls apply immediately on change. Unlike the ledger's booking field there is
 * nothing to type -- each is a short closed list, and a selection is a complete intent -- so
 * an Apply button would be an extra click before every filter with nothing to gather.
 *
 * `source` is a closed vocabulary and an unknown value is a **422**, not an empty page. The
 * options are therefore built from the schema's own list rather than from whatever codes
 * happen to appear in the current page of data.
 */
export function ReviewFilters({ value, onChange, disabled = false }: ReviewFiltersProps) {
  const sourceId = useId()
  const publicationId = useId()

  return (
    <div className={styles.bar}>
      <div className={styles.controls}>
        <div className={styles.field}>
          {/*
            * "Filter by channel", not "Channel".
            *
            * The recording form below has a `Channel` select too, and while it is open both
            * are on the page at once. Two controls with the same accessible name are
            * indistinguishable to anyone navigating by label, and picking the wrong one here
            * means filtering the list when you meant to say where a review came from. The
            * same collision was found on the ledger screen in Stage 5.8.
            */}
          <label className={styles.label} htmlFor={sourceId}>
            Filter by channel
          </label>
          <select
            id={sourceId}
            className={styles.select}
            value={value.source}
            disabled={disabled}
            onChange={(event) => {
              onChange({ ...value, source: event.target.value as ReviewSource | '' })
            }}
          >
            <option value="">All channels</option>
            {REVIEW_SOURCES.map((source) => (
              <option key={source} value={source}>
                {sourceLabel(source)}
              </option>
            ))}
          </select>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={publicationId}>
            Filter by visibility
          </label>
          <select
            id={publicationId}
            className={styles.select}
            value={value.publication}
            disabled={disabled}
            onChange={(event) => {
              onChange({
                ...value,
                publication: event.target.value as FilterState['publication'],
              })
            }}
          >
            {/*
              * Three states, because `is_published` is a tri-state on the wire: omitted
              * means both, and the two booleans mean what they say. An omitted parameter is
              * not the same request as `is_published=false`.
              */}
            <option value="">Published and hidden</option>
            <option value="published">Published only</option>
            <option value="hidden">Hidden only</option>
          </select>
        </div>

        {hasActiveReviewFilters(value) ? (
          <div className={styles.actions}>
            <Button
              variant="ghost"
              size="sm"
              disabled={disabled}
              onClick={() => {
                onChange(NO_REVIEW_FILTERS)
              }}
            >
              Clear filters
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  )
}
