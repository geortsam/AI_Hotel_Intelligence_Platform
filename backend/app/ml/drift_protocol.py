"""The observation protocol: declared here, in code, before any distribution is computed.

Stage 6.10. Same discipline as Stage 6.9's accuracy protocol, and for the same reason: a summary
whose conventions are chosen after the numbers are visible has summarised nothing. Every rule
this stage applies is a frozen constant in this module, it is committed, and it carries a
deterministic content checksum reported with every result.

## Observation, not detection

This stage reports numbers and movement between two windows. It **detects nothing**. There is no
threshold, no verdict, no alert, no anomaly flag and no composite statistic -- no PSI, no KS, no
Jensen-Shannon -- in this protocol or anywhere this stage reaches, and a test asserts the field
list rather than trusting the prose.

That is a deliberate deferral, not an oversight. Stage 6.8 §9 wrote the reason down: "Choosing a
drift statistic and what to do when it moves is a later stage's decision, and making it now --
before there is a single row to look at -- would be guessing." A fresh deployment still has no
rows, so the argument has not expired. What *has* changed since then is that the distributions
themselves can now be looked at, which is what this stage provides and where it stops.

## The reference is a window, not the training set

The baseline is an **explicitly supplied second window of stored predictions**, never the Stage
6.2 training distribution. That answers "have the inputs moved *since*" rather than "moved *away
from what the model was fitted on*", and the difference is worth stating plainly.

The training reference was considered and rejected on evidence: the committed manifest
``ml/manifests/demand_daily_v1.json`` carries ``target_statistics`` and no per-feature
statistics at all, and the production image
ships neither ``ml/manifests/`` nor ``ml/data/``. An absolute reference would therefore need a
new committed artifact and a Dockerfile change. A window-versus-window reference needs neither,
and every number it produces comes from rows this platform actually served.

## The quantile convention, written out

One method, declared: **linear interpolation between order statistics** -- the convention the
standard library calls ``inclusive`` and R calls type 7.

For a sorted series of ``n`` values and a probability ``p``::

    h  = (n - 1) * p
    lo = floor(h)
    hi = ceil(h)
    q  = xs[lo] + (h - lo) * (xs[hi] - xs[lo])

It is implemented explicitly in :mod:`app.ml.drift` rather than delegated, so the convention is
visible in the code that applies it; a test cross-checks it against ``statistics.quantiles`` with
``method="inclusive"`` so the two can never quietly disagree. Under this convention the median is
exactly the ``p50`` quantile, for odd and even ``n`` alike, and a test pins that too.

## Empty and single-observation series

An empty series reports ``count = 0`` and **``None`` for every statistic**, never ``0.0``. Zero
movement and nothing observed are different claims, and only one of them is good news -- the same
rule ``ml/metrics.py`` already applies to an empty metric set.

A single observation is summarised without error: minimum, maximum, mean, median and every
quantile are that one value.

## Segmentation is Stage 6.9's, imported

``below_calibration`` and ``within_calibration``, split at the forty room nights Stage 6.6
measured to sit below the model's lowest learned bin edge. This module **imports** that boundary
from :mod:`app.ml.accuracy_protocol` rather than restating it: a second copy of the number is a
second definition that eventually disagrees with the first.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from typing import Any

from app.ml.accuracy_protocol import (
    SEGMENT_BELOW_CALIBRATION,
    SEGMENT_FEATURES,
    SEGMENT_WITHIN_CALIBRATION,
    SMALL_HOTEL_MAX_LAG_ROOM_NIGHTS,
)
from app.ml.serving import APPROVED_MODEL

#: Bump whenever any value in :data:`PROTOCOL` changes. Reported with every result.
PROTOCOL_VERSION = "distribution_v1"

#: What is summarised: the model's own nine features, in its own column order, then the output.
#:
#: Taken from ``APPROVED_MODEL.feature_columns`` rather than typed out, so a model whose columns
#: changed could not leave this list quietly describing the previous one.
OBSERVED_FIELDS: tuple[str, ...] = (*APPROVED_MODEL.feature_columns, "predicted_room_nights")

#: The descriptive statistics reported for every observed field. Exactly these, in this order.
SUMMARY_STATISTICS: tuple[str, ...] = ("count", "minimum", "maximum", "mean", "median")

#: The quantiles reported for every observed field, as (label, probability).
#:
#: Five, covering both tails: movement in a distribution's shoulders is exactly what a mean and a
#: median together will hide.
QUANTILES: tuple[tuple[str, float], ...] = (
    ("p05", 0.05),
    ("p25", 0.25),
    ("p50", 0.50),
    ("p75", 0.75),
    ("p95", 0.95),
)

#: The one quantile convention this stage uses. See the module docstring for the formula.
QUANTILE_METHOD = "linear interpolation between order statistics (inclusive, R type 7)"

EMPTY_SEMANTICS = (
    "an empty series reports count=0 and None for every statistic and quantile; never 0.0"
)
SINGLE_OBSERVATION_SEMANTICS = (
    "a series of one reports count=1 and that value for minimum, maximum, mean, median and "
    "every quantile"
)
COMPARISON_SEMANTICS = (
    "with a baseline window: target minus baseline, per statistic and per quantile, with None "
    "wherever either side is None; identical windows therefore give zero differences. Without a "
    "baseline window: comparison is None, never a fabricated zero"
)
SEGMENTATION_SEMANTICS = (
    "Stage 6.9's segments, imported not restated: below_calibration when "
    "max(demand_lag_7, demand_lag_14, demand_lag_28) <= 40 in the stored feature values, "
    "within_calibration otherwise; summarised separately, never combined, counts always visible"
)
MODEL_VERSION_SEMANTICS = "results are grouped per model_version and never pooled across versions"
OUT_OF_SCOPE_SEMANTICS = (
    "a prediction whose canonical_model_digest is not the approved model's is counted as "
    "out_of_scope_model_digest and summarised into nothing"
)
SELECTION_SEMANTICS = (
    "inherited from Stage 6.9 unchanged: one prediction per (target_date, horizon, "
    "model_version), the earliest generated_at with the lowest id breaking a tie"
)


@dataclass(frozen=True, slots=True)
class DistributionProtocol:
    """Every rule Stage 6.10 applies, and nothing that could turn a summary into a verdict.

    The field list is the guarantee. There is no threshold, no verdict, no alert, no anomaly
    flag and no composite statistic here, so the observation *cannot* reach a conclusion -- there
    is nothing in its rules to reach one against. A test asserts these names exactly.
    """

    version: str
    observed_fields: tuple[str, ...]
    summary_statistics: tuple[str, ...]
    quantiles: tuple[tuple[str, float], ...]
    quantile_method: str
    empty_semantics: str
    single_observation_semantics: str
    comparison_semantics: str
    segmentation_semantics: str
    segment_below_calibration: str
    segment_within_calibration: str
    segment_features: tuple[str, ...]
    small_hotel_max_lag_room_nights: int
    model_version_semantics: str
    out_of_scope_semantics: str
    selection_semantics: str

    def as_dict(self) -> dict[str, Any]:
        """The protocol as plain data, in declaration order."""
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @property
    def quantile_labels(self) -> tuple[str, ...]:
        return tuple(label for label, _ in self.quantiles)

    @property
    def checksum(self) -> str:
        """SHA-256 over the protocol's own values, in a form a reader can reproduce.

        Compact JSON with sorted keys, UTF-8, hex digest -- the same shape Stage 6.8's
        ``feature_digest`` and Stage 6.9's accuracy protocol use.
        """
        canonical = json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=list
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: The one protocol this stage applies. Frozen, committed, and checksummed.
PROTOCOL = DistributionProtocol(
    version=PROTOCOL_VERSION,
    observed_fields=OBSERVED_FIELDS,
    summary_statistics=SUMMARY_STATISTICS,
    quantiles=QUANTILES,
    quantile_method=QUANTILE_METHOD,
    empty_semantics=EMPTY_SEMANTICS,
    single_observation_semantics=SINGLE_OBSERVATION_SEMANTICS,
    comparison_semantics=COMPARISON_SEMANTICS,
    segmentation_semantics=SEGMENTATION_SEMANTICS,
    segment_below_calibration=SEGMENT_BELOW_CALIBRATION,
    segment_within_calibration=SEGMENT_WITHIN_CALIBRATION,
    segment_features=SEGMENT_FEATURES,
    small_hotel_max_lag_room_nights=SMALL_HOTEL_MAX_LAG_ROOM_NIGHTS,
    model_version_semantics=MODEL_VERSION_SEMANTICS,
    out_of_scope_semantics=OUT_OF_SCOPE_SEMANTICS,
    selection_semantics=SELECTION_SEMANTICS,
)


__all__ = [
    "COMPARISON_SEMANTICS",
    "EMPTY_SEMANTICS",
    "MODEL_VERSION_SEMANTICS",
    "OBSERVED_FIELDS",
    "OUT_OF_SCOPE_SEMANTICS",
    "PROTOCOL",
    "PROTOCOL_VERSION",
    "QUANTILES",
    "QUANTILE_METHOD",
    "SEGMENTATION_SEMANTICS",
    "SELECTION_SEMANTICS",
    "SINGLE_OBSERVATION_SEMANTICS",
    "SUMMARY_STATISTICS",
    "DistributionProtocol",
]
