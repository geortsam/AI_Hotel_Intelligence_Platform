"""Current-state statements that the code once falsified stay corrected (M2).

Four statements described the system as it was before a later stage changed it:

* ``docs/copilot-frontend.md`` §8 said no route reports in advance whether the copilot is on --
  ``GET /api/v1/`` has reported ``copilot_enabled`` since the V2 remediation pass;
* ``README.md`` said the served demand model has no front-end surface, and
* ``docs/ml-serving.md`` §9 item 7 said the same of its endpoints -- the Analytics page and the
  forecast-performance section have called both since the analytics reporting view;
* ``backend/app/core/config.py``'s module docstring said ``secret_key`` is unread because there is
  no authentication -- it signs every access token.

Each is checked two ways. The stale sentence must be absent; and the corrected text must name a
fact that is checked against the code, so the correction cannot drift the other way either.
Matching ignores line wrapping, because prose is re-wrapped freely; positive controls run the
matcher over each original wrapped sentence, so an absence check can never pass by matching
nothing.

``docs/ml-serving.md`` keeps "No production accuracy is established", which four other tests pin.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.core.security import require_secret
from app.schemas.meta import ApiMetaResponse

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COPILOT_FRONTEND = REPOSITORY_ROOT / "docs" / "copilot-frontend.md"
README = REPOSITORY_ROOT / "README.md"
ML_SERVING = REPOSITORY_ROOT / "docs" / "ml-serving.md"
CONFIG = REPOSITORY_ROOT / "backend" / "app" / "core" / "config.py"
ML_SERVICE = REPOSITORY_ROOT / "frontend" / "src" / "services" / "ml" / "mlService.ts"
ANALYTICS_PAGE = REPOSITORY_ROOT / "frontend" / "src" / "pages" / "AnalyticsPage.tsx"


def flat(text: str) -> str:
    """*text* with every run of whitespace -- line breaks and indentation included -- as one
    space, so a sentence is found however it is wrapped."""
    return re.sub(r"\s+", " ", text)


def states(text: str, claim: str) -> bool:
    return flat(claim) in flat(text)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def serving_item_seven() -> str:
    """§9 item 7 of ml-serving.md, up to item 8."""
    text = read(ML_SERVING)
    start = text.index("7. **")
    return text[start : text.index("8. **", start)]


#: The stale claims, as they were written, and where they were. Each must stay absent.
STALE = [
    (COPILOT_FRONTEND, "No route says in advance whether the copilot is on"),
    (README, "and with no front-end surface"),
    (ML_SERVING, "7. **No frontend surface.**"),
    (ML_SERVING, "Still no frontend."),
    (CONFIG, "``secret_key`` remains declared but unread -- there is no authentication yet."),
]

#: The original sentences, wrapped exactly as they were in the files, for the positive controls.
ORIGINALS = [
    "- **`LLM_DISABLED` is learned by asking.** No route says in advance whether the copilot is"
    " on, and\n  by the dependency order a question to a disabled deployment is still charged",
    "sits beside it — served, but **not scientifically validated**, and with no front-end\n"
    "surface — as",
    "7. **No frontend surface.** Nothing in `frontend/` was touched; the endpoint is independently",
    "router — `GET .../ml/demand-predictions`, which reads stored predictions and scores nothing.\n"
    "   Still no frontend. See",
    "never built. ``secret_key`` remains declared but unread -- there is no\nauthentication yet.",
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


# ======================================================================================
# The corrected statements match the code
# ======================================================================================


def test_the_copilot_screen_documents_the_capability_the_api_reports() -> None:
    text = read(COPILOT_FRONTEND)
    assert states(text, "`GET /api/v1/` reports `copilot_enabled`")
    assert "copilot_enabled" in ApiMetaResponse.model_fields
    # What is still true is still said: the unreadable case, and the charge it costs.
    assert states(text, "Only when that answer cannot be read (`unknown`) is a question offered")
    assert states(text, "still charged to the hourly allowance")


def test_the_served_model_s_frontend_callers_are_the_ones_the_frontend_has() -> None:
    item = serving_item_seven()
    service = read(ML_SERVICE)
    for route in ("ml/demand-forecast", "ml/demand-predictions"):
        assert f"GET .../{route}" in item, route
        assert f"/{route}`" in service, route
    assert "`frontend/src/services/ml/mlService.ts`" in item
    assert "No production accuracy is established" in read(ML_SERVING)


def test_the_readme_puts_the_model_where_the_page_shows_it() -> None:
    assert states(read(README), "shown on the Analytics page with its limitations")
    assert "import { DemandForecastPanel }" in read(ANALYTICS_PAGE)


def test_the_settings_docstring_says_what_the_secret_is_for() -> None:
    docstring = read(CONFIG).split('"""')[1]
    assert states(docstring, "``secret_key`` signs and verifies authentication tokens")
    assert "require_secret" in docstring
    assert "settings.secret_key" in inspect.getsource(require_secret)
