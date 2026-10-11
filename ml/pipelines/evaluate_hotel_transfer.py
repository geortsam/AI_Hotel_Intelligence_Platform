"""R2.2b: run the pre-registered ``hotel_transfer_v1`` diagnostic experiment.

    python -m ml.pipelines.evaluate_hotel_transfer            # run, write the record
    python -m ml.pipelines.evaluate_hotel_transfer --check    # re-run, compare, write nothing

Refuses to start unless the protocol's settings still hash to the committed checksum; verifies
the dataset against its manifest and the pinned digest; builds the record twice and requires
identical content checksums; writes ``ml/evaluations/hotel_transfer_v1.json``.

The served artifact is never loaded. Its files, when present, are hashed before and after the
run only to show that nothing here touched them. The four models live only in memory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

import numpy
import scipy
import sklearn

from ml.artifact import DEFAULT_ARTIFACT_DIRECTORY
from ml.hotel_transfer import (
    PROTOCOL_VERSION,
    HotelTransferError,
    assert_protocol_frozen,
    build_record,
)
from ml.loading import (
    REPOSITORY_ROOT,
    DatasetLoadError,
    load_dataset_manifest,
    load_processed_dataset,
    require_versions,
    verify_against_manifest,
)
from ml.manifests import content_checksum, serialise, stamp_content_checksum

DEFAULT_RECORD = REPOSITORY_ROOT / "ml" / "evaluations" / f"{PROTOCOL_VERSION}.json"
#: The commit that put ``hotel_transfer_v1`` and its checksum on ``main``, pushed before the
#: first experiment metric was computed.
PROTOCOL_COMMIT = "2dc0807b27347800756489c958998f8585a8ec95"


def _git(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip()


def _served_files(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("*"))
        if path.is_file()
    }


def evaluate(
    *,
    served_directory: Path = DEFAULT_ARTIFACT_DIRECTORY,
    generated_at: dt.datetime | None = None,
) -> dict[str, object]:
    assert_protocol_frozen()
    manifest = load_dataset_manifest()
    require_versions(manifest, dataset_version="v1", feature_version="v1")
    dataset = load_processed_dataset()
    verify_against_manifest(dataset, manifest)

    served_before = _served_files(served_directory)
    first = build_record(dataset)
    second = build_record(dataset)
    if content_checksum(first) != content_checksum(second):
        raise HotelTransferError("two runs in one process produced different records")
    served_after = _served_files(served_directory)
    if served_after != served_before:
        raise HotelTransferError("the served artifact's files changed during the experiment")

    record = dict(first)
    protocol = record["protocol"]
    assert isinstance(protocol, dict)
    record["protocol"] = {**protocol, "committed_in": PROTOCOL_COMMIT}
    record["generation"] = {
        "note": "Wall-clock, revision and machine metadata, outside the content checksum.",
        "generated_at": (generated_at or dt.datetime.now(dt.UTC)).isoformat(),
        "pipeline": "ml/pipelines/evaluate_hotel_transfer.py",
        "code_revision": _git("rev-parse", "HEAD"),
        "working_tree_clean": _git("status", "--porcelain") == "",
        "runs_compared": 2,
        "served_artifact_files_unchanged": served_before == served_after,
        "served_artifact_sha256": served_before,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.system(),
            "numpy": numpy.__version__,
            "scipy": scipy.__version__,
            "scikit_learn": sklearn.__version__,
        },
    }
    return stamp_content_checksum(record)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="re-run and compare the content checksum with the committed record",
    )
    args = parser.parse_args(argv)

    record = evaluate()
    digest = content_checksum(record)
    if args.check:
        committed = json.loads(DEFAULT_RECORD.read_text(encoding="utf-8"))
        if content_checksum(committed) != digest:
            raise HotelTransferError(f"{DEFAULT_RECORD} differs from a fresh run")
        print(f"{PROTOCOL_VERSION}: the committed record reproduces ({digest})")
        return 0

    DEFAULT_RECORD.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_RECORD.write_bytes(serialise(record))
    print(f"{PROTOCOL_VERSION}: wrote {DEFAULT_RECORD.relative_to(REPOSITORY_ROOT)} ({digest})")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    try:
        raise SystemExit(main())
    except (HotelTransferError, DatasetLoadError) as exc:  # pragma: no cover
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
