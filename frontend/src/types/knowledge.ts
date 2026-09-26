/**
 * One hotel document version, as `GET /hotels/{h}/documents/{d}` returns it
 * (`app.schemas.knowledge.DocumentDetail`, Stage 7.9).
 *
 * Only the read the copilot's citations need is transcribed: this application offers no
 * document upload, versioning or withdrawal screen (Stage 7.13 left those out of scope).
 */

export type DocumentStatus = 'active' | 'superseded' | 'withdrawn'

export interface DocumentChunk {
  readonly public_id: string
  readonly ordinal: number
  /** Verbatim document text. Untrusted: rendered as text, never as markup. */
  readonly text: string
  readonly token_count: number
}

export interface DocumentDetail {
  readonly public_id: string
  readonly title: string
  readonly source: string | null
  readonly language: string
  readonly version: number
  readonly status: DocumentStatus
  readonly content_checksum: string
  readonly supersedes_public_id: string | null
  readonly chunk_count: number
  readonly created_at: string
  readonly chunks: readonly DocumentChunk[]
}
