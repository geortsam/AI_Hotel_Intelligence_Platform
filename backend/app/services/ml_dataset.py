"""Assembling the V2 demand dataset: extraction, features, validation, splits.

Stage 6.1. **Nothing here trains a model.** There is no estimator, no fitted parameter and no
accuracy claim anywhere in this stage -- only the observations a later stage could learn from.

## Where this sits

    MlDemandRepository          SQL, one hotel at a time      (app.repositories.ml_demand)
        |
        v
    MlDatasetService            orchestration, this module
        |
        v
    app.ml.dataset              pure features, rules, splits  (no SQL, no session)

Read-only throughout. This service neither commits nor rolls back, because it writes nothing --
the asymmetry the architecture audit checks for, and the honest signal that a dataset build
cannot corrupt the operational data it reads.

## Why there is no endpoint

None is needed for Stage 6.1, and adding one would mean deciding who may export a hotel's full
demand history before there is any use for it. The pipeline is called programmatically; if an
API arrives later it can be authorised on its own terms rather than inheriting a hole.

## Tenant isolation

Every method takes one hotel and passes one ``hotel_id`` to every query. There is no method here
that reads more than one hotel, and :meth:`build_for_hotels` is a loop over single-hotel builds
rather than a widened query -- so a mistake produces a missing hotel, never a mixed one.
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.ml.dataset import (
    DATASET_VERSION,
    DEFAULT_HORIZON_DAYS,
    DEFAULT_LAG_DAYS,
    DEFAULT_ROLLING_WINDOWS,
    FEATURE_VERSION,
    DatasetReport,
    DemandDataset,
    DemandRow,
    HotelHistory,
    InsufficientDataError,
    assert_no_feature_is_known_only_after_prediction,
    build_feature_specs,
    build_rows,
    validate_rows,
)
from app.repositories.ml_demand import MlDemandRepository

#: The longest history any default feature reaches back for. Extraction starts this far before
#: the first target date so that the earliest rows can still have their lags, instead of being
#: silently emitted with everything missing.
DEFAULT_LOOKBACK_DAYS = max((*DEFAULT_LAG_DAYS, *DEFAULT_ROLLING_WINDOWS))


class MlDatasetService:
    """Builds demand datasets from the operational tables. Writes nothing."""

    def __init__(self, repository: MlDemandRepository) -> None:
        self._repository = repository

    # --- one hotel ----------------------------------------------------------------------------

    def build_history(
        self,
        hotel_id: int,
        hotel_public_id: uuid.UUID,
        *,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        date_from: dt.date | None = None,
        date_to: dt.date | None = None,
    ) -> tuple[HotelHistory, tuple[dt.date, ...]]:
        """Extract everything one hotel's rows will need, and say which dates are targets.

        Two ranges, and the difference between them is the point. Features reach back
        ``lookback_days`` before the first target date, so the extraction window is wider than
        the window of rows produced. Without that, the first 28 days of every dataset would
        arrive with their lags empty for no reason other than where the query started.
        """
        bounds = self._repository.first_and_last_stay_date(hotel_id)
        if bounds is None:
            raise InsufficientDataError(
                f"hotel {hotel_public_id} has no occupied room nights: there is nothing to "
                "build a demand dataset from"
            )
        first_seen, last_seen = bounds
        target_from = max(first_seen, date_from) if date_from else first_seen
        target_to = min(last_seen, date_to) if date_to else last_seen
        if target_from > target_to:
            raise InsufficientDataError(
                f"hotel {hotel_public_id} has no occupied room nights between "
                f"{date_from} and {date_to}"
            )

        extract_from = target_from - dt.timedelta(days=lookback_days + horizon_days)

        demand = self._repository.demand_by_date(hotel_id, extract_from, target_to)
        on_books = self._repository.on_books_room_nights_by_date(
            hotel_id, target_from, target_to, horizon_days
        )
        rooms = self._repository.rooms_existing_by_date(
            hotel_id, target_from, target_to, horizon_days
        )

        history = HotelHistory(
            hotel_public_id=hotel_public_id,
            demand_by_date=demand,
            on_books_by_date=on_books,
            rooms_existing_by_date=rooms,
        )
        targets = tuple(day for day in sorted(demand) if target_from <= day <= target_to)
        return history, targets

    def build_for_hotel(
        self,
        hotel_id: int,
        hotel_public_id: uuid.UUID,
        *,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        lag_days: tuple[int, ...] = DEFAULT_LAG_DAYS,
        rolling_windows: tuple[int, ...] = DEFAULT_ROLLING_WINDOWS,
        date_from: dt.date | None = None,
        date_to: dt.date | None = None,
        generated_at: dt.datetime | None = None,
    ) -> DemandDataset:
        """One hotel's dataset. The single-tenant path, and the one every caller should prefer."""
        return self._assemble(
            [(hotel_id, hotel_public_id)],
            horizon_days=horizon_days,
            lag_days=lag_days,
            rolling_windows=rolling_windows,
            date_from=date_from,
            date_to=date_to,
            generated_at=generated_at,
        )

    def build_for_hotels(
        self,
        hotels: list[tuple[int, uuid.UUID]],
        *,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        lag_days: tuple[int, ...] = DEFAULT_LAG_DAYS,
        rolling_windows: tuple[int, ...] = DEFAULT_ROLLING_WINDOWS,
        date_from: dt.date | None = None,
        date_to: dt.date | None = None,
        generated_at: dt.datetime | None = None,
    ) -> DemandDataset:
        """Several hotels, built one at a time and concatenated.

        A loop rather than a widened query, so an error in the predicate cannot mix tenants: the
        worst a bug here can do is omit a hotel. Each hotel's history is extracted and turned
        into rows independently, and the hotel identity travels on every row.
        """
        return self._assemble(
            hotels,
            horizon_days=horizon_days,
            lag_days=lag_days,
            rolling_windows=rolling_windows,
            date_from=date_from,
            date_to=date_to,
            generated_at=generated_at,
        )

    # --- internals -------------------------------------------------------------------------------

    def _assemble(
        self,
        hotels: list[tuple[int, uuid.UUID]],
        *,
        horizon_days: int,
        lag_days: tuple[int, ...],
        rolling_windows: tuple[int, ...],
        date_from: dt.date | None,
        date_to: dt.date | None,
        generated_at: dt.datetime | None,
    ) -> DemandDataset:
        specs = build_feature_specs(lag_days, rolling_windows)
        # Checked on every build, not once at import: the specs depend on the configuration, so
        # a caller could otherwise introduce an after-the-fact feature by passing one in.
        assert_no_feature_is_known_only_after_prediction(specs)

        histories: list[HotelHistory] = []
        targets: dict[uuid.UUID, tuple[dt.date, ...]] = {}
        for hotel_id, hotel_public_id in hotels:
            history, hotel_targets = self.build_history(
                hotel_id,
                hotel_public_id,
                horizon_days=horizon_days,
                lookback_days=max((*lag_days, *rolling_windows)),
                date_from=date_from,
                date_to=date_to,
            )
            histories.append(history)
            targets[hotel_public_id] = hotel_targets

        rows = build_rows(
            histories,
            horizon_days=horizon_days,
            lag_days=lag_days,
            rolling_windows=rolling_windows,
            target_dates=targets,
        )
        return DemandDataset(
            rows=rows,
            feature_specs=specs,
            dataset_version=DATASET_VERSION,
            feature_version=FEATURE_VERSION,
            horizon_days=horizon_days,
            generated_at=generated_at,
        )

    # --- reporting --------------------------------------------------------------------------------

    @staticmethod
    def report(dataset: DemandDataset, *, minimum: int = 1) -> DatasetReport:
        """Validate the assembled rows and describe what is there.

        Separate from assembly so a caller can build a dataset, look at the report, and decide
        whether it is fit for the purpose in hand -- rather than having that judgement made for
        them inside the builder.
        """
        return validate_rows(dataset.rows, minimum=minimum)

    @staticmethod
    def rows_with_complete_features(dataset: DemandDataset) -> tuple[DemandRow, ...]:
        """The subset a model could actually train on today.

        Offered as a filter rather than applied during assembly: dropping incomplete rows
        silently is how a dataset quietly becomes shorter than its date range suggests, and the
        gap between the two numbers is exactly what the report is for.
        """
        return tuple(row for row in dataset.rows if row.has_complete_features)
