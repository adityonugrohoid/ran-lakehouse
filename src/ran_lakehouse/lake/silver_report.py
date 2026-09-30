"""Silver build report (rules L2, D1 to D5): the demo run's 12 weeks of
bronze built into silver partition by partition, with its cost and what
silver did with every planted delivery anomaly.

The anomaly answers are evaluation-only (rule A3), so only counts per kind
appear here, never a period or a network element. Writes
results/silver_build.json and results/silver_build.md.
Run: `uv run python -m ran_lakehouse.lake.silver_report --warehouse demo`
after the bronze backfill (`python -m ran_lakehouse.collect.report`).
"""

import argparse
import json
import resource
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ran_lakehouse.collect.backfill import EMS_LIST
from ran_lakehouse.collect.delivery import plan_delivery
from ran_lakehouse.lake import silver
from ran_lakehouse.lake.catalog import DUCKDB_MEMORY_LIMIT, commit_retries, connect
from ran_lakehouse.lake.catalog import pyiceberg as catalog
from ran_lakehouse.lake.silver import GRACE, LOADS, MEASUREMENTS, SilverBuild, scalar
from ran_lakehouse.lake.silver_eval import KINDS, evaluate
from ran_lakehouse.model import default_model
from ran_lakehouse.world import build_world

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "silver_build.json"
RECORD_MD = RESULTS / "silver_build.md"
PROFILE = "demo"
WEEKS = 12


def build(warehouse: str) -> dict[str, Any]:
    """Build silver from the warehouse's bronze and measure it.

    Args:
        warehouse: Warehouse holding the bronze backfill and no silver yet.

    Returns:
        The record.

    Raises:
        RuntimeError: If silver was already built in the warehouse.
    """
    con = connect(warehouse)
    silver.create_tables(con, True)
    if scalar(con, f"SELECT count(*) FROM {LOADS}"):
        raise RuntimeError(f"silver already built in {warehouse}; the report needs a fresh build")
    started = time.perf_counter()
    load_id = f"silver-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    stats = SilverBuild(con, load_id, GRACE).run(None)
    build_s = time.perf_counter() - started
    peak_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0)
    started = time.perf_counter()
    rows = {
        t.removeprefix("lk."): int(scalar(con, f"SELECT count(*) FROM {t}")) for t in silver.TABLES
    }
    loads = {
        kind: {
            "loads": int(n),
            "files": int(files),
            "bronze_rows": int(bronze_rows),
            "silver_rows": int(silver_rows),
            "seconds_total": round(float(total), 1),
            "seconds_max": round(float(most), 1),
        }
        for kind, n, files, bronze_rows, silver_rows, total, most in con.execute(
            f"SELECT kind, count(*), sum(files), sum(bronze_rows), sum(silver_rows), "
            f"sum(seconds), max(seconds) FROM {LOADS} GROUP BY kind ORDER BY kind"
        ).fetchall()
    }
    flags = con.execute(
        f"SELECT count(*) FILTER (WHERE derived), count(*) FILTER (WHERE suspect), "
        f"count(*) FILTER (WHERE conflict), count(*) FILTER (WHERE late), "
        f"count(*) FILTER (WHERE granularity_min = 15), "
        f"count(*) FILTER (WHERE granularity_min = 60) FROM {MEASUREMENTS}"
    ).fetchone()
    if flags is None:
        raise RuntimeError("no flag counts from silver")
    mapping = {
        f"{release}: {'3GPP name' if is_3gpp else 'labelled vendor-style'}": int(n)
        for release, is_3gpp, n in con.execute(
            f"SELECT release, measurement NOT LIKE '%vendor-style%', count(*) "
            f"FROM {silver.MAP} GROUP BY ALL ORDER BY ALL"
        ).fetchall()
    }
    model = default_model(build_world(PROFILE))
    plan = plan_delivery(model, EMS_LIST, 7 * WEEKS)
    outcomes = {kind: o.counts() for kind, o in evaluate(con, model, plan).items()}
    lake = catalog(warehouse)
    storage = {}
    for table in silver.TABLES:
        name = table.removeprefix("lk.")
        tasks = list(lake.load_table(name).scan().plan_files())
        storage[name] = {
            "data_files": len(tasks),
            "bytes": sum(t.file.file_size_in_bytes for t in tasks),
        }
    return {
        "profile": PROFILE,
        "weeks": WEEKS,
        "grace_min": GRACE.total_seconds() / 60,
        "partitions_built": stats["partitions"],
        "late_files_merged": stats["catch_up_files"],
        "rows": rows,
        "loads": loads,
        "flags": dict(
            zip(
                ("derived", "suspect", "conflict", "late", "15 min", "60 min"),
                (int(v) for v in flags),
                strict=True,
            )
        ),
        "counter_map": mapping,
        "anomalies": {kind: outcomes[kind] for kind in KINDS},
        "storage": storage,
        "timing_s": {
            "silver build": round(build_s, 1),
            "counts, evaluation and storage": round(time.perf_counter() - started, 1),
        },
        "peak_rss_mb_during_build": peak_mb,
        "commit_retries": commit_retries["count"],
        "duckdb_memory_limit": DUCKDB_MEMORY_LIMIT,
    }


