# Analytics design — moved

> **Redirect.** This page was the analytics design's stage snapshot at Stage 3B.10. On
> 2026-11-25 (roadmap R6.1) its content was consolidated into
> [architecture.md §5](architecture.md#analytics-stage-3b10), which is maintained; the snapshot
> as last written is in the repository's history at `2a07405`
> (`git show 2a07405:docs/analytics-design.md`). Code comments and stage records that cite this
> path — for the metric definitions in particular — find each section here:

| Section of the snapshot | Now in |
|---|---|
| 1. `daily_hotel_metrics` — role, and why analytics does not use it | [architecture.md §5, Analytics](architecture.md#analytics-stage-3b10) and [the database README §3.11](../database/README.md#311-daily_hotel_metrics) |
| 2. Metric definitions — sources, date columns, Issue F1's hotel-local days, occupancy, ADR and RevPAR | [architecture.md §5, Analytics](architecture.md#analytics-stage-3b10) |
| 3. Known limitations | the same section, under **Limitations** |
| 4. Currency | the same section, under **Currency** |
| 5. The `is_room_revenue` split | the same section |
| 6. Join multiplication | the same section |
| 7. Endpoints | the same section, under **Routes** |
| 8. Deferred to later stages | the same section, and [the database README §8](../database/README.md#8-daily-metrics-strategy) |
