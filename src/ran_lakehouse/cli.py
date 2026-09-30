"""The `ranlake` command line."""

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

from ran_lakehouse import __version__

RUNS = Path("runs")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the `ranlake` command.

    Returns:
        The parser, with one subcommand required.
    """
    parser = argparse.ArgumentParser(
        prog="ranlake",
        description="Mini telecom lakehouse for a synthetic LTE and GSM operator.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("version", help="print the package version")
    backfill = commands.add_parser(
        "backfill", help="simulate a run's history and collect it into bronze, unpaced"
    )
    add_run_arguments(backfill)
    backfill.add_argument("--weeks", type=int, required=True, help="weeks of history")
    backfill.add_argument(
        "--run-weeks",
        type=int,
        required=True,
        help="weeks in the whole run (a live run can continue after the history)",
    )
    run = commands.add_parser(
        "run", help="simulate and collect days on a real or accelerated clock (labelled)"
    )
    add_run_arguments(run)
    run.add_argument("--weeks", type=int, required=True, help="weeks in the whole run")
    run.add_argument("--first-day", type=int, required=True, help="first day to simulate")
    run.add_argument("--days", type=int, required=True, help="days to simulate")
    run.add_argument(
        "--speedup",
        type=float,
        required=True,
        help="simulated seconds per wall second: 1 is the real 15-minute cadence",
    )
    build_silver = commands.add_parser(
        "silver", help="build silver from bronze: every partition whose cutoff has passed"
    )
    build_silver.add_argument("--warehouse", required=True, help="Lakekeeper warehouse name")
    return parser


def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    """Arguments shared by backfill and run.

    Args:
        parser: The subcommand parser.
    """
    parser.add_argument("--profile", required=True, choices=["demo", "tiny"])
    parser.add_argument("--warehouse", required=True, help="Lakekeeper warehouse name")
    parser.add_argument(
        "--landing", type=Path, default=Path("landing"), help="landing root (default: landing)"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the `ranlake` command.

    Args:
        argv: Command-line arguments without the program name, or None to
            read them from sys.argv.

    Returns:
        The process exit code.

    Raises:
        AssertionError: If a parsed subcommand has no handler.
    """
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    # Imported here so that `ranlake version` does not load the lake stack.
    from ran_lakehouse.collect.backfill import Clock, Pacer, drive

    if args.command == "silver":
        from datetime import UTC, datetime

        from ran_lakehouse.lake import silver
        from ran_lakehouse.lake.catalog import connect

        con = connect(args.warehouse)
        silver.create_tables(con, True)
        load_id = f"silver-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        print(json.dumps(silver.SilverBuild(con, load_id, silver.GRACE).run(None), indent=2))
        return 0
    if args.command == "backfill":
        stats = drive(
            args.profile, args.run_weeks, 0, 7 * args.weeks, args.warehouse, args.landing, None
        )
        print(json.dumps(stats, indent=2))
        return 0
    if args.command == "run":
        if args.speedup <= 0:
            raise SystemExit("--speedup must be positive")
        pacer = Pacer(Clock(args.speedup), RUNS / args.warehouse / "status.json")
        stats = drive(
            args.profile,
            args.weeks,
            args.first_day,
            args.days,
            args.warehouse,
            args.landing,
            pacer,
        )
        print(json.dumps(stats, indent=2))
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
