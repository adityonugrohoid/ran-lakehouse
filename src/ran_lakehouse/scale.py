"""Scale test (rule E1): one simulated day through the whole pipeline, measured.

`ranlake scale-test` runs each stage in a process of its own, so each has
its own wall time and peak resident set (from os.wait4), and the peak size of
DuckDB's spill folder, sampled every second:
- collect: build the network, simulate the day, render the files, deliver
  and collect them into bronze (one process; its timings split simulate,
  render and collect);
- silver: build the day's partitions;
- gold: build the day's KPIs through dbt.
Then it counts rows and Iceberg storage per layer and writes a run record.

`python -m ran_lakehouse.scale report --slice R --full R` writes
results/scale_test.md and .json from the slice run (the demo profile) and
the full run (the scale profile): files and rows per second, storage per
cell-day, processing time per 15-minute period, and an extrapolation to
250,000 cells with its method and assumptions. One run each, stated so.
Synthetic network.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ran_lakehouse.lake.catalog import SPILL_DIR

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "scale_test.json"
RECORD_MD = RESULTS / "scale_test.md"
LANDING = REPO_ROOT / "landing"
PERIODS_PER_DAY = 96
TARGET_CELLS = 250_000
STAGES = ("collect", "silver", "gold")
SAMPLE_S = 1.0  # spill folder sampling interval
LAYERS = ("bronze", "silver", "gold")


def run_stage(stage: str, profile: str, warehouse: str, days: int) -> dict[str, Any]:
    """Run one stage in this process and return what it did.

    Args:
        stage: "model", "collect", "silver" or "gold".
        profile: World profile.
        warehouse: Warehouse name.
        days: Days to simulate (collect).

    Returns:
        The stage's own counts and timings.

    Raises:
        ValueError: For an unknown stage.
    """
    if stage == "model":
        from ran_lakehouse.model import default_model
        from ran_lakehouse.world import build_world

        model = default_model(build_world(profile))
        tech = model.state.technology
        return {
            "cells": len(tech),
            "lte_cells": int((tech == "LTE").sum()),
            "gsm_cells": int((tech == "GSM").sum()),
            "grid_points": int(model.grid.x_km.size),
        }
    if stage == "collect":
        from ran_lakehouse.collect.backfill import drive

        weeks = max(1, -(-days // 7))
        return drive(profile, weeks, 0, days, warehouse, LANDING, None)
    if stage == "silver":
        from ran_lakehouse.lake import silver
        from ran_lakehouse.lake.catalog import connect

        con = connect(warehouse)
        silver.create_tables(con, True)
        stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        return silver.SilverBuild(con, f"silver-{stamp}", silver.GRACE).run(None)
    if stage == "gold":
        from ran_lakehouse.lake.gold import KPI_REVISION, GoldBuild, Target

        stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        return GoldBuild(Target("lake", warehouse), f"gold-{stamp}", KPI_REVISION).run()
    raise ValueError(f"unknown stage {stage}")


def measured(stage: str, profile: str, warehouse: str, days: int) -> dict[str, Any]:
    """Run a stage as a child process and measure it.

    Args:
        stage: Stage name.
        profile: World profile.
        warehouse: Warehouse name.
        days: Days to simulate.

    Returns:
        Wall time, peak resident set (MB) and the stage's own output.

    Raises:
        RuntimeError: If the stage fails or is killed.
    """
    args = [
        sys.executable,
        "-m",
        "ran_lakehouse.scale",
        "stage",
        stage,
        "--profile",
        profile,
        "--warehouse",
        warehouse,
        "--days",
        str(days),
    ]
    started = time.perf_counter()
    child = subprocess.Popen(args, stdout=subprocess.PIPE, text=True)
    chunks: list[str] = []
    # Drain the child's output while sampling, so a full pipe never blocks it.
    reader = threading.Thread(
        target=lambda: chunks.append(child.stdout.read() if child.stdout else "")
    )
    reader.start()
    spill_peak = 0
    while True:
        pid, status, usage = os.wait4(child.pid, os.WNOHANG)
        if pid:
            break
        spill_peak = max(spill_peak, folder_bytes(SPILL_DIR))
        time.sleep(SAMPLE_S)
    reader.join()
    seconds = time.perf_counter() - started
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise RuntimeError(f"stage {stage} failed (wait status {status})")
    return {
        "wall_s": round(seconds, 1),
        # Linux reports ru_maxrss in kB.
        "peak_rss_mb": round(usage.ru_maxrss / 1024.0),
        "peak_spill_mb": round(spill_peak / 1e6),
        "output": json.loads("".join(chunks).strip().splitlines()[-1]),
    }


def folder_bytes(folder: Path) -> int:
    """Bytes of the files in a folder tree (0 when it does not exist).

    Args:
        folder: Folder.

    Returns:
        Total size.
    """
    if not folder.exists():
        return 0
    total = 0
    for path in folder.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            # DuckDB removes spill files as it goes; one gone between the
            # listing and the stat no longer counts.
            continue
    return total


def layer_sizes(warehouse: str) -> dict[str, dict[str, int]]:
    """Rows, data files and bytes per layer, from the current snapshots.

    Args:
        warehouse: Warehouse name.

    Returns:
        Layer to rows, data files and bytes.
    """
    from ran_lakehouse.lake.catalog import pyiceberg

    lake = pyiceberg(warehouse)
    out: dict[str, dict[str, int]] = {}
    for layer in LAYERS:
        rows = files = size = 0
        for identifier in lake.list_tables(layer):
            tasks = list(lake.load_table(identifier).scan().plan_files())
            files += len(tasks)
            size += sum(t.file.file_size_in_bytes for t in tasks)
            rows += sum(t.file.record_count for t in tasks)
        out[layer] = {"rows": rows, "data_files": files, "bytes": size}
    return out


def scale_run(profile: str, warehouse: str, days: int, record: Path) -> dict[str, Any]:
    """The whole measured run, written to a record.

    Args:
        profile: World profile.
        warehouse: A new warehouse name.
        days: Days to simulate.
        record: Where to write the run record (runs/ is not committed).

    Returns:
        The run record.
    """
    started = time.perf_counter()
    network = measured("model", profile, warehouse, days)
    stages = {stage: measured(stage, profile, warehouse, days) for stage in STAGES}
    out = {
        "profile": profile,
        "warehouse": warehouse,
        "days": days,
        "network": network["output"],
        "network_build": {k: network[k] for k in ("wall_s", "peak_rss_mb")},
        "stages": stages,
        "layers": layer_sizes(warehouse),
        "wall_s": round(time.perf_counter() - started, 1),
        "finished": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(out, indent=2) + "\n")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """Run a stage (internal), or write the report.

    Args:
        argv: Arguments.

    Returns:
        Exit code.
    """
    parser = argparse.ArgumentParser(prog="python -m ran_lakehouse.scale")
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage", help="run one stage in this process (internal)")
    stage.add_argument("name", choices=("model", *STAGES))
    stage.add_argument("--profile", required=True)
    stage.add_argument("--warehouse", required=True)
    stage.add_argument("--days", type=int, required=True)
    report = sub.add_parser("report", help="write results/scale_test.md and .json")
    report.add_argument("--slice", type=Path, required=True, help="run record of the slice")
    report.add_argument("--full", type=Path, required=True, help="run record of the full run")
    args = parser.parse_args(argv)
    if args.command == "stage":
        out = run_stage(args.name, args.profile, args.warehouse, args.days)
        print(json.dumps(out, default=str))
        return 0
    from ran_lakehouse.scale_report import write

    write(json.loads(args.slice.read_text()), json.loads(args.full.read_text()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
