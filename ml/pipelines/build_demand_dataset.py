"""Build the versioned offline training dataset.

    python -m ml.pipelines.build_demand_dataset --download
    python -m ml.pipelines.build_demand_dataset --source ml/data/raw/hotels.csv

Acquisition, transformation, checksums and the manifest, in one command. **It trains nothing**
and imports nothing that could.

The raw file is written to ``ml/data/raw/`` and the processed dataset to ``ml/data/processed/``;
both are payloads and both are ignored by ``.gitignore``. What *is* committed is the manifest in
``ml/manifests/`` -- the checksums, the date ranges and the partition boundaries -- because that
is what makes the ignored payload verifiable rather than merely absent.

Reproducibility is checked rather than asserted: ``--verify`` builds the dataset twice from the
same bytes and compares the two checksums.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import urllib.request
from pathlib import Path

from ml.pipelines.offline_demand import (
    OFFLINE_DATASET_NAME,
    SOURCE_BYTES,
    SOURCE_SHA256,
    SOURCE_URL,
    OfflineSourceError,
    build_from_source,
    serialise_manifest,
    sha256_hex,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW = REPOSITORY_ROOT / "ml" / "data" / "raw" / f"{OFFLINE_DATASET_NAME}_source.csv"
DEFAULT_PROCESSED = REPOSITORY_ROOT / "ml" / "data" / "processed" / f"{OFFLINE_DATASET_NAME}.csv"
DEFAULT_MANIFEST = REPOSITORY_ROOT / "ml" / "manifests" / f"{OFFLINE_DATASET_NAME}.json"


def download(destination: Path) -> bytes:
    """Fetch the pinned source file and return its bytes.

    The URL names a commit, so what comes back is fixed for all time; the checksum check in
    :func:`verify_source` is what turns that from an expectation into a guarantee.
    """
    if not SOURCE_URL.startswith("https://"):  # pragma: no cover - the constant is a literal
        raise OfflineSourceError("the source URL must be https")
    with urllib.request.urlopen(SOURCE_URL, timeout=300) as response:
        payload: bytes = response.read()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return payload


def verify_source(payload: bytes) -> str:
    """Refuse anything that is not the file this pipeline was written against."""
    checksum = sha256_hex(payload)
    if checksum != SOURCE_SHA256:
        raise OfflineSourceError(
            f"source checksum mismatch: expected {SOURCE_SHA256}, got {checksum} "
            f"({len(payload)} bytes, expected {SOURCE_BYTES}). The dataset at the pinned URL "
            "is not the one this pipeline was written against; investigate rather than "
            "updating the constant."
        )
    return checksum


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--download", action="store_true", help="fetch the pinned source file")
    group.add_argument("--source", type=Path, help="use an already-downloaded source file")
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--processed", type=Path, default=DEFAULT_PROCESSED)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="build twice from the same bytes and require identical checksums",
    )
    args = parser.parse_args(argv)

    if args.download:
        print(f"downloading {SOURCE_URL}")
        payload = download(args.raw)
        raw_path = args.raw
    else:
        raw_path = args.source
        payload = raw_path.read_bytes()
    checksum = verify_source(payload)
    print(f"source      : {raw_path} ({len(payload)} bytes)")
    print(f"source sha256: {checksum}")

    with raw_path.open(newline="", encoding="utf-8") as handle:
        result = build_from_source(handle, source_checksum=checksum)

    if args.verify:
        with raw_path.open(newline="", encoding="utf-8") as handle:
            again = build_from_source(handle, source_checksum=checksum)
        if again.processed_checksum != result.processed_checksum:
            print("::error::the pipeline is not deterministic", file=sys.stderr)
            return 1
        print(f"reproducible: two builds agree on {result.processed_checksum}")

    dataset = result.dataset
    args.processed.parent.mkdir(parents=True, exist_ok=True)
    args.processed.write_bytes(result.processed)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_bytes(serialise_manifest(result.manifest))

    first, last = min(r.target_date for r in dataset.rows), max(r.target_date for r in dataset.rows)
    print(f"processed   : {args.processed} ({len(result.processed)} bytes)")
    print(f"processed sha256: {result.processed_checksum}")
    print(f"manifest    : {args.manifest}")
    print(f"rows        : {len(dataset.rows)} over {first} .. {last}")
    print(f"hotels      : {len(dataset.hotel_keys)}")
    print(
        "partitions  : "
        f"train={len(dataset.split.train)} "
        f"validation={len(dataset.split.validation)} "
        f"test={len(dataset.split.test)} "
        f"(train<= {dataset.split.train_end}, validation<= {dataset.split.validation_end})"
    )
    print(f"rejected    : {len(dataset.parsed.rejected)} source rows")
    for reason, count in dataset.parsed.rejection_counts.items():
        print(f"    {count:>7} {reason}")
    print(f"no model was trained (generated {dt.datetime.now(dt.UTC).date()})")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
