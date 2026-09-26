import type { ReactNode } from 'react'
import { AlertTriangle, BotOff, Building2, Plus, WifiOff } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import { PageContainer } from '@/components/ui/PageContainer'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { ConversationList } from '@/features/copilot/ConversationList'
import { ExchangeView } from '@/features/copilot/ExchangeView'
import { ModeSelector } from '@/features/copilot/ModeSelector'
import { QuestionForm } from '@/features/copilot/QuestionForm'
import { useCopilot } from '@/features/copilot/useCopilot'
import {
  CONVERSATION_DISCLOSURE,
  describeCopilotFailure,
  ONE_OFF_DISCLOSURE,
  PROVIDER_DISCLOSURE,
} from '@/features/copilot/vocabulary'
import { StateMessage } from '@/features/dashboard/StateMessage'
import { formatCount, formatDateTime } from '@/lib/format'
import { useHotelContext } from '@/session/HotelProvider'

import styles from './CopilotPage.module.css'

/**
 * Copilot — questions about one property, answered from its own data (Stage 7.13).
 *
 * ## What this screen is a front end for
 *
 * The backend of Stages 7.7–7.12, unchanged: `POST …/copilot/ask` for a one-off question, the
 * five `…/copilot/conversations` operations for a stored conversation, and the document read
 * a citation opens. No endpoint was added for this screen.
 *
 * ## What it never does
 *
 * * **Decide authorization.** No role is known here and none is inferred. The server offers
 *   the model only the tools the caller's role permits, per request; the screen shows the
 *   lookups a response reports, and nothing else.
 * * **Render model or document text as markup.** Answers, questions and excerpts are text
 *   nodes. Nothing is parsed as Markdown, nothing becomes a link.
 * * **Say what a refusal meant in the server's words.** Every failure is fixed copy chosen by
 *   its code (`features/copilot/vocabulary.ts`).
 * * **Keep anything in the browser.** One-off answers live in memory for this visit; a
 *   conversation lives on the server, under its retention rule. Nothing is written to
 *   `localStorage` or `sessionStorage`.
 *
 * ## Disclosure comes before the question
 *
 * What leaves the process — the question and the lookup results, to an external provider —
 * is stated above the question box in both modes, and conversation mode adds what is stored
 * and for how long. The user reads it before they can ask, not after.
 */
