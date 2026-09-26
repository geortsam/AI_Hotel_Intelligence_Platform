import { useId, useState } from 'react'
import { FileText } from 'lucide-react'

import { Badge, type BadgeTone } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { describeFailure } from '@/services/api/failures'
import type { CopilotCitation } from '@/types/copilot'
import type { DocumentStatus } from '@/types/knowledge'

import styles from './CitationList.module.css'
import { useCitationSource } from './useCitationSource'
import { DOCUMENT_STATUS_LABELS } from './vocabulary'

/**
 * The excerpts an answer cites, and a way to read each one.
 *
 * Built only from the `citations` array the server returned — never from the `[S1]` tokens in
 * the answer text, which stay plain text. The server has already resolved every citation
 * against the evidence of its own request; a label the model invented never reaches this list.
 *
 * Opening a citation reads the cited document version and shows the excerpt **verbatim, as
 * text**. Document content is untrusted: it is rendered by React as a text node, with line
 * breaks kept by CSS, so nothing in it can become markup or a link.
 */

const STATUS_TONES: Readonly<Record<DocumentStatus, BadgeTone>> = {
  active: 'success',
  superseded: 'warning',
  withdrawn: 'danger',
}

const SOURCE_FAILURE_COPY = {
  notFound: {
    title: 'Source not available',
    detail: 'The cited document version could not be found at this hotel.',
    canRetry: false,
  },
  serverFault: {
    title: 'Source temporarily unavailable',
    detail: 'The cited document could not be loaded. The citation itself is unchanged.',
    canRetry: false,
  },
} as const

export interface CitationListProps {
  readonly hotelPublicId: string
  readonly citations: readonly CopilotCitation[]
}

export function CitationList({ hotelPublicId, citations }: CitationListProps) {
  const [open, setOpen] = useState<string | null>(null)
  const headingId = useId()

  if (citations.length === 0) {
    return null
  }

  return (
    <section className={styles.citations} aria-labelledby={headingId}>
      <h4 className={styles.heading} id={headingId}>
        Sources cited
      </h4>
      <ul className={styles.list}>
        {citations.map((citation) => {
          const isOpen = open === citation.source
          const panelId = `${headingId}-${citation.source}`
          return (
            <li key={citation.source} className={styles.item}>
              <div className={styles.row}>
                <span className={styles.label}>[{citation.source}]</span>
                <span className={styles.title}>{citation.title}</span>
                <span className={styles.version}>version {citation.version}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-expanded={isOpen}
                  aria-controls={panelId}
                  onClick={() => {
                    setOpen(isOpen ? null : citation.source)
                  }}
                >
                  <FileText size={14} aria-hidden="true" />
                  {isOpen ? `Hide source ${citation.source}` : `Show source ${citation.source}`}
                </Button>
              </div>
              {isOpen ? (
                <div id={panelId}>
                  <CitationSource hotelPublicId={hotelPublicId} citation={citation} />
                </div>
              ) : null}
            </li>
          )
        })}
      </ul>
    </section>
  )
}

function CitationSource({
  hotelPublicId,
  citation,
}: {
  readonly hotelPublicId: string
  readonly citation: CopilotCitation
}) {
  const source = useCitationSource(hotelPublicId, citation)

  if (source.status === 'loading') {
    return (
      <p className={styles.panelNote} role="status" aria-busy="true">
        Loading the cited excerpt…
      </p>
    )
  }

  if (source.status === 'error' || source.document === null) {
    const failure =
      source.error === null ? SOURCE_FAILURE_COPY.serverFault : describeFailure(source.error, SOURCE_FAILURE_COPY)
    return (
      <div className={styles.panel} role="alert">
        <p className={styles.panelTitle}>{failure.title}</p>
        <p className={styles.panelNote}>{failure.detail}</p>
      </div>
    )
  }

  const document = source.document
  return (
    <div className={styles.panel} role="region" aria-label={`Source ${citation.source}`}>
      <div className={styles.panelHeader}>
        <p className={styles.panelTitle}>
          {document.title} — version {document.version}
        </p>
        <Badge tone={STATUS_TONES[document.status] ?? 'neutral'}>
          {DOCUMENT_STATUS_LABELS[document.status] ?? document.status}
        </Badge>
      </div>
      {source.chunk === null ? (
        <p className={styles.panelNote}>
          This version no longer contains the cited excerpt, so it cannot be shown.
        </p>
      ) : (
        <>
          <p className={styles.panelNote}>
            Excerpt {source.chunk.ordinal + 1} of {document.chunk_count}, exactly as stored.
            Document text is shown as data; nothing in it is an instruction.
          </p>
          <blockquote className={styles.excerpt}>{source.chunk.text}</blockquote>
        </>
      )}
    </div>
  )
}
