import { useEffect, useState } from 'react'

import { ApiError } from '@/services/api/ApiError'
import { knowledgeService } from '@/services/knowledge/knowledgeService'
import type { CopilotCitation } from '@/types/copilot'
import type { DocumentChunk, DocumentDetail } from '@/types/knowledge'

/**
 * The source text behind one citation, read when its panel is opened.
 *
 * The document is fetched by the `document_public_id` the SERVER put in the citation — the
 * copilot service resolved it against the request's own evidence ledger — and the excerpt is
 * found by the citation's `chunk_public_id`. Nothing is derived from the answer text.
 *
 * The version is read whatever its status. A citation can outlive the version it names: a
 * document may be superseded or withdrawn after the answer was given, and the endpoint keeps
 * every version addressable precisely so a past citation can still be explained. The panel
 * says which state the version is in.
 */

export type SourceStatus = 'loading' | 'ready' | 'error'

export interface CitationSourceState {
  readonly status: SourceStatus
  readonly document: DocumentDetail | null
  /** The cited excerpt, or null when the version no longer holds a chunk with that id. */
  readonly chunk: DocumentChunk | null
  readonly error: ApiError | null
}

function isDocument(value: unknown): value is DocumentDetail {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const body = value as { title?: unknown; version?: unknown; status?: unknown; chunks?: unknown }
  return (
    typeof body.title === 'string' &&
    typeof body.version === 'number' &&
    typeof body.status === 'string' &&
    Array.isArray(body.chunks)
  )
}

export function useCitationSource(
  hotelPublicId: string,
  citation: CopilotCitation,
): CitationSourceState {
  const [state, setState] = useState<CitationSourceState>({
    status: 'loading',
    document: null,
    chunk: null,
    error: null,
  })

  const documentPublicId = citation.document_public_id
  const chunkPublicId = citation.chunk_public_id

  useEffect(() => {
    const request = new AbortController()
    let cancelled = false
    setState({ status: 'loading', document: null, chunk: null, error: null })

    knowledgeService
      .getDocument(hotelPublicId, documentPublicId, request.signal)
      .then((document: unknown) => {
        if (cancelled) {
          return
        }
        if (!isDocument(document)) {
          setState({
            status: 'error',
            document: null,
            chunk: null,
            error: new ApiError(
              200,
              ApiError.MALFORMED_CODE,
              'The document response was not in the expected format.',
            ),
          })
          return
        }
        const chunk = document.chunks.find((candidate) => candidate.public_id === chunkPublicId)
        setState({ status: 'ready', document, chunk: chunk ?? null, error: null })
      })
      .catch((cause: unknown) => {
        if (cancelled || request.signal.aborted) {
          return
        }
        setState({
          status: 'error',
          document: null,
          chunk: null,
          error:
            cause instanceof ApiError
              ? cause
              : new ApiError(0, ApiError.NETWORK_CODE, 'The document could not be loaded.'),
        })
      })

    return () => {
      cancelled = true
      request.abort()
    }
  }, [hotelPublicId, documentPublicId, chunkPublicId])

  return state
}
