"""The V2 demand dataset: its contract, its features, and the rules that keep it honest.

Stage 6.1 builds a dataset and a feature pipeline. **It trains nothing.** There is no model
here, no estimator, no fitted parameter and no accuracy claim -- only the observations a later
stage could learn from, and the guarantees that make learning from them meaningful.

This module is **pure**, on the same terms as :mod:`app.ml.timeseries`: no SQLAlchemy, no
session, no request, no import from ``app``. It takes plain dated facts and returns plain rows,
which is what lets every rule below be tested without a database.

## The one thing this module exists to prevent

Leakage. A forecasting dataset is easy to build and easy to build *wrong*: the wrongness does
not raise, it inflates the score. Every feature here is therefore tied to an explicit instant --
:func:`prediction_cutoff` -- and a feature may only use facts that were true before it.

    target_date         the day whose demand is being predicted
    horizon_days        how far ahead the prediction is made, in whole days (>= 1)
    cutoff_date         target_date - horizon_days; the last day whose demand is known
    prediction_cutoff   midnight UTC ending cutoff_date, i.e. start of (cutoff_date + 1)

A fact counts as known when its timestamp is **strictly before** ``prediction_cutoff``. Demand
realised on ``cutoff_date`` is known; demand on any later day, including ``target_date`` itself,
is not. :data:`FEATURE_SPECS` records that judgement for every feature, and
:func:`assert_no_feature_is_known_only_after_prediction` refuses a contract that breaks it.

## What is deliberately not modelled

Two facts about the schema bound what can honestly be reconstructed, and both are limitations
rather than choices:

* **Booking status has no history.** Only ``booked_at`` and ``cancelled_at`` are timestamped, so
  "was this booking confirmed or still pending at the cutoff?" is not answerable. The
  on-the-books feature is therefore status-agnostic: it counts room nights whose booking existed
  and was not yet cancelled at the cutoff. It is an upper bound on confirmed demand, and it is
  named so that it cannot be mistaken for one.
* **Room activation has no history.** ``rooms.is_active`` is current state, so capacity as of a
  past date cannot be recovered from it. Capacity is counted from ``rooms.created_at`` alone --
  rooms that demonstrably existed at the cutoff -- and ``is_active`` is not consulted. Using it
  would mean reading a fact that post-dates the cutoff.

Both are stated in ``docs/ml-dataset-design.md`` rather than hidden behind a feature name.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

# --- versions ------------------------------------------------------------------------------
#
# Two versions, because they answer different questions. A model records the FEATURE version it
# was trained against, so a later feature change makes the incompatibility explicit instead of
# silently shifting the inputs underneath it. The DATASET version covers the row grain, the
# target definition and the split policy -- the shape of the problem rather than the columns.
#
# Deliberately two constants and not a registry: a registry is the right answer when several
# versions must coexist, and none does yet.
DATASET_VERSION = "v1"
FEATURE_VERSION = "v1"

#: How far ahead a prediction is made, in whole days. One means "tomorrow, decided today".
DEFAULT_HORIZON_DAYS = 1

#: Lags are expressed relative to ``target_date``. A lag shorter than the horizon would read a
#: day the forecaster cannot have seen, so :func:`build_rows` refuses that configuration rather
#: than quietly dropping the feature.
DEFAULT_LAG_DAYS: tuple[int, ...] = (1, 7, 14, 28)

#: Rolling means end at ``cutoff_date`` -- never at ``target_date``.
DEFAULT_ROLLING_WINDOWS: tuple[int, ...] = (7, 14, 28)

#: Below this many rows, a chronological three-way split cannot produce three non-empty parts
#: that mean anything. The pipeline fails rather than returning partitions of one row.
MIN_ROWS_FOR_SPLIT = 30


class DatasetError(Exception):
    """Base class for every refusal this module issues."""


class InsufficientDataError(DatasetError):
    """There is not enough history to build what was asked for.

    Raised rather than returned, and never softened into an empty dataset: a caller that gets
    back zero rows tends to carry on, and a caller that gets an exception does not.
    """


class DatasetContractError(DatasetError):
    """The rows do not satisfy the dataset's own invariants.

    Every instance of this is a bug in the pipeline or in the data it was given, not a
    condition a caller can recover from by asking differently.
    """


class FeatureAvailability(StrEnum):
    """When a feature's value becomes knowable, relative to the prediction cutoff.

    The whole point of writing this down per feature is that ``AFTER_PREDICTION`` is then a
    thing the code can refuse, rather than a thing a reviewer has to notice.
    """

    #: Derivable from the target date alone, so knowable arbitrarily far in advance.
    BEFORE_PREDICTION = "before_prediction"
    #: Depends on facts recorded up to and including the cutoff.
    AT_PREDICTION = "at_prediction"
    #: Only knowable once the target date has passed. Never a feature; this is the target.
    AFTER_PREDICTION = "after_prediction"


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """One feature, and the promise attached to it."""

    name: str
    availability: FeatureAvailability
    description: str


def _calendar_specs() -> tuple[FeatureSpec, ...]:
    before = FeatureAvailability.BEFORE_PREDICTION
    return (
        FeatureSpec("day_of_week", before, "Monday=0 .. Sunday=6, from the target date."),
        FeatureSpec("day_of_month", before, "1..31, from the target date."),
        FeatureSpec("month", before, "1..12, from the target date."),
        FeatureSpec("week_of_year", before, "ISO week number, 1..53."),
        FeatureSpec("day_of_year", before, "1..366."),
        FeatureSpec("is_weekend", before, "1 when the target date is Saturday or Sunday."),
    )


def build_feature_specs(
    lag_days: Sequence[int] = DEFAULT_LAG_DAYS,
    rolling_windows: Sequence[int] = DEFAULT_ROLLING_WINDOWS,
) -> tuple[FeatureSpec, ...]:
    """The full feature contract for one configuration, in a stable order.

    Order is part of the contract: a model that consumes these positionally must see the same
    columns in the same places on every run.
    """
    at = FeatureAvailability.AT_PREDICTION
    specs = list(_calendar_specs())
    for lag in lag_days:
        specs.append(
            FeatureSpec(
                f"demand_lag_{lag}",
                at,
                f"Realised demand on target_date - {lag} days. None when that day is "
                "outside the extracted history.",
            )
        )
    for window in rolling_windows:
        specs.append(
            FeatureSpec(
                f"demand_rolling_mean_{window}",
                at,
                f"Mean realised demand over the {window} days ending at cutoff_date. "
                "None unless every day in the window is present.",
            )
        )
    specs.append(
        FeatureSpec(
            "on_books_room_nights_at_cutoff",
            at,
            "Room nights for the target date from bookings that existed at the cutoff and "
            "were not cancelled by then. Status-agnostic: booking status has no history, so "
            "pending and confirmed cannot be told apart at a past instant.",
        )
    )
    specs.append(
        FeatureSpec(
            "rooms_existing_at_cutoff",
            at,
            "Rooms whose created_at precedes the cutoff. `is_active` is deliberately not "
            "consulted -- it carries no history, so reading it would import a fact from "
            "after the cutoff.",
        )
    )
    return tuple(specs)


#: The default contract, for callers that do not configure lags or windows.
FEATURE_SPECS: tuple[FeatureSpec, ...] = build_feature_specs()


def assert_no_feature_is_known_only_after_prediction(
    specs: Iterable[FeatureSpec],
) -> None:
    """Refuse a contract containing a feature that cannot be known at prediction time.

    Cheap, and the cheapness is the argument for having it: the alternative is trusting that
    nobody ever adds a column sourced from the target day.
    """
    offenders = [s.name for s in specs if s.availability is FeatureAvailability.AFTER_PREDICTION]
    if offenders:
        raise DatasetContractError(
            "these features are only knowable after the target date and cannot be inputs: "
            + ", ".join(sorted(offenders))
        )


# --- the facts the pipeline is given -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DailyDemand:
    """Realised demand for one hotel-day: the target, and the source of every lag."""

    stay_date: dt.date
    room_nights: int


@dataclass(frozen=True, slots=True)
class HotelHistory:
    """Everything extracted for one hotel, already aggregated to day grain.

    Keyed by date rather than listed, because every lookup here is by date and a list would
    invite a linear scan per feature per row.
    """

    hotel_public_id: uuid.UUID
    demand_by_date: Mapping[dt.date, int]
    on_books_by_date: Mapping[dt.date, int]
    rooms_existing_by_date: Mapping[dt.date, int]


# --- the rows the pipeline produces --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DemandRow:
    """One training observation: one hotel, one calendar date.

    Identity is the hotel's PUBLIC id. Internal BIGINT keys are used while extracting -- they
    are what the schema joins on -- and are dropped here, so nothing downstream can serialise
    one by accident.
    """

    hotel_public_id: uuid.UUID
    target_date: dt.date
    prediction_cutoff: dt.datetime
    horizon_days: int
    features: Mapping[str, float | int | None]
    target_room_nights: int

    @property
    def cutoff_date(self) -> dt.date:
        """The last day whose realised demand this row is allowed to have seen."""
        return self.target_date - dt.timedelta(days=self.horizon_days)

    @property
    def has_complete_features(self) -> bool:
        return all(value is not None for value in self.features.values())


@dataclass(frozen=True, slots=True)
class DemandDataset:
    """Rows plus the metadata needed to reproduce and to version them."""

    rows: tuple[DemandRow, ...]
    feature_specs: tuple[FeatureSpec, ...]
    dataset_version: str = DATASET_VERSION
    feature_version: str = FEATURE_VERSION
    horizon_days: int = DEFAULT_HORIZON_DAYS
    generated_at: dt.datetime | None = None
    source: str = "postgresql:booking_room_nights"

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.feature_specs)

    @property
    def hotel_public_ids(self) -> tuple[uuid.UUID, ...]:
        seen: dict[uuid.UUID, None] = {}
        for row in self.rows:
            seen.setdefault(row.hotel_public_id, None)
        return tuple(seen)

    @property
    def date_range(self) -> tuple[dt.date, dt.date] | None:
        if not self.rows:
            return None
        dates = [row.target_date for row in self.rows]
        return min(dates), max(dates)


@dataclass(frozen=True, slots=True)
class TemporalSplit:
    """A chronological three-way split, and the boundaries that produced it.

    The boundaries are returned rather than implied so a reader can check the ordering claim
    instead of trusting it.
    """

    train: tuple[DemandRow, ...]
    validation: tuple[DemandRow, ...]
    test: tuple[DemandRow, ...]
    train_end: dt.date
    validation_end: dt.date

    @property
    def sizes(self) -> tuple[int, int, int]:
        return len(self.train), len(self.validation), len(self.test)


@dataclass(frozen=True, slots=True)
class DatasetReport:
    """What the pipeline found, including what it could not do.

    Built for the honest cases: too little history, gaps in the middle, rows whose lags fall off
    the front of the extract. These are conditions to report, not to paper over.
    """

    hotels: int
    rows: int
    rows_with_complete_features: int
    date_range: tuple[dt.date, dt.date] | None
    missing_feature_counts: Mapping[str, int] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


# --- cutoff ---------------------------------------------------------------------------------


def cutoff_date_for(target_date: dt.date, horizon_days: int) -> dt.date:
    """The last day whose realised demand a prediction for *target_date* may use."""
    if horizon_days < 1:
        raise DatasetContractError(
            f"horizon_days must be at least 1 day, got {horizon_days}: a horizon of zero would "
            "let a prediction read the day it is predicting"
        )
    return target_date - dt.timedelta(days=horizon_days)


def prediction_cutoff(target_date: dt.date, horizon_days: int) -> dt.datetime:
    """The instant a fact must precede to be usable as a feature.

    Midnight UTC at the END of ``cutoff_date`` -- equivalently, the start of the following day.
    UTC explicitly and always: a cutoff that moved with a server's local zone would make the
    dataset depend on where it was built.
    """
    end_of_cutoff_day = cutoff_date_for(target_date, horizon_days) + dt.timedelta(days=1)
    return dt.datetime.combine(end_of_cutoff_day, dt.time.min, tzinfo=dt.UTC)


def is_known_at_cutoff(moment: dt.datetime, target_date: dt.date, horizon_days: int) -> bool:
    """Whether a timestamped fact was already true at the cutoff.

    Naive datetimes are refused rather than assumed to be UTC. An assumed zone is exactly how a
    few hours of leakage gets in without anyone noticing.
    """
    if moment.tzinfo is None:
        raise DatasetContractError(
            "a naive datetime cannot be compared against the cutoff; attach a timezone"
        )
    return moment < prediction_cutoff(target_date, horizon_days)


# --- features ---------------------------------------------------------------------------------


def calendar_features(target_date: dt.date) -> dict[str, int]:
    """Derived from the date itself, so knowable at any horizon."""
    iso = target_date.isocalendar()
    return {
        "day_of_week": target_date.weekday(),
        "day_of_month": target_date.day,
        "month": target_date.month,
        "week_of_year": iso.week,
        "day_of_year": target_date.timetuple().tm_yday,
        "is_weekend": 1 if target_date.weekday() >= 5 else 0,
    }


def lag_features(
    demand_by_date: Mapping[dt.date, int],
    target_date: dt.date,
    horizon_days: int,
    lag_days: Sequence[int],
) -> dict[str, int | None]:
    """Realised demand at fixed offsets before the target date.

    A lag shorter than the horizon is a contract error, not a missing value: it would mean the
    configuration itself asks for a day the forecaster cannot have seen.

    A lag that simply falls outside the extracted history is ``None``. It is NOT zero -- zero is
    a real demand value here, and a hotel with no history would otherwise look like a hotel that
    sold nothing.
    """
    out: dict[str, int | None] = {}
    for lag in lag_days:
        if lag < horizon_days:
            raise DatasetContractError(
                f"demand_lag_{lag} would read {lag} day(s) before the target while the horizon "
                f"is {horizon_days} day(s): that value is not known at the cutoff"
            )
        out[f"demand_lag_{lag}"] = demand_by_date.get(target_date - dt.timedelta(days=lag))
    return out


def rolling_mean_features(
    demand_by_date: Mapping[dt.date, int],
    target_date: dt.date,
    horizon_days: int,
    windows: Sequence[int],
) -> dict[str, float | None]:
    """Mean realised demand over whole windows ending at ``cutoff_date``.

    All-or-nothing on purpose: a mean over a partially present window silently changes meaning
    with the amount of history available, which is the kind of feature that looks fine in
    training and drifts in production. Absent history stays absent.
    """
    end = cutoff_date_for(target_date, horizon_days)
    out: dict[str, float | None] = {}
    for window in windows:
        if window < 1:
            raise DatasetContractError(f"rolling window must be at least 1 day, got {window}")
        days = [end - dt.timedelta(days=offset) for offset in range(window)]
        values = [demand_by_date.get(day) for day in days]
        out[f"demand_rolling_mean_{window}"] = (
            None if any(v is None for v in values) else statistics.fmean([int(v) for v in values])  # type: ignore[arg-type]
        )
    return out


def build_row(
    history: HotelHistory,
    target_date: dt.date,
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    lag_days: Sequence[int] = DEFAULT_LAG_DAYS,
    rolling_windows: Sequence[int] = DEFAULT_ROLLING_WINDOWS,
) -> DemandRow:
    """Assemble one observation from already-extracted history.

    The target is read from the same series the lags are read from, which is what keeps the two
    definitions from drifting apart. A target date absent from the extract is an error rather
    than a zero: not selling a room and not having been asked about the day are different
    things.
    """
    target = history.demand_by_date.get(target_date)
    if target is None:
        raise DatasetContractError(
            f"no realised demand extracted for {history.hotel_public_id} on {target_date}"
        )

    features: dict[str, float | int | None] = {}
    features.update(calendar_features(target_date))
    features.update(lag_features(history.demand_by_date, target_date, horizon_days, lag_days))
    features.update(
        rolling_mean_features(history.demand_by_date, target_date, horizon_days, rolling_windows)
    )
    features["on_books_room_nights_at_cutoff"] = history.on_books_by_date.get(target_date)
    features["rooms_existing_at_cutoff"] = history.rooms_existing_by_date.get(target_date)

    return DemandRow(
        hotel_public_id=history.hotel_public_id,
        target_date=target_date,
        prediction_cutoff=prediction_cutoff(target_date, horizon_days),
        horizon_days=horizon_days,
        features=MappingProxyType(features),
        target_room_nights=int(target),
    )


def build_rows(
    histories: Sequence[HotelHistory],
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    lag_days: Sequence[int] = DEFAULT_LAG_DAYS,
    rolling_windows: Sequence[int] = DEFAULT_ROLLING_WINDOWS,
    target_dates: Mapping[uuid.UUID, Sequence[dt.date]] | None = None,
) -> tuple[DemandRow, ...]:
    """Every observation for every hotel, sorted chronologically then by hotel.

    Sorting here rather than at the point of use is what makes the output comparable between
    runs, and it is the ordering every temporal operation downstream assumes.
    """
    if any(lag < horizon_days for lag in lag_days):
        raise DatasetContractError(
            f"lags {sorted(lag_days)} are not all at least the horizon ({horizon_days} days)"
        )

    rows: list[DemandRow] = []
    for history in histories:
        dates = (
            sorted(history.demand_by_date)
            if target_dates is None
            else sorted(target_dates.get(history.hotel_public_id, ()))
        )
        for target_date in dates:
            rows.append(
                build_row(
                    history,
                    target_date,
                    horizon_days=horizon_days,
                    lag_days=lag_days,
                    rolling_windows=rolling_windows,
                )
            )
    rows.sort(key=lambda row: (row.target_date, str(row.hotel_public_id)))
    return tuple(rows)


# --- validation ---------------------------------------------------------------------------------


def validate_rows(rows: Sequence[DemandRow], *, minimum: int = 1) -> DatasetReport:
    """Check every invariant the dataset claims, and report what is missing.

    Raises on anything that would make the rows wrong, and reports -- without raising -- on
    anything that merely makes them incomplete. A gap in the history is a fact about the hotel;
    a duplicate hotel-day is a fact about the pipeline.
    """
    if len(rows) < minimum:
        raise InsufficientDataError(
            f"{len(rows)} observation(s) built, at least {minimum} required"
        )

    seen: set[tuple[uuid.UUID, dt.date]] = set()
    missing: dict[str, int] = {}
    for row in rows:
        key = (row.hotel_public_id, row.target_date)
        if key in seen:
            raise DatasetContractError(
                f"duplicate observation for hotel {row.hotel_public_id} on {row.target_date}"
            )
        seen.add(key)

        if row.target_room_nights < 0:
            raise DatasetContractError(
                f"negative demand ({row.target_room_nights}) on {row.target_date}"
            )
        if row.prediction_cutoff != prediction_cutoff(row.target_date, row.horizon_days):
            raise DatasetContractError(
                f"cutoff {row.prediction_cutoff} does not match the horizon on {row.target_date}"
            )
        # A row whose cutoff reaches the target day would be reading the answer.
        if row.cutoff_date >= row.target_date:
            raise DatasetContractError(
                f"cutoff date {row.cutoff_date} is not before target date {row.target_date}"
            )

        capacity = row.features.get("rooms_existing_at_cutoff")
        if isinstance(capacity, int) and capacity > 0 and row.target_room_nights > capacity:
            # Reported, not raised: the schema permits a room to be sold on a date before that
            # room's row was created, and back-dated imports do exactly that. Silently dropping
            # those rows would hide a real data-quality problem.
            missing["demand_exceeds_rooms_existing_at_cutoff"] = (
                missing.get("demand_exceeds_rooms_existing_at_cutoff", 0) + 1
            )

        for name, value in row.features.items():
            if value is None:
                missing[name] = missing.get(name, 0) + 1
            elif isinstance(value, float) and not math.isfinite(value):
                raise DatasetContractError(f"non-finite value for {name} on {row.target_date}")

    ordered = list(rows)
    if ordered != sorted(ordered, key=lambda r: (r.target_date, str(r.hotel_public_id))):
        raise DatasetContractError("rows are not in chronological order")

    dates = [row.target_date for row in rows]
    complete = sum(1 for row in rows if row.has_complete_features)
    notes: list[str] = []
    if complete == 0:
        notes.append(
            "no row has a complete feature vector: the extracted history is shorter than the "
            "longest lag or rolling window"
        )
    return DatasetReport(
        hotels=len({row.hotel_public_id for row in rows}),
        rows=len(rows),
        rows_with_complete_features=complete,
        date_range=(min(dates), max(dates)),
        missing_feature_counts=dict(sorted(missing.items())),
        notes=tuple(notes),
    )


# --- temporal splitting --------------------------------------------------------------------------


def split_chronologically(
    rows: Sequence[DemandRow],
    *,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
    minimum_rows: int = MIN_ROWS_FOR_SPLIT,
) -> TemporalSplit:
    """Split by DATE, never by row, and never at random.

    Two properties matter and only one of them is obvious.

    The obvious one: validation and test must lie entirely after training, or the evaluation is
    measuring memorisation.

    The less obvious one: the boundary falls between *dates*, not between rows. With several
    hotels a row-index split would put one hotel's Tuesday in training and another hotel's same
    Tuesday in test, which leaks through any feature that is shared across hotels and makes the
    partitions incomparable. Splitting on the date axis keeps a day whole.

    There is deliberately no ``shuffle`` argument and no ``random_state``. Neither would have an
    honest value.
    """
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1:
        raise DatasetContractError("train and validation fractions must lie strictly in (0, 1)")
    if train_fraction + validation_fraction >= 1:
        raise DatasetContractError(
            f"train ({train_fraction}) + validation ({validation_fraction}) leaves nothing for test"
        )
    if len(rows) < minimum_rows:
        raise InsufficientDataError(
            f"{len(rows)} observation(s) is below the minimum of {minimum_rows} for a "
            "three-way chronological split; build more history rather than splitting thinner"
        )

    dates = sorted({row.target_date for row in rows})
    if len(dates) < 3:
        raise InsufficientDataError(
            f"{len(dates)} distinct date(s): a chronological split needs at least three"
        )

    train_count = max(1, int(len(dates) * train_fraction))
    validation_count = max(1, int(len(dates) * validation_fraction))
    if train_count + validation_count >= len(dates):
        # Give test at least one date by taking from validation first, then training.
        validation_count = max(1, len(dates) - train_count - 1)
        if train_count + validation_count >= len(dates):
            train_count = len(dates) - 2
            validation_count = 1
    if train_count < 1 or validation_count < 1 or train_count + validation_count >= len(dates):
        raise InsufficientDataError(
            f"{len(dates)} distinct dates cannot be split into three non-empty periods"
        )

    train_end = dates[train_count - 1]
    validation_end = dates[train_count + validation_count - 1]

    train = tuple(r for r in rows if r.target_date <= train_end)
    validation = tuple(r for r in rows if train_end < r.target_date <= validation_end)
    test = tuple(r for r in rows if r.target_date > validation_end)

    if not train or not validation or not test:
        raise InsufficientDataError(
            "the chronological split produced an empty partition "
            f"(train={len(train)}, validation={len(validation)}, test={len(test)})"
        )
    return TemporalSplit(
        train=train,
        validation=validation,
        test=test,
        train_end=train_end,
        validation_end=validation_end,
    )


def assert_split_is_chronological(split: TemporalSplit) -> None:
    """Prove the ordering claim rather than documenting it.

    Cheap enough to run after every split, and it catches the one failure mode that would
    otherwise be invisible in the numbers.
    """
    if max(r.target_date for r in split.train) >= min(r.target_date for r in split.validation):
        raise DatasetContractError("training data reaches into the validation period")
    if max(r.target_date for r in split.validation) >= min(r.target_date for r in split.test):
        raise DatasetContractError("validation data reaches into the test period")
