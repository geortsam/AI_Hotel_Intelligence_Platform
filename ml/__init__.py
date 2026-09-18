"""Offline machine-learning work: data acquisition, preparation and (later) training.

Deliberately OUTSIDE ``backend/``. Nothing here is imported by the application, and the
repository's ``.dockerignore`` excludes this whole directory from the backend build context, so
neither the code below nor the data it writes can reach the API image.

The dependency runs one way only: this package imports :mod:`app.ml.dataset` -- the pure Stage
6.1 contract -- so that an offline dataset is held to the same rules as one built from the
production database. It imports nothing from the web layer, and the web layer imports nothing
from here.
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
