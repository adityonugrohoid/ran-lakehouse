"""PM file report (rules P1-P5): sizes and speed of one demo day.

Simulates one demo day, writes it through both simulated EMS (Huawei-style
3GPP PM XML, Nokia-style OMeS) into data/ (never committed, rule 6), parses
them back, and records file sizes, write and parse speed, and the file
counts of a 12-week run against one file per network element. Writes results/pm_files.json and
results/pm_files.md. Run: `uv run python -m ran_lakehouse.files.report`.
"""

import gzip
import json
import shutil
import time
from datetime import UTC
from pathlib import Path
from typing import Any

import numpy as np

from ran_lakehouse.files import omes
from ran_lakehouse.files.dialects import HUAWEI_R1, NOKIA_R1, Dialect
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
HUAWEI_EMS = Ems("EMS-HW-01", HUAWEI_R1, WIB, "Huawei-style synthetic EMS", "3gpp-xml")
NOKIA_EMS = Ems("EMS-NK-01", NOKIA_R1, UTC, "Nokia-style synthetic EMS", "omes")


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


def measure(model: Any, day: Any, ems: Ems) -> dict[str, Any]:
    """Write one day through an EMS, read it back and measure both.

    Args:
        model: The network.
        day: The simulated day.
        ems: The EMS.

    Returns:
        Sizes, speed and counts.
    """
    folder = SAMPLE_DIR / ems.ems_id
    folder.mkdir(parents=True)
    sizes, names = [], []
    t0 = time.perf_counter()
    for name, content in day_files(model, day, ems):
        (folder / name).write_bytes(content)
        names.append(name)
        sizes.append(len(content))
    t_write = time.perf_counter() - t0
    raw_sizes = [len(gzip.decompress((folder / n).read_bytes())) for n in names]
    t0 = time.perf_counter()
    values = 0
    objects: set[str] = set()
    for n in names:
        content = (folder / n).read_bytes()
        if ems.file_format == "3gpp-xml":
            columns = parse_file(content)["columns"]
            objects |= set(columns["managed_element"])
        else:
            columns = omes.parse_file(content)["columns"]
            objects |= set(columns["dn"])
        values += len(columns["value"])
    t_parse = time.perf_counter() - t0
    mb = 1024.0 * 1024.0
    return {
        "format": ems.file_format,
        "sample_file": names[len(names) // 2],
        "files_per_day": len(names),
        "network_elements_or_objects_per_file": len(objects),
        "cells_in_region": int((model.state.vendor == ems.dialect.vendor).sum()),
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
        "dictionary": dictionary_summary(ems.dialect),
    }


def build() -> dict[str, Any]:
    """Write and read one demo day through both EMS and measure them.

    Returns:
        The record.
    """
    model = default_model(build_world("demo"))
    day = next(simulate_days(model, DAY, 1))
    if SAMPLE_DIR.exists():
        shutil.rmtree(SAMPLE_DIR)
    all_elements = {c.managed_element for c in model.world.cells}
    periods = 96 * 7 * WEEKS
    return {
        "ems": {ems.ems_id: measure(model, day, ems) for ems in (HUAWEI_EMS, NOKIA_EMS)},
        "twelve_week_files": {
            "one file per EMS per period (both EMS)": 2 * periods,
            "type A, one per network element": len(all_elements) * periods,
        },
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
        "Synthetic network. One demo day written by both simulated EMS and read back: the",
        "Huawei-style EMS writes type B 3GPP PM XML (TS 32.435 V19.0.0 measCollecFile, names",
        "per TS 32.432 V19.0.0 clause 5.1.2) in local time with +0700; the Nokia-style EMS",
        "writes the OMeS-shaped format in UTC (rules P1-P5, W5); both gzip. Measured on the",
        "build machine; times vary run to run. Written by `python -m ran_lakehouse.files.report`",
        "from `pm_files.json`.",
    ]
    for ems_id, e in record["ems"].items():
        d = e["dictionary"]
        lines += [
            "",
            f"## {ems_id} ({e['format']})",
            "",
            f"Sample file name: `{e['sample_file']}`. {e['files_per_day']} files per day, "
            f"{e['network_elements_or_objects_per_file']} network elements or objects per "
            f"file, {e['cells_in_region']} cells in the region, {e['values_per_day']:,} values "
            "per day.",
            "",
            "| Size | MB |",
            "|---|---|",
            *[f"| {k} | {v} |" for k, v in e["size_mb"].items()],
            "",
            "| Speed | Value |",
            "|---|---|",
            *[f"| {k} | {v:,} |" for k, v in e["speed"].items()],
            "",
            f"Dictionary release {d['release']}: {d['counters']} counters in "
            f"{', '.join(d['meas_infos'])}.",
            "",
            "| Attestation of the counter name | Counters |",
            "|---|---|",
            *[f"| {k} | {v} |" for k, v in d["by_attestation"].items()],
        ]
    lines += [
        "",
        "## Files in a 12-week run (rule P2)",
        "",
        "| Layout | Files |",
        "|---|---|",
        *[f"| {k} | {v:,} |" for k, v in record["twelve_week_files"].items()],
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
