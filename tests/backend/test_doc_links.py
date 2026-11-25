"""A8 (roadmap R6.1): the documentation's links, anchors and index hold, and the checker works.

`scripts/doc_links.py` checks every relative link and anchor in the root README and in `docs/`,
and that every document under `docs/` is reachable from the index, `docs/README.md`. The first
group of tests runs it over the repository. The rest build small trees under `tmp_path` and plant
one fault each -- a broken file link, a missing anchor, an unreachable document and the rest --
so that a checker which stopped looking would fail here, not pass by finding nothing.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path, PurePosixPath

import pytest

from scripts import doc_links

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


# --- the repository ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def report() -> doc_links.Report:
    return doc_links.check(REPOSITORY_ROOT)


def test_the_documentation_has_no_broken_link_anchor_or_unreachable_document(
    report: doc_links.Report,
) -> None:
    assert [str(problem) for problem in report.problems] == []


def test_the_check_reads_the_root_readme_and_every_document_under_docs(
    report: doc_links.Report,
) -> None:
    on_disk = sorted(
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in (REPOSITORY_ROOT / "docs").rglob("*.md")
    )
    # The database README holds the schema's design and implementation record since R6.1, so it
    # is checked like the root README.
    assert report.files == ["README.md", "database/README.md", *on_disk]
    assert "docs/README.md" in on_disk and "docs/deployment/tls.md" in on_disk
    # Hundreds of relative links and dozens of anchors: a parser that matched nothing would pass
    # the check above, and fails here.
    assert report.links > 300 and report.anchors > 50


def index_entries() -> Counter[str]:
    """The documents the index's tables give an entry: a row whose first cell is a link."""
    text = (REPOSITORY_ROOT / doc_links.INDEX).read_text(encoding="utf-8")
    entries: Counter[str] = Counter()
    for line in doc_links.prose_lines(text):
        first = re.match(r"^\| \[[^\]]+\]\(([^)#]+)(?:#[^)]*)?\) \|", line)
        if first:
            resolved = PurePosixPath("docs", first.group(1))
            parts: list[str] = []
            for part in resolved.parts:
                if part == "..":
                    parts.pop()
                else:
                    parts.append(part)
            entries["/".join(parts)] += 1
    return entries


def test_the_index_gives_every_document_exactly_one_entry() -> None:
    documents = {
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in (REPOSITORY_ROOT / "docs").rglob("*.md")
    } - {doc_links.INDEX}
    entries = index_entries()
    assert {doc: entries[doc] for doc in sorted(documents)} == dict.fromkeys(sorted(documents), 1)


#: Since R6.1 consolidated the six superseded snapshots, a document is current, a frozen
#: record, or a redirect to where its content went. "Superseded" is no longer a status.
STATUSES = ("Current", "Frozen record", "Redirect")


def test_every_index_entry_states_one_of_the_three_statuses() -> None:
    text = (REPOSITORY_ROOT / doc_links.INDEX).read_text(encoding="utf-8")
    rows = [
        line
        for line in text.splitlines()
        if re.match(r"^\| \[[^\]]+\]\([^)]*\) \|", line) and line.count("|") == 4
    ]
    assert rows
    for row in rows:
        status = row.rsplit("|", 2)[1].strip()
        assert status.startswith(STATUSES), row


def test_the_root_readme_points_to_the_index() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    assert "](docs/README.md)" in readme


def test_the_checker_reads_the_maintained_documents_outside_docs(tmp_path: Path) -> None:
    """A broken link in the database README is caught, as one in the root README is."""
    healthy(tmp_path, {"database/README.md": "# Database\n\nSee [the design](design.md).\n"})
    assert problems(tmp_path) == ["database/README.md:3: design.md: no such file"]


# --- the checker, on planted faults --------------------------------------------------------------


def tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def healthy(root: Path, extra: dict[str, str] | None = None) -> Path:
    """A minimal documentation tree with no problem, plus the *extra* files."""
    files = {
        "README.md": "# Project\n\nSee [the docs](docs/README.md).\n",
        "docs/README.md": "# Index\n\n- [Guide](guide.md)\n- [Runbook](ops/runbook.md#2-restore)\n",
        "docs/guide.md": "# Guide\n\n## Setup\n\nBack to the [index](README.md#index).\n",
        "docs/ops/runbook.md": "# Runbook\n\n## 1. Backup\n\n## 2. Restore\n",
    }
    files.update(extra or {})
    return tree(root, files)


def problems(root: Path) -> list[str]:
    return [str(problem) for problem in doc_links.check(root).problems]


def test_a_healthy_tree_has_no_problem(tmp_path: Path) -> None:
    report = doc_links.check(healthy(tmp_path))
    assert report.problems == []
    assert report.files == ["README.md", "docs/README.md", "docs/guide.md", "docs/ops/runbook.md"]
    assert (report.links, report.anchors) == (4, 2)


