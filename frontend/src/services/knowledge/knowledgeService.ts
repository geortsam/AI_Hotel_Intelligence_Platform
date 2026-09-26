import { api } from '@/services/api/client'
import type { DocumentDetail } from '@/types/knowledge'

/**
 * The one hotel-document read this application makes: the version a copilot citation names.
 *
 * `GET /hotels/{h}/documents/{d}` (Stage 7.9) returns one version and every chunk, whatever
 * its status — a superseded or withdrawn version stays addressable precisely so that a past
 * citation can still be explained. Any member may read it.
 *
 * The document identifier always comes from a citation the SERVER returned, never from text
 * the model wrote: an `[S1]` in an answer is only a label, and no URL is ever built from it.
 */
export const knowledgeService = {
  /** One document version with its chunks. 404 if it is not this hotel's. */
  getDocument(hotelPublicId: string, documentPublicId: string, signal?: AbortSignal) {
    return api.get<DocumentDetail>(
      `/hotels/${hotelPublicId}/documents/${encodeURIComponent(documentPublicId)}`,
      { ...(signal ? { signal } : {}) },
    )
  },
} as const
