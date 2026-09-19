"""The acceptance policy: declared here, in code, before any result is computed.

A validation stage that decides its own pass mark after seeing the numbers has not validated
anything. So the policy is a frozen constant in this module, it is committed, it carries its own
checksum, and the function that applies it is pure.

## The structural guarantee

:class:`AcceptanceEvidence` is the **only** thing :func:`evaluate_acceptance` can see, and it
carries no metric value and no comparison. There is no MAE in it, no RMSE, no sMAPE and no
learned-minus-baseline difference -- only counts, versions, checksums and booleans. The
acceptance decision therefore *cannot* depend on which method scored better, because that number
is not among its inputs. A test asserts the field list, and another test perturbs every metric
in the evaluation and requires the acceptance result to be unchanged.

That is deliberate, and it is why there is no criterion reading "the learned model must beat the
baseline". Such a criterion would make acceptance a competition result rather than a statement
about whether the measurement is trustworthy, and on 54 folds with a per-fold spread of roughly
plus or minus fifty room nights it would be a coin flip dressed as a gate. What this policy asks
is narrower and answerable: *was the thing measured, measured properly?*

## Versioning

``ACCEPTANCE_POLICY_VERSION`` changes whenever any threshold changes. The registry records both
the version and the policy's content checksum, so a later reader can tell whether a recorded
PASS was issued under the same rules they are reading.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from ml.manifests import serialise
from ml.models import MODEL_VERSION
from ml.pipelines.offline_demand import sha256_hex

#: Bump this whenever a threshold below changes. The registry records it next to every result.
ACCEPTANCE_POLICY_VERSION = "acceptance_v1"

#: The Stage 6.2 dataset this policy was written against, by content rather than by name.
REQUIRED_DATASET_SHA256 = "904b819f84f252350ad3387db60fa8fb18f56771447b2c369f3741f65f57095d"


class PolicyError(Exception):
    """The policy was applied to evidence it does not describe."""


@dataclass(frozen=True, slots=True)
class AcceptancePolicy:
    """Every threshold, stated once, before the run.

    The defaults are the declared policy. They are not tuned: each is either a property the
    protocol must have by construction (a version, a checksum, a horizon) or a floor chosen for
    a reason given in :data:`CRITERION_RATIONALE`.
    """

    version: str = ACCEPTANCE_POLICY_VERSION

    #: Counts. Floors, not targets.
    minimum_paired_observations: int = 500
    minimum_folds: int = 40
    maximum_skipped_predictions: int = 0
    maximum_incomplete_windows: int = 1
    minimum_hotels_with_sufficient_coverage: int = 2

    #: Identity. These are equalities: a different dataset or contract is a different question.
    required_dataset_version: str = "v1"
    required_feature_version: str = "v1"
    required_dataset_sha256: str = REQUIRED_DATASET_SHA256
    required_forecast_horizon_days: int = 7
    required_model_version: str = MODEL_VERSION

    #: Properties of the run rather than of the numbers.
    require_deterministic_execution: bool = True
    require_leakage_checks: bool = True
    require_all_metrics: bool = True

    def as_dict(self) -> dict[str, object]:
        return dict(sorted(asdict(self).items()))

    def checksum(self) -> str:
        """sha256 over the serialised thresholds. Changes the moment any of them changes."""
        return sha256_hex(serialise(self.as_dict()))


#: Why each criterion is where it is. Carried into the registry so a reader never has to guess
#: whether a number was reasoned about or reached for.
CRITERION_RATIONALE: dict[str, str] = {
    "minimum_paired_observations": (
        "500 paired hotel-days. Below roughly a year of two-hotel coverage the pooled metrics "
        "describe a season rather than a year."
    ),
    "minimum_folds": (
        "40 origins. Fewer, and a single unusual week moves the pooled figure enough that the "
        "backtest is measuring that week."
    ),
    "maximum_skipped_predictions": (
        "zero. A method that declines to forecast some days and is scored only on the rest is "
        "being graded on a set it chose; if any day is skipped the result must be read "
        "differently, so it fails the gate rather than being averaged over."
    ),
    "maximum_incomplete_windows": (
        "one. The final evaluation window is truncated whenever the remaining dates are not a "
        "multiple of the step, which is arithmetic rather than a defect. A second incomplete "
        "window would mean something else is wrong."
    ),
    "minimum_hotels_with_sufficient_coverage": (
        "two, which is every hotel in the dataset. This is a completeness check, NOT a "
        "generalisation claim: see the model card."
    ),
    "required_dataset_version": "identity: a different contract is a different question.",
    "required_feature_version": "identity: the feature contract must be the one measured.",
    "required_dataset_sha256": (
        "identity by content rather than by name. A dataset that hashes differently is a "
        "different dataset whatever it is called."
    ),
    "required_forecast_horizon_days": (
        "seven, the Stage 6.3 horizon. A different horizon admits a different feature set and "
        "produces numbers that are not comparable."
    ),
    "required_model_version": (
        "the candidate under validation. A mismatch must stop the run rather than silently "
        "validate something else."
    ),
    "require_deterministic_execution": (
        "two full evaluations of the same bytes must agree exactly, predictions and content "
        "checksum alike. Without this nothing else in the record can be reproduced."
    ),
    "require_leakage_checks": (
        "every fold's chronology, every window's disjointness and every selected feature's lead "
        "time, re-checked on this run rather than inherited from the last one."
    ),
    "require_all_metrics": (
        "MAE, RMSE and sMAPE must all be present for both methods. A missing metric is a "
        "measurement that did not happen, not a zero."
    ),
}

#: Deliberately absent, and named so that the absence is visible rather than merely true.
EXCLUDED_CRITERIA: dict[str, str] = {
    "learned_model_beats_baseline": (
        "NOT a criterion. Acceptance here asks whether the measurement is trustworthy, not "
        "which method won. The learned model leads on a bare majority of folds with a pooled "
        "gap far smaller than the fold-to-fold spread, so such a gate would encode a coin "
        "flip. It is also structurally impossible: AcceptanceEvidence carries no metric value."
    ),
    "metric_threshold": (
        "NOT a criterion. No MAE, RMSE or sMAPE ceiling is declared, because no operational "
        "requirement exists to derive one from. A number invented here would become a "
        "'target accuracy' the moment it was written down."
    ),
}

ACCEPTANCE_POLICY = AcceptancePolicy()


@dataclass(frozen=True, slots=True)
class AcceptanceEvidence:
    """Everything the policy is allowed to look at -- and nothing else.

    No MAE. No RMSE. No sMAPE. No learned-minus-baseline difference. Adding one would be a
    change to what acceptance *means*, so the field list is asserted by test.
    """

    paired_observations: int
    folds: int
    baseline_skipped: int
    learned_skipped: int
    incomplete_windows: int
    hotels_with_sufficient_coverage: int
    dataset_version: str
    feature_version: str
    dataset_sha256: str
    forecast_horizon_days: int
    model_version: str
    deterministic: bool
    leakage_checks_passed: bool
    metrics_present: bool

    def as_dict(self) -> dict[str, object]:
        return dict(sorted(asdict(self).items()))


#: Field names that must never appear on :class:`AcceptanceEvidence`. Checked by test rather
#: than trusted: the guarantee is only worth as much as the thing that enforces it.
FORBIDDEN_EVIDENCE_FRAGMENTS: tuple[str, ...] = (
    "mae",
    "rmse",
    "smape",
    "error",
    "difference",
    "comparison",
    "winner",
    "better",
    "beats",
    "score",
)


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    """One criterion, what it required, what was observed, and whether that satisfied it."""

    name: str
    requirement: str
    observed: object
    passed: bool
    rationale: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "criterion": self.name,
            "requirement": self.requirement,
            "observed": self.observed,
            "passed": self.passed,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    """The outcome, with every criterion kept whether it passed or not."""

    policy_version: str
    policy_sha256: str
    checks: tuple[AcceptanceCheck, ...]
    evidence: AcceptanceEvidence
    excluded_criteria: dict[str, str] = field(default_factory=lambda: dict(EXCLUDED_CRITERIA))

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failed(self) -> tuple[AcceptanceCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "policy_sha256": self.policy_sha256,
            "result": "PASS" if self.passed else "FAIL",
            "criteria_total": len(self.checks),
            "criteria_passed": sum(1 for check in self.checks if check.passed),
            "criteria_failed": [check.name for check in self.failed],
            "checks": [check.as_dict() for check in self.checks],
            "evidence": self.evidence.as_dict(),
            "excluded_criteria": dict(self.excluded_criteria),
        }


def _at_least(name: str, observed: int, floor: int) -> AcceptanceCheck:
    return AcceptanceCheck(
        name=name,
        requirement=f">= {floor}",
        observed=observed,
        passed=observed >= floor,
        rationale=CRITERION_RATIONALE.get(name, ""),
    )


def _at_most(name: str, observed: int, ceiling: int) -> AcceptanceCheck:
    return AcceptanceCheck(
        name=name,
        requirement=f"<= {ceiling}",
        observed=observed,
        passed=observed <= ceiling,
        rationale=CRITERION_RATIONALE.get(name, ""),
    )


def _equals(name: str, observed: object, expected: object) -> AcceptanceCheck:
    return AcceptanceCheck(
        name=name,
        requirement=f"== {expected!r}",
        observed=observed,
        passed=observed == expected,
        rationale=CRITERION_RATIONALE.get(name, ""),
    )


def evaluate_acceptance(policy: AcceptancePolicy, evidence: AcceptanceEvidence) -> AcceptanceResult:
    """Apply the policy. Pure: same policy plus same evidence gives the same result, always.

    Every criterion is evaluated and kept, including the ones that passed, so the record shows
    what was asked rather than only what went wrong.
    """
    checks: list[AcceptanceCheck] = [
        _at_least(
            "minimum_paired_observations",
            evidence.paired_observations,
            policy.minimum_paired_observations,
        ),
        _at_least("minimum_folds", evidence.folds, policy.minimum_folds),
        _at_most(
            "maximum_skipped_predictions",
            max(evidence.baseline_skipped, evidence.learned_skipped),
            policy.maximum_skipped_predictions,
        ),
        _at_most(
            "maximum_incomplete_windows",
            evidence.incomplete_windows,
            policy.maximum_incomplete_windows,
        ),
        _at_least(
            "minimum_hotels_with_sufficient_coverage",
            evidence.hotels_with_sufficient_coverage,
            policy.minimum_hotels_with_sufficient_coverage,
        ),
        _equals(
            "required_dataset_version",
            evidence.dataset_version,
            policy.required_dataset_version,
        ),
        _equals(
            "required_feature_version",
            evidence.feature_version,
            policy.required_feature_version,
        ),
        _equals("required_dataset_sha256", evidence.dataset_sha256, policy.required_dataset_sha256),
        _equals(
            "required_forecast_horizon_days",
            evidence.forecast_horizon_days,
            policy.required_forecast_horizon_days,
        ),
        _equals("required_model_version", evidence.model_version, policy.required_model_version),
        _equals(
            "require_deterministic_execution",
            evidence.deterministic,
            policy.require_deterministic_execution,
        ),
        _equals(
            "require_leakage_checks",
            evidence.leakage_checks_passed,
            policy.require_leakage_checks,
        ),
        _equals("require_all_metrics", evidence.metrics_present, policy.require_all_metrics),
    ]
    return AcceptanceResult(
        policy_version=policy.version,
        policy_sha256=policy.checksum(),
        checks=tuple(checks),
        evidence=evidence,
    )


def criteria_names(policy: AcceptancePolicy = ACCEPTANCE_POLICY) -> Sequence[str]:
    """Every criterion the policy will evaluate, without needing evidence to find out."""
    return tuple(check.name for check in evaluate_acceptance(policy, _NULL_EVIDENCE).checks)


#: Only used to enumerate criterion names. Deliberately failing, so it cannot be mistaken for a
#: default that might slip into a real result.
_NULL_EVIDENCE = AcceptanceEvidence(
    paired_observations=0,
    folds=0,
    baseline_skipped=1,
    learned_skipped=1,
    incomplete_windows=99,
    hotels_with_sufficient_coverage=0,
    dataset_version="",
    feature_version="",
    dataset_sha256="",
    forecast_horizon_days=0,
    model_version="",
    deterministic=False,
    leakage_checks_passed=False,
    metrics_present=False,
)
