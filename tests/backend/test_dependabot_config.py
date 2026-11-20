"""R0.4a: the prepared Dependabot configuration covers every dependency manifest.

GitHub reads `.github/dependabot.yml` only from the default branch, so on a feature branch nothing
exercises it. These tests check what can be checked offline: it parses, it names only directories
that hold the ecosystem's manifest, every manifest in the repository is covered, and the cadence
is the one the decision brief recommends (weekly, grouped, a few pull requests at a time).
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPOSITORY_ROOT / ".github" / "dependabot.yml"

# The file each ecosystem looks for in its directory.
MANIFEST: dict[str, tuple[str, ...]] = {
    "pip": ("requirements.txt", "requirements-ml.txt"),
    "npm": ("package.json",),
    "docker": ("Dockerfile",),
    "docker-compose": ("docker-compose.yml",),
    "github-actions": (".github/workflows",),
}

# Directories that hold installed or generated files, never a manifest of this repository.
NOT_SOURCE = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        "coverage",
    }
)


@pytest.fixture(scope="module")
def updates() -> list[dict[str, Any]]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert config["version"] == 2
    return list(config["updates"])


def source_files() -> list[PurePosixPath]:
    """Every file in the working tree outside installed and generated directories.

    A walk rather than `git ls-files`, so the scan also works in the mutation harness's copy,
    which holds the tracked files but is not a checkout.
    """
    found: list[PurePosixPath] = []
    for top, dirs, files in os.walk(REPOSITORY_ROOT):
        dirs[:] = [d for d in dirs if d not in NOT_SOURCE]
        relative = PurePosixPath(Path(top).relative_to(REPOSITORY_ROOT).as_posix())
        found.extend(relative / name for name in files)
    return found


def directory(entry: dict[str, Any]) -> Path:
    return REPOSITORY_ROOT / str(entry["directory"]).lstrip("/")


def covered(updates: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(e["package-ecosystem"], str(e["directory"]).rstrip("/") or "/") for e in updates}


def as_directory(path: PurePosixPath) -> str:
    parent = str(path.parent)
    return "/" if parent == "." else f"/{parent}"


def test_every_entry_points_at_its_ecosystems_manifest(updates: list[dict[str, Any]]) -> None:
    for entry in updates:
        names = MANIFEST[entry["package-ecosystem"]]
        assert any((directory(entry) / name).exists() for name in names), entry


def test_every_manifest_in_the_repository_is_covered(updates: list[dict[str, Any]]) -> None:
    files = source_files()
    needed = {("pip", as_directory(p)) for p in files if p.name in MANIFEST["pip"]}
    needed |= {("npm", as_directory(p)) for p in files if p.name == "package.json"}
    needed |= {("docker", as_directory(p)) for p in files if p.name == "Dockerfile"}
    needed |= {("docker-compose", as_directory(p)) for p in files if p.name == "docker-compose.yml"}
    if any(str(p).startswith(".github/workflows/") for p in files):
        needed.add(("github-actions", "/"))
    assert needed, "no manifests found; the scan is broken"
    assert needed <= covered(updates), sorted(needed - covered(updates))


def test_no_ecosystem_and_directory_is_listed_twice(updates: list[dict[str, Any]]) -> None:
    assert len(covered(updates)) == len(updates)


def test_updates_are_weekly_grouped_and_limited(updates: list[dict[str, Any]]) -> None:
    for entry in updates:
        assert entry["schedule"]["interval"] == "weekly", entry
        assert entry.get("groups"), entry
        assert 1 <= int(entry["open-pull-requests-limit"]) <= 5, entry


def test_it_targets_the_default_branch(updates: list[dict[str, Any]]) -> None:
    """A `target-branch` would aim updates at a branch other than the one CI protects."""
    assert all("target-branch" not in entry for entry in updates)
