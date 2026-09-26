import { useId } from 'react'

import { Badge } from '@/components/ui/Badge'
import type { CopilotToolUse } from '@/types/copilot'

import styles from './ToolCallList.module.css'
import {
  LOOKUPS_NOT_RECORDED,
  NO_LOOKUPS,
  OUTCOME_LABELS,
  TOOL_LABELS,
  UNNAMED_TOOL_LABEL,
  outcomeSucceeded,
} from './vocabulary'

/**
 * The data lookups the model made for one answer — shown, never hidden.
 *
 * Exactly the `tools_used` array the server returned, in its order, failures included. Nothing
 * here lists the tools a caller *could* have used: the server decides that from the caller's
 * role before the model is asked, and the frontend neither knows the role nor guesses it. A
 * viewer therefore sees only what the server let the model use for them.
 *
 * `null` means "not recorded" — a turn read back from a stored conversation — and says so; it
 * is never rendered as "no lookups were made".
 */

export interface ToolCallListProps {
  readonly toolsUsed: readonly CopilotToolUse[] | null
}

export function ToolCallList({ toolsUsed }: ToolCallListProps) {
  const headingId = useId()

  return (
    <section className={styles.lookups} aria-labelledby={headingId}>
      <h4 className={styles.heading} id={headingId}>
        Data lookups
      </h4>
      {toolsUsed === null ? (
        <p className={styles.note}>{LOOKUPS_NOT_RECORDED}</p>
      ) : toolsUsed.length === 0 ? (
        <p className={styles.note}>{NO_LOOKUPS}</p>
      ) : (
        <ol className={styles.list}>
          {toolsUsed.map((call, index) => (
            // The call's position is its identity: the same tool may legitimately run twice.
            <li key={index} className={styles.item}>
              <ToolName name={call.tool} />
              <Badge tone={outcomeSucceeded(call.outcome) ? 'success' : 'warning'}>
                {OUTCOME_LABELS[call.outcome] ?? call.outcome}
              </Badge>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function ToolName({ name }: { readonly name: string | null }) {
  if (name === null) {
    return <span className={styles.name}>{UNNAMED_TOOL_LABEL}</span>
  }
  const label = TOOL_LABELS[name]
  if (label === undefined) {
    // A name this screen does not know yet: shown exactly as the server sent it.
    return <code className={styles.raw}>{name}</code>
  }
  return (
    <span className={styles.name}>
      {label} <code className={styles.raw}>{name}</code>
    </span>
  )
}
