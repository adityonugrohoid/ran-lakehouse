"""Time travel and lineage report (rules D7, D8): one worked example of each.

D7 stages a planted late file on the tiny profile in a fresh warehouse
(lake.timetravel) and reads the changed gold value as of its first
publication and now. D8 traces gold KPI values of the demo warehouse to
their silver rows, bronze rows and source files (lake.lineage). Writes
results/time_travel_lineage.json and results/time_travel_lineage.md.
Run: `uv run python -m ran_lakehouse.lake.lineage_report --warehouse demo`.
"""

import argparse
import json
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ran_lakehouse.lake.catalog import connect
from ran_lakehouse.lake.lineage import LINEAGE_SQL, lineage
from ran_lakehouse.lake.timetravel import demonstrate

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "time_travel_lineage.json"
RECORD_MD = RESULTS / "time_travel_lineage.md"
LANDING = REPO_ROOT / "landing"
# Worked lineage examples on the demo warehouse: a plain counter KPI per
# technology and a value silver derived from two vendor counters.
EXAMPLES = (
    ("LTE_ERAB_DROP", 1, "ENB0001_B3_1", "day", date(2026, 2, 10)),
    ("GSM_TCH_BLOCK", 1, "BTS0001_G900_1", "day", date(2026, 2, 10)),
    ("LTE_PRB_UTIL", 1, "ENB0001_B3_1", "15m", datetime(2026, 2, 10, 4, tzinfo=UTC)),
)
SAMPLE_COLUMNS = (
    "measurement",
    "silver_value",
    "dictionary_release",
    "bronze_counter",
    "bronze_value",
    "managed_element",
    "file_name",
    "arrival_time",
)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What a lineage walk found.

    Args:
        rows: Lineage rows.

    Returns:
        The KPI value, counts of silver rows, bronze rows and files, the
        dictionary releases and vendor counters, the D-case flags seen, and
        the first row.
    """
    silver_rows = {(r["period_start"], r["object_dn"], r["measurement"], r["bin"]) for r in rows}
    return {
        "kpi_value": rows[0]["kpi_value"],
        "coverage": rows[0]["coverage"],
        "silver_rows": len(silver_rows),
        "bronze_rows": sum(r["bronze_value"] is not None for r in rows),
        "files": len({r["file_hash"] for r in rows}),
        "dictionary_releases": sorted({r["dictionary_release"] for r in rows}),
        "vendor_counters": sorted({r["bronze_counter"] for r in rows}),
        "flags": {
            flag: sum(bool(r[flag]) for r in rows)
            for flag in ("derived", "suspect", "late", "conflict")
        },
        "first_row": {k: str(rows[0][k]) for k in SAMPLE_COLUMNS},
    }


def build(warehouse: str) -> dict[str, Any]:
    """Run both worked examples.

    Args:
        warehouse: The demo warehouse (bronze, silver and gold built).

    Returns:
        The record.
    """
    started = time.perf_counter()
    staged = f"time-travel-{datetime.now(UTC):%Y%m%dT%H%M%S}".lower()
    d7 = demonstrate(staged, LANDING / staged)
    d7_s = time.perf_counter() - started
    con = connect(warehouse)
    d8 = []
    started = time.perf_counter()
    for kpi_id, version, cell, granularity, period in EXAMPLES:
        rows = lineage(con, kpi_id, version, cell, granularity, period)
        if not rows:
            raise RuntimeError(f"no gold value for {kpi_id} {cell} {period}")
        d8.append(
            {
                "kpi": f"{kpi_id} v{version}",
                "cell": cell,
                "granularity": granularity,
                "period": period.isoformat(),
            }
            | summarize(rows)
        )
    d8_s = time.perf_counter() - started
    con.close()
    return {
        "time_travel": d7,
        "lineage": d8,
        "timing_s": {"time travel staging": round(d7_s, 1), "lineage walks": round(d8_s, 1)},
    }


def render_markdown(record: dict[str, Any]) -> str:
    """Render the report.

    Args:
        record: The record as loaded from JSON.

    Returns:
        The Markdown report.
    """
    t = record["time_travel"]
    lines = [
        "# Time travel and lineage report",
        "",
        "Synthetic network (rules D7, D8). Written by",
        "`python -m ran_lakehouse.lake.lineage_report` from `time_travel_lineage.json`.",
        "",
        "## Time travel (rule D7)",
        "",
        "Every gold write is an Iceberg snapshot, so a KPI can be read as it was at any",
        "earlier time: `SELECT ... FROM lk.gold.lte_kpi_hour AT (TIMESTAMP => <time>)`",
        "(lake.timetravel.as_of). Worked example, staged on the "
        f"{t['profile']} profile in a fresh warehouse:",
        f"a planted late file of {t['late_file']['ems']} for the period from "
        f"{t['late_file']['period_start']}",
        f"arrived at {t['late_file']['arrival']} ({t['late_file']['detail']}),",
        "after its UTC day had been built into silver and published in gold. Silver then merged",
        "it into its hour and gold rebuilt the day.",
        "",
        f"| {t['kpi']}, {t['cell']}, hour from {t['hour']} | Value | Coverage | Periods |",
        "|---|---|---|---|",
        f"| as of the first publication ({t['published_at']}) | "
        f"{t['as_of_publication']['value']:.4f} | {t['as_of_publication']['coverage']} | "
        f"{t['as_of_publication']['periods_reported']} |",
        f"| now | {t['now']['value']:.4f} | {t['now']['coverage']} | "
        f"{t['now']['periods_reported']} |",
        "",
        f"gold.lte_kpi_hour held {t['snapshots_of_lte_kpi_hour']} snapshots after the staging.",
        "",
        "## Lineage (rule D8)",
        "",
        "One query from a gold KPI value to every silver row it was computed from, the bronze",
        "row each came from and the file that carried it (`ranlake lineage`, lake.lineage).",
        "DuckDB's Iceberg catalog has no views, so the query is kept in code:",
        "",
        "```sql",
        LINEAGE_SQL.strip(),
        "```",
        "",
        "Worked examples on the demo warehouse:",
        "",
        "| KPI | Cell | Period | Value | Silver rows | Bronze rows | Files | Releases | "
        "Vendor counters | Derived | Suspect | Late | Conflict |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for x in record["lineage"]:
        f = x["flags"]
        lines.append(
            f"| {x['kpi']} | {x['cell']} | {x['granularity']} {x['period']} | "
            f"{x['kpi_value']:.4f} | {x['silver_rows']} | {x['bronze_rows']} | {x['files']} | "
            f"{', '.join(x['dictionary_releases'])} | {', '.join(x['vendor_counters'])} | "
            f"{f['derived']} | {f['suspect']} | {f['late']} | {f['conflict']} |"
        )
    lines += ["", "First row of each walk:", ""]
    for x in record["lineage"]:
        lines.append(f"- {x['kpi']}: " + "; ".join(f"{k} {v}" for k, v in x["first_row"].items()))
    lines += [
        "",
        "## Cost",
        "",
        "| Step | Seconds |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in record["timing_s"].items()],
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the examples, then write the record and the report.

    Args:
        argv: Command-line arguments, or None for sys.argv.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse", required=True, help="the demo warehouse")
    args = parser.parse_args(argv)
    RESULTS.mkdir(exist_ok=True)
    RECORD_JSON.write_text(json.dumps(build(args.warehouse), indent=2, default=str) + "\n")
    RECORD_MD.write_text(render_markdown(json.loads(RECORD_JSON.read_text())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
