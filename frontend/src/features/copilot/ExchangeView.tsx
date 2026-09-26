import { Badge, type BadgeTone } from '@/components/ui/Badge'
import { Card } from '@/components/ui/Card'

import { CitationList } from './CitationList'
import type { Exchange } from './exchange'
import styles from './ExchangeView.module.css'
import { ToolCallList } from './ToolCallList'
import {
  EVIDENCE_EXPLANATIONS,
  GENERATED_LABEL,
  GENERATED_NOTE,
  STORED_STOP_REASONS,
  WITHHELD_ANSWER,
} from './vocabulary'

/**
 * One question and its answer.
 *
 * ## The answer is text, and only text
 *
 * The answer is model output. It is rendered as a single React text node inside an element
 * whose CSS keeps line breaks — no Markdown, no HTML, no auto-linking. Markdown syntax, an
 * `<img>` tag or a URL in an answer reach the screen as the characters they are. The same
 * holds for the question, which the user typed. `[S1]`-style labels stay plain text too; the
 * citations below come from the server's validated list, not from parsing this text.
 *
 * ## Every answer says what it is
 *
 * Every answer carries the "Generated" label and the sentence saying what was checked,
 * whatever its state — complete, partial, withheld or replaced — and whether it was answered
 * now or read back from a stored conversation.
 */

interface StatusBadge {
  readonly tone: BadgeTone
  readonly label: string
}

function statusOf(exchange: Exchange): StatusBadge {
  if (exchange.stopReason === 'ungrounded_figures') {
    return { tone: 'danger', label: 'Withheld' }
  }
  if (exchange.documentEvidence === 'citation_rejected') {
    return { tone: 'warning', label: 'Replaced: invalid citation' }
  }
  if (exchange.documentEvidence === 'not_found') {
    return { tone: 'warning', label: 'Replaced: nothing cited' }
  }
  if (!exchange.complete) {
    return { tone: 'warning', label: 'Incomplete' }
  }
  return { tone: 'success', label: 'Complete' }
}

/** The notice for a partial answer: the server's own when live, fixed copy when stored. */
function noticeOf(exchange: Exchange): string | null {
  if (exchange.recorded === 'live') {
    return exchange.notice
  }
  if (exchange.stopReason === 'completed') {
    return null
  }
  return STORED_STOP_REASONS[exchange.stopReason] ?? null
}

export interface ExchangeViewProps {
  readonly hotelPublicId: string
  readonly exchange: Exchange
}

export function ExchangeView({ hotelPublicId, exchange }: ExchangeViewProps) {
  const status = statusOf(exchange)
  const notice = noticeOf(exchange)
  const evidence = EVIDENCE_EXPLANATIONS[exchange.documentEvidence] ?? null

  return (
    <article className={styles.exchange} aria-label={exchange.turn === null ? 'Question and answer' : `Turn ${exchange.turn}`}>
      <div className={styles.question}>
        <p className={styles.questionLabel}>
          {exchange.turn === null ? 'You asked' : `Turn ${exchange.turn} — you asked`}
        </p>
        <p className={styles.questionText}>{exchange.question}</p>
      </div>

      <Card className={styles.answerCard}>
        <div className={styles.badges}>
          <Badge tone="info">{GENERATED_LABEL}</Badge>
          <Badge tone={status.tone} withDot>
            {status.label}
          </Badge>
          {exchange.recorded === 'stored' ? <Badge>Stored turn</Badge> : null}
        </div>
        <p className={styles.generatedNote}>{GENERATED_NOTE}</p>

        {exchange.answer === '' ? (
          <p className={styles.withheld}>{WITHHELD_ANSWER}</p>
        ) : (
          <p className={styles.answer} data-testid="copilot-answer">
            {exchange.answer}
          </p>
        )}

        {notice !== null ? (
          <p className={styles.notice} role="note">
            {notice}
          </p>
        ) : null}
        {evidence !== null ? (
          <p className={styles.notice} role="note">
            {evidence}
          </p>
        ) : null}

        <ToolCallList toolsUsed={exchange.toolsUsed} />
        <CitationList hotelPublicId={hotelPublicId} citations={exchange.citations} />

        {exchange.invocationPublicId !== null ? (
          <p className={styles.reference}>
            Reference for support requests: <code>{exchange.invocationPublicId}</code>
          </p>
        ) : null}
      </Card>
    </article>
  )
}