export function CopilotPage() {
  const hotelContext = useHotelContext()
  const hotel = hotelContext.selected
  const hotelPublicId = hotel?.public_id ?? null
  const timeZone = hotel?.timezone ?? 'UTC'

  const copilot = useCopilot(hotelPublicId)

  if (hotelContext.status === 'empty') {
    return (
      <Frame>
        <StateMessage
          icon={Building2}
          tone="status"
          title="No hotel is linked to your account"
          detail="The copilot answers questions about one property's own data, and this account is not yet a member of one."
        />
      </Frame>
    )
  }

  if (hotelContext.status === 'error') {
    return (
      <Frame>
        <StateMessage
          icon={WifiOff}
          tone="alert"
          title="Could not load your hotels"
          detail="The list of properties you have access to could not be retrieved. Check your connection and try again."
          onRetry={hotelContext.retry}
        />
      </Frame>
    )
  }

  if (hotelPublicId === null) {
    return (
      <Frame>
        <p className={styles.note} role="status" aria-busy="true">
          Loading your hotels…
        </p>
      </Frame>
    )
  }

  const conversationMode = copilot.mode === 'conversation'
  const open = copilot.conversation
  const exchanges = conversationMode ? (open?.exchanges ?? []) : copilot.oneOff
  const busy = copilot.pending !== null
  const failure =
    copilot.failure === null
      ? null
      : describeCopilotFailure(copilot.failure.error, copilot.failure.context)
  const full = conversationMode && open !== null && open.turnsRemaining === 0

  return (
    <Frame>
      <ModeSelector mode={copilot.mode} disabled={busy} onChange={copilot.setMode} />

      {conversationMode ? (
        <section className={styles.block} aria-labelledby="copilot-conversations">
          <div className={styles.blockHeader}>
            <h2 className={styles.blockTitle} id="copilot-conversations">
              Your conversations
            </h2>
            <Button
              variant="secondary"
              size="sm"
              disabled={busy || open === null}
              onClick={copilot.newConversation}
            >
              <Plus size={14} aria-hidden="true" />
              New conversation
            </Button>
          </div>
          <ConversationList
            list={copilot.list}
            openPublicId={open?.publicId ?? null}
            busy={busy}
            timeZone={timeZone}
            onOpen={(publicId) => {
              void copilot.openConversation(publicId)
            }}
            onDelete={copilot.deleteConversation}
            onRetry={copilot.reloadList}
          />
        </section>
      ) : null}

      <section className={styles.block} aria-labelledby="copilot-thread">
        <h2 className={styles.blockTitle} id="copilot-thread">
          {conversationMode
            ? open === null
              ? 'New conversation'
              : 'Open conversation'
            : 'Questions this visit'}
        </h2>
        {conversationMode && open !== null ? (
          <p className={styles.note}>
            {formatCount(open.turnsRemaining)} of 20 turns left
            {open.expiresAt === null
              ? '.'
              : `. Deleted after ${formatDateTime(open.expiresAt, timeZone)} unless used again.`}
          </p>
        ) : null}

        {copilot.pending === 'opening' ? (
          <p className={styles.note} role="status" aria-busy="true">
            Opening the conversation…
          </p>
        ) : exchanges.length === 0 && copilot.pendingQuestion === null ? (
          <p className={styles.note} role="status">
            {conversationMode
              ? 'Ask a question to start a conversation, or reopen one of yours above.'
              : 'Ask a question about this hotel’s figures, forecast or documents.'}
          </p>
        ) : (
          <ol className={styles.thread}>
            {exchanges.map((exchange) => (
              <li key={exchange.key}>
                <ExchangeView hotelPublicId={hotelPublicId} exchange={exchange} />
              </li>
            ))}
            {copilot.pendingQuestion !== null ? (
              <li>
                <div className={styles.pending} role="status" aria-busy="true">
                  <p className={styles.pendingQuestion}>{copilot.pendingQuestion}</p>
                  <p className={styles.note}>
                    Answering… this can take a minute when the assistant needs several lookups.
                  </p>
                </div>
              </li>
            ) : null}
          </ol>
        )}
      </section>

      {failure !== null ? (
        <div className={styles.failure} role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <div>
            <p className={styles.failureTitle}>{failure.title}</p>
            <p className={styles.failureDetail}>{failure.detail}</p>
          </div>
        </div>
      ) : null}

      <section className={styles.block} aria-labelledby="copilot-ask">
        <h2 className={styles.blockTitle} id="copilot-ask">
          Ask
        </h2>
        {copilot.disabled ? (
          <StateMessage
            icon={BotOff}
            tone="status"
            title="Questions are not available on this visit"
            detail="This deployment has no language model enabled. Your stored conversations can still be read and deleted."
          />
        ) : (
          <>
            <div className={styles.disclosure} role="note" aria-label="What happens to your question">
              <p>{PROVIDER_DISCLOSURE}</p>
              <p>{conversationMode ? CONVERSATION_DISCLOSURE : ONE_OFF_DISCLOSURE}</p>
            </div>
            {full ? (
              <p className={styles.note} role="status">
                This conversation holds its maximum of 20 turns. Start a new conversation to keep
                asking.
              </p>
            ) : (
              <QuestionForm
                busy={busy}
                submitLabel={
                  conversationMode
                    ? open === null
                      ? 'Start conversation'
                      : 'Ask in this conversation'
                    : 'Ask'
                }
                onAsk={copilot.ask}
              />
            )}
          </>
        )}
      </section>
    </Frame>
  )
}

/** The page heading, shared by every state so the frame never jumps. */
function Frame({ children }: { children: ReactNode }) {
  return (
    <PageContainer>
      <SectionHeader
        as="h1"
        title="Copilot"
        description="Ask about this property in plain language. A language model answers using read-only lookups of this hotel’s own figures, forecast and documents; every answer is labelled as generated and shows the lookups it used."
      />
      <div className={styles.page}>{children}</div>
    </PageContainer>
  )
}
