"""What a copilot answer may cite, and what it did cite. Checked, not trusted (Stage 7.10).

Architecture §6.6 and Amendment A5. Pure: no database, no session, no hotel.

## Source labels, not identifiers

The model never sees a chunk's or a document's `public_id`. The knowledge tool labels each excerpt
it returns `S1`, `S2`, ... and the model cites a label in square brackets, `[S1]`. This module
holds the mapping from label to chunk for ONE request -- the `EvidenceLedger` -- and it is filled
only from what `KnowledgeService.search` returned for the request's own hotel. So:

- a label is meaningful only within the request that issued it; a label from an earlier request,
  or one a document's text happens to contain, maps to nothing unless this request's own search
  issued it;
- a chunk the search did not return -- another hotel's, a withdrawn or a superseded version's --
  can never be in the ledger, because the search filters on hotel and status in its SQL;
- a UUID written into an answer is text, never a citation: nothing here parses one.

Citation identity is therefore enforced by the set of chunks actually retrieved, not by the syntax
of what the model wrote. The Stage 7.6 rule that a tool returns no row identifier is kept.

## Numbered from a chosen first label (Stage 7.11)

A ledger numbers its labels from `first_label`, 1 by default. A multi-turn caller starts each
turn's ledger where the previous turn's labels ended (turn 1 issues S1..S5, turn 2 starts at S6),
so a label an earlier turn issued never names a different chunk in a later turn -- and, since a
ledger holds only its own request's labels, never resolves there at all.

## Staged, then admitted

A label is staged when the tool builds its output and admitted only when the invocation service
has validated that output and is about to hand it to the model. A tool call that fails after
staging leaves nothing citable behind: the model never saw those labels.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

#: A well-formed citation: `[S` + a positive number without a leading zero + `]`.
CITATION = re.compile(r"\[S([1-9][0-9]{0,3})\]")

#: Anything that looks like an attempt at one -- `[S01]`, `[S1, S2]`, `[s3]`, `[S 4]`, `[Source
#: 2]`. A malformed citation is not ignored: it is treated as a citation that does not resolve,
#: because an answer that tries to cite something this server cannot check is not a cited answer.
#: An ordinary bracketed word (`[Sunday]`) or number (`[2026]`) is not citation-like, and a number
#: in one stays subject to the figure check.
_CITATION_LIKE_TOKEN = r"\[\s*(?:[Ss]\s*\d|[Ss]ource)[^\]]*\]"
CITATION_LIKE = re.compile(_CITATION_LIKE_TOKEN)


def label_for(number: int) -> str:
    return f"S{number}"


@dataclass(frozen=True, slots=True)
class Evidence:
    """One chunk a search returned in this request, and the label the model saw it under."""

    label: str
    chunk_public_id: uuid.UUID
    document_public_id: uuid.UUID
    title: str
    version: int
    text: str

    @property
    def grounding_text(self) -> str:
        """What a figure in a sentence citing this excerpt may be traced to."""
        return f"{self.title}\n{self.version}\n{self.text}"


@dataclass(slots=True)
class EvidenceLedger:
    """The excerpts one request's searches returned, by label. Never shared between requests."""

    #: The number the first label issued by this ledger carries. See the module docstring.
    first_label: int = 1
    _admitted: dict[str, Evidence] = field(default_factory=dict)
    _staged: dict[str, Evidence] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.first_label < 1:
            raise ValueError("Source labels are numbered from 1.")

    def stage(
        self,
        *,
        chunk_public_id: uuid.UUID,
        document_public_id: uuid.UUID,
        title: str,
        version: int,
        text: str,
    ) -> str:
        """The label for this chunk: its existing one, or the next free one. Staged, not citable."""
        for existing in (*self._admitted.values(), *self._staged.values()):
            if existing.chunk_public_id == chunk_public_id:
                return existing.label
        label = label_for(self.first_label + len(self._admitted) + len(self._staged))
        self._staged[label] = Evidence(
            label=label,
            chunk_public_id=chunk_public_id,
            document_public_id=document_public_id,
            title=title,
            version=version,
            text=text,
        )
        return label

    def admit(self) -> None:
        """The staged labels reached the model: they are citable from now on."""
        self._admitted.update(self._staged)
        self._staged.clear()

    def discard(self) -> None:
        """The call that staged them failed: the model never saw these labels."""
        self._staged.clear()

    def get(self, label: str) -> Evidence | None:
        return self._admitted.get(label)

    def __len__(self) -> int:
        return len(self._admitted)

    @property
    def next_label(self) -> int:
        """The number the next ledger should start at: one past every label this one issued."""
        return self.first_label + len(self._admitted)


@dataclass(frozen=True, slots=True)
class CitationCheck:
    """What an answer cited, split into what resolves in this request and what does not."""

    #: Resolved citations, in order of first appearance, each once.
    valid: tuple[Evidence, ...]
    #: Every citation-like token that did not resolve, as written, each once.
    invalid: tuple[str, ...]

    @property
    def cited(self) -> bool:
        return bool(self.valid)


def check_citations(text: str, ledger: EvidenceLedger) -> CitationCheck:
    """Resolve every citation in *text* against *ledger*. Pure; order-preserving."""
    valid: list[Evidence] = []
    invalid: list[str] = []
    for token in CITATION_LIKE.finditer(text):
        written = token.group(0)
        match = CITATION.fullmatch(written)
        evidence = ledger.get(label_for(int(match.group(1)))) if match else None
        if evidence is None:
            if written not in invalid:
                invalid.append(written)
        elif evidence not in valid:
            valid.append(evidence)
    return CitationCheck(valid=tuple(valid), invalid=tuple(invalid))


def strip_citations(text: str) -> str:
    """*text* without its citation tokens: their digits are labels, not figures."""
    return CITATION_LIKE.sub(" ", text)


#: Where one sentence ends: a terminator followed by whitespace, or a line break. A full stop
#: between digits (`7.30`, `15.00`) is not an ending, because no whitespace follows it.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
_LEADING_CITATIONS = re.compile(rf"^\s*((?:{_CITATION_LIKE_TOKEN}\s*)+)")


def sentences_with_citations(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Each sentence of *text* with the labels it cites.

    A citation written after a sentence's full stop (`... at 07:00. [S1]`) belongs to that
    sentence, not to the next, so labels opening a sentence are moved back to the one before it.
    """
    parts = [part for part in _SENTENCE_END.split(text) if part.strip()]
    sentences: list[list[str]] = []
    for part in parts:
        leading = _LEADING_CITATIONS.match(part)
        if leading and sentences:
            sentences[-1][1] += leading.group(1)
            part = part[leading.end() :]
            if not part.strip():
                continue
        sentences.append([part, part])
    result: list[tuple[str, tuple[str, ...]]] = []
    for body, with_citations in sentences:
        labels = tuple(
            label_for(int(match.group(1))) for match in CITATION.finditer(with_citations)
        )
        result.append((strip_citations(body), labels))
    return result


def evidence_texts(
    labels: Iterable[str], ledger: EvidenceLedger | Mapping[str, Evidence]
) -> list[str]:
    """The grounding text of each resolvable label. An unresolvable label contributes nothing."""
    texts: list[str] = []
    for label in labels:
        evidence = ledger.get(label)
        if evidence is not None:
            texts.append(evidence.grounding_text)
    return texts


__all__ = [
    "CITATION",
    "CITATION_LIKE",
    "CitationCheck",
    "Evidence",
    "EvidenceLedger",
    "check_citations",
    "evidence_texts",
    "label_for",
    "sentences_with_citations",
    "strip_citations",
]