def test_a_broken_relative_file_link_is_caught(tmp_path: Path) -> None:
    healthy(tmp_path)
    (tmp_path / "docs" / "guide.md").write_text(
        "# Guide\n\nSee [the old page](setup.md) and [the index](README.md).\n", encoding="utf-8"
    )
    assert problems(tmp_path) == ["docs/guide.md:3: setup.md: no such file"]


def test_a_missing_heading_anchor_is_caught(tmp_path: Path) -> None:
    healthy(tmp_path)
    (tmp_path / "docs" / "README.md").write_text(
        "# Index\n\n- [Guide](guide.md#installation)\n- [Runbook](ops/runbook.md)\n",
        encoding="utf-8",
    )
    assert problems(tmp_path) == [
        "docs/README.md:3: guide.md#installation: no heading or id 'installation'"
    ]


def test_a_missing_anchor_in_the_same_file_is_caught(tmp_path: Path) -> None:
    healthy(tmp_path)
    (tmp_path / "docs" / "guide.md").write_text(
        "# Guide\n\n## Setup\n\nAbove: [setup](#set-up).\n", encoding="utf-8"
    )
    assert problems(tmp_path) == ["docs/guide.md:5: #set-up: no heading or id 'set-up'"]


def test_an_unreachable_document_is_caught(tmp_path: Path) -> None:
    healthy(tmp_path, {"docs/orphan.md": "# Orphan\n\nLinks out to [the guide](guide.md).\n"})
    assert problems(tmp_path) == ["docs/orphan.md:0: not reachable from docs/README.md"]


def test_a_document_linked_only_from_the_root_readme_is_still_unreachable(tmp_path: Path) -> None:
    healthy(tmp_path, {"docs/orphan.md": "# Orphan\n"})
    (tmp_path / "README.md").write_text(
        "# Project\n\n[Docs](docs/README.md), [orphan](docs/orphan.md).\n", encoding="utf-8"
    )
    assert problems(tmp_path) == ["docs/orphan.md:0: not reachable from docs/README.md"]


def test_a_document_reached_through_another_document_is_reachable(tmp_path: Path) -> None:
    healthy(tmp_path, {"docs/ops/deep.md": "# Deep\n"})
    (tmp_path / "docs" / "ops" / "runbook.md").write_text(
        "# Runbook\n\n## 1. Backup\n\n## 2. Restore\n\nSee [deep](deep.md).\n", encoding="utf-8"
    )
    assert problems(tmp_path) == []


def test_a_missing_index_is_caught(tmp_path: Path) -> None:
    healthy(tmp_path)
    (tmp_path / "docs" / "README.md").unlink()
    found = problems(tmp_path)
    assert "docs/README.md:0: the documentation index is missing" in found
    assert "README.md:3: docs/README.md: no such file" in found


def test_a_link_spelled_in_the_wrong_case_is_caught(tmp_path: Path) -> None:
    """A case-insensitive file system would find the file; Linux and GitHub would not."""
    healthy(tmp_path)
    (tmp_path / "docs" / "README.md").write_text(
        "# Index\n\n- [Guide](Guide.md)\n- [Runbook](ops/runbook.md)\n- [g](guide.md)\n",
        encoding="utf-8",
    )
    assert problems(tmp_path) == ["docs/README.md:3: Guide.md: no such file"]


def test_a_link_out_of_the_repository_is_caught(tmp_path: Path) -> None:
    healthy(tmp_path)
    (tmp_path / "README.md").write_text(
        "# Project\n\n[Docs](docs/README.md) and [outside](../elsewhere.md).\n", encoding="utf-8"
    )
    assert problems(tmp_path) == ["README.md:3: ../elsewhere.md: leaves the repository"]


def test_reference_definitions_html_links_and_images_are_checked(tmp_path: Path) -> None:
    healthy(tmp_path)
    (tmp_path / "docs" / "guide.md").write_text(
        "# Guide\n\n[index]: README.md\n[gone]: gone.md\n"
        '<a href="missing.md">x</a> <img src="pic.png"> ![shot](shot.png)\n',
        encoding="utf-8",
    )
    assert problems(tmp_path) == [
        "docs/guide.md:4: gone.md: no such file",
        "docs/guide.md:5: shot.png: no such file",
        "docs/guide.md:5: missing.md: no such file",
        "docs/guide.md:5: pic.png: no such file",
    ]


def test_an_image_inside_a_link_is_checked(tmp_path: Path) -> None:
    healthy(tmp_path)
    (tmp_path / "README.md").write_text(
        "# Project\n\n[![badge](badge.svg)](docs/README.md)\n", encoding="utf-8"
    )
    assert problems(tmp_path) == ["README.md:3: badge.svg: no such file"]


