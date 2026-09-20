"""The accuracy protocol: declared here, in code, before any production number is computed.

Stage 6.9. A measurement whose rules are chosen after the numbers are visible has measured
nothing. So every rule this stage applies is a frozen constant in this module, it is committed,
it carries a deterministic content checksum, and every function that applies it is pure.

## What is deliberately absent

There is **no threshold, no pass mark, no baseline and no ranking field** in
:class:`AccuracyProtocol`, and a test asserts the field list. Stage 6.4's acceptance policy
answered "was the thing measured, measured properly?" and refused to be a competition result.
This protocol is narrower still: it does not judge at all. It fixes which predictions are in
scope, which one of several is scored, how they are grouped, and what counts as skipped. What
the resulting error *means* is not decided here, and is not decided by this stage.

## The settlement lag is an assumption, not a proof

:data:`SETTLEMENT_LAG_DAYS` is **28 days**, and it is an operational assumption rather than a
mathematical guarantee. The distinction matters enough to write out.

What the schema does settle, from ``BOOKING_STATUS_TRANSITIONS`` and ``OCCUPANCY_STATUSES``:

===================  ==========================================================================
Status covering *D*  Can the night still enter or leave occupancy?
===================  ==========================================================================
``checked_in``       No. It transitions only to ``checked_out``, and both are occupancy
``checked_out``      No. Terminal
``cancelled``        No. Terminal, and not occupancy
``no_show``          No. Terminal, and not occupancy
``confirmed``        **Yes** -- may become ``no_show`` or ``cancelled``, removing the night
``pending``          **Yes** -- may become ``confirmed``, adding the night
===================  ==========================================================================

So a night's count is already final for every checked-in or terminal allocation, however long
the guest stays; waiting for checkout buys nothing. Only ``pending`` and ``confirmed`` are
volatile, and :data:`VOLATILE_OCCUPANCY_STATUSES` derives exactly those two from the existing
vocabulary rather than restating them.

Every allocation covering night *D* arrived on or before *D*, so its arrival decision was
**due on or before D itself**. The lag is therefore recording slack over a decision already
due, not lifecycle time.

Why twenty-eight: it is one period of the model's own longest lag feature, ``demand_lag_28``,
and the Stage 6.1 lookback window, so this protocol introduces no new time constant into a
codebase that already runs on a 7/14/28 rhythm; and it is four times the forecast horizon, so a
settlement window can never be misread as a horizon.

**What twenty-eight is not.** Two sources of change are unbounded in this schema: a
``confirmed`` or ``pending`` row a property never resolves, and a booking created after the
stay -- which this repository has *measured* happening, at 133 of 166 demo bookings
(``docs/ml-dataset-design.md`` §11). No finite lag can guarantee settlement against either.
Stage 6.2 met the same wall from the other side and wrote it down rather than papering over it:
"The rule cannot bound a stay that began before the window and ran longer than anything inside
it. Nothing can, from this file."

Because the assumption cannot be proved, the evaluation **measures whether it held**:
``unsettled_allocations`` counts the volatile allocations covering the scored window, and
``settled`` is false when any remain. An assumption that reports its own violations is a
different thing from one that is merely asserted.

## The selection rule

When several predictions exist for one ``(hotel, target_date, horizon, model_version)`` they
share a ``prediction_cutoff`` -- it is derived from the target date and the horizon -- and
differ only in when the request was made, and so in how much late-recorded history the lag
features had picked up. Exactly one is scored: the **earliest** ``generated_at``, tie-broken by
the lowest ``id``.

Earliest, not latest, for one decisive reason: **once a target date has been scored, no later
prediction can change that score.** Scoring the most recent would let a hotelier re-requesting a
forecast for a past date silently rewrite accuracy already measured, so two runs over the same
window would disagree and the disagreement would be invisible. It also happens to be the honest
reading of the question Stage 6.8 asked -- *what did we tell this hotel* -- and it refuses the
quiet flattery of scoring the best-informed run.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from app.models.enums import BOOKING_STATUS_TRANSITIONS, OCCUPANCY_STATUSES


def _volatile_statuses() -> tuple[str, ...]:
    """Statuses from which a booking can still enter or leave ``OCCUPANCY_STATUSES``.

    Derived from the two existing constants rather than listed, which is the whole point: a
    hand-written ``("pending", "confirmed")`` here would be a second definition of occupancy
    semantics, and it would not follow a future migration that changed the transition graph.

    A status is volatile when some transition out of it crosses the occupancy boundary.
    """
    occupancy = frozenset(OCCUPANCY_STATUSES)
    volatile = [
        status
        for status, targets in BOOKING_STATUS_TRANSITIONS.items()
        if any(
            target != status and (target in occupancy) != (status in occupancy)
            for target in targets
        )
    ]
    return tuple(sorted(volatile))


#: The statuses whose nights can still appear in or disappear from occupancy. Currently
#: ``("confirmed", "pending")``; derived, never typed out.
VOLATILE_OCCUPANCY_STATUSES: tuple[str, ...] = _volatile_statuses()

#: Bump whenever any value in :data:`PROTOCOL` changes. Reported with every result, so a reader
#: can tell which rules produced a number they are looking at.
PROTOCOL_VERSION = "accuracy_v1"

#: Days after ``target_date`` before a prediction may be scored. See the module docstring for
#: why this is an assumption and how the evaluation reports its own violations.
SETTLEMENT_LAG_DAYS = 28

#: The largest realised demand level measured to sit below the model's lowest learned bin edge.
#:
#: Stage 6.6 measured, against this artifact, that a flat history of 1, 3, 5, 10 **or 40** room
#: nights a night all produce the same prediction, about 165.83 -- while 60 a night produces
#: about 133.29. ``test_the_model_cannot_tell_small_hotels_apart`` pins that behaviour. Forty is
#: taken from that measurement, made before this stage existed and before any production number
#: was computed.
SMALL_HOTEL_MAX_LAG_ROOM_NIGHTS = 40

#: The segment whose predictions fall below the scale the model can distinguish. Reported
#: always, excluded never: hiding it would hide the model's worst documented failure, and
#: pooling it into one headline would produce a number that is arithmetically correct and
#: substantively meaningless.
SEGMENT_BELOW_CALIBRATION = "below_calibration"
SEGMENT_WITHIN_CALIBRATION = "within_calibration"

#: The lag features segment membership reads. Inputs only -- never the realised demand, never
#: the error -- so a prediction's segment is fixed at the moment it was made.
SEGMENT_FEATURES: tuple[str, ...] = ("demand_lag_7", "demand_lag_14", "demand_lag_28")

SELECTION_RULE = "earliest generated_at, tie-break lowest id"

#: What is counted as what, stated once so a denominator can be read without the source.
SKIP_SEMANTICS: tuple[str, ...] = (
    "ineligible_by_settlement: target_date + settlement_lag_days > as_of_date; not scored and "
    "not counted in any metric denominator",
    "out_of_scope_model_digest: canonical_model_digest is not the approved model's; reported "
    "separately and never pooled into an approved-model metric",
    "skipped_missing_ground_truth: eligible and in scope, but the target date has no recorded "
    "occupied room nights; counted in MetricSet.skipped, never scored as zero",
    "scored: everything else; one prediction per (target_date, horizon, model_version) group",
)


@dataclass(frozen=True, slots=True)
class AccuracyProtocol:
    """Every rule Stage 6.9 applies, and nothing that could turn a measurement into a verdict.

    The field list is the guarantee. There is no threshold, no pass mark, no baseline, no
    comparator and no ranking here, so the evaluation *cannot* reach a verdict -- there is
    nothing in its rules to reach one against. A test asserts these names exactly.
    """

    version: str
    settlement_lag_days: int
    selection_rule: str
    small_hotel_max_lag_room_nights: int
    segment_below_calibration: str
    segment_within_calibration: str
    segment_features: tuple[str, ...]
    volatile_occupancy_statuses: tuple[str, ...]
    skip_semantics: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """The protocol as plain data, in declaration order."""
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @property
    def checksum(self) -> str:
        """SHA-256 over the protocol's own values, in a form a reader can reproduce.

        Compact JSON with sorted keys, UTF-8, hex digest -- the same shape Stage 6.8's
        ``feature_digest`` uses, and for the same reason: a rule written down somewhere other
        than in the thing it governs eventually disagrees with it.
        """
        canonical = json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=list
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: The one protocol this stage applies. Frozen, committed, and checksummed.
PROTOCOL = AccuracyProtocol(
    version=PROTOCOL_VERSION,
    settlement_lag_days=SETTLEMENT_LAG_DAYS,
    selection_rule=SELECTION_RULE,
    small_hotel_max_lag_room_nights=SMALL_HOTEL_MAX_LAG_ROOM_NIGHTS,
    segment_below_calibration=SEGMENT_BELOW_CALIBRATION,
    segment_within_calibration=SEGMENT_WITHIN_CALIBRATION,
    segment_features=SEGMENT_FEATURES,
    volatile_occupancy_statuses=VOLATILE_OCCUPANCY_STATUSES,
    skip_semantics=SKIP_SEMANTICS,
)


def last_settled_target_date(
    as_of_date: dt.date, *, protocol: AccuracyProtocol = PROTOCOL
) -> dt.date:
    """The latest ``target_date`` that may be scored when evaluating at *as_of_date*.

    A date, derived from a date. No clock is consulted here or anywhere below it -- the caller
    states the day it is evaluating as of, exactly as ``RetentionPolicy.cutoff`` requires its
    caller to state *now*.
    """
    return as_of_date - dt.timedelta(days=protocol.settlement_lag_days)


def is_eligible(
    target_date: dt.date, as_of_date: dt.date, *, protocol: AccuracyProtocol = PROTOCOL
) -> bool:
    """Whether *target_date* has cleared the settlement lag by *as_of_date*.

    The boundary is inclusive: ``target_date + settlement_lag_days == as_of_date`` is eligible.
    """
    return target_date <= last_settled_target_date(as_of_date, protocol=protocol)


def segment_of(feature_values: Mapping[str, Any], *, protocol: AccuracyProtocol = PROTOCOL) -> str:
    """Which segment a prediction belongs to, from its stored inputs alone.

    Reads only the three lag features. A prediction whose row is missing one of them -- which
    the serving path cannot produce, since all nine are written together -- is treated as
    within calibration rather than silently dropped, because dropping a row on a shape error
    would quietly shrink a denominator.
    """
    levels = [
        float(feature_values[name]) for name in protocol.segment_features if name in feature_values
    ]
    if not levels:
        return protocol.segment_within_calibration
    if max(levels) <= protocol.small_hotel_max_lag_room_nights:
        return protocol.segment_below_calibration
    return protocol.segment_within_calibration


__all__ = [
    "PROTOCOL",
    "PROTOCOL_VERSION",
    "SEGMENT_BELOW_CALIBRATION",
    "SEGMENT_FEATURES",
    "SEGMENT_WITHIN_CALIBRATION",
    "SELECTION_RULE",
    "SETTLEMENT_LAG_DAYS",
    "SKIP_SEMANTICS",
    "SMALL_HOTEL_MAX_LAG_ROOM_NIGHTS",
    "VOLATILE_OCCUPANCY_STATUSES",
    "AccuracyProtocol",
    "is_eligible",
    "last_settled_target_date",
    "segment_of",
]
