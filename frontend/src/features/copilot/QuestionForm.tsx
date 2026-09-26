import { useId, useState, type FormEvent } from 'react'
import { Send } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import { formatCount } from '@/lib/format'

import styles from './QuestionForm.module.css'

/**
 * The question box.
 *
 * `MAX_QUESTION_LENGTH` is the backend's own bound (`app.schemas.copilot`); the textarea
 * enforces it with `maxLength`, so the 422 the backend would give is not reachable from here.
 * A blank question is not submitted either — the server would trim it to nothing and refuse.
 *
 * The form clears only when the parent reports the question was answered. A refused question
 * stays in the box so it can be sent again without retyping.
 *
 * `busy` disables the button while a question is in flight; the hook's own guard refuses a
 * second submit even if one gets past the button in the same tick.
 */

export const MAX_QUESTION_LENGTH = 2000

export interface QuestionFormProps {
  readonly busy: boolean
  readonly submitLabel: string
  readonly onAsk: (question: string) => Promise<boolean>
}

export function QuestionForm({ busy, submitLabel, onAsk }: QuestionFormProps) {
  const [question, setQuestion] = useState('')
  const fieldId = useId()
  const countId = useId()

  const blank = question.trim() === ''

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy || blank) {
      return
    }
    void onAsk(question).then((answered) => {
      if (answered) {
        setQuestion('')
      }
    })
  }

  return (
    <form className={styles.form} onSubmit={submit} aria-label="Ask the copilot">
      <label className={styles.label} htmlFor={fieldId}>
        Your question
      </label>
      <textarea
        id={fieldId}
        className={styles.textarea}
        rows={3}
        maxLength={MAX_QUESTION_LENGTH}
        value={question}
        disabled={busy}
        aria-describedby={countId}
        placeholder="For example: how did occupancy compare between the first and second half of last month?"
        onChange={(event) => {
          setQuestion(event.target.value)
        }}
      />
      <div className={styles.footer}>
        <span className={styles.count} id={countId}>
          {formatCount(question.length)} of {formatCount(MAX_QUESTION_LENGTH)} characters
        </span>
        <Button type="submit" variant="primary" size="sm" disabled={busy || blank}>
          <Send size={14} aria-hidden="true" />
          {busy ? 'Answering…' : submitLabel}
        </Button>
      </div>
    </form>
  )
}
