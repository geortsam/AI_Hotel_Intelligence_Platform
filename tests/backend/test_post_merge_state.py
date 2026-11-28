"""After PR #1's merge, the documents say what is true now and keep what was true then.

PR #1 was merged on 2026-11-28 as ``eb0a54b``, a merge commit of ``2a14dc6`` and the tested head
``9102904``. GitHub reads ``.github/dependabot.yml`` only from the default branch, so the merge
activated Dependabot. Documents describing the current state must no longer call ``main``
``2a14dc6``, PR #1 open, or Dependabot inert. The dated audits keep what they recorded at the time
and point to the addendum that states the current state:

* the README, the walkthrough and the reproducibility guide describe Dependabot as active;
* the closure audit has its §16 addendum, its gap register no longer calls PR #1 unmerged, and
  its strict figure is the current one, with the audited figure kept beside it;
* the completion audit counts R0.4a as in effect, so strict completion is 55 / 71.

``test_decision_records.py`` holds the decision register itself.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPOSITORY_ROOT / "docs"
MERGE = "eb0a54b"

#: Documents that describe the current state, not a dated audit.
CURRENT_STATE = (
    REPOSITORY_ROOT / "README.md",
    DOCS / "demo-walkthrough.md",
    DOCS / "deployment" / "reproducibility.md",
)


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\n[ \t]*>", "\n", text))


def read(path: Path) -> str:
    return flat(path.read_text(encoding="utf-8"))


def test_no_current_document_calls_dependabot_inert_or_absent() -> None:
    for path in CURRENT_STATE:
        text = read(path)
        assert "Dependabot" in text, path
        for stale in (
            "inert until it reaches `main`",
            "exists but is inert",
            "and it has not reached it",
            "no Renovate and no Dependabot run",
            "There is no Renovate, no Dependabot",
        ):
            assert stale not in text, (path, stale)


def test_the_readme_says_dependabot_proposes_and_nothing_applies() -> None:
    text = read(REPOSITORY_ROOT / "README.md")
    assert "Dependabot configuration (active on `main` since the merge of 2026-11-28)" in text
    assert "- **Dependabot proposes pin updates; nothing applies them.**" in text
    assert "it became active when PR #1 was merged on 2026-11-28" in text
    assert "fails the retrieval-pin gate until it is re-measured and re-pinned (§12.6" in text


def test_the_reproducibility_guide_states_what_it_does_not_claim_as_it_now_is() -> None:
    text = read(DOCS / "deployment" / "reproducibility.md")
    assert "No SBOM is generated" not in text
    assert "Transitive Python dependencies still float" not in text
    assert "CI generates CycloneDX SBOMs of both builds of each image" in text
    assert "nothing merges them automatically" in text


def test_the_closure_audit_records_the_merge_in_an_addendum() -> None:
    text = read(DOCS / "project-closure-audit.md")
    assert "## 16. Addendum: PR #1 merged and Dependabot active (2026-11-28)" in text
    assert f"`main` is now `{MERGE}`, and Dependabot is active on it." in text
    assert "PR #1 is unmerged" not in text
    assert f"| G16 | Nothing is released: PR #1 is merged (`main` is `{MERGE}`" in text
    # The figure at the audit is kept beside the current one, not overwritten.
    assert "**55 / 71 = 77.5%** (54 / 71 = 76.1% at `15a29d1`" in text
    # The audited repository state stays as it was checked on 2026-11-25.
    assert "| `main` | `2a14dc6852784a51934d0aee3543ba49f9e2fd90`, unchanged |" in text
    assert "@@" not in text


def test_the_completion_audit_counts_r04a_as_in_effect() -> None:
    text = read(DOCS / "project-completion-audit.md")
    assert "- **Strict** (R0.2 not counted as done): 55 of 71 = 77.5% since 2026-11-28" in text
    assert "R0.4a (config tested; inert until on `main`" not in text
    assert "| 1. Implemented and verified | R0.3, R0.4a (active on `main`" in text
    assert "Dependabot still inert" not in text
    assert "@@" not in text
