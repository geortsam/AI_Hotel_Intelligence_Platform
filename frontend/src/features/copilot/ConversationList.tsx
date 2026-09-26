import { useState } from 'react'
import { MessagesSquare, Trash2 } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import { formatCount, formatDateTime } from '@/lib/format'
import type { ConversationSummary } from '@/types/copilot'

import styles from './ConversationList.module.css'
import type { ConversationListState } from './useCopilot'

/**
 * The caller's own conversations at this hotel, most recently used first.
 *
 * The server lists only the caller's conversations; nobody else's — manager and owner
 * included — can appear here, because the query filters by owner in SQL. Each row shows when
 * the conversation will be deleted unless used again, which is the retention rule made
 * visible per conversation rather than stated once in a help page.
 *
 * Deleting takes two clicks: the first asks, the second deletes. It is physical and cannot be
 * undone, so the confirmation says so.
 */

export interface ConversationListProps {
  readonly list: ConversationListState
  readonly openPublicId: string | null
  readonly busy: boolean
  readonly timeZone: string
  readonly onOpen: (publicId: string) => void
  readonly onDelete: (publicId: string) => Promise<boolean>
  readonly onRetry: () => void
}

export function ConversationList({
  list,
  openPublicId,
  busy,
  timeZone,
  onOpen,
  onDelete,
  onRetry,
}: ConversationListProps) {
  const [confirming, setConfirming] = useState<string | null>(null)

  if (list.status === 'error') {
    return (
      <div className={styles.state} role="alert">
        <p className={styles.stateTitle}>Your conversations could not be loaded</p>
        <Button variant="secondary" size="sm" onClick={onRetry}>
          Try again
        </Button>
      </div>
    )
  }

  if (list.status !== 'ready') {
    return (
      <p className={styles.note} role="status" aria-busy="true">
        Loading your conversations…
      </p>
    )
  }

  if (list.items.length === 0) {
    return (
      <p className={styles.note} role="status">
        You have no stored conversations at this hotel.
      </p>
    )
  }

  return (
    <div className={styles.wrapper}>
      <p className={styles.note}>
        {list.total > list.items.length
          ? `The ${formatCount(list.items.length)} most recently used of your ${formatCount(list.total)} conversations.`
          : `${formatCount(list.total)} ${list.total === 1 ? 'conversation' : 'conversations'}, most recently used first.`}
      </p>
      <ul className={styles.list} aria-label="Your conversations">
        {list.items.map((item) => (
          <ConversationRow
            key={item.public_id}
            item={item}
            isOpen={item.public_id === openPublicId}
            confirming={confirming === item.public_id}
            busy={busy}
            timeZone={timeZone}
            onOpen={() => {
              setConfirming(null)
              onOpen(item.public_id)
            }}
            onAskDelete={() => {
              setConfirming(item.public_id)
            }}
            onCancelDelete={() => {
              setConfirming(null)
            }}
            onConfirmDelete={() => {
              void onDelete(item.public_id).then(() => {
                setConfirming(null)
              })
            }}
          />
        ))}
      </ul>
    </div>
  )
}

interface ConversationRowProps {
  readonly item: ConversationSummary
  readonly isOpen: boolean
  readonly confirming: boolean
  readonly busy: boolean
  readonly timeZone: string
  readonly onOpen: () => void
  readonly onAskDelete: () => void
  readonly onCancelDelete: () => void
  readonly onConfirmDelete: () => void
}

function ConversationRow({
  item,
  isOpen,
  confirming,
  busy,
  timeZone,
  onOpen,
  onAskDelete,
  onCancelDelete,
  onConfirmDelete,
}: ConversationRowProps) {
  return (
    <li className={`${styles.item} ${isOpen ? styles.open : ''}`}>
      <div className={styles.summary}>
        <p className={styles.preview}>{item.first_question_preview}</p>
        <p className={styles.meta}>
          {formatCount(item.turn_count)} {item.turn_count === 1 ? 'turn' : 'turns'} · last used{' '}
          {formatDateTime(item.last_activity_at, timeZone)} · deleted after{' '}
          {formatDateTime(item.expires_at, timeZone)} unless used again
        </p>
      </div>
      {confirming ? (
        <div className={styles.actions} role="group" aria-label="Confirm deletion">
          <span className={styles.confirmText}>
            Delete this conversation and all {formatCount(item.turn_count)}{' '}
            {item.turn_count === 1 ? 'turn' : 'turns'}? This cannot be undone.
          </span>
          <Button variant="danger" size="sm" disabled={busy} onClick={onConfirmDelete}>
            Delete permanently
          </Button>
          <Button variant="ghost" size="sm" disabled={busy} onClick={onCancelDelete}>
            Keep it
          </Button>
        </div>
      ) : (
        <div className={styles.actions}>
          <Button variant="secondary" size="sm" disabled={busy || isOpen} onClick={onOpen}>
            <MessagesSquare size={14} aria-hidden="true" />
            {isOpen ? 'Showing below' : 'Reopen'}
          </Button>
          <Button variant="ghost" size="sm" disabled={busy} onClick={onAskDelete}>
            <Trash2 size={14} aria-hidden="true" />
            Delete
          </Button>
        </div>
      )}
    </li>
  )
}
