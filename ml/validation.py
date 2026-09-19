"""Robustness, regime and error analysis for a rolling-origin backtest.

Stage 6.3 produced one pooled number per metric per method. This module asks the questions that
a pooled number cannot answer:

* **How much does it move between origins?** Mean, median, population standard deviation,
  minimum and maximum of the per-fold metric, for both methods.
* **Where does it move?** The same metrics grouped by month, by calendar quarter and by hotel,
  with an explicit insufficient-coverage answer where a group is too thin to score.
* **Which days go wrong?** The largest absolute errors for each method, kept exactly as they
  are -- not clipped, not winsorised, not dropped.

Everything here is **descriptive**. Nothing ranks the methods, nothing labels a regime good or
bad, and nothing feeds back into the model: the error records exist to be read, and a
configuration changed in response to them would make the next evaluation a re-fit of this one.

The stability guards at the top are the other half of the stage. A validation run must refuse an
input it was not written for -- a different feature order, a different model version -- rather
than quietly producing a number about something else.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from app.ml.dataset import build_feature_specs
from ml.evaluation import (
    MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC,
    EvaluationResult,
    Prediction,
    comparison,
    pooled_metrics,
)
from ml.models import MODEL_VERSION, feature_lead_days

#: Bump when the shape of the validation report changes.
VALIDATION_VERSION = "validation_v1"

#: How many worst-error observations to keep per method. Ten is enough to see whether the
#: failures cluster and few enough that the record stays readable.
DEFAULT_ERROR_SAMPLE = 10

METHODS: tuple[str, ...] = ("baseline", "learned")
METRICS: tuple[str, ...] = ("mae", "rmse", "smape")

#: The Stage 6.1 contract's column order. The dataset is expected to present its features in
#: exactly this order, because the design matrix is built positionally from it.
CANONICAL_FEATURE_ORDER: tuple[str, ...] = tuple(spec.name for spec in build_feature_specs())


class ValidationError(Exception):
    """The run was asked to validate something other than what it was written for."""


# --- stability guards ---------------------------------------------------------------------------


def assert_canonical_feature_order(names: Sequence[str]) -> None:
    """Refuse a feature list that is not the contract's, in the contract's order.

    Rejection rather than silent re-ordering, and the distinction matters: the design matrix is
    built positionally, so a permuted column list would train a model on the same numbers under
    different names and produce a result that looks entirely normal. Sorting it here would hide
    the fact that the dataset changed.
    """
    given = tuple(names)
    if given == CANONICAL_FEATURE_ORDER:
        return
    if sorted(given) == sorted(CANONICAL_FEATURE_ORDER):
        raise ValidationError(
            "the dataset's feature columns are the contract's but in a different order; "
            f"expected {list(CANONICAL_FEATURE_ORDER)}, got {list(given)}"
        )
    missing = [name for name in CANONICAL_FEATURE_ORDER if name not in given]
    unexpected = [name for name in given if name not in CANONICAL_FEATURE_ORDER]
    raise ValidationError(
        "the dataset's feature columns are not the Stage 6.1 contract: "
        f"missing {missing}, unexpected {unexpected}"
    )


def assert_model_version(requested: str) -> None:
    """Refuse to validate a model version this code does not implement."""
    if requested != MODEL_VERSION:
        raise ValidationError(
            f"this code implements {MODEL_VERSION}, not {requested!r}; a version mismatch must "
            "stop the run rather than validate a different candidate under the wrong name"
        )


# --- fold stability -------------------------------------------------------------------------------


def _metric_of(result: EvaluationResult, method: str, metric: str) -> list[float]:
    if method not in METHODS:
        raise ValidationError(f"unknown method {method!r}")
    if metric not in METRICS:
        raise ValidationError(f"unknown metric {metric!r}")
    values: list[float] = []
    for fold in result.folds:
        measured = fold.baseline if method == "baseline" else fold.learned
        value = getattr(measured, metric)
        if value is not None:
            values.append(float(value))
    return values


def dispersion(values: Sequence[float], *, digits: int = 6) -> dict[str, object]:
    """Five numbers and a count, or an explicit statement that there were none.

    The standard deviation is the **population** one: these 54 folds are every fold the protocol
    generates over this dataset, not a sample drawn from a larger pool, so there is no
    population to estimate and no reason to apply Bessel's correction.
    """
    if not values:
        return {"folds": 0, "mean": None, "median": None, "stdev": None, "min": None, "max": None}
    ordered = sorted(values)
    return {
        "folds": len(values),
        "mean": round(statistics.fmean(values), digits),
        "median": round(statistics.median(ordered), digits),
        "stdev": round(statistics.pstdev(values), digits) if len(values) > 1 else 0.0,
        "min": round(ordered[0], digits),
        "max": round(ordered[-1], digits),
    }


def fold_stability(result: EvaluationResult) -> dict[str, object]:
    """Per-fold dispersion of every metric, for both methods."""
    return {
        "stdev_definition": (
            "population standard deviation over the evaluated folds; these folds are the whole "
            "population the protocol produces, not a sample from a larger one"
        ),
        "methods": {
            method: {metric: dispersion(_metric_of(result, method, metric)) for metric in METRICS}
            for method in METHODS
        },
    }


def fold_win_counts(result: EvaluationResult) -> dict[str, object]:
    """How many folds each method scored lower on -- a count, not a verdict.

    "Lower" and not "better": these are error metrics, so lower is smaller error, and the
    counting stops there. Folds where either metric is unavailable are reported as not
    comparable rather than being assigned to one side.
    """
    out: dict[str, object] = {}
    for metric in METRICS:
        learned_lower = baseline_lower = ties = not_comparable = 0
        for fold in result.folds:
            left = getattr(fold.learned, metric)
            right = getattr(fold.baseline, metric)
            if left is None or right is None:
                not_comparable += 1
            elif left < right:
                learned_lower += 1
            elif left > right:
                baseline_lower += 1
            else:
                ties += 1
        out[metric] = {
            "folds": len(result.folds),
            "learned_lower": learned_lower,
            "baseline_lower": baseline_lower,
            "ties": ties,
            "not_comparable": not_comparable,
        }
    return out


def fold_boundaries(result: EvaluationResult) -> tuple[dict[str, object], ...]:
    """The identity of every fold, reduced to what must not drift between runs."""
    return tuple(
        {
            "fold": fold.fold.index,
            "origin": fold.fold.origin.isoformat(),
            "train_start": fold.fold.train_start.isoformat(),
            "train_end": fold.fold.train_end.isoformat(),
            "evaluation_start": fold.fold.evaluation_start.isoformat(),
            "evaluation_end": fold.fold.evaluation_end.isoformat(),
            "train_rows": fold.fold.train_rows,
            "evaluation_rows": fold.fold.evaluation_rows,
        }
        for fold in result.folds
    )


def assert_fold_boundaries_match(
    result: EvaluationResult, recorded: Sequence[Mapping[str, object]]
) -> None:
    """Require this run's folds to be the folds the committed record describes.

    The protocol is supposed to be a function of the dataset and the policy alone. If it is,
    re-running it a stage later reproduces the same 54 windows; if it is not, every comparison
    against the earlier record is meaningless, and that must surface as a failure rather than as
    a slightly different table.
    """
    current = fold_boundaries(result)
    if len(current) != len(recorded):
        raise ValidationError(
            f"fold count changed: the record has {len(recorded)}, this run produced {len(current)}"
        )
    for produced, stored in zip(current, recorded, strict=True):
        for key, value in produced.items():
            if stored.get(key) != value:
                raise ValidationError(
                    f"fold {produced['fold']} differs on {key}: record has "
                    f"{stored.get(key)!r}, this run produced {value!r}"
                )


# --- regime analysis ------------------------------------------------------------------------------


def _group_block(group: Sequence[Prediction], minimum: int) -> dict[str, object]:
    evaluated = sum(1 for p in group if p.baseline is not None or p.learned is not None)
    if evaluated < minimum:
        return {
            "sufficient_coverage": False,
            "observations": len(group),
            "evaluated_observations": evaluated,
            "minimum_required": minimum,
            "reason": "insufficient coverage; no metric is reported for this group",
        }
    return {
        "sufficient_coverage": True,
        "observations": len(group),
        "baseline": pooled_metrics(group, "baseline").as_dict(),
        "learned": pooled_metrics(group, "learned").as_dict(),
        "comparison": comparison(group),
    }


def regime_metrics(
    predictions: Sequence[Prediction],
    key: Callable[[Prediction], str],
    *,
    minimum: int = MINIMUM_OBSERVATIONS_FOR_GROUP_METRIC,
) -> dict[str, object]:
    """Group the predictions and score each group, or say why a group was not scored.

    Grouping only. No group is called good or bad, and nothing here infers that a pattern seen
    in one month of one year would recur.
    """
    groups: dict[str, list[Prediction]] = {}
    for prediction in predictions:
        groups.setdefault(key(prediction), []).append(prediction)
    return {label: _group_block(groups[label], minimum) for label in sorted(groups)}


def month_key(prediction: Prediction) -> str:
    return f"{prediction.target_date.month:02d}"


def quarter_key(prediction: Prediction) -> str:
    """Calendar quarters, not meteorological seasons.

    A season label would carry a hemisphere assumption into a record that is supposed to state
    only what was measured; the hotels happen to be northern-hemisphere, and the next dataset
    may not be.
    """
    return f"Q{(prediction.target_date.month - 1) // 3 + 1}"


def year_month_key(prediction: Prediction) -> str:
    return prediction.target_date.strftime("%Y-%m")


def hotel_key(prediction: Prediction) -> str:
    return prediction.hotel_key


# --- error analysis ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    """One badly forecast day, kept whole.

    ``signed_error`` is ``forecast - actual``: positive means the method predicted more demand
    than the hotel saw. Stating the direction in the field's definition rather than leaving it
    to a reader's assumption is the point of having the field at all.
    """

    method: str
    hotel_key: str
    target_date: dt.date
    actual: int
    prediction: float
    absolute_error: float
    signed_error: float
    fold_index: int
    origin: dt.date

    def as_dict(self, *, digits: int = 6) -> dict[str, object]:
        return {
            "method": self.method,
            "hotel_key": self.hotel_key,
            "target_date": self.target_date.isoformat(),
            "actual": self.actual,
            "prediction": round(self.prediction, digits),
            "absolute_error": round(self.absolute_error, digits),
            "signed_error": round(self.signed_error, digits),
            "fold": self.fold_index,
            "origin": self.origin.isoformat(),
        }


def largest_errors(
    predictions: Sequence[Prediction],
    method: str,
    origins: Mapping[int, dt.date],
    *,
    limit: int = DEFAULT_ERROR_SAMPLE,
) -> tuple[ErrorRecord, ...]:
    """The worst *limit* days for one method, in a deterministic order.

    Ties are broken by date then hotel, so two runs cannot disagree about which of two equally
    bad days is listed first. Nothing is removed from the dataset and nothing is clipped: these
    records are a diagnosis, and a diagnosis that edits the patient is not one.
    """
    if method not in METHODS:
        raise ValidationError(f"unknown method {method!r}")
    if limit < 1:
        raise ValidationError(f"limit must be at least 1, got {limit}")

    records: list[ErrorRecord] = []
    for prediction in predictions:
        value = prediction.baseline if method == "baseline" else prediction.learned
        if value is None:
            continue
        signed = value - float(prediction.actual)
        records.append(
            ErrorRecord(
                method=method,
                hotel_key=prediction.hotel_key,
                target_date=prediction.target_date,
                actual=prediction.actual,
                prediction=value,
                absolute_error=abs(signed),
                signed_error=signed,
                fold_index=prediction.fold_index,
                origin=origins[prediction.fold_index],
            )
        )
    records.sort(key=lambda r: (-r.absolute_error, r.target_date, r.hotel_key))
    return tuple(records[:limit])


def origins_by_fold(result: EvaluationResult) -> dict[int, dt.date]:
    return {fold.fold.index: fold.fold.origin for fold in result.folds}


def error_analysis(
    result: EvaluationResult, *, limit: int = DEFAULT_ERROR_SAMPLE
) -> dict[str, object]:
    origins = origins_by_fold(result)
    return {
        "sample_size": limit,
        "ordering": "absolute error descending, then target date, then hotel key",
        "signed_error_definition": (
            "forecast - actual; positive means the method predicted more demand than was realised"
        ),
        "treatment": (
            "diagnostic only; no observation was removed, clipped, winsorised or altered, and "
            "no model configuration was changed in response"
        ),
        "methods": {
            method: [
                record.as_dict()
                for record in largest_errors(result.predictions, method, origins, limit=limit)
            ]
            for method in METHODS
        },
    }


# --- leakage, re-checked on this run -----------------------------------------------------------


def leakage_report(result: EvaluationResult) -> dict[str, object]:
    """Re-run the structural leakage checks and record what they found.

    Inherited assurance is not assurance. Every claim below is computed from the folds this run
    produced, not copied from the Stage 6.3 record.
    """
    chronological = all(
        fold.fold.train_end <= fold.fold.origin < fold.fold.evaluation_start
        for fold in result.folds
    )
    windows_within_horizon = all(
        fold.fold.evaluation_end <= fold.fold.origin + dt.timedelta(days=result.policy.horizon_days)
        for fold in result.folds
    )
    keys = [(p.hotel_key, p.target_date) for p in result.predictions]
    no_repeated_observation = len(keys) == len(set(keys))
    horizon = result.policy.horizon_days
    admissible = all(
        (feature_lead_days(name) is None) or (feature_lead_days(name) or 0) >= horizon
        for name in result.selection.selected
    )
    expanding = [fold.fold.train_rows for fold in result.folds] == sorted(
        fold.fold.train_rows for fold in result.folds
    )
    checks = {
        "train_end_at_or_before_origin_before_evaluation_start": chronological,
        "evaluation_window_within_horizon": windows_within_horizon,
        "no_hotel_day_evaluated_twice": no_repeated_observation,
        "every_selected_feature_knowable_at_the_horizon": admissible,
        "training_window_never_contracts": expanding,
        "no_shuffle_or_random_split": not result.policy.as_dict()["shuffle"],
    }
    return {"passed": all(checks.values()), "checks": checks}


# --- the report --------------------------------------------------------------------------------


def build_validation_report(
    result: EvaluationResult,
    *,
    error_sample: int = DEFAULT_ERROR_SAMPLE,
) -> dict[str, object]:
    """Everything Stage 6.4 measured, with nothing summarised away."""
    return {
        "validation_version": VALIDATION_VERSION,
        "purpose": (
            "descriptive robustness analysis of the Stage 6.3 backtest; no method is ranked, no "
            "regime is labelled good or bad, and no model configuration was changed"
        ),
        "folds": {
            "count": len(result.folds),
            "skipped": [fold.as_dict() for fold in result.skipped_folds],
            "incomplete_windows": [
                fold.fold.index for fold in result.folds if not fold.fold.complete_window
            ],
            "boundaries": list(fold_boundaries(result)),
        },
        "stability": fold_stability(result),
        "fold_comparison_counts": fold_win_counts(result),
        "regimes": {
            "month": regime_metrics(result.predictions, month_key),
            "quarter": regime_metrics(result.predictions, quarter_key),
            "year_month": regime_metrics(result.predictions, year_month_key),
            "hotel": regime_metrics(result.predictions, hotel_key),
        },
        "errors": error_analysis(result, limit=error_sample),
        "leakage": leakage_report(result),
        "cross_hotel_claim": (
            "NONE. Both hotels appear in the training data at every origin; there is no "
            "held-out hotel and, with two, there could not be a meaningful one. The per-hotel "
            "numbers say the result is not driven by one of them, and say nothing about a third."
        ),
    }
