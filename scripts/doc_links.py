"""Relative links and anchors in the documentation, checked (A8, roadmap R6.1).

The documentation is the root ``README.md``, the database README (``database/README.md``, which
since R6.1 holds the schema's design and implementation record) and every Markdown file under
``docs/``; its entry point is ``docs/README.md``. Three things are checked, offline and with the
standard library only:

1. **Links resolve.** Every relative link -- an inline link or image, a reference definition, an
   HTML ``href`` or ``src`` -- names a file or directory that exists, spelled with the exact case
   on disk (a Windows checkout would otherwise pass a link Linux and GitHub break), inside the
   repository.
2. **Anchors resolve.** A ``#fragment`` into a Markdown file names one of its headings, slugged as
   GitHub slugs them (duplicates numbered ``-1``, ``-2`` ...), or an explicit HTML ``id`` or
   ``name``. A fragment into any other file must be a GitHub line anchor (``#L10``,
   ``#L10-L20``).
3. **Every document is reachable.** Following links from ``docs/README.md`` reaches every Markdown
   file under ``docs/``, and every other file under ``docs/`` (the screenshots) is linked from
   some checked file.

What is not a link is not checked: fenced code blocks and inline code spans are blanked first,
so a path quoted as code (`` `docs/x.md` ``) or a link written inside an example is left alone.
External links -- anything with a URL scheme (``https:``, ``mailto:``) or starting ``//`` -- are
not fetched; this check never touches the network.

    python -m scripts.doc_links          # from the repository root; exit 1 on any problem
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import unquote

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]

#: The documentation's single entry point.
INDEX: Final = "docs/README.md"
ROOT_README: Final = "README.md"
DOCS_DIR: Final = "docs"
#: Maintained documents outside docs/, checked like the root README. Not subject to the
#: reachability rule, which is about docs/ only; the index links them anyway.
MAINTAINED_OUTSIDE_DOCS: Final = ("database/README.md",)

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(.+?)\1")
_INLINE_LINK = re.compile(
    r"!?\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(\s*(<[^>]*>|[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)
_REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
_HTML_TARGET = re.compile(r"<(?:a|img|source)\b[^>]*?\s(?:href|src)\s*=\s*\"([^\"]*)\"", re.I)
_HTML_ID = re.compile(r"<[a-zA-Z][^>]*?\s(?:id|name)\s*=\s*\"([^\"]+)\"")
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_LINE_ANCHOR = re.compile(r"^L\d+(?:C\d+)?(?:-L\d+(?:C\d+)?)?$")


@dataclass(frozen=True)
class Link:
    source: str  # repository-relative POSIX path of the file the link is in
    line: int
    target: str  # as written, angle brackets removed


@dataclass(frozen=True)
class Problem:
    source: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.source}:{self.line}: {self.message}"


# --- reading Markdown --------------------------------------------------------------------------


def prose_lines(text: str) -> list[str]:
    """*text*'s lines with fenced code blocks and inline code spans blanked, numbering kept."""
    lines = text.splitlines()
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        if fence is None:
            opened = _FENCE.match(line)
            if opened:
                fence = opened.group(1)
                out.append("")
                continue
            out.append(_INLINE_CODE.sub(lambda m: " " * len(m.group(0)), line))
        else:
            closing = _FENCE.match(line)
            if (
                closing
                and closing.group(1)[0] == fence[0]
                and len(closing.group(1)) >= len(fence)
                and not line.strip()[len(closing.group(1)) :].strip()
            ):
                fence = None
            out.append("")
    return out


def _unbracket(target: str) -> str:
    target = target.strip()
    if target.startswith("<") and target.endswith(">"):
        return target[1:-1].strip()
    return target


def _inline_targets(line: str) -> Iterator[str]:
    """Inline link and image targets in *line*, an image inside a link's text included."""
    for match in _INLINE_LINK.finditer(line):
        yield from _inline_targets(match.group(1))
        yield _unbracket(match.group(2))


def links(source: str, text: str) -> list[Link]:
    """Every link target written in *text* outside code."""
    found: list[Link] = []
    for number, line in enumerate(prose_lines(text), start=1):
        definition = _REFERENCE_DEFINITION.match(line)
        if definition:
            found.append(Link(source, number, _unbracket(definition.group(1))))
        for target in _inline_targets(line):
            found.append(Link(source, number, target))
        for match in _HTML_TARGET.finditer(line):
            found.append(Link(source, number, match.group(1).strip()))
    return found


