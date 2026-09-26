"""`insight_ranking_v1`: how the attention list's day ranking is measured. Declared first.

Stage 7.12. The attention list (`GET …/intelligence/priorities`) ranks the upcoming days of one
hotel by the platform's deterministic seasonal-naive forecast and lists the busiest `K`. This
module fixes -- before any measurement is run, and frozen by a checksum afterwards -- exactly how
that ranking is compared with a simpler one on the frozen offline dataset. The service reads its
horizon, training window and `K` from here, so what is served is what is measured.

## The two methods

    platform   the day-of-week median forecast of `app.ml.timeseries.forecast_series` -- the
               same function every intelligence endpoint uses -- over the TRAINING_DAYS days
               ending at the origin
    baseline   "popularity": each upcoming day scored by the realised demand of the most recent
               day with the same weekday ON OR BEFORE the origin -- last week's demand for that
               weekday, as it was known at the origin (never a day after it)

## The task, the metrics and the tie-breaks

For every origin and every hotel separately, the HORIZON_DAYS days after the origin are ranked by
each method (score descending, then date ascending). The realised busiest `K` days, ranked by
realised room nights descending then date ascending, are the ideal. Then:

    precision@K   |method's top K  ∩  realised top K| / K
    NDCG@K        DCG@K / IDCG@K, gain = realised room nights (linear), discount log2(rank + 1)

## Which windows are scored

A window is scored only when every one of its HORIZON_DAYS days has a realised value in the
dataset AND both methods produce a score for every day, AND its ideal DCG is above zero. Every
other window is counted as skipped, with its reason, and excluded from both methods alike -- so
the two methods are always compared on exactly the same windows.

## The comparison rule

For each hotel and pooled over both: the mean of each metric over scored windows, per method,
side by side; and for each metric the number of windows in which the platform method scored
higher, lower, or the same as the baseline. **Nothing more is concluded**: no threshold, no
winner, no significance test, and no claim about hotel operations or business value. The result
describes ranking behaviour on this frozen dataset only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from app.ml.timeseries import MODEL_NAME

#: The frozen offline dataset: two hotels, one row per hotel and date, committed in Stage 6.3.
DATASET_ID = "demand_daily_v1"
DATASET_SHA256 = "904b819f84f252350ad3387db60fa8fb18f56771447b2c369f3741f65f57095d"

#: History the platform method learns from: the days ending at the origin (or, when served, at
#: the observation window's last day).
TRAINING_DAYS = 90
#: Upcoming days ranked in one window, starting the day after the origin.
HORIZON_DAYS = 14
#: Origins advance by one horizon, so evaluation windows tile and no day is scored twice.
STEP_DAYS = 14
#: How many of the busiest upcoming days are listed and scored.
K = 3

BASELINE_METHOD = "last-week-same-weekday"


@dataclass(frozen=True, slots=True)
class RankingProtocol:
    protocol_id: str
    version: str
    dataset_id: str
    dataset_sha256: str
    training_days: int
    horizon_days: int
    step_days: int
    k: int
    platform_method: str
    baseline_method: str
    metrics: tuple[str, ...]
    gain: str
    tie_break: str
    first_origin_rule: str
    skip_rule: str
    comparison_rule: str

    @property
    def identity(self) -> str:
        return f"{self.protocol_id}_{self.version}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def checksum(self) -> str:
        """SHA-256 over compact, key-sorted JSON -- the shape every protocol checksum here uses."""
        canonical = json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=list
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


PROTOCOL = RankingProtocol(
    protocol_id="insight_ranking",
    version="v1",
    dataset_id=DATASET_ID,
    dataset_sha256=DATASET_SHA256,
    training_days=TRAINING_DAYS,
    horizon_days=HORIZON_DAYS,
    step_days=STEP_DAYS,
    k=K,
    platform_method=MODEL_NAME,
    baseline_method=BASELINE_METHOD,
    metrics=("precision_at_k", "ndcg_at_k"),
    gain="realised room nights, linear; discount log2(rank + 1)",
    tie_break="score descending, then date ascending; the ideal ranks realised values the same way",
    first_origin_rule=(
        "per hotel: the first dataset date with TRAINING_DAYS days of calendar history at or "
        "before it; then every STEP_DAYS days while origin + HORIZON_DAYS is a dataset date"
    ),
    skip_rule=(
        "a window is scored only if all HORIZON_DAYS days have a realised value, both methods "
        "score every day, and the ideal DCG is above zero; otherwise it is skipped for both "
        "methods, with its reason counted"
    ),
    comparison_rule=(
        "per hotel and pooled: the mean of each metric over scored windows for each method, side "
        "by side, and per metric the count of windows where the platform scored higher, lower "
        "or equal; no threshold, no winner, no significance test, no business-value claim"
    ),
)


__all__ = [
    "BASELINE_METHOD",
    "DATASET_ID",
    "DATASET_SHA256",
    "HORIZON_DAYS",
    "PROTOCOL",
    "STEP_DAYS",
    "TRAINING_DAYS",
    "K",
    "RankingProtocol",
]
