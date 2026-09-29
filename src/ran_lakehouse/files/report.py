"""PM file report (rules P1-P3, P5): sizes and speed of one demo day.

Simulates one demo day, writes it through the Huawei-style EMS as type B
3GPP PM XML files into data/ (never committed, rule 6), parses them back,
and records file sizes, write and parse speed, and the file counts of a
12-week run for type B against type A. Writes results/pm_files.json and
results/pm_files.md. Run: `uv run python -m ran_lakehouse.files.report`.
"""

import gzip
import json
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np

from ran_lakehouse.files.dialects import HUAWEI_R1, Dialect
from ran_lakehouse.files.ems import WIB, Ems, day_files
from ran_lakehouse.files.pm_xml import parse_file
from ran_lakehouse.model import default_model, simulate_days
from ran_lakehouse.world import build_world

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "pm_files.json"
RECORD_MD = RESULTS / "pm_files.md"
SAMPLE_DIR = REPO_ROOT / "data" / "pm-sample"
DAY = 0
WEEKS = 12
HUAWEI_EMS = Ems("EMS-HW-01", HUAWEI_R1, WIB, "Huawei-style synthetic EMS")


def dictionary_summary(dialect: Dialect) -> dict[str, Any]:
    """Counters of a dialect by attestation.

    Args:
        dialect: The dialect.

    Returns:
        measInfo counts and attestation counts.
    """
    entries = [e for g in dialect.groups for e in g.entries]
    by: dict[str, int] = {}
    for e in entries:
        by[e.attestation] = by.get(e.attestation, 0) + 1
    return {
        "release": dialect.release,
        "meas_infos": [g.meas_info_id for g in dialect.groups],
        "counters": len(entries),
        "by_attestation": dict(sorted(by.items())),
    }


def build() -> dict[str, Any]:
    """Write and read one demo day and measure it.

    Returns:
        The record.
    """
    model = default_model(build_world("demo"))
    day = next(simulate_days(model, DAY, 1))
    if SAMPLE_DIR.exists():
        shutil.rmtree(SAMPLE_DIR)
    SAMPLE_DIR.mkdir(parents=True)
    sizes, raw_sizes, names = [], [], []
    t0 = time.perf_counter()
    for name, content in day_files(model, day, HUAWEI_EMS):
        (SAMPLE_DIR / name).write_bytes(content)
        names.append(name)
        sizes.append(len(content))
    t_write = time.perf_counter() - t0
    raw_sizes = [len(gzip.decompress((SAMPLE_DIR / n).read_bytes())) for n in names]
    t0 = time.perf_counter()
    values = 0
    elements = set()
    for n in names:
        parsed = parse_file((SAMPLE_DIR / n).read_bytes())
        values += len(parsed["columns"]["value"])
        elements |= set(parsed["columns"]["managed_element"])
    t_parse = time.perf_counter() - t0
    huawei = model.state.vendor == HUAWEI_EMS.dialect.vendor
    all_elements = {c.managed_element for c in model.world.cells}
    periods = 96 * 7 * WEEKS
    mb = 1024.0 * 1024.0
    return {
        "sample_file": names[len(names) // 2],
        "files_per_day": len(names),
        "network_elements_per_file": len(elements),
        "cells_in_region": int(huawei.sum()),
        "values_per_day": values,
        "size_mb": {
            "gzip, total per day": round(sum(sizes) / mb, 2),
            "gzip, mean per file": round(float(np.mean(sizes)) / mb, 3),
            "xml, total per day": round(sum(raw_sizes) / mb, 2),
            "gzip ratio": round(sum(raw_sizes) / sum(sizes), 1),
        },
        "speed": {
            "write, s per day": round(t_write, 1),
            "write, values per s": round(values / t_write),
            "parse and check, s per day": round(t_parse, 1),
            "parse, values per s": round(values / t_parse),
        },
        "twelve_week_files": {
            "type B, both EMS": 2 * periods,
            "type A, one per network element": len(all_elements) * periods,
        },
        "dictionary": dictionary_summary(HUAWEI_EMS.dialect),
    }


def render_markdown(record: dict[str, Any]) -> str:
    """Render the report from the record.

    Args:
        record: The record as loaded from JSON.

    Returns:
        The Markdown report.
    """
    lines = [
        "# PM file report",
        "",
        "Synthetic network. One demo day written by the Huawei-style EMS as type B 3GPP PM XML",
        "files (TS 32.435 V19.0.0 measCollecFile, names per TS 32.432 V19.0.0 clause 5.1.2,",
        "gzip), local time with +0700 (rule W5), then parsed and structure-checked. Measured on",
        "the build machine; times vary run to run. Written by",
        "`python -m ran_lakehouse.files.report` from `pm_files.json`.",
        "",
        f"Sample file name: `{record['sample_file']}`. {record['files_per_day']} files per day, "
        f"{record['network_elements_per_file']} network elements per file, "
        f"{record['cells_in_region']} cells in the region, {record['values_per_day']:,} values "
        "per day.",
        "",
        "| Size | MB |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in record["size_mb"].items()],
        "",
        "| Speed | Value |",
        "|---|---|",
        *[f"| {k} | {v:,} |" for k, v in record["speed"].items()],
        "",
        "## Files in a 12-week run (rule P2)",
        "",
        "| Layout | Files |",
        "|---|---|",
        *[f"| {k} | {v:,} |" for k, v in record["twelve_week_files"].items()],
        "",
        "## Huawei-style dictionary",
        "",
        f"Release {record['dictionary']['release']}: {record['dictionary']['counters']} "
        f"counters in measInfos {', '.join(record['dictionary']['meas_infos'])}.",
        "",
        "| Attestation of the counter name | Counters |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in record["dictionary"]["by_attestation"].items()],
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    """Write the record and the Markdown report.

    Returns:
        The process exit code.
    """
    RESULTS.mkdir(exist_ok=True)
    RECORD_JSON.write_text(json.dumps(build(), indent=2) + "\n")
    RECORD_MD.write_text(render_markdown(json.loads(RECORD_JSON.read_text())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
