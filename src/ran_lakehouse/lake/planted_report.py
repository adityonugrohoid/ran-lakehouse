"""Planted-case report (rule E3): each data-quality rule D1 to D8, what was
planted, what the pipeline did, and the tests that guard it.

Every number comes from the committed run records (bronze, silver, gold,
time travel and lineage); this report only gathers them, so it is rebuilt
from them and a test checks that it matches. The planted positions are
evaluation-only (rule A3): counts per kind only.

    uv run python -m ran_lakehouse.lake.planted_report
"""

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "planted_cases.json"
RECORD_MD = RESULTS / "planted_cases.md"
SOURCES = {
    "bronze": RESULTS / "bronze_backfill.json",
    "silver": RESULTS / "silver_build.json",
    "gold": RESULTS / "gold_build.json",
    "time_travel_lineage": RESULTS / "time_travel_lineage.json",
}

# What each rule plants (spec section 7) and the tests that fail when the
# property breaks, as "file::function" or "file::function[parameter]".
RULES: dict[str, dict[str, Any]] = {
    "D1": {
        "name": "Late file",
        "planted": "a file arrives after its successors",
        "tests": [
            "tests/test_collect.py::test_late_file_arrives_after_its_successors",
            "tests/test_silver.py::test_planted_anomaly_is_flagged_where_planted[D1]",
            "tests/test_silver.py::test_late_file_merges_into_its_own_hour",
        ],
    },
    "D2": {
        "name": "Duplicate file",
        "planted": "the same content delivered twice, and the same name with different content",
        "tests": [
            "tests/test_collect.py::test_duplicates_are_delivered_twice",
            "tests/test_collect.py::test_same_content_loads_once_and_a_conflict_loads_again",
            "tests/test_silver.py::test_planted_anomaly_is_flagged_where_planted[D2_same]",
            "tests/test_silver.py::test_planted_anomaly_is_flagged_where_planted[D2_conflict]",
            "tests/test_silver.py::test_a_second_run_changes_nothing",
        ],
    },
    "D3": {
        "name": "Missing file",
        "planted": "a gap per network element and period",
        "tests": [
            "tests/test_collect.py::test_missing_file_is_never_delivered",
            "tests/test_silver.py::test_planted_anomaly_is_flagged_where_planted[D3]",
            "tests/test_silver.py::test_gaps_are_never_zero_filled",
            "tests/test_gold.py::test_coverage_and_suspect_share_show_planted_gaps",
        ],
    },
    "D4": {
        "name": "Suspect data",
        "planted": "suspect flags on values of an interrupted collection",
        "tests": [
            "tests/test_collect.py::test_suspect_flag_reaches_bronze",
            "tests/test_silver.py::test_planted_anomaly_is_flagged_where_planted[D4]",
            "tests/test_gold.py::test_coverage_and_suspect_share_show_planted_gaps",
        ],
    },
    "D5": {
        "name": "Counter rename after a software upgrade",
        "planted": "part of the Huawei-style network moves to a release that renames counters",
        "tests": [
            "tests/test_collect.py::test_upgrade_moves_part_of_the_network_to_r2",
            "tests/test_silver.py::test_every_vendor_counter_maps_once",
            "tests/test_silver.py::test_planted_anomaly_is_flagged_where_planted[D5]",
            "tests/test_silver.py::test_units_match_the_model",
        ],
    },
    "D6": {
        "name": "KPI formula change",
        "planted": "a gold formula is revised during the run",
        "tests": ["tests/test_gold.py::test_revision_keeps_both_versions_over_the_whole_history"],
    },
    "D7": {
        "name": "Time travel",
        "planted": "a late file changes a published value",
        "tests": [
            "tests/test_timetravel.py::test_as_of_read_returns_the_value_before_the_late_file"
        ],
    },
    "D8": {
        "name": "Lineage",
        "planted": "any gold value must trace back to its source files",
        "tests": [
            "tests/test_gold.py::test_lineage_walks_a_value_back_to_its_files",
            "tests/test_gold.py::test_lineage_follows_a_derived_value_to_both_counters",
        ],
    },
}


def load(name: str) -> dict[str, Any]:
    """One source record.

    Args:
        name: Key of SOURCES.

    Returns:
        The record.
    """
    record: dict[str, Any] = json.loads(SOURCES[name].read_text())
    return record


def mapped_per_release(silver: dict[str, Any]) -> dict[str, int]:
    """Counter-map entries per dictionary release.

    Args:
        silver: The silver record.

    Returns:
        Release to entries.
    """
    out: dict[str, int] = {}
    for key, n in silver["counter_map"].items():
        release = key.split(":")[0]
        out[release] = out.get(release, 0) + int(n)
    return out


