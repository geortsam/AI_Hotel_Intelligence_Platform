"""One fictional hotel's data, served to the real tools by fixture-backed services (Stage 7.8).

The copilot's tools delegate to three services. For evaluation those three are replaced -- and
only those three -- by the classes below, which return responses from a small, fixed dataset.
Everything else in the stack is real: the registry, argument validation, the role check, output
validation, the tool loop, the figure check and the response mapping.

**Why fixed data rather than a database.** An evaluation compares a model's behaviour across runs
and across models. That comparison is only meaningful if the model sees the same tool results
every time, so the inputs are frozen here. The database path is already covered by the Stage
7.6 and 7.7 integration suites.

**Every response is built through the production schema.** `OverviewResponse(...)` and friends
validate each value exactly as the real services' responses do, so a fixture cannot drift into a
shape the real service would never return.

**The numbers are internally consistent where the tools make them checkable**: occupancy is
occupied / available, ADR is revenue / occupied, RevPAR is revenue / available. They are
invented, belong to no real property, and are chosen to be distinctive so a scorer can tell one
figure from another.

A request the dataset does not cover is refused with the same typed `ValidationError` a real
service raises for a bad request, so an evaluated model that asks for an unexpected range sees a
tool failure -- and the tool-selection scorer records the miss.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from app.core.errors import ValidationError
from app.ml.accuracy_protocol import PROTOCOL
from app.ml.serving import APPROVED_MODEL, METHODOLOGY, MODEL_STATUS
from app.schemas.analytics import (
    BookingStatusCounts,
    DailyMetricsRow,
    DailySeriesResponse,
    DateRange,
    MoneyByCurrency,
    OccupancyMetrics,
    OverviewResponse,
    RevenueBreakdownResponse,
    RevenueCategoryBreakdown,
    ReviewMetrics,
    RoomRevenueByCurrency,
    StayFlowMetrics,
)
from app.schemas.ml_performance import (
    MEASUREMENT_STATEMENT,
    AccuracyMetrics,
    ForecastAccuracyResponse,
    MeasurementMetadata,
    ModelVersionAccuracyResponse,
    SegmentAccuracyResponse,
)
from app.schemas.ml_serving import (
    SERVED_HORIZON_DAYS,
    DemandModelMetadata,
    DemandPredictionResponse,
)

#: The evaluated hotel. Fictional; the UUID is fixed so reports are reproducible.
EVAL_HOTEL = uuid.UUID("7e57e7a1-0000-4000-8000-000000000001")
#: Twenty rooms throughout.
ROOMS = 20
CURRENCY = "EUR"

NO_DATA = "No evaluation data exists for this request."

MAY = (dt.date(2026, 5, 1), dt.date(2026, 5, 31))
APRIL = (dt.date(2026, 4, 1), dt.date(2026, 4, 30))
FIRST_WEEK_OF_MAY = (dt.date(2026, 5, 1), dt.date(2026, 5, 7))
FORECAST_TARGET = dt.date(2026, 6, 8)
ACCURACY_AS_OF = dt.date(2026, 6, 30)
ACCURACY_WINDOW = (dt.date(2026, 4, 1), dt.date(2026, 5, 31))


def _money(amount: str) -> list[MoneyByCurrency]:
    return [MoneyByCurrency(currency=CURRENCY, amount=Decimal(amount))]


def _overview(
    window: tuple[dt.date, dt.date],
    *,
    occupied: int,
    comp: int,
    rate: str,
    revenue: str,
    adr: str,
    revpar: str,
    other: str,
    ledger_room: str,
    expenses: str,
    net: str,
    created: int,
    arrivals: int,
    cancellations: int,
    reviews: int,
    published: int,
    rating: str,
) -> OverviewResponse:
    days = (window[1] - window[0]).days + 1
    counts = BookingStatusCounts(
        total=created,
        pending=0,
        confirmed=created - arrivals - cancellations,
        checked_in=0,
        checked_out=arrivals,
        cancelled=cancellations,
        no_show=0,
    )
    return OverviewResponse(
        hotel_public_id=EVAL_HOTEL,
        range=DateRange(date_from=window[0], date_to=window[1], days=days),
        bookings_created=counts,
        bookings_by_stay=counts,
        stay_flow=StayFlowMetrics(
            arrivals=arrivals, departures=arrivals, cancellations=cancellations
        ),
        occupancy=OccupancyMetrics(
            occupied_room_nights=occupied,
            room_nights_sold=occupied - comp,
            complimentary_room_nights=comp,
            available_room_nights=ROOMS * days,
            occupancy_rate=Decimal(rate),
        ),
        room_revenue=[
            RoomRevenueByCurrency(
                currency=CURRENCY,
                room_revenue=Decimal(revenue),
                adr=Decimal(adr),
                revpar=Decimal(revpar),
            )
        ],
        other_revenue=_money(other),
        ledger_room_revenue=_money(ledger_room),
        total_expenses=_money(expenses),
        net_operating_result=_money(net),
        is_multi_currency=False,
        reviews=ReviewMetrics(
            review_count=reviews,
            published_count=published,
            average_rating_normalized=Decimal(rating),
        ),
    )


#: May 2026: 434 of 620 room nights, 70.00% occupancy, ADR 120.00, RevPAR 84.00.
OVERVIEWS: dict[tuple[dt.date, dt.date], OverviewResponse] = {
    MAY: _overview(
        MAY,
        occupied=434,
        comp=4,
        rate="0.7000",
        revenue="52080.00",
        adr="120.00",
        revpar="84.00",
        other="3150.00",
        ledger_room="51800.00",
        expenses="30400.00",
        net="24830.00",
        created=212,
        arrivals=187,
        cancellations=11,
        reviews=38,
        published=35,
        rating="0.84",
    ),
    #: April 2026: 366 of 600 room nights, 61.00% occupancy, ADR 110.00, RevPAR 67.10.
    APRIL: _overview(
        APRIL,
        occupied=366,
        comp=2,
        rate="0.6100",
        revenue="40260.00",
        adr="110.00",
        revpar="67.10",
        other="2580.00",
        ledger_room="40100.00",
        expenses="29100.00",
        net="13740.00",
        created=176,
        arrivals=158,
        cancellations=9,
        reviews=29,
        published=27,
        rating="0.81",
    ),
}

#: Occupied room nights, arrivals and departures for 1-7 May 2026. The busiest day is 2 May.
_DAILY: tuple[tuple[int, int, int], ...] = (
    (13, 9, 4),
    (18, 7, 2),
    (16, 3, 5),
    (11, 2, 7),
    (12, 5, 4),
    (14, 6, 4),
    (15, 5, 4),
)


def _daily_row(day: dt.date, occupied: int, arrivals: int, departures: int) -> DailyMetricsRow:
    revenue = Decimal(occupied * 120).quantize(Decimal("0.01"))
    return DailyMetricsRow(
        date=day,
        occupied_room_nights=occupied,
        room_nights_sold=occupied,
        available_room_nights=ROOMS,
        occupancy_rate=(Decimal(occupied) / Decimal(ROOMS)).quantize(Decimal("0.0001")),
        room_revenue=[
            RoomRevenueByCurrency(
                currency=CURRENCY,
                room_revenue=revenue,
                adr=Decimal("120.00"),
                revpar=(revenue / ROOMS).quantize(Decimal("0.01")),
            )
        ],
        other_revenue=_money("100.00"),
        total_expenses=_money("980.00"),
        arrivals=arrivals,
        departures=departures,
        bookings_created=6,
        cancellations=0,
    )


DAILY: dict[tuple[dt.date, dt.date], DailySeriesResponse] = {
    FIRST_WEEK_OF_MAY: DailySeriesResponse(
        hotel_public_id=EVAL_HOTEL,
        range=DateRange(date_from=FIRST_WEEK_OF_MAY[0], date_to=FIRST_WEEK_OF_MAY[1], days=7),
        days=[
            _daily_row(FIRST_WEEK_OF_MAY[0] + dt.timedelta(days=offset), *values)
            for offset, values in enumerate(_DAILY)
        ],
    )
}

BREAKDOWNS: dict[tuple[dt.date, dt.date], RevenueBreakdownResponse] = {
    MAY: RevenueBreakdownResponse(
        hotel_public_id=EVAL_HOTEL,
        range=DateRange(date_from=MAY[0], date_to=MAY[1], days=31),
        categories=[
            RevenueCategoryBreakdown(
                category_code="ROOM",
                is_room_revenue=True,
                currency=CURRENCY,
                amount=Decimal("51800.00"),
                tax_amount=Decimal("4662.00"),
                entry_count=212,
            ),
            RevenueCategoryBreakdown(
                category_code="FNB",
                is_room_revenue=False,
                currency=CURRENCY,
                amount=Decimal("2450.00"),
                tax_amount=Decimal("318.50"),
                entry_count=96,
            ),
            RevenueCategoryBreakdown(
                category_code="SPA",
                is_room_revenue=False,
                currency=CURRENCY,
                amount=Decimal("700.00"),
                tax_amount=Decimal("91.00"),
                entry_count=14,
            ),
        ],
    )
}

FORECASTS: dict[dt.date, DemandPredictionResponse] = {
    FORECAST_TARGET: DemandPredictionResponse(
        hotel_public_id=EVAL_HOTEL,
        target_date=FORECAST_TARGET,
        forecast_horizon_days=SERVED_HORIZON_DAYS,
        cutoff_date=FORECAST_TARGET - dt.timedelta(days=SERVED_HORIZON_DAYS),
        prediction_cutoff=dt.datetime.combine(
            FORECAST_TARGET - dt.timedelta(days=SERVED_HORIZON_DAYS - 1), dt.time(0), dt.UTC
        ),
        predicted_room_nights=15.43,
        model=DemandModelMetadata(
            model_name=APPROVED_MODEL.model_name,
            model_version=APPROVED_MODEL.model_version,
            feature_version=APPROVED_MODEL.feature_version,
            dataset_version=APPROVED_MODEL.dataset_version,
            status=MODEL_STATUS,
            production_ready=False,
            methodology=METHODOLOGY,
        ),
        features_used=list(APPROVED_MODEL.feature_columns),
    )
}


def _segment(
    name: str, observations: int, mae: float | None, rmse: float | None, smape: float | None
) -> SegmentAccuracyResponse:
    return SegmentAccuracyResponse(
        segment=name,
        metrics=AccuracyMetrics(
            observations=observations, skipped=0, mae=mae, rmse=rmse, smape=smape
        ),
    )


ACCURACY: dict[tuple[dt.date, dt.date, dt.date], ForecastAccuracyResponse] = {
    (ACCURACY_AS_OF, *ACCURACY_WINDOW): ForecastAccuracyResponse(
        hotel_public_id=EVAL_HOTEL,
        as_of_date=ACCURACY_AS_OF,
        window_from=ACCURACY_WINDOW[0],
        window_to=ACCURACY_WINDOW[1],
        scored_from=ACCURACY_WINDOW[0],
        scored_to=ACCURACY_WINDOW[1],
        settlement_lag_days=PROTOCOL.settlement_lag_days,
        candidates=61,
        ineligible_by_settlement=0,
        out_of_scope_model_digest=0,
        unsettled_allocations=0,
        settled=True,
        by_model_version=[
            ModelVersionAccuracyResponse(
                model_version=APPROVED_MODEL.model_version,
                below_calibration=_segment("below_calibration", 0, None, None, None),
                within_calibration=_segment("within_calibration", 61, 2.41, 3.12, 18.74),
            )
        ],
        measurement=MeasurementMetadata(
            protocol_version=PROTOCOL.version,
            protocol_checksum=PROTOCOL.checksum,
            establishes_production_accuracy=False,
            statement=MEASUREMENT_STATEMENT,
        ),
    )
}


def _require_hotel(hotel_public_id: uuid.UUID) -> None:
    # The invocation service has already resolved the path hotel; a fixture service handed any
    # other hotel would mean the stack passed a hotel it should not have, so it refuses loudly.
    if hotel_public_id != EVAL_HOTEL:
        raise AssertionError(f"a fixture service was asked about hotel {hotel_public_id}")


class FixtureAnalytics:
    """Stands where `AnalyticsService` does. Same three methods, same signatures."""

    def overview(
        self, hotel_public_id: uuid.UUID, date_from: dt.date, date_to: dt.date
    ) -> OverviewResponse:
        _require_hotel(hotel_public_id)
        try:
            return OVERVIEWS[(date_from, date_to)]
        except KeyError:
            raise ValidationError(NO_DATA) from None

    def daily(
        self, hotel_public_id: uuid.UUID, date_from: dt.date, date_to: dt.date
    ) -> DailySeriesResponse:
        _require_hotel(hotel_public_id)
        try:
            return DAILY[(date_from, date_to)]
        except KeyError:
            raise ValidationError(NO_DATA) from None

    def revenue_breakdown(
        self, hotel_public_id: uuid.UUID, date_from: dt.date, date_to: dt.date
    ) -> RevenueBreakdownResponse:
        _require_hotel(hotel_public_id)
        try:
            return BREAKDOWNS[(date_from, date_to)]
        except KeyError:
            raise ValidationError(NO_DATA) from None


class FixtureForecasts:
    """Stands where `DemandPredictionService` does. Records nothing: there is nowhere to."""

    def forecast_demand(
        self, hotel_public_id: uuid.UUID, target_date: dt.date, horizon_days: int
    ) -> DemandPredictionResponse:
        _require_hotel(hotel_public_id)
        if horizon_days != SERVED_HORIZON_DAYS or target_date not in FORECASTS:
            raise ValidationError(NO_DATA)
        return FORECASTS[target_date]


class FixtureInsight:
    """Stands where `InsightService` does (Stage 7.12). No evaluation case asks for the attention
    list, so it holds no data: a request is the same typed refusal a real service gives for a
    request it cannot answer, and a case that tried would record a tool failure."""

    def priorities(self, hotel_public_id: uuid.UUID, date_from: dt.date, date_to: dt.date) -> Any:
        _require_hotel(hotel_public_id)
        raise ValidationError(NO_DATA)


class FixtureAccuracy:
    """Stands where `ForecastPerformanceService` does."""

    def forecast_accuracy(
        self,
        hotel_public_id: uuid.UUID,
        *,
        as_of_date: dt.date,
        window_from: dt.date,
        window_to: dt.date,
    ) -> ForecastAccuracyResponse:
        _require_hotel(hotel_public_id)
        try:
            return ACCURACY[(as_of_date, window_from, window_to)]
        except KeyError:
            raise ValidationError(NO_DATA) from None


__all__ = [
    "ACCURACY",
    "BREAKDOWNS",
    "DAILY",
    "EVAL_HOTEL",
    "FORECASTS",
    "NO_DATA",
    "OVERVIEWS",
    "FixtureAccuracy",
    "FixtureAnalytics",
    "FixtureForecasts",
]
