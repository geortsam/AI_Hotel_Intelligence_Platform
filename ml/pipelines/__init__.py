"""Reproducible offline data-preparation pipelines.

Stage 6.2 adds one: :mod:`ml.pipelines.offline_demand`, which turns a published historical
hotel-booking dataset into the daily room-night demand rows that Stage 6.1 defined, and
:mod:`ml.pipelines.build_demand_dataset`, the command that runs it end to end.

**No model is trained here.** That is a later stage.
"""
