"""Two current-state statements that the code once falsified stay corrected (A6).

* ``README.md`` said "any LLM/RAG/agent capability" is not implemented, beside sections that
  describe the copilot that exists. It now says exactly what exists: seven read-only tools,
  PostgreSQL full-text search over the hotel's documents, one declared write, and no vector
  search or agent.
* ``frontend/README.md`` gave the breakpoints as ``< 768px`` / ``>= 768px`` / ``>= 1024px``, from
  before the UI redesign moved the table/card switch to 1024px and added 600 and 1280.

As in ``test_current_state_docs.py`` (M2), each is checked two ways. The stale sentence must be
absent, with a positive control that runs the matcher over the sentence as it was wrapped, so the
absence check cannot pass by matching nothing. And the corrected text must name facts that are
checked against the code, so the correction cannot drift the other way either: the README's tool
list is the registry's, its declared write is the only tool with a side effect, and no table has a
vector column; every breakpoint the frontend README lists is a media query the stylesheets use.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy.dialects.postgresql import TSVECTOR

import app.models  # noqa: F401 -- registers every table on Base.metadata
from app.copilot.registry import build_default_registry
from app.db.base import Base

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
README = REPOSITORY_ROOT / "README.md"
FRONTEND_README = REPOSITORY_ROOT / "frontend" / "README.md"
FRONTEND_SRC = REPOSITORY_ROOT / "frontend" / "src"
TOKENS = FRONTEND_SRC / "styles" / "tokens.css"
USE_IS_COMPACT = FRONTEND_SRC / "lib" / "useIsCompact.ts"

#: Number words for the tool count the README states.
NUMBER_WORDS = {"five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def flat(text: str) -> str:
    """*text* with every run of whitespace as one space, so a sentence is found however wrapped."""
    return re.sub(r"\s+", " ", text)


def states(text: str, claim: str) -> bool:
    return flat(claim) in flat(text)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def section(text: str, start: str, end: str) -> str:
    """The part of *text* from the heading *start* up to (not including) *end*."""
    flat_text = flat(text)
    begin = flat_text.index(flat(start))
    return flat_text[begin : flat_text.index(flat(end), begin)]


#: The stale claims, as written, and where they were. Each must stay absent.
STALE = [
    (README, "any LLM/RAG/agent capability"),
    (
        FRONTEND_README,
        "Breakpoints are `< 768px` (mobile), `>= 768px` (tablet) and `>= 1024px` (desktop",
    ),
]

#: The original sentences, wrapped exactly as they were in the files, for the positive controls.
ORIGINALS = [
    "room-image classification, recommendations, any LLM/RAG/agent capability, drift detection,\n"
    "retraining, a registry holding more than one model,",
    "Breakpoints are `< 768px` (mobile), `>= 768px` (tablet) and `>= 1024px` (desktop, where the\n"
    "sidebar becomes permanent).",
]


# ======================================================================================
# The stale claims stay gone
# ======================================================================================


@pytest.mark.parametrize(("path", "claim"), STALE, ids=[c[:40] for _, c in STALE])
def test_a_corrected_stale_claim_is_absent(path: Path, claim: str) -> None:
    assert not states(read(path), claim), f"{path.name} again says: {claim}"


@pytest.mark.parametrize(
    ("claim", "original"),
    list(zip([claim for _, claim in STALE], ORIGINALS, strict=True)),
    ids=[c[:40] for _, c in STALE],
)
def test_the_matcher_finds_each_claim_in_its_original_wrapping(claim: str, original: str) -> None:
    """Positive control: the absence check above would catch the claim as it was written."""
    assert states(original, claim)


@pytest.mark.parametrize("claim", [claim for _, claim in STALE], ids=[c[:40] for _, c in STALE])
def test_the_matcher_finds_each_claim_rewrapped_across_lines(claim: str) -> None:
    """Both claims sat on one line when they were found, so the control above passes whether or
    not the matcher ignores wrapping. Prose is re-wrapped freely; a claim that came back broken
    across a line, with the next line indented, must still be caught."""
    words = claim.split(" ")
    middle = len(words) // 2
    rewrapped = " ".join(words[:middle]) + "\n  " + " ".join(words[middle:])
    assert rewrapped != claim
    assert states(f"Some text before it.\n{rewrapped}\nand after.", claim)


# ======================================================================================
# The README's account of the language-model side matches the code
# ======================================================================================


def language_model_paragraph() -> str:
    return section(
        read(README), "**What the language-model side is, exactly.**", "What is **not** implemented"
    )


def test_the_readme_lists_exactly_the_registered_copilot_tools() -> None:
    paragraph = language_model_paragraph()
    listed = set(re.findall(r"`((?:get|search)_[a-z_]+)`", paragraph))
    registered = set(build_default_registry().names())
    assert listed == registered, f"README lists {sorted(listed)}, registry has {sorted(registered)}"
    count = re.search(r"through (\w+) read-only tools", paragraph)
    assert count is not None, "the README no longer states how many tools there are"
    assert NUMBER_WORDS[count.group(1)] == len(registered)


def test_the_readme_s_one_declared_write_is_the_only_tool_with_a_side_effect() -> None:
    writers = {
        contract.name: contract.side_effect
        for contract in build_default_registry().contracts()
        if contract.side_effect != "none"
    }
    assert writers == {"get_demand_forecast": "records_served_prediction"}
    assert states(
        language_model_paragraph(),
        "its one declared write is that `get_demand_forecast` records the prediction it serves",
    )


def test_the_readme_s_search_claim_is_full_text_with_no_vector_column() -> None:
    assert states(language_model_paragraph(), "found by PostgreSQL full-text search")
    assert states(read(README), "What is **not** implemented, and not claimed anywhere in this")
    assert states(read(README), "vector search or embeddings (Stage 7.15 is conditional")
    column_types = {
        f"{table.name}.{column.name}": type(column.type).__name__
        for table in Base.metadata.tables.values()
        for column in table.columns
    }
    assert not [
        c for c, kind in column_types.items() if "vector" in kind.lower() and kind != "TSVECTOR"
    ]
    chunks = Base.metadata.tables["hotel_document_chunks"]
    assert isinstance(chunks.columns["search_vector"].type, TSVECTOR)


# ======================================================================================
# Every breakpoint the frontend README names is one the stylesheets use
# ======================================================================================


def stylesheet_media_queries() -> set[str]:
    queries: set[str] = set()
    for css in FRONTEND_SRC.rglob("*.css"):
        queries.update(re.findall(r"@media \(((?:min|max)-width: \d+px)\)", read(css)))
    return queries


def documented_breakpoints() -> list[tuple[str, int]]:
    """(``below`` or ``from``, width) for each row of the frontend README's breakpoint table."""
    table = section(read(FRONTEND_README), "**Breakpoints.**", "Below 1024px every list")
    rows = re.findall(r"\| (below (\d+)px|(\d+)px and up) \|", table)
    return [("below", int(b)) if b else ("from", int(f)) for _, b, f in rows]


