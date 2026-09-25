"""The copilot evaluation harness (Stage 7.8). `docs/copilot-evaluation.md` is its specification.

    questions.py        copilot_eval_v1: the frozen, checksummed question set
    fixture_hotel.py    one fictional hotel's service responses, built through the real schemas
    replay.py           the replay format, the replaying model and the recording model
    scorers.py          mechanical scorers, independent of the production figure check
    harness.py          runs a case through the REAL copilot stack and scores it; the report
    fixtures/           reference_replays.json -- hand-written reference exchanges
    reference_report.json   the pinned report of those exchanges; CI fails if it moves

**What CI evaluates is the pipeline and the scorers, not a language model.** The reference
exchanges were written by hand. The first evaluation of a real model is a live capture run by
someone holding a provider key (`scripts/copilot_live_eval.py`), never by CI.
"""
