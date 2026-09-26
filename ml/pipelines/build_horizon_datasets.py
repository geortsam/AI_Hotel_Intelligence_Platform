"""Build the three Stage 7.14 horizon-matched datasets from the pinned Stage 6.2 source.

    python -m ml.pipelines.build_horizon_datasets            # build, verify, write
    python -m ml.pipelines.build_horizon_datasets --check    # build, verify, compare; write nothing

Each dataset is built **twice** from the same source bytes and must come back byte-identical,
with an identical SHA-256, before anything is written; and that SHA-256 must be the one the frozen
``multi_horizon_v1`` protocol records. ``demand_daily_v1`` is not read, rebuilt or touched.

Offline only, and it trains nothing. The raw source is not committed (it is 17 MB and
``.gitignore`` excludes it); the processed CSVs and their manifests are, as ``demand_daily_v1``'s
are, so CI can measure without downloading anything.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from ml.horizons import HORIZONS, HorizonError, HorizonSpec, build_horizon_dataset
from ml.pipelines.build_demand_dataset import DEFAULT_RAW, verify_source
from ml.pipelines.offline_demand import (
    OfflineSourceError,
    read_source,
    serialise_manifest,
)


def build_twice(raw: bytes, spec: HorizonSpec, source_checksum: str) -> tuple[bytes, bytes]:
    """Two independent builds; returns the processed bytes and the manifest bytes of the first."""
    builds = []
    for _ in range(2):
        parsed = read_source(io.StringIO(raw.decode("utf-8"), newline=""))
        builds.append(build_horizon_dataset(parsed, spec, source_checksum=source_checksum))
    first, second = builds
    if first.processed != second.processed or first.sha256 != second.sha256:
        raise HorizonError(f"{spec.dataset_name}: two builds from the same bytes differ")
    if first.sha256 != spec.dataset_sha256:
        raise HorizonError(
            f"{spec.dataset_name}: built {first.sha256}, but multi_horizon_v1 records "
            f"{spec.dataset_sha256}"
        )
    return first.processed, serialise_manifest(first.manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_RAW)
    parser.add_argument(
        "--check",
        action="store_true",
        help="build and verify, then compare with the committed files instead of writing",
    )
    args = parser.parse_args(argv)

    raw = args.source.read_bytes()
    source_checksum = verify_source(raw)

    for spec in HORIZONS:
        processed, _manifest = build_twice(raw, spec, source_checksum)
        if args.check:
            committed = spec.dataset_path.read_bytes() if spec.dataset_path.is_file() else b""
            if committed != processed:
                raise HorizonError(f"{spec.dataset_path} differs from a fresh build")
            print(f"{spec.dataset_name}: matches ({spec.dataset_sha256})")
            continue
        spec.dataset_path.parent.mkdir(parents=True, exist_ok=True)
        spec.dataset_path.write_bytes(processed)
        spec.manifest_path.write_bytes(_manifest)
        print(f"{spec.dataset_name}: {len(processed)} bytes, sha256 {spec.dataset_sha256}")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    try:
        raise SystemExit(main())
    except (HorizonError, OfflineSourceError, OSError) as exc:  # pragma: no cover
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
