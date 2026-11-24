"""A8 (roadmap R6.1): the contradictions the documentation consolidation found stay resolved.

Each was a current-tense statement that the code, or another document checked against the code,
contradicted. Each is now held to the code itself, or, where the corrected text states no
figure, its stale form must be absent -- with a positive control, so the absence check cannot
pass by matching nothing.

* ``docs/database-design.md`` said the head was over 28 application tables; there are 29.
* ``docs/database-implementation.md`` said sixteen migrations followed ``0001`` while listing
  twenty-two of them.
* ``docs/ml-design.md`` said there was no LLM anywhere in the repository; there is a copilot.
* ``docs/architecture.md`` called its Stage 7.14 counts the current state.
* ``tests/README.md`` stated a frontend test count that no test computed, long out of date.
"""

from __future__ import annotations

import re
from pathlib import Path

import app.models  # noqa: F401 -- registers every table on Base.metadata
from app.db.base import Base

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPOSITORY_ROOT / "docs"
VERSIONS = REPOSITORY_ROOT / "database" / "migrations" / "versions"
TESTS_README = REPOSITORY_ROOT / "tests" / "README.md"

_UNITS = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = {"twenty": 20, "thirty": 30, "forty": 40}


def number_word(word: str) -> int:
    """``"Twenty-two"`` as 22; up to forty-nine, which is as far as a migration count needs."""
    word = word.lower()
    if word in _UNITS:
        return _UNITS.index(word)
    tens, _, unit = word.partition("-")
    return _TENS[tens] + (_UNITS.index(unit) if unit else 0)


def flat(text: str) -> str:
    """One line, blockquote markers dropped, so a re-wrapped sentence still matches."""
    return re.sub(r"\s+", " ", re.sub(r"\n[ \t]*>", "\n", text))


def read(path: Path) -> str:
    return flat(path.read_text(encoding="utf-8"))


def test_the_database_design_banner_states_the_table_count() -> None:
    found = re.findall(r"over (\d+) application tables", read(DOCS / "database-design.md"))
    assert found and {int(n) for n in found} == {len(Base.metadata.tables)}


def test_the_database_implementation_banner_counts_the_migrations_after_the_first() -> None:
    text = read(DOCS / "database-implementation.md")
    found = re.findall(r"([A-Za-z]+(?:-[a-z]+)?) migrations have been added after its", text)
    assert found
    assert [number_word(word) for word in found] == [len(list(VERSIONS.glob("*.py"))) - 1]


def test_the_number_words_are_read_as_written() -> None:
    assert [number_word(w) for w in ("Sixteen", "Twenty", "Twenty-two", "forty-nine")] == [
        16,
        20,
        22,
        49,
    ]


STALE_NO_LLM = "with no LLM anywhere in the repository"
STALE_CURRENT_STATE = "Current state (through Stage 7.14)"
STALE_FRONTEND_COUNT = re.compile(r"\d[\d,]* test files and \d[\d,]* tests")


def test_ml_design_no_longer_says_the_repository_holds_no_llm() -> None:
    assert STALE_NO_LLM not in read(DOCS / "ml-design.md")


def test_architecture_no_longer_calls_its_stage_7_14_counts_current() -> None:
    assert STALE_CURRENT_STATE not in read(DOCS / "architecture.md")


def test_the_tests_readme_states_no_frontend_test_count() -> None:
    assert STALE_FRONTEND_COUNT.findall(read(TESTS_README)) == []


def test_the_matchers_see_the_stale_sentences_as_they_were_wrapped() -> None:
    """Positive controls: each absence check would have caught the text it replaced."""
    ml_design = flat(
        "> exactly what that layer is — a deterministic standard-library baseline, with no LLM"
        " anywhere in\n> the repository — and Stages 6.1 to 6.11 have since added"
    )
    assert STALE_NO_LLM in ml_design
    architecture = flat("**Current state (through Stage 7.14):** `api/` 34 files,")
    assert STALE_CURRENT_STATE in architecture
    tests_readme = flat("There are 33 test files and 1,263 tests. CI runs them in their own")
    assert STALE_FRONTEND_COUNT.findall(tests_readme) == ["33 test files and 1,263 tests"]
