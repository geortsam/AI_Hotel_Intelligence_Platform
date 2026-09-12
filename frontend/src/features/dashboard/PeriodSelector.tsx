import { useId } from 'react'

import { PERIOD_OPTIONS, type PeriodId } from '@/features/dashboard/period'

import styles from './PeriodSelector.module.css'

export interface PeriodSelectorProps {
  readonly value: PeriodId
  readonly onChange: (period: PeriodId) => void
  /** Disables the control while a request is in flight. */
  readonly disabled?: boolean
}

/**
 * The reporting period, as a segmented control.
 *
 * **Native radio inputs**, visually restyled -- not buttons with `aria-pressed`, and not a
 * listbox. The choice is genuinely "one of these", which is what a radio group means, and
 * using the real element buys the entire keyboard contract for free: arrow keys move within
 * the group, Tab moves past it as a single stop, and the group's name and the selected
 * option are announced without a line of ARIA. A hand-built version of that is code to get
 * wrong, and the usual first bug is that arrow keys do nothing.
 *
 * The `<fieldset>`/`<legend>` pairing is what names the group. The legend is visually
 * hidden rather than removed: a sighted user reads the periods and infers what they are for,
 * while a screen-reader user hears "Reporting period" before the options instead of four
 * unexplained dates.
 *
 * Every option here is a whole number of days ending on the hotel's own today, because that
 * is what the backend's `date_from`/`date_to` can express exactly. See `period.ts`.
 */
export function PeriodSelector({ value, onChange, disabled = false }: PeriodSelectorProps) {
  const name = useId()

  return (
    <fieldset className={styles.group} disabled={disabled}>
      <legend className={styles.legend}>Reporting period</legend>
      <div className={styles.options}>
        {PERIOD_OPTIONS.map((option) => (
          <label key={option.id} className={styles.option}>
            <input
              type="radio"
              className={styles.input}
              name={name}
              value={option.id}
              checked={value === option.id}
              onChange={() => {
                onChange(option.id)
              }}
            />
            <span className={styles.optionLabel}>{option.label}</span>
          </label>
        ))}
      </div>
    </fieldset>
  )
}
