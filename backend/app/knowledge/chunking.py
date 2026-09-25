"""How a document's text becomes its checksum and its citable chunks (Stage 7.9). Pure.

No database, no session, no hotel: text in, text out. Kept apart from `app.services.knowledge` for
the same reason `app.copilot.grounding` is kept apart from the copilot service -- a service
orchestrates, and text processing is not orchestration.

## The rule, deterministically

1. **Normalise:** CRLF to LF, trailing whitespace trimmed from each line, the whole trimmed.
2. **Checksum:** SHA-256 of the normalised text.
3. **Split** at blank lines into paragraphs, and each paragraph into runs of at most
   `MAX_CHUNK_WORDS` whitespace-separated words. A chunk's text is its words joined by single
   spaces.
4. **Ordinals** from 0. `token_count` is the **word count** -- no tokenizer is a dependency.

The same content always produces the same chunks, in the same order, with the same ordinals.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.core.errors import ValidationError
from app.models.knowledge import MAX_CHUNK_LENGTH

#: The most words in one chunk. A paragraph longer than this is split into consecutive runs.
MAX_CHUNK_WORDS = 200
#: The most chunks one version may have: bounds what one upload can store.
MAX_CHUNKS = 1000


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    """A chunk before it has a row: position, text and word count."""

    ordinal: int
    text: str
    token_count: int


def normalise(content: str) -> str:
    """The canonical form the checksum and the chunks are computed from."""
    return "\n".join(line.rstrip() for line in content.replace("\r\n", "\n").split("\n")).strip()


def checksum(content: str) -> str:
    """SHA-256 of the normalised content: equal for two uploads that differ only in layout."""
    return hashlib.sha256(normalise(content).encode("utf-8")).hexdigest()


def chunk_text(content: str) -> list[ChunkDraft]:
    """Split normalised content into ordered chunks.

    Raises `ValidationError` for content that cannot be chunked within the schema's bounds: no
    text at all, a single "word" longer than a chunk may be, or more chunks than one version may
    have.
    """
    drafts: list[ChunkDraft] = []
    for paragraph in normalise(content).split("\n\n"):
        words = paragraph.split()
        for start in range(0, len(words), MAX_CHUNK_WORDS):
            run = words[start : start + MAX_CHUNK_WORDS]
            text = " ".join(run)
            if len(text) > MAX_CHUNK_LENGTH:
                raise ValidationError(
                    "The content contains an unbroken run of text too long to index; "
                    "separate it with spaces or line breaks."
                )
            drafts.append(ChunkDraft(ordinal=len(drafts), text=text, token_count=len(run)))
    if not drafts:
        raise ValidationError("The content contains no text.")
    if len(drafts) > MAX_CHUNKS:
        raise ValidationError(f"The content would produce more than {MAX_CHUNKS} chunks.")
    return drafts


__all__ = ["MAX_CHUNKS", "MAX_CHUNK_WORDS", "ChunkDraft", "checksum", "chunk_text", "normalise"]
