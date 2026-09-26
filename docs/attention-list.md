# The attention list — Stage 7.12

> **What exists:** `GET /hotels/{h}/intelligence/priorities` returns a ranked, deterministic
> reading of one hotel's own data — the busiest upcoming days by the seasonal forecast, the
> strongest anomalies the window saw, and the booking-demand trend when it moved. The copilot
> can read the same list through its seventh tool, `get_hotel_priorities`. **What this is not:**
> advice. No item tells anyone what to do, and nothing here acts on a hotel's behalf.

Specified by [v2-roadmap.md](v2-roadmap.md) §7.12, with the six decisions taken with the user
recorded in its done note.

---

## 1. What "recommendations" means here

Manager-facing findings over a single hotel's own data. The recorded backlog item of the same
name — ranked hotel suggestions for guests, from "user and hotel history" — is **not** this stage:
the platform has no guest accounts, and such suggestions would need data across hotels. It stays
in the backlog.

The route is named for what it is. The word "recommend" remains banned from every path and from
this code by four existing architecture tests; none was relaxed.

## 2. What the list is made of

| Kind | Source | Which | Figures |
|---|---|---|---|
| `upcoming_peak_day` | `IntelligenceService.occupancy_forecast` | the **3** busiest of the **14** days after the window, by forecast room nights (ties: earlier date first) | forecast, interval bounds, on the books, available |
| `observed_anomaly` | `IntelligenceService.anomalies` | the **3** with the largest absolute modified z-score | value, median, MAD, z-score, threshold |
| `demand_trend` | `IntelligenceService.demand_trend` | one, only when classified `increasing` or `decreasing` | both half-window medians, relative change, threshold |

The forecast is the platform's deterministic **seasonal day-of-week median** — the same function
every intelligence endpoint uses — over the **90** days ending at the window's last day. It is
not the learned demand model. The 3, 14 and 90 come from the `insight_ranking_v1` protocol, so
what is served is what is measured.

**Order:** kinds in the table's order; within a kind, its own measure descending, then date. The
same data always yields the same list, and `rank` is contiguous from 1.

## 3. What an item may say

Every item has: `rank`, `kind`, dates, `measure`, `figures` (each with its `unit` and the service
method it came from), an `observation`, a `comparison`, a `limitation`, and a `look_at` naming one
of three existing GET views. The three sentences are **fixed templates** — nine in all, in
`app/services/insight.py` — filled only from the item's own figures. A test reads every template
and every rendered sentence and refuses instruction and operational vocabulary (should, must,
consider, raise, lower, price, rate, staff, schedule, allocate, adjust, apply, execute, automate,
…). There is no generated text, and no language model is involved.

## 4. `insight_ranking_v1` — how the day ranking is measured

Declared in `app/ml/ranking_protocol.py` and checksummed (`37245dec…7efa`) **before** the first
measurement.

| | |
|---|---|
| Dataset | `demand_daily_v1` (frozen, committed; two hotels' historical daily demand, 1,462 rows), digest pinned |
| Origins | per hotel, every 14 days once 90 days of history exist; windows tile |
| Platform method | `forecast_series` over the 90 days ending at the origin |
| Baseline ("popularity") | last week's realised demand for the same weekday, as known at the origin |
| Truth | realised room nights of each of the 14 days |
| Metrics | precision@3; NDCG@3 with linear gain and a log2 discount |
| Ties | score descending, then date ascending — for predictions and the ideal alike |
| Skips | a window is scored only if every day has a realised value, both methods score every day, and ideal DCG > 0; skips are counted |
| Comparison rule | per hotel and pooled: each method's mean, side by side, and per metric how many windows the platform scored higher, lower or equal. **No threshold, no winner, no significance test, no business-value claim.** |

It runs in `tests/evaluation/insight_ranking.py` — beside the evaluation harness, because the
offline `ml/` package may not import the application and the method measured must be the
application's own function. The report is pinned in `tests/evaluation/insight_ranking_report.json`
and recomputed by the test suite.

### The measurement

91 windows (46 city, 45 resort), none skipped.

| | platform mean | baseline mean | platform higher / lower / equal |
|---|---|---|---|
| NDCG@3 | 0.9034 | 0.8795 | 56 / 30 / 5 |
| precision@3 | 0.2418 | 0.2674 | 20 / 24 / 47 |

The two metrics point in different directions: the platform's ranking tends to put days with more
realised demand near the top (NDCG), while the baseline more often names exactly the busiest three
(precision). Neither is declared better. For scale only — this is arithmetic, not part of the
protocol — choosing 3 of 14 days at random would share 3/14 ≈ 0.214 of the top 3 on average.

## 5. What is not claimed

- That the list helps run a hotel, raises revenue, or is better than a manager's own judgement.
- That the platform's ranking is better than the baseline: the protocol declares no winner.
- Anything about real hotels beyond the two in the frozen dataset.
- The served ranking caps each forecast at room capacity; the offline dataset has no capacity, so
  the evaluation ranks unclamped forecasts. The two differ only when several days exceed capacity.
