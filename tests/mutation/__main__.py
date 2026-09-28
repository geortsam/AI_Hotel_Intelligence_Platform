"""``python -m tests.mutation`` -- re-run the recorded mutation checks. See ``harness.py``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from tests.mutation.harness import Verdict, discover, exit_code, run
from tests.mutation.inventory import MUTATIONS


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.mutation", description=__doc__)
    parser.add_argument("--only", nargs="+", metavar="ID", help="run these mutation ids")
    parser.add_argument(
        "--stage", nargs="+", metavar="STAGE", help="run these stages, e.g. 7.10 F2"
    )
    parser.add_argument(
        "--database-url",
        help="a disposable PostgreSQL database for the mutations that need one "
        "(scripts/testdb.py create); never read from TEST_DATABASE_URL",
    )
    parser.add_argument("--no-frontend", action="store_true", help="skip the Vitest mutations")
    parser.add_argument(
        "--require-all", action="store_true", help="fail when any selected mutation could not run"
    )
    parser.add_argument("--list", action="store_true", help="print the inventory and exit")
    parser.add_argument(
        "--discover",
        action="store_true",
        help="print every failing test per mutation; judge nothing",
    )
    args = parser.parse_args(argv)

    selected = [
        m
        for m in MUTATIONS
        if (args.only is None or m.id in args.only)
        and (args.stage is None or m.stage in args.stage)
    ]
    unknown = set(args.only or ()) - {m.id for m in MUTATIONS}
    if unknown or not selected:
        parser.error(f"no such mutation: {sorted(unknown)}" if unknown else "nothing selected")

    if args.list:
        for m in selected:
            flags = " [database]" if m.needs_database else ""
            print(f"{m.id:<10} {m.runner:<7}{flags} {m.breaks}")
            for killer in m.killers:
                print(f"{'':>11}must fail: {killer}")
        return 0
    if args.discover:
        discover(selected, database_url=args.database_url)
        return 0

    outcomes = run(selected, database_url=args.database_url, frontend=not args.no_frontend)
    counts = {verdict: sum(o.verdict is verdict for o in outcomes) for verdict in Verdict}
    print(
        f"\n{len(outcomes)} mutations: {counts[Verdict.KILLED]} killed, "
        f"{counts[Verdict.SURVIVED]} survived, {counts[Verdict.NOT_RUN]} not run, "
        f"{counts[Verdict.ERROR]} errors"
    )
    for outcome in outcomes:
        if outcome.verdict is not Verdict.KILLED:
            print(f"  {outcome.verdict:<9} {outcome.mutation.id}: {outcome.detail}")
    return exit_code(outcomes, require_all=args.require_all)


if __name__ == "__main__":
    sys.exit(main())