def build() -> dict[str, Any]:
    """The record, gathered from the source records.

    Returns:
        Per rule: name, what was planted, the counts and outcome, the tests.
    """
    bronze, silver = load("bronze"), load("silver")
    gold, tt = load("gold"), load("time_travel_lineage")
    planted = bronze["delivery_anomalies_planted"]
    found = silver["anomalies"]
    flags = silver["flags"]
    coverage = gold["coverage"]

    def delivery(kind: str) -> dict[str, int]:
        return {"planted in the run": planted[kind]} | {
            f"{k} (silver scope)" if k == "planted" else k: v for k, v in found[kind].items()
        }

    outcome = {
        "D1": {
            "counts": delivery("D1"),
            "pipeline": (
                "flagged late; merged into its own partition, KPIs recomputed; "
                f"{flags['late']:,} late rows in silver, {silver['late_files_merged']} files "
                "merged after their partition was built"
            ),
        },
        "D2": {
            "counts": {f"same content, {k}": v for k, v in delivery("D2_same").items()}
            | {f"changed content, {k}": v for k, v in delivery("D2_conflict").items()},
            "pipeline": (
                "same content loaded once (idempotent by EMS, name and hash); changed content "
                f"loaded again, latest version wins, {flags['conflict']:,} changed values "
                "flagged conflict"
            ),
        },
        "D3": {
            "counts": delivery("D3"),
            "pipeline": (
                "a gap per network element and period, no row and never zero; gold states "
                f"coverage: {coverage['lte']['below_full_coverage']:,} of "
                f"{coverage['lte']['cell_days']:,} LTE and "
                f"{coverage['gsm']['below_full_coverage']:,} of {coverage['gsm']['cell_days']:,} "
                "GSM cell-days below full coverage"
            ),
        },
        "D4": {
            "counts": delivery("D4"),
            "pipeline": (
                f"suspect flag carried per value: {flags['suspect']:,} suspect silver rows; "
                f"{coverage['lte']['with_suspect_values']:,} LTE cell-days in gold carry a "
                "suspect share"
            ),
        },
        "D5": {
            "counts": delivery("D5"),
            "pipeline": (
                "the counts are network elements moved to the renamed release; silver maps "
                "every vendor counter of each release to its 3GPP name ("
                + ", ".join(f"{rel} {n}" for rel, n in mapped_per_release(silver).items())
                + " entries) and the KPI series stays continuous across the upgrade"
            ),
        },
        "D6": {
            "counts": {"revisions": len(gold["revision"])},
            "pipeline": "; ".join(
                f"{r['kpi_id']} v{r['from_version']} to v{r['to_version']} from "
                f"{r['effective_day']}, {r['detail']}; both versions queryable and labelled"
                for r in gold["revision"]
            ),
        },
        "D7": {
            "counts": {
                "snapshots of the KPI table": tt["time_travel"]["snapshots_of_lte_kpi_hour"]
            },
            "pipeline": (
                f"{tt['time_travel']['kpi']} of {tt['time_travel']['cell']} at "
                f"{tt['time_travel']['hour']}: as of its first publication "
                f"{tt['time_travel']['as_of_publication']['value']:.4f} (coverage "
                f"{tt['time_travel']['as_of_publication']['coverage']:g}), now "
                f"{tt['time_travel']['now']['value']:.4f} (coverage "
                f"{tt['time_travel']['now']['coverage']:g}) after the late file; read from "
                f"Iceberg table history ({tt['time_travel']['profile']} profile)"
            ),
        },
        "D8": {
            "counts": {"values traced": len(tt["lineage"])},
            "pipeline": "; ".join(
                f"{w['kpi']} {w['granularity']} {w['period']}: {w['silver_rows']} silver rows, "
                f"{w['bronze_rows']} bronze rows, {w['files']} files"
                for w in tt["lineage"]
            ),
        },
    }
    return {
        "sources": {k: str(v.relative_to(REPO_ROOT)) for k, v in SOURCES.items()},
        "profile": silver["profile"],
        "weeks": silver["weeks"],
        "rules": {
            rule: {"name": spec["name"], "planted": spec["planted"]}
            | outcome[rule]
            | {"tests": spec["tests"]}
            for rule, spec in RULES.items()
        },
    }


def render_markdown(record: dict[str, Any]) -> str:
    """The report.

    Args:
        record: The record.

    Returns:
        Markdown.
    """
    lines = [
        "# Planted data-quality cases",
        "",
        "Every data-quality case of rule D, planted on purpose in the synthetic "
        f"{record['profile']} run ({record['weeks']} weeks): what was planted, what the "
        "pipeline did, and the tests that fail when the property breaks (rule E3). Gathered "
        "by `python -m ran_lakehouse.lake.planted_report` from "
        f"{', '.join('`' + s.split('/')[-1] + '`' for s in record['sources'].values())}; the "
        "planted positions are evaluation-only (rule A3), so counts per kind only. "
        '"Silver scope" leaves out the first and last period of the delivered span.',
        "",
        "| Rule | Case | Planted | Counts | What the pipeline did | Tests |",
        "|---|---|---|---|---|---|",
    ]
    for rule, r in record["rules"].items():
        counts = "; ".join(f"{k}: {v:,}" for k, v in r["counts"].items())
        tests = "<br>".join(f"`{t.split('::')[1]}`" for t in r["tests"])
        lines.append(
            f"| {rule} | {r['name']} | {r['planted']} | {counts} | {r['pipeline']} | {tests} |"
        )
    lines += [
        "",
        "Test files: "
        + ", ".join(
            sorted({f"`{t.split('::')[0]}`" for r in record["rules"].values() for t in r["tests"]})
        )
        + ". `test_timetravel.py` needs the compose stack and runs in the CI lake job "
        "(`pytest -m lake`).",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    """Write the record and the report.

    Returns:
        The process exit code.
    """
    record = build()
    RECORD_JSON.write_text(json.dumps(record, indent=2) + "\n")
    RECORD_MD.write_text(render_markdown(json.loads(RECORD_JSON.read_text())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
