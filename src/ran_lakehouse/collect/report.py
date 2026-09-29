"""Bronze backfill report (rules P7, L1, D1-D5): the demo run's 12 weeks of
history delivered, collected and loaded into bronze, with its cost.

The delivery anomalies are planted and their answers are evaluation-only
(rule A3), so only counts per kind appear here, never a period or a
network element. Writes results/bronze_backfill.json and
results/bronze_backfill.md.
Run: `uv run python -m ran_lakehouse.collect.report --warehouse <new warehouse>`.
"""

import argparse
import json
import os
import platform
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb
import pyiceberg

from ran_lakehouse.collect.backfill import drive
from ran_lakehouse.collect.collector import RETENTION_DAYS
from ran_lakehouse.lake.bronze import BRONZE, EVALUATION, TABLES
from ran_lakehouse.lake.catalog import DUCKDB_MEMORY_LIMIT, connect
from ran_lakehouse.lake.catalog import pyiceberg as catalog

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "bronze_backfill.json"
RECORD_MD = RESULTS / "bronze_backfill.md"
LANDING = REPO_ROOT / "landing"
PROFILE = "demo"
WEEKS = 12


def scalar(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    """One value from a query.

    Args:
        con: DuckDB with the warehouse attached.
        sql: The query.

    Returns:
        The first column of the first row.

    Raises:
        RuntimeError: If the query returns no row.
    """
    row = con.execute(sql).fetchone()
    if row is None:
        raise RuntimeError(f"no row from {sql}")
    return row[0]


def storage(warehouse: str) -> dict[str, dict[str, int]]:
    """Data files and bytes of each table's current snapshot, read with PyIceberg.

    Args:
        warehouse: The warehouse.

    Returns:
        Table to data file count and bytes.
    """
    lake = catalog(warehouse)
    out = {}
    for table in TABLES:
        name = table.removeprefix("lk.")
        tasks = list(lake.load_table(name).scan().plan_files())
        out[name] = {
            "data_files": len(tasks),
            "bytes": sum(t.file.file_size_in_bytes for t in tasks),
        }
    return out


def build(warehouse: str) -> dict[str, Any]:
    """Run the backfill and measure what it loaded.

    Args:
        warehouse: A new warehouse (bronze must be empty).

    Returns:
        The record.
    """
    run = drive(PROFILE, WEEKS, 0, 7 * WEEKS, warehouse, LANDING, None)
    con = connect(warehouse)
    rows = {t.removeprefix("lk."): int(scalar(con, f"SELECT count(*) FROM {t}")) for t in TABLES}
    by_granularity = {
        f"{minutes} min": int(n)
        for minutes, n in con.execute(
            f"SELECT CAST(epoch(period_end - period_start) / 60 AS INTEGER) AS m, count(*) "
            f"FROM {BRONZE}.pm_values GROUP BY m ORDER BY m"
        ).fetchall()
    }
    by_release = {
        str(release): int(n)
        for release, n in con.execute(
            f"SELECT coalesce(dictionary_release, 'none declared (OMeS)'), count(*) "
            f"FROM {BRONZE}.pm_values GROUP BY 1 ORDER BY 1"
        ).fetchall()
    }
    arrivals = {
        f"{kind} {'loaded' if loaded else 'skipped, same file'}": int(n)
        for kind, loaded, n in con.execute(
            f"SELECT kind, loaded, count(*) FROM {BRONZE}.file_arrivals "
            "GROUP BY kind, loaded ORDER BY kind, loaded DESC"
        ).fetchall()
    }
    planted = {
        str(kind): int(n)
        for kind, n in con.execute(
            f"SELECT kind, count(*) FROM {EVALUATION}.delivery_anomalies "
            "GROUP BY kind ORDER BY kind"
        ).fetchall()
    }
    landing = [p for p in (LANDING / warehouse).glob("*/*") if p.is_file()]
    return {
        "profile": PROFILE,
        "weeks": WEEKS,
        "clock": run["clock"],
        "files_rendered": run["files_rendered"],
        "collector": run["collector"],
        "file_arrivals": arrivals,
        "rows": rows,
        "pm_rows_by_granularity": by_granularity,
        "pm_rows_by_dictionary_release": by_release,
        "delivery_anomalies_planted": planted,
        "storage": storage(warehouse),
        "landing_at_end": {
            "retention_days": RETENTION_DAYS,
            "files": len(landing),
            "bytes": sum(p.stat().st_size for p in landing),
        },
        "timing_s": run["timing_s"],
        "peak_rss_mb": run["peak_rss_mb"],
        "machine": {
            "cpus": os.cpu_count(),
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "pyiceberg": pyiceberg.__version__,
            "duckdb_memory_limit": DUCKDB_MEMORY_LIMIT,
        },
    }


def mb(n: int) -> str:
    """Bytes in MB, one decimal.

    Args:
        n: Bytes.

    Returns:
        The text.
    """
    return f"{n / 1e6:,.1f}"


def render_markdown(record: dict[str, Any]) -> str:
    """Render the report from the record.

    Args:
        record: The record as loaded from JSON.

    Returns:
        The Markdown report.
    """
    total_bytes = sum(s["bytes"] for s in record["storage"].values())
    lines = [
        "# Bronze backfill report",
        "",
        f"Synthetic network, {record['profile']} profile. {record['weeks']} weeks of PM, CM and FM",
        "files from both simulated EMS, delivered to landing at their arrival times with the",
        "planted delivery anomalies (rule D1 to D5), picked up by the collector and loaded into",
        "the bronze Iceberg tables (rules P7, L1). Clock: " + record["clock"] + ". The anomaly",
        "answers are evaluation-only, so only counts per kind appear here. Written by",
        "`python -m ran_lakehouse.collect.report` from `bronze_backfill.json`.",
        "",
        "## Delivery and collection",
        "",
        f"Files rendered: {record['files_rendered']:,}. Deliveries seen by the collector: "
        f"{record['collector']['deliveries']:,}, loaded {record['collector']['loaded']:,}, "
        "skipped as already loaded (same name and hash) "
        f"{record['collector']['skipped_same_file']:,}.",
        "",
        "| File kind and outcome | Deliveries |",
        "|---|---|",
        *[f"| {k} | {v:,} |" for k, v in record["file_arrivals"].items()],
        "",
        "| Planted delivery anomaly | Count |",
        "|---|---|",
        *[f"| {k} | {v:,} |" for k, v in record["delivery_anomalies_planted"].items()],
        "",
        "D5 counts the network elements moved to the renamed dictionary release.",
        "",
        "## Bronze",
        "",
        "| Table | Rows | Data files | MB |",
        "|---|---|---|---|",
        *[
            f"| {t} | {record['rows'][t]:,} | {s['data_files']:,} | {mb(s['bytes'])} |"
            for t, s in record["storage"].items()
        ],
        f"| total | | | {mb(total_bytes)} |",
        "",
        "| PM rows by granularity (rule P1, P4) | Rows |",
        "|---|---|",
        *[f"| {k} | {v:,} |" for k, v in record["pm_rows_by_granularity"].items()],
        "",
        "| PM rows by dictionary release (rule D5) | Rows |",
        "|---|---|",
        *[f"| {k} | {v:,} |" for k, v in record["pm_rows_by_dictionary_release"].items()],
        "",
        f"Landing at the end keeps {record['landing_at_end']['retention_days']} days: "
        f"{record['landing_at_end']['files']:,} files, "
        f"{mb(record['landing_at_end']['bytes'])} MB.",
        "",
        "## Cost",
        "",
        "Measured on the build machine; varies run to run. Peak RSS is the process's",
        "maximum resident set size.",
        "",
        "| Step | Seconds |",
        "|---|---|",
        *[f"| {k} | {v:,} |" for k, v in record["timing_s"].items()],
        "",
        f"Peak RSS: {record['peak_rss_mb']:,} MB.",
        "",
        "| Machine | |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in record["machine"].items()],
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the backfill, then write the record and the Markdown report.

    Args:
        argv: Command-line arguments, or None for sys.argv.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse", required=True, help="a new Lakekeeper warehouse")
    args = parser.parse_args(argv)
    RESULTS.mkdir(exist_ok=True)
    RECORD_JSON.write_text(json.dumps(build(args.warehouse), indent=2) + "\n")
    RECORD_MD.write_text(render_markdown(json.loads(RECORD_JSON.read_text())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
