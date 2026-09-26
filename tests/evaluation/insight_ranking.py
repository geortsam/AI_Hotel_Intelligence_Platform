"""Runs `insight_ranking_v1` over the frozen `demand_daily_v1` dataset (Stage 7.12).

The protocol -- `app.ml.ranking_protocol` -- was declared and checksummed before this ran. This
module applies it and nothing else:

    for each hotel, for each origin:
        platform   `app.ml.timeseries.forecast_series` over the TRAINING_DAYS days ending at the
                   origin, scoring each of the next HORIZON_DAYS days -- the function the
                   attention list itself calls
        baseline   last week's realised demand for the same weekday, as known at the origin
        truth      each upcoming day's realised room nights
        score      precision@K and NDCG@K for each method, on the same windows

It lives beside the evaluation harness rather than in `ml/`, because the offline package may
not import the running application, and the method measured must be the application's own
function -- not a copy of it. It reads the dataset through `ml.loading` and refuses to run on
any file whose digest is not the one the protocol names.

`python -m tests.evaluation.insight_ranking --write-report` regenerates
`insight_ranking_report.json`; the test suite recomputes it and compares, so a change to the
method, the dataset or the scorers is a visible change to a pinned file.

**What the report is not**: evidence that either method helps run a hotel. It describes how two
rankings of upcoming days line up with what then happened, on two hotels' historical data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.ml.ranking_protocol import PROTOCOL, RankingProtocol
from app.ml.timeseries import Observation, forecast_series
from ml.loading import DEFAULT_DATASET, load_processed_dataset

HERE = Path(__file__).resolve().parent
REPORT = HERE / "insight_ranking_report.json"

STATEMENT = (
    "Ranking behaviour of two deterministic methods on the frozen demand_daily_v1 dataset (two "
    "hotels' historical daily demand), under insight_ranking_v1. It describes how each method's "
    "ranking of upcoming days lined up with the realised busiest days on this data, and nothing "
    "more: no winner is declared, no significance is tested, and nothing is claimed about hotel "
    "operations, revenue or business value."
)


# --- the scorers: independent of the service, pure functions of rankings -------------------------


def ranked(scores: Mapping[dt.date, float]) -> list[dt.date]:
    """Days by score descending, then date ascending -- the protocol's tie-break."""
    return sorted(scores, key=lambda day: (-scores[day], day))


def precision_at_k(predicted: Sequence[dt.date], ideal: Sequence[dt.date], k: int) -> float:
    return len(set(predicted[:k]) & set(ideal[:k])) / k


def dcg_at_k(order: Sequence[dt.date], gains: Mapping[dt.date, float], k: int) -> float:
    return sum(gains[day] / math.log2(position + 2) for position, day in enumerate(order[:k]))


def ndcg_at_k(
    predicted: Sequence[dt.date], ideal: Sequence[dt.date], gains: Mapping[dt.date, float], k: int
) -> float | None:
    ideal_dcg = dcg_at_k(ideal, gains, k)
    if ideal_dcg <= 0:
        return None
    return dcg_at_k(predicted, gains, k) / ideal_dcg


# --- the methods ---------------------------------------------------------------------------------


def platform_scores(
    history: Mapping[dt.date, int], origin: dt.date, horizon: Sequence[dt.date], training_days: int
) -> dict[dt.date, float] | None:
    """The platform's forecast for each horizon day, or None when it cannot score them all."""
    observations = [
        Observation(date=day, value=Decimal(value))
        for day, value in sorted(history.items())
        if origin - dt.timedelta(days=training_days) < day <= origin
    ]
    points = forecast_series(observations, list(horizon))
    if any(point.value is None for point in points):
        return None
    return {point.date: float(point.value) for point in points if point.value is not None}


def baseline_scores(
    history: Mapping[dt.date, int], origin: dt.date, horizon: Sequence[dt.date]
) -> dict[dt.date, float] | None:
    """Last week's realised demand for each day's weekday, as known at the origin."""
    scores: dict[dt.date, float] = {}
    for day in horizon:
        known = origin - dt.timedelta(days=(origin.weekday() - day.weekday()) % 7)
        if known not in history:
            return None
        scores[day] = float(history[known])
    return scores


# --- one run -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Window:
    hotel: str
    origin: dt.date
    skipped: str | None
    platform_precision: float | None = None
    platform_ndcg: float | None = None
    baseline_precision: float | None = None
    baseline_ndcg: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "hotel": self.hotel,
            "origin": self.origin.isoformat(),
            "skipped": self.skipped,
            "platform": {
                "precision_at_k": _r(self.platform_precision),
                "ndcg_at_k": _r(self.platform_ndcg),
            },
            "baseline": {
                "precision_at_k": _r(self.baseline_precision),
                "ndcg_at_k": _r(self.baseline_ndcg),
            },
        }


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def origins(dates: Sequence[dt.date], protocol: RankingProtocol) -> list[dt.date]:
    first, last = min(dates), max(dates)
    origin = first + dt.timedelta(days=protocol.training_days - 1)
    found: list[dt.date] = []
    while origin + dt.timedelta(days=protocol.horizon_days) <= last:
        found.append(origin)
        origin += dt.timedelta(days=protocol.step_days)
    return found


