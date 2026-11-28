"""R6.1, read literally: the six stage snapshots are consolidated, and their paths still lead on.

On 2026-11-25 the six superseded snapshots were merged into three maintained documents --
``docs/architecture.md``, ``database/README.md`` and ``docs/roadmap-v2.0-execution.md`` -- and
each old path became a short redirect. These tests hold that in place:

* the index marks exactly those six as redirects, and nothing as superseded any more;
* each redirect stays a map, not a second copy: short, no sections of its own, naming its
  destination and the commit at which the snapshot as last written can be read;
* the paths that code comments and frozen records cite still exist;
* every section the snapshots held still exists in its destination;
* the design register keeps decisions 1-21, once each, and every "approved decision N" cited in
  code names one of them;
* the figures the moved text quotes from the code -- thresholds, bounds, constants -- are the
  code's own.

The link checker (``scripts/doc_links.py``) separately proves that every link and anchor in the
redirects and their destinations resolves.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest

from app.ml import timeseries
from app.schemas import analytics as analytics_schema
from app.schemas import intelligence as intelligence_schema
from scripts import doc_links

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE = "docs/architecture.md"
DATABASE_README = "database/README.md"
EXECUTION = "docs/roadmap-v2.0-execution.md"

#: Each consolidated snapshot, and the maintained document its content went to.
REDIRECTS: dict[str, str] = {
    "docs/backend-architecture.md": ARCHITECTURE,
    "docs/analytics-design.md": ARCHITECTURE,
    "docs/ml-design.md": ARCHITECTURE,
    "docs/database-design.md": DATABASE_README,
    "docs/database-implementation.md": DATABASE_README,
    "docs/roadmap-v2.0.md": EXECUTION,
}
#: The last commit at which the six snapshots exist in full.
CONSOLIDATED_AT = "2a07405"
#: A redirect is a map: a title, a paragraph and a table of sections.
MAX_REDIRECT_LINES = 30


def read(name: str) -> str:
    return (REPOSITORY_ROOT / name).read_text(encoding="utf-8")


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\n[ \t]*>", "\n", text))


def index_statuses() -> dict[str, str]:
    """Every document the index lists in a four-column row, and the status it gives it."""
    statuses: dict[str, str] = {}
    for line in doc_links.prose_lines(read(doc_links.INDEX)):
        first = re.match(r"^\| \[[^\]]+\]\(([^)#]+)(?:#[^)]*)?\) \|", line)
        if first and line.count("|") == 4:
            parts: list[str] = []
            for part in PurePosixPath("docs", first.group(1)).parts:
                if part == "..":
                    parts.pop()
                else:
                    parts.append(part)
            statuses["/".join(parts)] = line.rsplit("|", 2)[1].strip()
    return statuses


# --- the index -----------------------------------------------------------------------------------


def test_the_index_marks_exactly_the_six_snapshots_as_redirects() -> None:
    redirects = {doc for doc, status in index_statuses().items() if status.startswith("Redirect")}
    assert redirects == set(REDIRECTS)


def test_no_document_is_superseded_any_more() -> None:
    statuses = index_statuses()
    assert len(statuses) > 40  # the parser reads the index; an empty result would pass below
    assert [doc for doc, status in statuses.items() if status.startswith("Superseded")] == []
    banner = "Stage snapshot, not a current-state document"
    carrying = sorted(
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in (REPOSITORY_ROOT / "docs").rglob("*.md")
        if banner in flat(path.read_text(encoding="utf-8"))
    )
    assert carrying == []


def test_each_redirect_names_its_destination_in_the_index() -> None:
    statuses = index_statuses()
    for redirect, destination in REDIRECTS.items():
        target = (
            PurePosixPath(destination).relative_to("docs")
            if destination.startswith("docs/")
            else PurePosixPath("..", destination)
        )
        assert f"]({target.as_posix()}" in statuses[redirect], redirect


# --- the redirects -------------------------------------------------------------------------------


@pytest.mark.parametrize("redirect", sorted(REDIRECTS))
def test_a_redirect_is_a_short_map_to_its_destination(redirect: str) -> None:
    text = read(redirect)
    lines = text.splitlines()
    assert len(lines) <= MAX_REDIRECT_LINES, f"{redirect} holds content again"
    assert lines[0].startswith("# ") and lines[0].endswith(" — moved")
    # One heading only: a section of its own would be the snapshot growing back.
    assert [line for line in doc_links.prose_lines(text) if line.startswith("#")] == [lines[0]]
    assert "**Redirect.**" in text
    assert f"`git show {CONSOLIDATED_AT}:{redirect}`" in flat(text)
    destination = REDIRECTS[redirect]
    resolved = {
        doc_links._resolve(redirect, doc_links.split_target(link.target)[0])
        for link in doc_links.links(redirect, text)
        if link.target and not doc_links.is_external(link.target)
    }
    assert PurePosixPath(destination) in resolved, f"{redirect} does not link {destination}"


def test_the_paths_that_code_and_frozen_records_cite_still_exist() -> None:
    """Code comments are not links, so the link checker does not see them; this does."""
    citations = {
        "backend/app/api/openapi.py": "docs/backend-architecture.md",
        "backend/app/schemas/analytics.py": "docs/analytics-design.md",
        "backend/app/ml/__init__.py": "docs/ml-design.md",
        "docs/development-roadmap.md": "database-implementation.md",
        "docs/ml-dataset-design.md": "ml-design.md",
    }
    for citing, cited in citations.items():
        assert cited in read(citing), f"{citing} no longer cites {cited}"
        path = cited if cited.startswith("docs/") else f"docs/{cited}"
        assert (REPOSITORY_ROOT / path).is_file(), path


# --- the destinations ----------------------------------------------------------------------------

#: One marker per section of each snapshot, in the document that now holds it.
MOVED_SECTIONS: list[tuple[str, str]] = [
    # backend-architecture.md, Stage 3B.12
    (ARCHITECTURE, "### 2.1 Domains"),
    (ARCHITECTURE, "### 2.2 What each layer owns, and how that is enforced"),
    (ARCHITECTURE, "### 2.3 Transaction boundaries and conditional updates"),
    (ARCHITECTURE, "### 2.4 Tenant isolation, in detail"),
    (ARCHITECTURE, "### 2.5 Identifiers and credentials"),
    (ARCHITECTURE, "### 2.6 Guarantees the database owns"),
    (ARCHITECTURE, "### 4.1 Error handling"),
    (ARCHITECTURE, "### 8.1 Known limitations of the backend"),
    (ARCHITECTURE, "### 8.2 The production requirements Stage 3B.12 listed, and where each stands"),
    (ARCHITECTURE, "### 8.3 What is deliberately absent"),
    # analytics-design.md, Stage 3B.10
    (ARCHITECTURE, "### Analytics (Stage 3B.10)"),
    (ARCHITECTURE, "**Metric definitions**"),
    (ARCHITECTURE, "**Why two booking counts.**"),
    (ARCHITECTURE, "**The `is_room_revenue` split.**"),
    (ARCHITECTURE, "**Join multiplication.**"),
    (ARCHITECTURE, "**Routes.**"),
    # ml-design.md, Stage 3B.11
    (ARCHITECTURE, "### The statistical intelligence layer (Stage 3B.11)"),
    (ARCHITECTURE, "**Two observation axes: stay dates and booking intake.**"),
    (ARCHITECTURE, "**Predictions are never mixed with facts.**"),
    (ARCHITECTURE, "**Temporal leakage is prevented by signature.**"),
    (ARCHITECTURE, "**Limitations of this layer.**"),
    # database-design.md, Stage 2A: the thirteen sections keep their numbers
    (DATABASE_README, "## Schema design"),
    *(
        (DATABASE_README, f"### {heading}")
        for heading in (
            "1. Architecture overview",
            "2. ER-style relationship description",
            "3. Table specifications",
            "4. Relationships and cardinality",
            "5. Constraints",
            "6. Indexes",
            "7. Booking overlap strategy",
            "8. Daily metrics strategy",
            "9. Money, date and time decisions",
            "10. Multi-hotel strategy",
            "11. AI/ML readiness",
            "12. Design decisions",
            "13. Assumptions",
        )
    ),
    (DATABASE_README, "#### Unresolved before any population job (Issue H9)"),
    # database-implementation.md, Stage 2B and 2C
    (DATABASE_README, "## Implementation"),
    (DATABASE_README, "### The migrations"),
    (DATABASE_README, "### The models"),
    (DATABASE_README, "### PostgreSQL-specific features"),
    (DATABASE_README, "### The exclusion constraint"),
    (DATABASE_README, "### The night-completeness constraint trigger"),
    (DATABASE_README, "### Room revenue derivation"),
    (DATABASE_README, "### Testing"),
    (DATABASE_README, "### Stage 2B and 2C, as verified then"),
    # roadmap-v2.0.md, the proposal of 2026-10-10
    (EXECUTION, "## 5. The proposal as written on 2026-10-10"),
    (EXECUTION, "### 5.1 What the platform did at v1.0.0"),
    (EXECUTION, "### 5.2 The limitations it named, and where each stands"),
    (EXECUTION, "### 5.3 The stages, as proposed"),
    (EXECUTION, "### 5.4 The recommended order, as proposed"),
    (EXECUTION, "### 5.5 The decisions it asked for before any stage started"),
    (EXECUTION, "Consolidate the stage-snapshot documents into a small set of maintained"),
]


@pytest.mark.parametrize(("document", "marker"), MOVED_SECTIONS)
def test_every_section_of_the_snapshots_lives_in_its_destination(
    document: str, marker: str
) -> None:
    assert marker in read(document)


def test_the_design_register_keeps_each_decision_once() -> None:
    text = read(DATABASE_README)
    register = text[text.index("### 12. Design decisions") : text.index("### 13. Assumptions")]
    numbers = [int(n) for n in re.findall(r"^\| (\d+) \| \*\*", register, re.MULTILINE)]
    assert numbers == list(range(1, 22))


EN_DASH = chr(0x2013)
#: "approved decision 21", "approved decisions 6-8" (written with a hyphen or an en dash).
CITED_DECISION = re.compile(rf"[Aa]pproved decisions? (\d+)(?:[{EN_DASH}-](\d+))?")


def test_every_decision_number_cited_in_code_is_in_the_register() -> None:
    cited: set[int] = set()
    # The mutation inventory is left out: it quotes citations in order to break them.
    inventory = REPOSITORY_ROOT / "tests" / "mutation"
    for top in ("backend/app", "tests", "database/migrations", "frontend/src", "scripts"):
        for path in (REPOSITORY_ROOT / top).rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or "__pycache__" in path.parts:
                continue
            if path.is_relative_to(inventory):
                continue
            for first, last in CITED_DECISION.findall(path.read_text(encoding="utf-8")):
                cited.update(range(int(first), int(last or first) + 1))
    # The citations the database README's key explains are among them: a scan that matched
    # nothing would pass the range check below.
    assert {1, 3, 10, 12, 13, 17, 18, 21} <= cited
    assert cited <= set(range(1, 22))


# --- figures the moved text quotes from the code -------------------------------------------------

MULTIPLICATION_SIGN = chr(0xD7)


def test_the_statistical_layer_quotes_its_own_constants() -> None:
    raw = read(ARCHITECTURE)
    text = flat(raw)
    assert f'`MODEL_VERSION = "{timeseries.MODEL_VERSION}"`' in text
    assert f"`MIN_BUCKET_OBSERVATIONS` ({timeseries.MIN_BUCKET_OBSERVATIONS})" in text
    assert f"scaled by {timeseries.MAD_TO_SIGMA}" in text
    assert f"score = {timeseries.MODIFIED_Z_CONSTANT} {MULTIPLICATION_SIGN}" in text
    assert f"flag when |score| > {timeseries.ANOMALY_THRESHOLD}" in text
    assert f"increasing  if relative_change >  {timeseries.TREND_THRESHOLD}" in raw
    low, high = intelligence_schema.MIN_TRAINING_DAYS, intelligence_schema.MAX_TRAINING_DAYS
    horizon = intelligence_schema.MAX_HORIZON_DAYS
    assert f"bounded [{low}, {high}] and [1, {horizon}]" in text


def test_analytics_quotes_its_own_range_limit() -> None:
    text = flat(read(ARCHITECTURE))
    assert f"The maximum range is **{analytics_schema.MAX_RANGE_DAYS} days**" in text
