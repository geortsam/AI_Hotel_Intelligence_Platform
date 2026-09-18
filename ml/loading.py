"""Read the Stage 6.2 processed dataset back, and refuse to proceed on anything but the file
the committed manifest describes.

Stage 6.2 wrote a dataset and a manifest. This module closes the loop: it reads the bytes,
hashes them, and compares the digest -- plus the row count, the date range, the column list and
both version strings -- against the manifest that was committed alongside. An evaluation run
against a *different* dataset than the one its manifest names would produce numbers that look
exactly as legitimate as real ones, which is why the check is a refusal rather than a warning.

Nothing here trains, predicts or scores. It is `csv`, `hashlib` and `json`.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ml.pipelines.offline_demand import IDENTITY_COLUMNS

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = REPOSITORY_ROOT / "ml" / "data" / "processed" / "demand_daily_v1.csv"
DEFAULT_DATASET_MANIFEST = REPOSITORY_ROOT / "ml" / "manifests" / "demand_daily_v1.json"


class DatasetLoadError(Exception):
    """The dataset on disk is not the dataset the manifest describes.

    Always a refusal to continue. There is no recovery that does not involve a human deciding
    which of the two is wrong.
    """


@dataclass(frozen=True, slots=True)
class ProcessedRow:
    """One row of the processed dataset, parsed but not interpreted.

    ``features`` keeps ``None`` for an absent value rather than substituting a zero, because
    Stage 6.1 wrote the empty string precisely to distinguish the two and the distinction is
    what stops a missing lag being learned as "sold nothing".
    """

    hotel_key: str
    hotel_public_id: uuid.UUID
    target_date: dt.date
    horizon_days: int
    prediction_cutoff: dt.datetime
    partition: str
    target_room_nights: int
    features: Mapping[str, float | None]


@dataclass(frozen=True, slots=True)
class ProcessedDataset:
    """The rows, the feature columns, and the digest of the exact bytes they came from."""

    rows: tuple[ProcessedRow, ...]
    feature_names: tuple[str, ...]
    sha256: str
    size_bytes: int
    path: Path

    @property
    def dates(self) -> tuple[dt.date, ...]:
        """Every distinct target date, ascending. The axis every fold is cut along."""
        return tuple(sorted({row.target_date for row in self.rows}))

    @property
    def hotel_keys(self) -> tuple[str, ...]:
        return tuple(sorted({row.hotel_key for row in self.rows}))


def _parse_feature(raw: str) -> float | None:
    """Empty means absent. Everything else is a number, and is required to be one."""
    if raw == "":
        return None
    try:
        return float(int(raw))
    except ValueError:
        return float(raw)


def parse_processed_csv(payload: bytes, *, path: Path = DEFAULT_DATASET) -> ProcessedDataset:
    """Parse the dataset from its bytes, hashing exactly what was parsed.

    Hashing the bytes rather than re-serialising the parsed rows is the point: it is the file
    that the manifest made a claim about, and a round trip through this parser could agree with
    itself while disagreeing with what Stage 6.2 wrote.
    """
    digest = hashlib.sha256(payload).hexdigest()
    text = payload.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = tuple(reader.fieldnames or ())
    missing = [column for column in IDENTITY_COLUMNS if column not in header]
    if missing:
        raise DatasetLoadError(f"{path} is missing identity column(s): {', '.join(missing)}")
    feature_names = tuple(name for name in header if name not in IDENTITY_COLUMNS)
    if not feature_names:
        raise DatasetLoadError(f"{path} carries no feature columns")

    rows: list[ProcessedRow] = []
    for line, record in enumerate(reader, start=2):
        try:
            rows.append(
                ProcessedRow(
                    hotel_key=record["hotel_key"],
                    hotel_public_id=uuid.UUID(record["hotel_public_id"]),
                    target_date=dt.date.fromisoformat(record["target_date"]),
                    horizon_days=int(record["horizon_days"]),
                    prediction_cutoff=dt.datetime.fromisoformat(record["prediction_cutoff"]),
                    partition=record["partition"],
                    target_room_nights=int(record["target_room_nights"]),
                    features={name: _parse_feature(record[name]) for name in feature_names},
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DatasetLoadError(f"{path} line {line} is malformed: {exc}") from exc

    if not rows:
        raise DatasetLoadError(f"{path} contains a header and no rows")
    return ProcessedDataset(
        rows=tuple(rows),
        feature_names=feature_names,
        sha256=digest,
        size_bytes=len(payload),
        path=path,
    )


def load_processed_dataset(path: Path = DEFAULT_DATASET) -> ProcessedDataset:
    """Read the dataset from disk.

    ``read_bytes`` and not ``open(..., newline="")``: the digest must be taken over the file as
    it sits, and ``.gitattributes`` pins this path to LF so that what sits there is the same on
    every platform.
    """
    if not path.is_file():
        raise DatasetLoadError(
            f"{path} does not exist. Build it with "
            "`python -m ml.pipelines.build_demand_dataset --download`."
        )
    return parse_processed_csv(path.read_bytes(), path=path)


def load_dataset_manifest(path: Path = DEFAULT_DATASET_MANIFEST) -> dict[str, object]:
    if not path.is_file():
        raise DatasetLoadError(f"{path} does not exist")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise DatasetLoadError(f"{path} is not a JSON object")
    return loaded


def verify_against_manifest(dataset: ProcessedDataset, manifest: Mapping[str, object]) -> None:
    """Refuse a dataset the manifest does not describe, and say which claim failed.

    Six claims, checked separately rather than as one digest comparison, because "the checksum
    differs" is a true but useless message when what actually happened is that the row count
    moved.
    """
    block = manifest.get("dataset")
    if not isinstance(block, dict):
        raise DatasetLoadError("the manifest has no 'dataset' block")

    checks: list[tuple[str, object, object]] = [
        ("processed_sha256", block.get("processed_sha256"), dataset.sha256),
        ("processed_bytes", block.get("processed_bytes"), dataset.size_bytes),
        ("rows", block.get("rows"), len(dataset.rows)),
        ("hotels", block.get("hotels"), len(dataset.hotel_keys)),
        ("date_min", block.get("date_min"), dataset.dates[0].isoformat()),
        ("date_max", block.get("date_max"), dataset.dates[-1].isoformat()),
    ]
    for name, expected, actual in checks:
        if expected != actual:
            raise DatasetLoadError(
                f"{dataset.path}: manifest says {name}={expected!r}, dataset has {actual!r}"
            )

    columns = block.get("columns")
    if isinstance(columns, list):
        expected_features = [c for c in columns if c not in IDENTITY_COLUMNS]
        if expected_features != list(dataset.feature_names):
            raise DatasetLoadError(
                f"{dataset.path}: manifest columns {expected_features} do not match "
                f"dataset columns {list(dataset.feature_names)}"
            )


def dataset_versions(manifest: Mapping[str, object]) -> tuple[str, str]:
    """The dataset and feature versions the manifest claims, as a pair."""
    block = manifest.get("dataset")
    if not isinstance(block, dict):
        raise DatasetLoadError("the manifest has no 'dataset' block")
    dataset_version = block.get("dataset_version")
    feature_version = block.get("feature_version")
    if not isinstance(dataset_version, str) or not isinstance(feature_version, str):
        raise DatasetLoadError("the manifest does not declare both version strings")
    return dataset_version, feature_version


def require_versions(
    manifest: Mapping[str, object], *, dataset_version: str, feature_version: str
) -> None:
    """Refuse a dataset built under a contract this evaluation was not written against."""
    found_dataset, found_feature = dataset_versions(manifest)
    if (found_dataset, found_feature) != (dataset_version, feature_version):
        raise DatasetLoadError(
            f"expected dataset_version={dataset_version} feature_version={feature_version}, "
            f"manifest declares {found_dataset} / {found_feature}: a version change is a new "
            "dataset and must not be substituted silently"
        )


def rows_by_hotel_and_date(
    rows: Sequence[ProcessedRow],
) -> dict[tuple[str, dt.date], ProcessedRow]:
    """Index for the one lookup that is not a scan: 'what actually happened on that day'."""
    index: dict[tuple[str, dt.date], ProcessedRow] = {}
    for row in rows:
        key = (row.hotel_key, row.target_date)
        if key in index:
            raise DatasetLoadError(f"duplicate observation for {key[0]} on {key[1]}")
        index[key] = row
    return index
