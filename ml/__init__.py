"""Offline machine-learning work: data acquisition, preparation and (later) training.

Deliberately OUTSIDE ``backend/``, and for most of this project's life nothing here could
reach the API image at all. Two stages changed that, and the change is narrow in both cases:

* **Stage 6.6** let the application import this package through exactly one module,
  ``backend/app/ml/artifact_store.py``, which loads the approved artifact behind a lazy import.
  A test enumerates that module as the only importer.
* **Stage 6.7** let ``ml/`` into the backend build context, because the production image
  regenerates the approved model from the committed dataset in a disposable build stage. The
  *image* still receives only an explicit allowlist: thirteen modules and two artifact files.
  No dataset, no notebook, and none of the five pipeline entry points ship.

The dependency still runs one way: this package imports :mod:`app.ml.dataset` -- the pure Stage
6.1 contract -- so that an offline dataset is held to the same rules as one built from the
production database. It imports nothing from the web layer.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The application package lives in backend/, which is not this package's parent. pytest is told
# that by `pythonpath = ["backend"]` and mypy by `mypy_path = "backend"` (both in the root
# pyproject.toml), but `python -m ml.pipelines.build_demand_dataset` is told by nobody -- so the
# same layout fact is stated here too, once, rather than pushed onto whoever runs the command.
#
# Guarded, so that under pytest -- where backend/ is already on the path -- this does nothing.
_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if _BACKEND.is_dir() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