def render_markdown(record: dict[str, Any]) -> str:
    """Render the report from the record.

    Args:
        record: The record as loaded from JSON.

    Returns:
        The Markdown report.
    """
    total_bytes = sum(s["bytes"] for s in record["storage"].values())
    lines = [
        "# Silver build report",
        "",
        f"Synthetic network, {record['profile']} profile. {record['weeks']} weeks of bronze PM",
        "values built into silver one partition (a UTC day of one EMS) at a time, each built",
        f"{record['grace_min']:.0f} minutes after its day ends from the files in by then; files",
        "arriving later are merged into their own hour (rules L2, D1 to D5). The anomaly",
        "answers are evaluation-only, so only counts per kind appear here. Written by",
        "`python -m ran_lakehouse.lake.silver_report` from `silver_build.json`.",
        "",
        "## Planted anomalies",
        "",
        "In scope: periods inside the span of delivered files, without its first and last",
        "period. Found: what silver flagged. Matched: found where it was planted.",
        "",
        "| Kind | Planted | Found | Matched | Silver's handling |",
        "|---|---|---|---|---|",
    ]
    handling = {
        "D1": "file flagged late; merged into its partition if after the build",
        "D2_same": "redelivery counted, loaded once",
        "D2_conflict": "latest version wins, changed values flagged conflict",
        "D3": "gap per network element and period, no row, never zero",
        "D4": "suspect flag carried per value",
        "D5": "both releases mapped, DRB.IPVolDl.sum continuous across the upgrade",
    }
    for kind, c in record["anomalies"].items():
        lines.append(
            f"| {kind} | {c['planted']} | {c['found']} | {c['matched']} | {handling[kind]} |"
        )
    lines += [
        "",
        "D5 counts network elements moved to the renamed release.",
        "",
        "## Silver",
        "",
        "| Table | Rows | Data files | MB |",
        "|---|---|---|---|",
        *[
            f"| {t} | {record['rows'][t]:,} | {s['data_files']:,} | {s['bytes'] / 1e6:,.1f} |"
            for t, s in record["storage"].items()
        ],
        f"| total | | | {total_bytes / 1e6:,.1f} |",
        "",
        "| pm_measurements rows | Rows |",
        "|---|---|",
        *[f"| {k} | {v:,} |" for k, v in record["flags"].items()],
        "",
        "Derived rows are 3GPP measurements converted from vendor-style quantities",
        "(RRU.PrbTotDl from used PRBs, RRU.CellUnavailableTime.sum from availability samples).",
        "",
        "| Counter map (vendor counters per release) | Counters |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in record["counter_map"].items()],
        "",
        "| Loads | Count | Files | Bronze rows read | Silver rows | Seconds, total | "
        "Seconds, max |",
        "|---|---|---|---|---|---|---|",
        *[
            f"| {k} | {v['loads']:,} | {v['files']:,} | {v['bronze_rows']:,} | "
            f"{v['silver_rows']:,} | {v['seconds_total']:,} | {v['seconds_max']} |"
            for k, v in record["loads"].items()
        ],
        "",
        "## Cost",
        "",
        "Measured on the build machine; varies run to run. Peak RSS is the process's",
        f"maximum resident set size during the build (DuckDB memory limit "
        f"{record['duckdb_memory_limit']}).",
        "",
        "| Step | Seconds |",
        "|---|---|",
        *[f"| {k} | {v:,} |" for k, v in record["timing_s"].items()],
        "",
        f"Peak RSS during the build: {record['peak_rss_mb_during_build']:,} MB.",
        "",
        f"Iceberg commits rejected and run again: {record['commit_retries']} (a wall-clock step",
        "makes DuckDB 1.5.x build on a stale snapshot; see `lake.catalog.write`).",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Build silver, then write the record and the Markdown report.

    Args:
        argv: Command-line arguments, or None for sys.argv.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse", required=True, help="warehouse with the bronze backfill")
    args = parser.parse_args(argv)
    RESULTS.mkdir(exist_ok=True)
    RECORD_JSON.write_text(json.dumps(build(args.warehouse), indent=2) + "\n")
    RECORD_MD.write_text(render_markdown(json.loads(RECORD_JSON.read_text())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
