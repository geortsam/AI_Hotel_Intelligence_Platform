"""The README tells a reviewer the truth about what was evaluated, what is open and what is done.

Found by the R0.1 pull-request audit (2026-11-27) and corrected before any merge:

* the development-stage table said R2.2 and R2.2b ran on the synthetic demo data; they ran on the
  published hotel-booking dataset of two Portuguese hotels (2015-2017), as the frozen evaluation
  record says;
* the Known limitations section did not list the open Analytics expenses-table clipping or the
  missing scripted demo walkthrough;
* the stage table stopped at A1.

Each is now held: the R2.2 row to the frozen record's own words, the two open items to the
Known limitations section (and the defect to the closure audit, which must agree that it is
open), and the stage table to the completed audit tasks, R6.1 and the R5 decision.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
README = REPOSITORY_ROOT / "README.md"
HOLDOUT_RECORD = REPOSITORY_ROOT / "docs" / "ml-frozen-holdout-evaluation.md"
CLOSURE_AUDIT = REPOSITORY_ROOT / "docs" / "project-closure-audit.md"

#: The dataset, in the frozen record's words.
EN_DASH = chr(0x2013)
DATASET = f"two Portuguese hotels (2015{EN_DASH}2017)"


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\n[ \t]*>", "\n", text))


def readme() -> str:
    return README.read_text(encoding="utf-8")


def stage_rows() -> dict[str, str]:
    """The development-stage table, by its first cell."""
    text = readme()
    table = text[text.index("## Development stages") : text.index("## How to run")]
    rows: dict[str, str] = {}
    for line in table.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("| ") and len(cells) == 3 and not set(cells[0]) <= {"-"}:
            rows[cells[0]] = line
    return rows


def known_limitations() -> str:
    text = readme()
    start = text.index("## Known limitations")
    following = text.find("\n## ", start + 1)
    return flat(text[start : following if following != -1 else len(text)])


def test_the_r22_row_names_the_dataset_the_frozen_record_names() -> None:
    assert DATASET in flat(HOLDOUT_RECORD.read_text(encoding="utf-8"))
    row = stage_rows()["R2.2, R2.2b"]
    assert DATASET in row
    # Only as a negation: the evaluation did not run on the synthetic demo data.
    assert re.findall(r"(\w+ )on the synthetic demo data", row) == ["not "]


def test_known_limitations_lists_the_open_defect_and_the_missing_walkthrough() -> None:
    section = known_limitations()
    assert "### Known defects and gaps" in section
    assert "**The Analytics expenses table is clipped at 1440px. Open, not fixed.**" in section
    assert "is 525px wide inside a 482px container" in section
    assert "**There is no scripted end-to-end demo walkthrough.**" in section


def test_the_closure_audit_agrees_that_the_defect_is_open() -> None:
    """If the defect is fixed, both documents must say so; until then both say it is open."""
    audit = flat(CLOSURE_AUDIT.read_text(encoding="utf-8"))
    assert "**Analytics expenses table, clipped at 1440px. Open, not fixed.**" in audit


def test_the_stage_table_lists_the_completed_audit_tasks_r61_and_the_r5_decision() -> None:
    rows = stage_rows()
    for stage in (*(f"A{n}" for n in range(1, 10)), "R6.1"):
        assert stage in rows, stage
        assert rows[stage].endswith("| **done** |"), stage
    # R5 is a decision, not a build: recorded as decided, never as done.
    assert rows["R5"].endswith("| decided |")
    assert "Academic / Professional Portfolio" in rows["R5"]