def test_the_frontend_readme_documents_the_breakpoint_set_in_tokens_css() -> None:
    assert states(
        read(FRONTEND_README), "documented at the foot of `tokens.css` as 600 / 768 / 1024 / 1280"
    )
    tokens = read(TOKENS)
    for width in (600, 768, 1024, 1280):
        assert re.search(rf"\*\s+{width}px\s", tokens), f"tokens.css no longer documents {width}px"


def test_every_breakpoint_row_is_a_media_query_the_stylesheets_use() -> None:
    rows = documented_breakpoints()
    assert [width for _, width in rows] == [600, 768, 1024, 1280]
    queries = stylesheet_media_queries()
    for kind, width in rows:
        query = f"max-width: {width - 1}px" if kind == "below" else f"min-width: {width}px"
        assert query in queries, (
            f"frontend/README.md's {kind} {width}px row has no @media ({query})"
        )


def test_the_card_switch_the_readme_states_is_use_is_compact_s_default() -> None:
    default = re.search(r"export function useIsCompact\(maxWidth = (\d+)\)", read(USE_IS_COMPACT))
    assert default is not None
    assert int(default.group(1)) == 1023
    text = read(FRONTEND_README)
    assert states(
        text,
        "list pages show tables instead of cards (`useIsCompact`, default `max-width: 1023px`)",
    )
    assert states(text, "Below 1024px every list renders as cards")