def test_links_inside_code_are_not_checked(tmp_path: Path) -> None:
    healthy(tmp_path)
    (tmp_path / "docs" / "guide.md").write_text(
        "# Guide\n\nRun `cat [x](nope.md)` first.\n\n```markdown\n[example](nope.md)\n```\n\n"
        "~~~\n[also](nope.md#none)\n~~~\n\nThen [back](README.md).\n",
        encoding="utf-8",
    )
    assert problems(tmp_path) == []


def test_external_links_are_not_checked_or_fetched(tmp_path: Path) -> None:
    healthy(tmp_path)
    (tmp_path / "docs" / "guide.md").write_text(
        "# Guide\n\n[a](https://example.invalid/x.md#nope) [b](mailto:someone@example.invalid) "
        "[c](//example.invalid/y) <https://example.invalid>\n",
        encoding="utf-8",
    )
    report = doc_links.check(tmp_path)
    assert report.problems == []
    assert report.links == 3  # the index's two, the root README's one; none of the external


def test_an_explicit_html_anchor_resolves(tmp_path: Path) -> None:
    healthy(tmp_path)
    (tmp_path / "docs" / "guide.md").write_text(
        '# Guide\n\n<a id="custom-spot"></a>\nText. [Here](#custom-spot), [there](#elsewhere).\n',
        encoding="utf-8",
    )
    assert problems(tmp_path) == ["docs/guide.md:4: #elsewhere: no heading or id 'elsewhere'"]


def test_anchors_into_other_files_must_be_line_anchors(tmp_path: Path) -> None:
    healthy(tmp_path, {"src/app.py": "print('x')\n"})
    (tmp_path / "docs" / "guide.md").write_text(
        "# Guide\n\n[l](../src/app.py#L1) [r](../src/app.py#L1-L3) [f](../src/app.py#main)\n",
        encoding="utf-8",
    )
    assert problems(tmp_path) == [
        "docs/guide.md:3: ../src/app.py#main: 'main' is not a line anchor"
    ]


def test_an_unlinked_file_under_docs_is_caught(tmp_path: Path) -> None:
    healthy(tmp_path, {"docs/screenshots/old.png": "not really a png", "docs/new.png": "png"})
    (tmp_path / "README.md").write_text(
        "# Project\n\n[Docs](docs/README.md) ![new](docs/new.png)\n", encoding="utf-8"
    )
    assert problems(tmp_path) == [
        "docs/screenshots/old.png:0: not linked from any documentation file"
    ]


def test_percent_encoded_and_titled_links_resolve(tmp_path: Path) -> None:
    healthy(tmp_path, {"docs/two words.md": "# Two words\n"})
    (tmp_path / "docs" / "README.md").write_text(
        '# Index\n\n- [Guide](guide.md "The guide")\n- [Runbook](<ops/runbook.md>)\n'
        "- [Two](two%20words.md)\n",
        encoding="utf-8",
    )
    assert problems(tmp_path) == []


def test_the_command_exits_non_zero_on_a_problem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = healthy(tmp_path)
    assert doc_links.main(["--root", str(root)]) == 0
    assert capsys.readouterr().out.endswith("4 files, 4 relative links, 2 anchors: OK\n")
    (root / "docs" / "guide.md").write_text("# Guide\n\n[x](nowhere.md)\n", encoding="utf-8")
    assert doc_links.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "docs/guide.md:3: nowhere.md: no such file" in out and "1 problem(s)" in out


# --- GitHub's heading anchors --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("3.5 Relevance grades", "35-relevance-grades"),
        (
            "12.6 Source hashing, CI artifacts and the baseline",
            "126-source-hashing-ci-artifacts-and-the-baseline",
        ),
        (
            "§4.6 A2 trigger correction — done (2026-11-24)",
            "46-a2-trigger-correction--done-2026-11-24",
        ),
        ("3.7 `booking_rooms`", "37-booking_rooms"),
        ("**Bold** and _emphasis_ and snake_case", "bold-and-emphasis-and-snake_case"),
        ("A [linked](x.md) heading", "a-linked-heading"),
        ("What the intelligence layer is, and is not", "what-the-intelligence-layer-is-and-is-not"),
        ("Ελληνικά Κεφάλαια", "ελληνικά-κεφάλαια"),
    ],
)
def test_github_slugs(heading: str, slug: str) -> None:
    assert doc_links.github_slug(heading) == slug


def test_duplicate_headings_are_numbered_and_setext_headings_count() -> None:
    text = "# Notes\n\n## Notes\n\n## Notes\n\nSetext title\n============\n\nTable\n| a |\n|---|\n"
    assert doc_links.anchors(text) == {"notes", "notes-1", "notes-2", "setext-title"}


def test_headings_inside_code_are_not_anchors() -> None:
    assert doc_links.anchors("# Real\n\n```\n# Not a heading\n```\n") == {"real"}