def score_window(
    hotel: str, history: Mapping[dt.date, int], origin: dt.date, protocol: RankingProtocol
) -> Window:
    horizon = [origin + dt.timedelta(days=n) for n in range(1, protocol.horizon_days + 1)]
    if any(day not in history for day in horizon):
        return Window(hotel, origin, "missing_realised_day")
    platform = platform_scores(history, origin, horizon, protocol.training_days)
    if platform is None:
        return Window(hotel, origin, "platform_cannot_score")
    baseline = baseline_scores(history, origin, horizon)
    if baseline is None:
        return Window(hotel, origin, "baseline_cannot_score")
    gains = {day: float(history[day]) for day in horizon}
    ideal = ranked(gains)
    platform_ndcg = ndcg_at_k(ranked(platform), ideal, gains, protocol.k)
    baseline_ndcg = ndcg_at_k(ranked(baseline), ideal, gains, protocol.k)
    if platform_ndcg is None or baseline_ndcg is None:
        return Window(hotel, origin, "ideal_dcg_zero")
    return Window(
        hotel,
        origin,
        None,
        platform_precision=precision_at_k(ranked(platform), ideal, protocol.k),
        platform_ndcg=platform_ndcg,
        baseline_precision=precision_at_k(ranked(baseline), ideal, protocol.k),
        baseline_ndcg=baseline_ndcg,
    )


def summarise(windows: Sequence[Window]) -> dict[str, Any]:
    scored = [w for w in windows if w.skipped is None]
    skipped: dict[str, int] = {}
    for window in windows:
        if window.skipped is not None:
            skipped[window.skipped] = skipped.get(window.skipped, 0) + 1
    summary: dict[str, Any] = {
        "windows": len(windows),
        "scored": len(scored),
        "skipped": dict(sorted(skipped.items())),
    }
    for metric, platform_field, baseline_field in (
        ("precision_at_k", "platform_precision", "baseline_precision"),
        ("ndcg_at_k", "platform_ndcg", "baseline_ndcg"),
    ):
        platform = [float(getattr(w, platform_field)) for w in scored]
        baseline = [float(getattr(w, baseline_field)) for w in scored]
        summary[metric] = {
            "platform_mean": _r(sum(platform) / len(platform)) if scored else None,
            "baseline_mean": _r(sum(baseline) / len(baseline)) if scored else None,
            "platform_higher": sum(1 for p, b in zip(platform, baseline, strict=True) if p > b),
            "platform_lower": sum(1 for p, b in zip(platform, baseline, strict=True) if p < b),
            "equal": sum(1 for p, b in zip(platform, baseline, strict=True) if p == b),
        }
    return summary


def run(protocol: RankingProtocol = PROTOCOL, path: Path = DEFAULT_DATASET) -> dict[str, Any]:
    dataset = load_processed_dataset(path)
    if dataset.sha256 != protocol.dataset_sha256:
        raise ValueError(f"{path.name} is not the dataset {protocol.identity} names")
    histories: dict[str, dict[dt.date, int]] = {}
    for row in dataset.rows:
        histories.setdefault(row.hotel_key, {})[row.target_date] = row.target_room_nights

    windows: list[Window] = []
    for hotel in sorted(histories):
        history = histories[hotel]
        for origin in origins(sorted(history), protocol):
            windows.append(score_window(hotel, history, origin, protocol))

    return {
        "protocol": {"identity": protocol.identity, "checksum": protocol.checksum},
        "dataset": {"id": protocol.dataset_id, "sha256": dataset.sha256, "rows": len(dataset.rows)},
        "k": protocol.k,
        "horizon_days": protocol.horizon_days,
        "methods": {"platform": protocol.platform_method, "baseline": protocol.baseline_method},
        "statement": STATEMENT,
        "pooled": summarise(windows),
        "per_hotel": {
            hotel: summarise([w for w in windows if w.hotel == hotel])
            for hotel in sorted(histories)
        },
        "windows": [window.as_dict() for window in windows],
    }


def canonical(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:  # pragma: no cover - a maintenance entry point
    parser = argparse.ArgumentParser(description="Recompute the insight_ranking_v1 report.")
    parser.add_argument("--write-report", action="store_true")
    options = parser.parse_args()
    report = canonical(run())
    if options.write_report:
        REPORT.write_text(report, encoding="utf-8", newline="\n")
        print(f"wrote tests/evaluation/{REPORT.name}")
    else:
        print(report)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "REPORT",
    "STATEMENT",
    "baseline_scores",
    "canonical",
    "dcg_at_k",
    "ndcg_at_k",
    "origins",
    "platform_scores",
    "precision_at_k",
    "ranked",
    "run",
    "score_window",
    "summarise",
]
