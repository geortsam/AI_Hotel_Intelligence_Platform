import styles from './ModeSelector.module.css'
import type { CopilotMode } from './useCopilot'

/**
 * One-off or conversation — the choice that decides whether anything is stored.
 *
 * One-off is the default, so nothing is kept unless the user chooses to keep it. Each option
 * says what it stores in its own label, rather than leaving that to a help page.
 */

export interface ModeSelectorProps {
  readonly mode: CopilotMode
  readonly disabled: boolean
  readonly onChange: (mode: CopilotMode) => void
}

const OPTIONS: readonly { readonly mode: CopilotMode; readonly label: string; readonly hint: string }[] = [
  {
    mode: 'one-off',
    label: 'One-off question',
    hint: 'Not stored. Each question is answered on its own.',
  },
  {
    mode: 'conversation',
    label: 'Conversation',
    hint: 'Stored for 30 days after last use, and deletable. Earlier turns are shown to the assistant as context.',
  },
]

export function ModeSelector({ mode, disabled, onChange }: ModeSelectorProps) {
  return (
    <fieldset className={styles.fieldset} disabled={disabled}>
      <legend className={styles.legend}>How to ask</legend>
      <div className={styles.options}>
        {OPTIONS.map((option) => (
          <label
            key={option.mode}
            className={`${styles.option} ${mode === option.mode ? styles.selected : ''}`}
          >
            <input
              type="radio"
              name="copilot-mode"
              value={option.mode}
              checked={mode === option.mode}
              onChange={() => {
                onChange(option.mode)
              }}
            />
            <span>
              <span className={styles.optionLabel}>{option.label}</span>
              <span className={styles.hint}>{option.hint}</span>
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  )
}
