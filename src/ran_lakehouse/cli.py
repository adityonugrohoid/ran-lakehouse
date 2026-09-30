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
    build_gold = commands.add_parser(
        "gold", help="build gold KPIs from silver through dbt, one UTC day at a time"
    )
    build_gold.add_argument("--warehouse", required=True, help="Lakekeeper warehouse name")
    planning = commands.add_parser(
        "planning", help="generate the expansion area's planning data into gold tables"
    )
    planning.add_argument("--warehouse", required=True, help="Lakekeeper warehouse name")
    solve = commands.add_parser(
        "plan", help="solve a constraint set (contract/plan_constraints.schema.json) on gold"
    )
    solve.add_argument("--warehouse", required=True, help="Lakekeeper warehouse name")
    solve.add_argument("--constraints", required=True, type=Path, help="constraint set, JSON")
    serve = commands.add_parser("serve", help="run the HTTP API against a warehouse")
    add_api_arguments(serve)
    serve.add_argument("--host", required=True, help="address to listen on, e.g. 127.0.0.1")
    serve.add_argument("--port", type=int, required=True, help="port to listen on")
    sample = commands.add_parser(
        "export-sample",
        help="write a small fixed dataset and recorded API responses (rule A4)",
    )
    add_api_arguments(sample)
    sample.add_argument("--out", type=Path, required=True, help="directory to write the sample to")
    trace = commands.add_parser(
        "lineage",
        help="trace one gold KPI value to its silver rows, bronze rows and source files",
    )
    trace.add_argument("--warehouse", required=True, help="Lakekeeper warehouse name")
    trace.add_argument("--kpi", required=True, help="KPI id, for example LTE_ERAB_DROP")
    trace.add_argument("--version", type=int, required=True, help="formula version")
    trace.add_argument("--cell", required=True, help="cell name")
    trace.add_argument("--granularity", required=True, choices=["15m", "hour", "day", "week"])
    trace.add_argument(
        "--period",
        required=True,
        help="UTC start (ISO, with offset) for 15m and hour; WIB date for day and week",
    )
    return parser


def add_api_arguments(parser: argparse.ArgumentParser) -> None:
    """Arguments shared by serve and export-sample.

    Args:
        parser: The subcommand parser.
    """
    parser.add_argument("--warehouse", required=True, help="Lakekeeper warehouse name")
    parser.add_argument("--profile", required=True, help="world profile of the run")
    parser.add_argument("--run-weeks", type=int, required=True, help="weeks in the whole run")


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
    if args.command == "planning":
        from ran_lakehouse.lake.catalog import connect
        from ran_lakehouse.model import default_model
        from ran_lakehouse.planning.build import build_plan, tables, write_gold
        from ran_lakehouse.world import build_world

        data = tables(build_plan(default_model(build_world("demo"))))
        write_gold(connect(args.warehouse), data)
        print(json.dumps({name: table.num_rows for name, table in data.items()}, indent=2))
        return 0
    if args.command == "plan":
        from ran_lakehouse.lake.catalog import connect
        from ran_lakehouse.planning.solver import BACKEND, PlanningData, solve_plan

        con = connect(args.warehouse)
        names = ("villages", "candidate_sites", "coverage", "backhaul_power_options")
        planning_data = PlanningData.from_tables(
            {n: con.execute(f"SELECT * FROM lk.gold.{n}").to_arrow_table() for n in names}
        )
        constraints = json.loads(args.constraints.read_text())
        print(json.dumps(solve_plan(planning_data, constraints, BACKEND), indent=2))
        return 0
    if args.command == "serve":
        import uvicorn

        from ran_lakehouse.api.serve import app_for

        app = app_for(args.warehouse, args.profile, args.run_weeks, RUNS)
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    if args.command == "export-sample":
        from ran_lakehouse.api.sample import export_sample

        summary = export_sample(args.warehouse, args.profile, args.run_weeks, RUNS, args.out)
        print(json.dumps(summary, indent=2))
        return 0
    if args.command == "lineage":
        from datetime import date, datetime

        from ran_lakehouse.lake.catalog import connect
        from ran_lakehouse.lake.lineage import lineage

        if args.granularity in ("15m", "hour"):
            period: date | datetime = datetime.fromisoformat(args.period)
        else:
            period = date.fromisoformat(args.period)
        rows = lineage(
            connect(args.warehouse), args.kpi, args.version, args.cell, args.granularity, period
        )
        if not rows:
            raise SystemExit("no such gold KPI value")
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if args.command == "gold":
        from datetime import UTC, datetime

        from ran_lakehouse.lake.gold import KPI_REVISION, GoldBuild, Target

        load_id = f"gold-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        build = GoldBuild(Target("lake", args.warehouse), load_id, KPI_REVISION)
        print(json.dumps(build.run(), indent=2))
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
