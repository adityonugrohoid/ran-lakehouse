"""Configuration and alarm report (rules C1, C2, F2): 12 weeks of CM and FM
exports of both simulated EMS, reported as counts and consistency checks.

The exports trace the planted faults, so only counts appear here; the
records themselves are lake input, never committed (rules 6, A3). Writes
results/config_alarms.json and results/config_alarms.md.
Run: `uv run python -m ran_lakehouse.files.oss_report`.
"""

import gzip
import json
import time
from collections import Counter
from datetime import UTC, date
from pathlib import Path
from typing import Any

from ran_lakehouse.faults.plant import Fault, plan_faults, uplink_rise_db
from ran_lakehouse.faults.traces import INTERFERENCE_ALARM_RISE_DB, INTERFERENCE_ALARM_VENDOR
from ran_lakehouse.files.cmfm import day_exports, run_days
from ran_lakehouse.files.dialects import HUAWEI_R1, NOKIA_R1
from ran_lakehouse.files.ems import WIB, Ems
from ran_lakehouse.model import RUN_START, NetworkModel, default_model
from ran_lakehouse.world import build_world

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "config_alarms.json"
RECORD_MD = RESULTS / "config_alarms.md"
WEEKS = 12
EMS_LIST = (
    Ems("EMS-HW-01", HUAWEI_R1, WIB, "Huawei-style synthetic EMS", "3gpp-xml"),
    Ems("EMS-NK-01", NOKIA_R1, UTC, "Nokia-style synthetic EMS", "omes"),
)


def expected(base: NetworkModel, faults: list[Fault], ems: Ems, last: date) -> dict[str, int]:
    """What the schedule implies the EMS must export over the run.

    Args:
        base: The network as built.
        faults: The fault schedule.
        ems: The EMS.
        last: Last day of the run.

    Returns:
        Expected change log entries and alarm notifications.
    """
    mine = [f for f in faults if base.state.vendor[f.cell] == ems.dialect.vendor]
    changes = sum(
        (f.start.date() <= last) + (f.end.date() <= last)
        for f in mine
        if f.kind in ("F1a", "F1b", "F1c")
    )
    alarmed = [f for f in mine if f.kind == "F1f"]
    if ems.dialect.vendor == INTERFERENCE_ALARM_VENDOR:
        alarmed += [
            f
            for f in mine
            if f.kind == "F1e"
            and float(uplink_rise_db(base, f)[f.cell]) >= INTERFERENCE_ALARM_RISE_DB
        ]
    notifications = sum((f.start.date() <= last) + (f.end.date() <= last) for f in alarmed)
    return {"change log entries": changes, "alarm notifications": notifications}


def build() -> dict[str, Any]:
    """Export 12 weeks of CM and FM and count them.

    Returns:
        The record.
    """
    t0 = time.perf_counter()
    base = default_model(build_world("demo"))
    faults = plan_faults(base, WEEKS)
    days = run_days(RUN_START.date(), 7 * WEEKS)
    per_ems: dict[str, Any] = {}
    for ems in EMS_LIST:
        counts: Counter[str] = Counter()
        bytes_by_kind: Counter[str] = Counter()
        attributes: Counter[str] = Counter()
        causes: Counter[str] = Counter()
        classes: Counter[str] = Counter()
        snapshot_rows = []
        for day in days:
            for name, content, n in day_exports(base, faults, ems, day):
                kind = name.split("_")[0]
                counts[kind] += n
                bytes_by_kind[kind] += len(content)
                if kind == "CM":
                    snapshot_rows.append(n)
                    if day == days[0]:
                        for line in gzip.decompress(content).decode().splitlines():
                            classes[json.loads(line)["objectClass"]] += 1
                if kind == "CMLOG":
                    for line in gzip.decompress(content).decode().splitlines():
                        attributes[json.loads(line)["attribute"]] += 1
                if kind == "FM":
                    for line in gzip.decompress(content).decode().splitlines():
                        record = json.loads(line)
                        causes[f"{record['notificationType']}: {record['probableCause']}"] += 1
        want = expected(base, faults, ems, days[-1])
        per_ems[ems.ems_id] = {
            "dn_style": "3GPP (TS 32.300)" if ems.file_format == "3gpp-xml" else "Nokia-style",
            "time_zone": "+07:00 local" if ems.tz == WIB else "UTC",
            "snapshot_objects_first_day_by_class": dict(sorted(classes.items())),
            "snapshot_rows_per_day_min_max": [min(snapshot_rows), max(snapshot_rows)],
            "change_log_entries_by_attribute": dict(sorted(attributes.items())),
            "alarm_notifications_by_type_and_cause": dict(sorted(causes.items())),
            "gzip_mb_over_the_run": {
                k: round(v / (1024.0 * 1024.0), 2) for k, v in sorted(bytes_by_kind.items())
            },
            "consistency": {
                "change log entries, exported / implied by the schedule": [
                    counts["CMLOG"],
                    want["change log entries"],
                ],
                "alarm notifications, exported / implied by the schedule": [
                    counts["FM"],
                    want["alarm notifications"],
                ],
            },
        }
    return {
        "days": len(days),
        "ems": per_ems,
        "timing_s": {
            "network, schedule and 12 weeks of exports": round(time.perf_counter() - t0, 1)
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
        "# Configuration and alarm report",
        "",
        f"Synthetic network. {record['days']} days of CM snapshots, CM change log and FM alarm",
        "log from both simulated EMS (rules C1, C2): object classes in the solution-set",
        "spellings (TS 28.659, TS 28.656), DNs per TS 32.300 in each EMS's naming style, alarm",
        "notifications with the TS 32.111-2 V19.0.0 fields. The exports trace the planted",
        "faults, so only counts are reported here. Written by",
        "`python -m ran_lakehouse.files.oss_report` from `config_alarms.json`.",
    ]
    for ems_id, e in record["ems"].items():
        lines += [
            "",
            f"## {ems_id} ({e['dn_style']} DNs, {e['time_zone']})",
            "",
            "| Snapshot objects on the first day | Count |",
            "|---|---|",
            *[f"| {k} | {v:,} |" for k, v in e["snapshot_objects_first_day_by_class"].items()],
            "",
            f"Snapshot rows per day: {e['snapshot_rows_per_day_min_max'][0]:,} to "
            f"{e['snapshot_rows_per_day_min_max'][1]:,} (neighbour relations come and go).",
            "",
            "| Change log entries by attribute | Count |",
            "|---|---|",
            *[f"| {k} | {v} |" for k, v in e["change_log_entries_by_attribute"].items()],
            "",
            "| Alarm notifications by type and probable cause | Count |",
            "|---|---|",
            *[f"| {k} | {v} |" for k, v in e["alarm_notifications_by_type_and_cause"].items()],
            "",
            "| Consistency with the planted schedule | Exported | Implied |",
            "|---|---|---|",
            *[f"| {k} | {v[0]} | {v[1]} |" for k, v in e["consistency"].items()],
            "",
            "| Gzip size over the run | MB |",
            "|---|---|",
            *[f"| {k} | {v} |" for k, v in e["gzip_mb_over_the_run"].items()],
        ]
    lines += [
        "",
        "## Timing",
        "",
        "Measured on the build machine; varies run to run.",
        "",
        "| Step | Seconds |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in record["timing_s"].items()],
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
