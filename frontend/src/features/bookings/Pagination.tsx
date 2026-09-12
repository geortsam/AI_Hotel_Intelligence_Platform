import { useId } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import { formatCount } from '@/lib/format'

import styles from './Pagination.module.css'

export interface PaginationProps {
  /** 1-based, as the backend counts. */
  readonly page: number
  readonly pageSize: number
  /** Rows across every page, from the backend's own `total`. */
  readonly total: number
  /** Pages, from the backend's own `pages`. Never derived here. */
  readonly pages: number
  readonly onPageChange: (page: number) => void
  readonly onPageSizeChange: (pageSize: number) => void
  /** Page sizes offered. Every one must be within the API's 1..100. */
  readonly pageSizeOptions: readonly number[]
  readonly disabled?: boolean
  /**
   * What is being paged through.
   *
   * Defaults to bookings, which is what this control was written for. The financial journals
   * reuse it and would otherwise announce "Bookings pagination" over a page of revenue lines
   * -- a landmark name that names the wrong thing is worse than a generic one, because a
   * screen-reader user navigating by landmark is told they are somewhere they are not.
   */
  readonly itemNoun?: { readonly singular: string; readonly plural: string }
}

/**
 * Offset pagination, exactly as the API defines it.
 *
 * ## Nothing here is invented
 *
 * `total` and `pages` come from the response envelope; neither is computed from the other,
 * and no count is estimated. The backend builds `pages` as
 * `ceil(total / page_size)` and returns **0** when there are no rows at all -- so "page 1 of
 * 0" is a real state and the control says "No pages" rather than pretending there is one.
 *
 * There is no cursor contract to honour: this API pages by offset, and `page=999` returns a
 * 200 with an empty `items` array rather than an error. That is why "next" is bounded by
 * `pages` -- walking past the end is not an error, it is just an empty screen, and a control
 * that allows it is a control that wastes a request to show nothing.
 *
 * ## Why the page size matters more than usual here
 *
 * With no server-side filtering, page size is the only lever an operator has on how much
 * they can look through at once. The options stop at 100 because the router's own
 * `le=100` makes anything larger a 422.
 *
 * Prev/next are real `<button>`s and are `disabled` at the ends rather than hidden, so the
 * control does not change shape as you page and a keyboard user's tab order stays put.
 */
export function Pagination({
  page,
  pageSize,
  total,
  pages,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions,
  disabled = false,
  itemNoun = { singular: 'booking', plural: 'bookings' },
}: PaginationProps) {
  const sizeId = useId()

  const first = total === 0 ? 0 : (page - 1) * pageSize + 1
  // Bounded by `total`, so the last page reads "161–166 of 166" rather than overshooting.
  const last = Math.min(page * pageSize, total)

  return (
    <nav className={styles.bar} aria-label={`${itemNoun.plural} pagination`}>
      <p className={styles.summary} role="status" aria-live="polite">
        {total === 0
          ? `No ${itemNoun.plural}`
          : `${formatCount(first)}–${formatCount(last)} of ${formatCount(total)}`}
        <span className={styles.pageOf}>
          {pages === 0 ? 'No pages' : `Page ${formatCount(page)} of ${formatCount(pages)}`}
        </span>
      </p>

      <div className={styles.controls}>
        <div className={styles.sizeField}>
          <label className={styles.sizeLabel} htmlFor={sizeId}>
            Per page
          </label>
          <select
            id={sizeId}
            className={styles.size}
            value={pageSize}
            disabled={disabled}
            onChange={(event) => {
              onPageSizeChange(Number(event.target.value))
            }}
          >
            {pageSizeOptions.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </div>

        <Button
          variant="secondary"
          size="sm"
          disabled={disabled || page <= 1}
          onClick={() => {
            onPageChange(page - 1)
          }}
        >
          <ChevronLeft size={15} aria-hidden="true" />
          Previous
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={disabled || pages === 0 || page >= pages}
          onClick={() => {
            onPageChange(page + 1)
          }}
        >
          Next
          <ChevronRight size={15} aria-hidden="true" />
        </Button>
      </div>
    </nav>
  )
}