def _heading_text(raw: str) -> str:
    """A heading's rendered text: link and image syntax, HTML tags and emphasis removed."""
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", raw)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("`", "")
    return re.sub(r"(?<![^\W_])_+|_+(?![^\W_])", "", text)


def github_slug(heading: str) -> str:
    """The anchor GitHub gives a heading whose text is *heading* (before de-duplication).

    Lower-cased; every character that is not a letter, mark, number, connector punctuation
    (``_``), space or hyphen removed; each space then a hyphen. So ``3.5 Relevance grades``
    becomes ``35-relevance-grades`` and ``§4.6 A2 — done`` becomes ``46-a2--done``.
    """
    kept = []
    for character in _heading_text(heading).strip().lower():
        category = unicodedata.category(character)
        if category[0] in "LMN" or category == "Pc" or character in " -":
            kept.append(character)
    return "".join(kept).replace(" ", "-")


def anchors(text: str) -> set[str]:
    """Every fragment a link into *text* may name: heading slugs and explicit HTML ids."""
    found: set[str] = set()
    seen: dict[str, int] = {}

    def add_heading(raw: str) -> None:
        slug = github_slug(raw)
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")

    lines = prose_lines(text)
    raw_lines = text.splitlines()
    for index, line in enumerate(lines):
        heading = _ATX_HEADING.match(line)
        if heading:
            # Headings are slugged from the written text, code spans included.
            raw = _ATX_HEADING.match(raw_lines[index])
            add_heading((raw.group(2) if raw else heading.group(2)) or "")
        elif (
            index > 0
            and _SETEXT_UNDERLINE.match(line)
            and lines[index - 1].strip()
            and raw_lines[index - 1].strip()
            and not _ATX_HEADING.match(lines[index - 1])
            and not lines[index - 1].lstrip().startswith(("|", "-", "*", "+", ">"))
        ):
            add_heading(raw_lines[index - 1].strip())
        for match in _HTML_ID.finditer(line):
            found.add(match.group(1))
    return found


# --- the repository ----------------------------------------------------------------------------


def documentation_files(root: Path) -> list[str]:
    """The checked files: the root README, the maintained documents outside docs/, and every
    Markdown file under docs/, sorted."""
    files = [ROOT_README] if (root / ROOT_README).is_file() else []
    files += [name for name in MAINTAINED_OUTSIDE_DOCS if (root / name).is_file()]
    docs = root / DOCS_DIR
    if docs.is_dir():
        files += sorted(
            path.relative_to(root).as_posix() for path in docs.rglob("*.md") if path.is_file()
        )
    return files


def _docs_assets(root: Path) -> list[str]:
    docs = root / DOCS_DIR
    if not docs.is_dir():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in docs.rglob("*")
        if path.is_file() and path.suffix != ".md"
    )


def _exists_exactly(root: Path, relative: PurePosixPath) -> bool:
    """Whether *relative* exists under *root* with every component's case exactly as written."""
    current = root
    for part in relative.parts:
        try:
            names = os.listdir(current)
        except (NotADirectoryError, FileNotFoundError):
            return False
        if part not in names:
            return False
        current = current / part
    return True


def _resolve(source: str, path: str) -> PurePosixPath | None:
    """*path*, written in *source*, as a repository-relative path; None if it leaves the root."""
    parts: list[str] = []
    for part in (PurePosixPath(source).parent / path).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


def split_target(target: str) -> tuple[str, str | None]:
    """A relative target's path and fragment, percent-decoded; the query dropped."""
    path, hash_mark, fragment = target.partition("#")
    path = path.split("?", 1)[0]
    return unquote(path), (unquote(fragment) if hash_mark else None)


def is_external(target: str) -> bool:
    return bool(_SCHEME.match(target)) or target.startswith("//")


@dataclass
class Report:
    files: list[str]
    links: int
    anchors: int
    problems: list[Problem]


def check(root: Path = REPOSITORY_ROOT) -> Report:
    """Every problem with the documentation under *root*."""
    root = root.resolve()
    files = documentation_files(root)
    texts = {name: (root / name).read_text(encoding="utf-8") for name in files}
    anchor_cache: dict[str, set[str]] = {}
    problems: list[Problem] = []
    graph: dict[str, set[str]] = {name: set() for name in files}
    referenced: set[str] = set()
    relative_links = 0
    anchor_links = 0

    def anchors_of(name: str) -> set[str]:
        if name not in anchor_cache:
            text = texts.get(name)
            if text is None:
                text = (root / name).read_text(encoding="utf-8")
            anchor_cache[name] = anchors(text)
        return anchor_cache[name]

    for name in files:
        for link in links(name, texts[name]):
            if not link.target or is_external(link.target):
                continue
            relative_links += 1
            path, fragment = split_target(link.target)
            if path:
                resolved = _resolve(name, path)
                if resolved is None:
                    problems.append(
                        Problem(name, link.line, f"{link.target}: leaves the repository")
                    )
                    continue
                if not _exists_exactly(root, resolved):
                    problems.append(Problem(name, link.line, f"{link.target}: no such file"))
                    continue
                target = resolved.as_posix()
            else:
                target = name
            referenced.add(target)
            if target.endswith(".md") and (root / target).is_file():
                graph[name].add(target)
            if fragment is None:
                continue
            anchor_links += 1
            if (root / target).is_dir():
                problems.append(
                    Problem(name, link.line, f"{link.target}: a directory has no anchors")
                )
            elif target.endswith(".md"):
                if fragment not in anchors_of(target):
                    problems.append(
                        Problem(name, link.line, f"{link.target}: no heading or id '{fragment}'")
                    )
            elif not _LINE_ANCHOR.match(fragment):
                problems.append(
                    Problem(name, link.line, f"{link.target}: '{fragment}' is not a line anchor")
                )

    problems += reachability_problems(root, files, graph)
    for asset in _docs_assets(root):
        if asset not in referenced:
            problems.append(Problem(asset, 0, "not linked from any documentation file"))
    return Report(files, relative_links, anchor_links, problems)


def reachable(start: str, graph: dict[str, set[str]]) -> set[str]:
    seen = {start}
    queue = deque([start])
    while queue:
        for target in sorted(graph.get(queue.popleft(), ())):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def reachability_problems(
    root: Path, files: Sequence[str], graph: dict[str, set[str]]
) -> list[Problem]:
    if not (root / INDEX).is_file():
        return [Problem(INDEX, 0, "the documentation index is missing")]
    seen = reachable(INDEX, graph)
    return [
        Problem(name, 0, f"not reachable from {INDEX}")
        for name in files
        if name.startswith(DOCS_DIR + "/") and name not in seen
    ]


def _format(problems: Iterable[Problem]) -> Iterator[str]:
    for problem in problems:
        yield str(problem)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    report = check(args.root)
    for line in _format(report.problems):
        print(line)
    print(
        f"{len(report.files)} files, {report.links} relative links, {report.anchors} anchors: "
        + ("OK" if not report.problems else f"{len(report.problems)} problem(s)")
    )
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
