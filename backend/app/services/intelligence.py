"""Intelligence domain logic: assemble series, run the models, explain the results.

**No metric is redefined here.** Occupancy, room revenue and booking counts all come from
``AnalyticsRepository`` -- the same queries, the same day grains, the same
``OCCUPANCY_STATUSES`` filter that Stage 3B.10 established. A second definition of occupancy
living in an ML service would drift from the analytics one within a release, and the two
would disagree on the same dashboard. There is deliberately no ``IntelligenceRepository``.

**Read-only.** Nothing here commits, rolls back or writes. Forecasts are computed on demand
and returned; none is persisted, which is why this stage needs no migration.

**Temporal leakage is prevented structurally, not by convention.** The training window is
derived as the N days ending the day *before* the horizon starts, and every repository call
is bounded by that window. No code path reads a date inside the horizon for the purpose of
predicting it -- the only forward read is ``on_the_books``, which is reported as a separate,
clearly-labelled fact and is never fed to a model.

**Currency follows Stage 3B.10**: independent per-currency forecasts, no FX, no totals.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

from app.core.errors import ValidationError
from app.ml.timeseries import (
    ANOMALY_THRESHOLD,
    CONFIDENCE_LEVEL,
    MIN_TRAINING_OBSERVATIONS,
    MODEL_NAME,
    MODEL_VERSION,
    TREND_THRESHOLD,
    Anomaly,
    ForecastMethod,
    ForecastPoint,
    Observation,
    detect_anomalies,
    forecast_series,
    measure_trend,
)
from app.models.hotel import Hotel
from app.repositories.analytics import AnalyticsRepository, RoomRevenueRow
from app.schemas.intelligence import (
    MAX_HORIZON_DAYS,
    MAX_OBSERVATION_DAYS,
    AnomalyPoint,
    AnomalyResponse,
    CurrencyForecast,
    DemandTrendResponse,
    ForecastHorizon,
    Insight,
    InsightsResponse,
    ModelMetadata,
    ObservationWindow,
    OccupancyForecastPoint,
    OccupancyForecastResponse,
    RevenueForecastPoint,
    RevenueForecastResponse,
    SeverityLiteral,
    SupportingMetric,
    TrainingWindow,
)
from app.services.scope import HotelScopeResolver

ZERO = decimal.Decimal("0")
MONEY_PLACES = decimal.Decimal("0.01")
RATE_PLACES = decimal.Decimal("0.0001")

METHODOLOGY = (
    "Day-of-week seasonal median over the training window, with a robust prediction "
    "interval from the median absolute deviation; anomalies by modified z-score; demand "
    "trend by split-window median comparison."
)

#: The metrics the anomaly scan covers. Named in the response so an empty result reads as
#: "nothing was unusual" rather than "nothing was looked at".
OCCUPANCY_METRIC = "occupied_room_nights"
BOOKINGS_METRIC = "bookings_created"
ROOM_REVENUE_METRIC = "room_revenue"


def _quantise(value: decimal.Decimal | None, places: decimal.Decimal) -> decimal.Decimal | None:
    """Round half-up to a fixed scale, preserving null.

    Explicit ``is None``: ``Decimal("0.00")`` is falsy, and an ``or`` fallback here would
    silently drop the scale on every zero -- a bug caught in Stage 3B.10's live run.
    """
    if value is None:
        return None
    return value.quantize(places, rounding=decimal.ROUND_HALF_UP)


class IntelligenceService:
    """Forecasting, trend detection, anomaly detection and insight assembly for one hotel."""

    def __init__(self, repository: AnalyticsRepository, scope: HotelScopeResolver) -> None:
        # The ANALYTICS repository, not one of its own: the metric definitions are shared.
        # No session, because nothing here writes.
        self._repository = repository
        self._scope = scope

    # --- forecasts ----------------------------------------------------------------------

    def occupancy_forecast(
        self,
        hotel_public_id: uuid.UUID,
        date_from: dt.date,
        date_to: dt.date,
        training_days: int,
    ) -> OccupancyForecastResponse:
        """Forecast occupied room nights per day, alongside what is already booked."""
        hotel = self._scope.require_hotel(hotel_public_id)
        horizon, window = self._windows(date_from, date_to, training_days)

        observations = self._occupancy_observations(hotel.id, window)
        horizon_days = self._days(horizon.date_from, horizon.date_to)
        predictions = forecast_series(observations, horizon_days)

        rooms = self._repository.active_room_count(hotel.id)
        booked = self._repository.occupied_nights_by_day(
            hotel.id, horizon.date_from, horizon.date_to
        )

        points = []
        for prediction in predictions:
            counts = booked.get(prediction.date)
            points.append(
                self._occupancy_point(prediction, rooms, counts.occupied if counts else 0)
            )

        return OccupancyForecastResponse(
            hotel_public_id=hotel.public_id,
            model=self._metadata(),
            training_window=window,
            horizon=horizon,
            points=points,
        )

    def revenue_forecast(
        self,
        hotel_public_id: uuid.UUID,
        date_from: dt.date,
        date_to: dt.date,
        training_days: int,
    ) -> RevenueForecastResponse:
        """Forecast room revenue per day, independently within each currency.

        The source is ``booking_room_nights.rate`` -- the per-night truth. Payments are money
        moving rather than revenue earned, and ``room_types.base_price`` is a list price; a
        test asserts neither reaches this calculation.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        horizon, window = self._windows(date_from, date_to, training_days)
        horizon_days = self._days(horizon.date_from, horizon.date_to)

        history = self._repository.room_revenue_by_day(hotel.id, window.date_from, window.date_to)
        booked = self._repository.room_revenue_by_day(hotel.id, horizon.date_from, horizon.date_to)
        training_days_list = self._days(window.date_from, window.date_to)

        # Only currencies with actual history are forecast. Inventing a zero series for a
        # currency the hotel has never traded in would manufacture training data.
        currencies = sorted({row.currency for rows in history.values() for row in rows})

        forecasts = []
        for currency in currencies:
            observations = [
                Observation(
                    date=day,
                    value=self._currency_amount(history.get(day, []), currency),
                )
                for day in training_days_list
            ]
            predictions = forecast_series(observations, horizon_days)
            forecasts.append(
                CurrencyForecast(
                    currency=currency,
                    points=[
                        RevenueForecastPoint(
                            date=prediction.date,
                            on_the_books_room_revenue=_quantise(
                                self._currency_amount(booked.get(prediction.date, []), currency),
                                MONEY_PLACES,
                            )
                            or ZERO,
                            predicted_room_revenue=_quantise(prediction.value, MONEY_PLACES),
                            interval_lower=_quantise(prediction.lower, MONEY_PLACES),
                            interval_upper=_quantise(prediction.upper, MONEY_PLACES),
                            confidence_level=self._confidence(prediction),
                            method=prediction.method.value,
                            observations=prediction.observations,
                        )
                        for prediction in predictions
                    ],
                )
            )

        return RevenueForecastResponse(
            hotel_public_id=hotel.public_id,
            model=self._metadata(),
            training_window=window,
            horizon=horizon,
            currencies=forecasts,
            is_multi_currency=len(currencies) > 1,
        )

    # --- trend and anomalies ------------------------------------------------------------

    def demand_trend(
        self, hotel_public_id: uuid.UUID, date_from: dt.date, date_to: dt.date
    ) -> DemandTrendResponse:
        """Classify booking demand over an observation window.

        Counted by ``bookings.booked_at``: a demand trend is about bookings being *taken*.
        Stay-dated counts answer a different question and would call a quiet booking month
        with a busy stay month "increasing".
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        window = self._observation_window(date_from, date_to)
        observations = self._booking_observations(hotel.id, window)
        result = measure_trend(observations)

        return DemandTrendResponse(
            hotel_public_id=hotel.public_id,
            model=self._metadata(),
            window=window,
            metric=BOOKINGS_METRIC,
            direction=result.direction,
            earlier_median=result.earlier_median,
            recent_median=result.recent_median,
            relative_change=_quantise(result.relative_change, RATE_PLACES),
            threshold=TREND_THRESHOLD,
        )

    def anomalies(
        self, hotel_public_id: uuid.UUID, date_from: dt.date, date_to: dt.date
    ) -> AnomalyResponse:
        """Scan occupancy, booking volume and room revenue for unusual days."""
        hotel = self._scope.require_hotel(hotel_public_id)
        window = self._observation_window(date_from, date_to)

        found: list[AnomalyPoint] = []
        found += self._scan(OCCUPANCY_METRIC, self._occupancy_observations(hotel.id, window))
        found += self._scan(BOOKINGS_METRIC, self._booking_observations(hotel.id, window))
        for currency, observations in self._revenue_observations(hotel.id, window).items():
            found += self._scan(f"{ROOM_REVENUE_METRIC}[{currency}]", observations)

        return AnomalyResponse(
            hotel_public_id=hotel.public_id,
            model=self._metadata(),
            window=window,
            metrics_scanned=sorted(
                {point.metric for point in found} | self._scannable(hotel.id, window)
            ),
            anomalies=sorted(found, key=lambda point: (point.metric, point.date)),
        )

    # --- insights -----------------------------------------------------------------------

    def insights(
        self,
        hotel_public_id: uuid.UUID,
        date_from: dt.date,
        date_to: dt.date,
        horizon_days: int,
    ) -> InsightsResponse:
        """Assemble structured findings from the trend, anomaly and forecast outputs.

        The horizon runs from the day after the observation window, so the forecast's
        training window is exactly the observed window -- no gap, no overlap, no leakage.

        Explanations are templates filled from the same numbers the insight already carries.
        There is no generated prose and no model of language anywhere in this stage.
        """
        hotel = self._scope.require_hotel(hotel_public_id)
        window = self._observation_window(date_from, date_to)
        horizon_from = window.date_to + dt.timedelta(days=1)
        horizon_to = horizon_from + dt.timedelta(days=horizon_days - 1)
        horizon = ForecastHorizon(date_from=horizon_from, date_to=horizon_to, days=horizon_days)

        found: list[Insight] = []
        found += self._trend_insight(hotel, window)
        found += self._anomaly_insights(hotel, window)
        found += self._occupancy_outlook_insight(hotel, window, horizon)

        order = {"critical": 0, "warning": 1, "info": 2}
        return InsightsResponse(
            hotel_public_id=hotel.public_id,
            model=self._metadata(),
            window=window,
            horizon=horizon,
            insights=sorted(
                found, key=lambda item: (order[item.severity], item.type, item.date_from)
            ),
        )

    # --- window handling ----------------------------------------------------------------

    @staticmethod
    def _days(date_from: dt.date, date_to: dt.date) -> list[dt.date]:
        return [
            date_from + dt.timedelta(days=offset)
            for offset in range((date_to - date_from).days + 1)
        ]

    @staticmethod
    def _windows(
        date_from: dt.date, date_to: dt.date, training_days: int
    ) -> tuple[ForecastHorizon, TrainingWindow]:
        """Validate the horizon and derive the training window that must precede it.

        The training window ends on ``date_from - 1``. That single line is the leakage
        guard: every subsequent query is bounded by this window, so no value from inside the
        horizon can influence a prediction about it.
        """
        if date_to < date_from:
            raise ValidationError("date_to must not be earlier than date_from.")
        days = (date_to - date_from).days + 1
        if days > MAX_HORIZON_DAYS:
            raise ValidationError(
                f"The requested horizon spans {days} days; the maximum is {MAX_HORIZON_DAYS}."
            )

        training_end = date_from - dt.timedelta(days=1)
        training_start = training_end - dt.timedelta(days=training_days - 1)
        return (
            ForecastHorizon(date_from=date_from, date_to=date_to, days=days),
            TrainingWindow(
                date_from=training_start,
                date_to=training_end,
                days=training_days,
                observations=training_days,
            ),
        )

    @staticmethod
    def _observation_window(date_from: dt.date, date_to: dt.date) -> ObservationWindow:
        if date_to < date_from:
            raise ValidationError("date_to must not be earlier than date_from.")
        days = (date_to - date_from).days + 1
        if days > MAX_OBSERVATION_DAYS:
            raise ValidationError(
                f"The requested window spans {days} days; the maximum is {MAX_OBSERVATION_DAYS}."
            )
        return ObservationWindow(date_from=date_from, date_to=date_to, days=days, observations=days)

    # --- series assembly ----------------------------------------------------------------

    def _occupancy_observations(
        self, hotel_id: int, window: TrainingWindow | ObservationWindow
    ) -> list[Observation]:
        """A DENSE daily series of occupied room nights.

        Densified on purpose: a day with no bookings is a real zero, not a missing reading,
        and dropping it would make a quiet week look like a short one and bias every median
        upwards.
        """
        counts = self._repository.occupied_nights_by_day(hotel_id, window.date_from, window.date_to)
        return [
            Observation(
                date=day,
                value=decimal.Decimal(counts[day].occupied if day in counts else 0),
            )
            for day in self._days(window.date_from, window.date_to)
        ]

    def _booking_observations(
        self, hotel_id: int, window: TrainingWindow | ObservationWindow
    ) -> list[Observation]:
        counts = self._repository.bookings_created_by_day(
            hotel_id, window.date_from, window.date_to
        )
        return [
            Observation(date=day, value=decimal.Decimal(counts.get(day, 0)))
            for day in self._days(window.date_from, window.date_to)
        ]

    def _revenue_observations(
        self, hotel_id: int, window: TrainingWindow | ObservationWindow
    ) -> dict[str, list[Observation]]:
        """One dense series per currency that has any history in the window."""
        history = self._repository.room_revenue_by_day(hotel_id, window.date_from, window.date_to)
        currencies = sorted({row.currency for rows in history.values() for row in rows})
        days = self._days(window.date_from, window.date_to)
        return {
            currency: [
                Observation(date=day, value=self._currency_amount(history.get(day, []), currency))
                for day in days
            ]
            for currency in currencies
        }

    @staticmethod
    def _currency_amount(rows: list[RoomRevenueRow], currency: str) -> decimal.Decimal:
        """Pull one currency's amount out of a day's buckets, defaulting to a real zero."""
        for row in rows:
            if row.currency == currency:
                return decimal.Decimal(row.room_revenue)
        return ZERO

    def _scannable(self, hotel_id: int, window: ObservationWindow) -> set[str]:
        """Every metric the scan covered, whether or not it produced a flag."""
        currencies = self._revenue_observations(hotel_id, window).keys()
        return {OCCUPANCY_METRIC, BOOKINGS_METRIC} | {
            f"{ROOM_REVENUE_METRIC}[{currency}]" for currency in currencies
        }

    # --- rendering ----------------------------------------------------------------------

    @staticmethod
    def _metadata() -> ModelMetadata:
        return ModelMetadata(
            model_name=MODEL_NAME,
            model_version=MODEL_VERSION,
            methodology=METHODOLOGY,
            generated_at=dt.datetime.now(dt.UTC),
        )

    @staticmethod
    def _confidence(prediction: ForecastPoint) -> decimal.Decimal | None:
        """No interval means no confidence to state."""
        return None if prediction.value is None else CONFIDENCE_LEVEL

    def _occupancy_point(
        self, prediction: ForecastPoint, rooms: int, on_the_books: int
    ) -> OccupancyForecastPoint:
        """Render one forecast day, clamping the prediction to physical capacity.

        ``ck_daily_hotel_metrics_occupied_rooms_within_available`` states the rule the schema
        itself believes: occupied cannot exceed available. A prediction above capacity is
        therefore corrected rather than published, and the correction is flagged.
        """
        capacity = decimal.Decimal(rooms)
        value = prediction.value
        clamped = value is not None and value > capacity
        if clamped:
            value = capacity

        rate = None
        if value is not None and rooms > 0:
            rate = _quantise(value / capacity, RATE_PLACES)

        return OccupancyForecastPoint(
            date=prediction.date,
            on_the_books_room_nights=on_the_books,
            available_room_nights=rooms,
            predicted_room_nights=_quantise(value, RATE_PLACES),
            predicted_occupancy_rate=rate,
            interval_lower=_quantise(prediction.lower, RATE_PLACES),
            interval_upper=_quantise(prediction.upper, RATE_PLACES),
            confidence_level=self._confidence(prediction),
            method=prediction.method.value,
            observations=prediction.observations,
            capacity_clamped=clamped,
        )

    @staticmethod
    def _scan(metric: str, observations: list[Observation]) -> list[AnomalyPoint]:
        return [
            AnomalyPoint(
                metric=metric,
                date=found.date,
                value=found.value,
                median=found.median,
                median_absolute_deviation=found.deviation,
                modified_z_score=_quantise(found.score, RATE_PLACES) or ZERO,
                threshold=ANOMALY_THRESHOLD,
                direction=found.direction,
            )
            for found in detect_anomalies(observations)
        ]

    # --- insight builders ---------------------------------------------------------------

    def _trend_insight(self, hotel: Hotel, window: ObservationWindow) -> list[Insight]:
        result = measure_trend(self._booking_observations(hotel.id, window))
        if result.direction == "insufficient_data":
            return [
                Insight(
                    type="data_sufficiency",
                    severity="info",
                    title="Not enough booking history to judge demand",
                    explanation=(
                        f"The window holds {result.observations} daily observations; at "
                        f"least {MIN_TRAINING_OBSERVATIONS} are needed before a demand "
                        "direction is reported. No trend has been estimated."
                    ),
                    supporting_metrics=[
                        SupportingMetric(name="observations", value=str(result.observations)),
                        SupportingMetric(
                            name="minimum_required", value=str(MIN_TRAINING_OBSERVATIONS)
                        ),
                    ],
                    date_from=window.date_from,
                    date_to=window.date_to,
                )
            ]

        severity: SeverityLiteral = "info" if result.direction != "decreasing" else "warning"
        change = _quantise(result.relative_change, RATE_PLACES)
        change_text = (
            "undefined (the earlier half had no bookings)" if change is None else str(change)
        )
        return [
            Insight(
                type="demand_trend",
                severity=severity,
                title=f"Booking demand is {result.direction}",
                explanation=(
                    f"Median bookings taken per day moved from {result.earlier_median} in the "
                    f"earlier half of the window to {result.recent_median} in the recent "
                    f"half. Relative change {change_text} against a threshold of "
                    f"{TREND_THRESHOLD}."
                ),
                supporting_metrics=[
                    SupportingMetric(name="earlier_median", value=str(result.earlier_median)),
                    SupportingMetric(name="recent_median", value=str(result.recent_median)),
                    SupportingMetric(name="threshold", value=str(TREND_THRESHOLD)),
                ],
                date_from=window.date_from,
                date_to=window.date_to,
            )
        ]

    def _anomaly_insights(self, hotel: Hotel, window: ObservationWindow) -> list[Insight]:
        found: list[Anomaly] = []
        metrics: list[tuple[str, list[Observation]]] = [
            (OCCUPANCY_METRIC, self._occupancy_observations(hotel.id, window)),
            (BOOKINGS_METRIC, self._booking_observations(hotel.id, window)),
        ]
        for currency, observations in self._revenue_observations(hotel.id, window).items():
            metrics.append((f"{ROOM_REVENUE_METRIC}[{currency}]", observations))

        insights: list[Insight] = []
        for metric, observations in metrics:
            for anomaly in detect_anomalies(observations):
                found.append(anomaly)
                insights.append(
                    Insight(
                        type="anomaly",
                        severity="warning",
                        title=f"Unusual {metric} on {anomaly.date}",
                        explanation=(
                            f"{metric} was {anomaly.value} on {anomaly.date}, "
                            f"{anomaly.direction} the window median of {anomaly.median}. "
                            f"Modified z-score {_quantise(anomaly.score, RATE_PLACES)} "
                            f"exceeds the threshold of {ANOMALY_THRESHOLD}, measured against "
                            f"a median absolute deviation of {anomaly.deviation}."
                        ),
                        supporting_metrics=[
                            SupportingMetric(name=metric, value=str(anomaly.value)),
                            SupportingMetric(name="window_median", value=str(anomaly.median)),
                            SupportingMetric(
                                name="median_absolute_deviation", value=str(anomaly.deviation)
                            ),
                            SupportingMetric(
                                name="modified_z_score",
                                value=str(_quantise(anomaly.score, RATE_PLACES)),
                            ),
                        ],
                        date_from=anomaly.date,
                        date_to=anomaly.date,
                    )
                )
        return insights

    def _occupancy_outlook_insight(
        self, hotel: Hotel, window: ObservationWindow, horizon: ForecastHorizon
    ) -> list[Insight]:
        """A forward-looking summary, built from the same forecast the endpoint returns."""
        observations = self._occupancy_observations(hotel.id, window)
        predictions = forecast_series(observations, self._days(horizon.date_from, horizon.date_to))
        usable = [point for point in predictions if point.value is not None]
        if not usable:
            return [
                Insight(
                    type="data_sufficiency",
                    severity="info",
                    title="Not enough history to forecast occupancy",
                    explanation=(
                        f"The window holds {len(observations)} daily observations; at least "
                        f"{MIN_TRAINING_OBSERVATIONS} are needed before occupancy is "
                        "forecast. No prediction has been made."
                    ),
                    supporting_metrics=[
                        SupportingMetric(name="observations", value=str(len(observations))),
                        SupportingMetric(
                            name="minimum_required", value=str(MIN_TRAINING_OBSERVATIONS)
                        ),
                    ],
                    date_from=horizon.date_from,
                    date_to=horizon.date_to,
                )
            ]

        rooms = self._repository.active_room_count(hotel.id)
        total = sum((point.value or ZERO) for point in usable)
        capacity = decimal.Decimal(rooms * len(usable))
        rate = _quantise(total / capacity, RATE_PLACES) if capacity > ZERO else None
        seasonal = sum(1 for point in usable if point.method is ForecastMethod.SEASONAL_DOW_MEDIAN)

        return [
            Insight(
                type="occupancy_outlook",
                severity="info",
                title=f"Forecast occupancy for the next {horizon.days} days",
                explanation=(
                    f"The model expects {total} occupied room nights across "
                    f"{horizon.days} days against a capacity of {capacity}"
                    + (f", an occupancy rate of {rate}" if rate is not None else "")
                    + f". {seasonal} of {len(usable)} days used a day-of-week seasonal "
                    "median; the rest fell back to the window median."
                ),
                supporting_metrics=[
                    SupportingMetric(name="predicted_room_nights", value=str(total)),
                    SupportingMetric(name="available_room_nights", value=str(capacity)),
                    SupportingMetric(
                        name="predicted_occupancy_rate",
                        value="undefined" if rate is None else str(rate),
                    ),
                ],
                date_from=horizon.date_from,
                date_to=horizon.date_to,
                confidence=CONFIDENCE_LEVEL,
            )
        ]


__all__ = ["METHODOLOGY", "IntelligenceService"]
