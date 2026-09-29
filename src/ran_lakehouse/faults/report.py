"""Fault report (rules F1-F4, M6): fingerprints, recovery and the schedule.

Each fault kind is demonstrated on its own cell for one week from the
start of the run, outside the planted schedule, so the report shows how
faults look without disclosing the planted answers (rule A3): the
schedule appears only as counts and aggregate recovery. Writes
results/faults.json (the one record), results/faults.md and one figure.
Run: `uv run python -m ran_lakehouse.faults.report`.
"""

import json
import resource
import time
from collections import Counter
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from ran_lakehouse.faults.evaluate import (
    MEASURED_OVER,
    PRIMARY_KPI,
    Evaluation,
    evaluate,
    right_fix,
)
from ran_lakehouse.faults.plant import (
    CAUSE,
    DURATION_H,
    KINDS,
    MISTAKEN_TILT_DEG,
    POWER_DROP_DB,
    RIGHT_ANSWER,
    SURGE_FACTOR,
    SURGE_RADIUS_KM,
    Fault,
    apply_faults,
    detail,
    eligible,
    plan_faults,
    uplink_rise_db,
)
from ran_lakehouse.faults.simulate import simulate_with_faults
from ran_lakehouse.faults.traces import cm_changes, fm_alarms
from ran_lakehouse.faults.whatif import Change, apply_changes, lte_kpis, replay
from ran_lakehouse.model import RUN_START, Day, NetworkModel, default_model, simulate_days
from ran_lakehouse.model.serving import TA_BIN_EDGES_STEPS
from ran_lakehouse.world import build_world

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "faults.json"
RECORD_MD = RESULTS / "faults.md"
FIG_RECOVERY = RESULTS / "faults_recovery.png"

WEEKS = 12  # rule W7 demo
DEMO_BAND = "B3"
FAR_TA_STEPS = 16  # "far" TA bins start here (about 1.25 km)
WRONG_FIX_F1A = [Change("power", -1, 3.0, -1)]  # cell filled in per demo

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
CLEAN_COLOR = "#8a8984"
FAULTY_COLOR = "#eb6834"
FIXED_COLOR = "#2a78d6"
WRONG_COLOR = "#1baf7a"

MAGNITUDE = {
    "F1a": f"tilt set to {MISTAKEN_TILT_DEG:g} deg (urban cells)",
    "F1b": "strongest neighbour relation deleted",
    "F1c": f"power {POWER_DROP_DB:g} dB",
    "F1d": f"persons x{SURGE_FACTOR:g} within {SURGE_RADIUS_KM:g} km (suburban cells)",
    "F1e": "uplink source 15 dBm, 0.3-0.8 km along the azimuth",
    "F1f": "cell down",
}
KPI_ROWS = (
    "E-RAB accessibility (%)",
    "RRC setup success (%)",
    "E-RAB drop rate (%)",
    "DL IP throughput (kbit/s)",
    "Handover success (%)",
    "Mean DL PRB use (%)",
    "RRC connections, mean",
    "Cell availability (%)",
)


def demo_cell(model: NetworkModel, kind: str) -> int:
    """The demonstration cell of a kind: the median-load eligible B3 cell.

    Args:
        model: The network as built.
        kind: Fault kind.

    Returns:
        Global cell index.
    """
    cells = eligible(model, kind)
    cells = cells[model.state.band[cells] == DEMO_BAND]
    if kind == "F1e":
        cells = cells[model.state.vendor[cells] == "huawei"]
    if kind in ("F1b", "F1c", "F1f"):
        cells = cells[model.state.area_class[cells] == "suburban"]
    order = np.argsort(model.serving.subscribers[cells], kind="stable")
    return int(cells[order[len(order) // 2]])


def rounded(kpis: dict[str, float]) -> dict[str, float | None]:
    """KPIs rounded for the record, NaN as None.

    Args:
        kpis: KPI name to value.

    Returns:
        JSON-ready KPIs.
    """
    return {k: None if np.isnan(v) else round(v, 3) for k, v in kpis.items()}


def far_ta_share(model: NetworkModel, cell: int) -> float:
    """Share of a cell's users beyond FAR_TA_STEPS timing advance steps.

    Args:
        model: The network.
        cell: Global cell index.

    Returns:
        The share.
    """
    edges = np.array(TA_BIN_EDGES_STEPS[:-1])
    return float(model.serving.ta_share[cell][edges >= FAR_TA_STEPS].sum())


def relation_week(days: Iterable[Day], pair: tuple[int, int]) -> float | None:
    """Weekly handover attempts on one relation, None when not reported.

    Args:
        days: Simulated days (consumed once).
        pair: (source, target).

    Returns:
        The attempts, or None if the relation is not configured.
    """
    total = 0.0
    reported = False
    for day in days:
        if pair not in day.lte.relations:
            return None
        values = day.lte.relation_values["HO.OutAttTarget.sum"][:, day.lte.relations.index(pair)]
        if not np.isnan(values).all():
            reported = True
            total += float(np.nansum(values))
    return total if reported else None


def ul_level(days: Iterable[Day], cell: int) -> float:
    """Mean vendor-style uplink interference level of a cell over the days.

    Args:
        days: Simulated days (consumed once).
        cell: Global cell index.

    Returns:
        Mean level, dBm per PRB.
    """
    name = "UL interference per PRB, dBm (vendor-style)"
    means = [
        float(day.lte.values[name][:, int(np.flatnonzero(day.lte.cells == cell)[0])].mean())
        for day in days
    ]
    return float(np.mean(means))


def fingerprint(base: NetworkModel, fault: Fault, evaluation: Evaluation) -> dict[str, Any]:
    """Kind-specific PM traces beyond the KPIs.

    Args:
        base: The network as built.
        fault: The demonstration fault.
        evaluation: Its evaluation.

    Returns:
        Named clean and faulty values.
    """
    faulty = apply_faults(base, [fault])
    cell = fault.cell
    out: dict[str, Any] = {
        "subscribers served by the cell": [
            round(float(base.serving.subscribers[cell])),
            round(float(faulty.serving.subscribers[cell])),
        ],
        "cells touched": int(evaluation.area_cells.size),
    }
    if fault.kind == "F1a":
        out["share of users beyond 16 TA steps"] = [
            round(far_ta_share(base, cell), 3),
            round(far_ta_share(faulty, cell), 3),
        ]
    if fault.kind == "F1b":
        columns = sorted(base.neighbours)
        pair = (cell, fault.target)
        out["HO.OutAttTarget.sum on the deleted relation, week"] = [
            relation_week(replay(base, 0, columns), pair),
            relation_week(replay(faulty, 0, columns), pair),
        ]
    if fault.kind == "F1e":
        columns = sorted(base.neighbours)
        out["uplink noise rise at the cell, dB"] = round(
            float(uplink_rise_db(base, fault)[cell]), 1
        )
        out["UL interference per PRB, dBm (vendor-style), week mean"] = [
            round(ul_level(replay(base, 0, columns), cell), 1),
            round(ul_level(replay(faulty, 0, columns), cell), 1),
        ]
    return out


def demonstrate(base: NetworkModel, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Plant one fault of a kind for a week and evaluate it.

    Args:
        base: The network as built.
        kind: Fault kind.

    Returns:
        The demonstration record and the daily primary-KPI series for the
        figure.
    """
    cell = demo_cell(base, kind)
    hours = 24 * 7 if kind != "F1f" else 6
    fault = detail(base, "F9000", kind, cell, RUN_START, RUN_START + timedelta(hours=hours))
    evaluation = evaluate(base, fault, try_parameters=kind == "F1e")
    kpi = PRIMARY_KPI[kind]
    over = MEASURED_OVER[kind]
    record: dict[str, Any] = {
        "cell": base.state.cell_names[cell],
        "area_class": str(base.state.area_class[cell]),
        "vendor": str(base.state.vendor[cell]),
        "window": f"{fault.start.isoformat()} to {fault.end.isoformat()}",
        "primary_kpi": kpi,
        "measured_over": over,
        "kpis": {
            scope: {
                "clean": rounded(evaluation.clean[scope]),
                "faulty": rounded(evaluation.faulty[scope]),
                "fixed": rounded(evaluation.fixed[scope]),
            }
            for scope in ("cell", "area")
        },
        "pm_fingerprint": fingerprint(base, fault, evaluation),
        "cm": [
            {
                "time": c.time.isoformat(),
                "dn": c.dn,
                "attribute": c.attribute,
                "old": c.old_value,
                "new": c.new_value,
            }
            for c in cm_changes(base, [fault])
        ],
        "fm": [
            {
                "raised": a.alarm_raised_time.isoformat(),
                "cleared": a.alarm_cleared_time.isoformat(),
                "eventType": a.event_type,
                "probableCause": a.probable_cause,
                "perceivedSeverity": a.perceived_severity,
                "specificProblem": a.specific_problem,
            }
            for a in fm_alarms(
                base, [fault], {fault.fault_id: float(uplink_rise_db(base, fault)[cell])}
            )
        ],
        "right_answer": RIGHT_ANSWER[kind],
        "right_fix": [
            {
                "kind": c.kind,
                "delta": c.delta,
                "target": base.state.cell_names[c.target] if c.target >= 0 else None,
            }
            for c in right_fix(base, fault)
        ],
        "recovery": None if np.isnan(evaluation.recovery) else round(evaluation.recovery, 3),
        "area_recovery": None
        if np.isnan(evaluation.area_recovery)
        else round(evaluation.area_recovery, 3),
    }
    if evaluation.tries:
        record["parameter_tries"] = [
            {
                "change": f"{t[0][0].kind} {t[0][0].delta:+g}",
                "recovery": None if np.isnan(t[1]) else round(t[1], 3),
            }
            for t in evaluation.tries
        ]
    series = daily_series(base, fault, kind)
    if kind == "F1a":
        wrong = [Change("power", cell, WRONG_FIX_F1A[0].delta, -1)]
        wrong_model = apply_changes(apply_faults(base, [fault]), wrong)
        columns = sorted(base.neighbours)
        wrong_kpis = lte_kpis(replay(wrong_model, 0, columns), evaluation.area_cells)
        record["wrong_fix"] = {
            "change": f"power {wrong[0].delta:+g} dB on the overshooting cell",
            kpi: round(wrong_kpis[kpi], 3),
            "recovery": round(
                (wrong_kpis[kpi] - evaluation.faulty[over][kpi])
                / (evaluation.clean[over][kpi] - evaluation.faulty[over][kpi]),
                3,
            ),
        }
        series["wrong"] = daily(replay(wrong_model, 0, columns), evaluation.area_cells, kpi)
    return record, series


def daily(days: Iterable[Day], cells: np.ndarray, kpi: str) -> list[float]:
    """A KPI per day.

    Args:
        days: Simulated days (consumed once).
        cells: Cells to measure over.
        kpi: KPI name.

    Returns:
        One value per day.
    """
    return [lte_kpis([d], cells)[kpi] for d in days]


def daily_series(base: NetworkModel, fault: Fault, kind: str) -> dict[str, Any]:
    """Primary KPI per day, clean, faulty and fixed, for the figure.

    Args:
        base: The network as built.
        fault: The demonstration fault.
        kind: Its kind.

    Returns:
        Series by run, plus the KPI name.
    """
    kpi = PRIMARY_KPI[kind]
    faulty = apply_faults(base, [fault])
    fix = right_fix(base, fault)
    fixed = apply_changes(faulty, fix) if fix else faulty
    columns = sorted(base.neighbours | fixed.neighbours)
    cells = np.array([fault.cell])
    if MEASURED_OVER[kind] == "area":
        cells = evaluate(base, fault, try_parameters=False).area_cells
    if kind == "F1f":
        faulty_series = daily(simulate_with_faults(base, [fault], 0, 7), cells, kpi)
        fixed_series = faulty_series
    else:
        faulty_series = daily(replay(faulty, 0, columns), cells, kpi)
        fixed_series = daily(replay(fixed, 0, columns), cells, kpi)
    return {
        "kpi": kpi,
        "clean": daily(replay(base, 0, columns), cells, kpi),
        "faulty": faulty_series,
        "fixed": fixed_series,
    }


def schedule_summary(faults: list[Fault]) -> dict[str, Any]:
    """Counts of the planted schedule, without cells or times.

    Args:
        faults: The schedule.

    Returns:
        Totals by kind, faults per week, quiet weeks and overlaps.
    """
    per_week = Counter((f.start - RUN_START).days // 7 + 1 for f in faults)
    overlaps = sum(
        1
        for i, a in enumerate(faults)
        for b in faults[i + 1 :]
        if a.start < b.end and b.start < a.end
    )
    return {
        "faults": len(faults),
        "by_kind": {k: sum(f.kind == k for f in faults) for k in KINDS},
        "per_week": {str(w): per_week.get(w, 0) for w in range(1, WEEKS + 1)},
        "quiet_weeks": [w for w in range(1, WEEKS + 1) if per_week.get(w, 0) == 0],
        "overlapping_pairs": overlaps,
    }


def schedule_recovery(base: NetworkModel, faults: list[Fault]) -> dict[str, Any]:
    """Recovery of the right answer over every planted fault, by kind.

    For kinds without a parameter fix the value is the best recovery any
    single bounded parameter change achieves.

    Args:
        base: The network as built.
        faults: The schedule.

    Returns:
        Kind to count and recovery quartiles.
    """
    values: dict[str, list[float]] = {k: [] for k in KINDS}
    area: dict[str, list[float]] = {k: [] for k in KINDS}
    for fault in faults:
        e = evaluate(base, fault, try_parameters=fault.kind == "F1e")
        best = max((r for _, r in e.tries if not np.isnan(r)), default=np.nan)
        value = best if e.tries else e.recovery
        if not np.isnan(value):
            values[fault.kind].append(value)
        if not e.tries and not np.isnan(e.area_recovery):
            area[fault.kind].append(e.area_recovery)
    return {
        kind: {
            "evaluated": len(values[kind]),
            "recovery_min_median_max": spread(values[kind]),
            "area_recovery_min_median_max": spread(area[kind]),
        }
        for kind in KINDS
    }


def spread(values: list[float]) -> list[float] | None:
    """Min, median and max, rounded.

    Args:
        values: Values.

    Returns:
        The three numbers, or None when there are no values.
    """
    if not values:
        return None
    v = np.array(values)
    return [round(float(x), 3) for x in (v.min(), np.median(v), v.max())]


def run_kpis(days: Iterable[Day], cells: np.ndarray) -> dict[str, float | None]:
    """Network KPIs over some days.

    Args:
        days: Simulated days (consumed once).
        cells: Every LTE cell.

    Returns:
        Rounded KPIs.
    """
    return rounded(lte_kpis(days, cells))


def build() -> dict[str, Any]:
    """Build the record, including the figure's daily series.

    Returns:
        The record.
    """
    t0 = time.perf_counter()
    base = default_model(build_world("demo"))
    faults = plan_faults(base, WEEKS)
    t_build = time.perf_counter() - t0
    demos: dict[str, Any] = {}
    series: dict[str, Any] = {}
    t0 = time.perf_counter()
    for kind in KINDS:
        demos[kind], series[kind] = demonstrate(base, kind)
    t_demo = time.perf_counter() - t0
    t0 = time.perf_counter()
    recovery = schedule_recovery(base, faults)
    t_recovery = time.perf_counter() - t0
    t0 = time.perf_counter()
    lte_cells = np.flatnonzero(base.state.technology == "LTE")
    with_faults = run_kpis(simulate_with_faults(base, faults, 0, 7 * WEEKS), lte_cells)
    t_run = time.perf_counter() - t0
    clean = run_kpis(simulate_days(base, 0, 7 * WEEKS), lte_cells)
    record = {
        "kinds": {
            k: {
                "cause": CAUSE[k],
                "change_in_model": MAGNITUDE[k],
                "duration_h": list(DURATION_H[k]),
                "right_answer": RIGHT_ANSWER[k],
                "primary_kpi": PRIMARY_KPI[k],
                "measured_over": MEASURED_OVER[k],
            }
            for k in KINDS
        },
        "demonstrations": demos,
        "schedule": schedule_summary(faults),
        "schedule_recovery": recovery,
        "twelve_weeks": {"clean": clean, "with_faults": with_faults},
        "daily_series": {
            kind: {
                name: value if name == "kpi" else [round(v, 3) for v in value]
                for name, value in s.items()
            }
            for kind, s in series.items()
        },
        "timing_s": {
            "demo network and fault plan": round(t_build, 1),
            "six demonstrations": round(t_demo, 1),
            "recovery of every planted fault": round(t_recovery, 1),
            f"{WEEKS} weeks of counters with faults": round(t_run, 1),
        },
    }
    return record


def plot_recovery(series: dict[str, Any], path: Path) -> None:
    """Primary KPI per day of each demonstration: clean, faulty, fixed.

    Args:
        series: Per-kind daily series.
        path: PNG path.
    """
    fig, axes = plt.subplots(2, 3, figsize=(12, 6.4), dpi=100, facecolor=SURFACE)
    days = np.arange(1, 8)
    for ax, kind in zip(axes.ravel(), KINDS, strict=True):
        s = series[kind]
        ax.set_facecolor(SURFACE)
        # Clean is a wide pale band under the others, so a fix that restores
        # it exactly stays visible on top of it.
        ax.plot(days, s["clean"], color=CLEAN_COLOR, lw=7, alpha=0.35, label="clean")
        ax.plot(days, s["faulty"], color=FAULTY_COLOR, lw=2, marker="s", ms=4, label="faulty")
        ax.plot(
            days,
            s["fixed"],
            color=FIXED_COLOR,
            lw=1.5,
            marker="^",
            ms=5,
            ls=(0, (4, 2)),
            label="right fix",
        )
        if "wrong" in s:
            ax.plot(days, s["wrong"], color=WRONG_COLOR, lw=2, marker="D", ms=4, label="wrong fix")
        ax.set_title(f"{kind}: {s['kpi']}", color=INK, fontsize=10)
        ax.set_xticks(days)
        ax.tick_params(colors=INK_SECONDARY)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.grid(axis="y", color="#e6e5e0", lw=0.8)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=4, frameon=False)
    for ax in axes[1]:
        ax.set_xlabel("day of the demonstration week", color=INK_SECONDARY)
    fig.suptitle(
        "Planted faults: primary KPI per day, clean, faulty and fixed (synthetic)", color=INK
    )
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def fmt(value: Any) -> str:
    """A value for a Markdown cell.

    Args:
        value: Any value.

    Returns:
        Its text; "not reported" for None.
    """
    return "not reported" if value is None else str(value)


def finding(kind: str, demo: dict[str, Any]) -> list[str]:
    """A plain-language finding, stated only when the recorded numbers show it.

    Args:
        kind: Fault kind.
        demo: The demonstration record.

    Returns:
        Report lines, possibly none.
    """
    thp = "DL IP throughput (kbit/s)"
    cell = demo["kpis"]["cell"]
    area = demo["kpis"]["area"]
    if (
        kind == "F1c"
        and cell["faulty"][thp] > cell["clean"][thp]
        and area["faulty"][thp] < area["clean"][thp]
    ):
        return [
            "",
            "Finding: the cell looks healthier while the area gets worse. With less power it "
            "serves fewer, closer users, and its former edge users load the neighbours.",
        ]
    recovery, area_recovery = demo["recovery"], demo["area_recovery"]
    if (
        kind == "F1d"
        and recovery is not None
        and area_recovery is not None
        and recovery > 1.0
        and area_recovery < 0.0
    ):
        return [
            "",
            "Finding: the offset moves the congestion. The cell recovers past its clean "
            "value while the area gets worse, because the neighbours share the surge; the "
            "capacity note is the durable answer.",
        ]
    return []


def quartiles(values: list[float] | None) -> str:
    """Min, median and max for a Markdown cell.

    Args:
        values: The three values, or None.

    Returns:
        "a / b / c", or "n/a".
    """
    return " / ".join(str(v) for v in values) if values else "n/a"


def render_markdown(record: dict[str, Any]) -> str:
    """Render the report from the record.

    Args:
        record: The record as loaded from JSON.

    Returns:
        The Markdown report.
    """
    lines = [
        "# Planted faults and what-if report",
        "",
        "Synthetic network (rules F1-F4, M6). Every fault is a change inside the network model;",
        "the what-if replays a week with the same random noise (common random numbers). Each kind",
        "is demonstrated on its own cell for the first week of the run, outside the planted",
        "schedule; the schedule itself appears only as counts and aggregate recovery, since its",
        "answers are evaluation-only (rule A3). Written by `python -m ran_lakehouse.faults.report`",
        "from `faults.json`.",
        "",
        "![Primary KPI per day](faults_recovery.png)",
        "",
        "## Fault kinds",
        "",
        "| Kind | Cause | Change in the model (START) | Duration, h | Right answer | Primary KPI |",
        "|---|---|---|---|---|---|",
        *[
            f"| {k} | {v['cause']} | {v['change_in_model']} | "
            f"{v['duration_h'][0]}-{v['duration_h'][1]} | "
            f"{v['right_answer']} | {v['primary_kpi']}, {v['measured_over']} |"
            for k, v in record["kinds"].items()
        ],
        "",
        "Recovery = (fixed - faulty) / (clean - faulty) on the primary KPI: 1 restores the clean",
        "value, 0 does nothing, below 0 makes it worse.",
    ]
    for kind, demo in record["demonstrations"].items():
        lines += [
            "",
            f"## {kind}: {record['kinds'][kind]['cause']}",
            "",
            f"Cell {demo['cell']} ({demo['area_class']}, {demo['vendor']}-style), "
            f"{demo['window']}.",
            "",
            "PM, KPIs over the week (clean / faulty / fixed):",
            "",
            "| KPI | Cell | Area (cell and touched cells) |",
            "|---|---|---|",
        ]
        for name in KPI_ROWS:
            cell = demo["kpis"]["cell"]
            area = demo["kpis"]["area"]
            lines.append(
                f"| {name} | {fmt(cell['clean'][name])} / {fmt(cell['faulty'][name])} / "
                f"{fmt(cell['fixed'][name])} | {fmt(area['clean'][name])} / "
                f"{fmt(area['faulty'][name])} / {fmt(area['fixed'][name])} |"
            )
        lines += ["", "PM traces (clean, faulty):", ""]
        lines += [f"- {k}: {fmt(v)}" for k, v in demo["pm_fingerprint"].items()]
        lines += ["", "CM change log:", ""]
        lines += [
            f"- {c['time']} {c['dn']} {c['attribute']}: {c['old']} to {c['new']}"
            for c in demo["cm"]
        ] or ["- none"]
        lines += ["", "FM alarm log:", ""]
        lines += [
            f"- raised {a['raised']}, cleared {a['cleared']}: {a['eventType']}, "
            f"{a['probableCause']}, "
            f'{a["perceivedSeverity"]}, "{a["specificProblem"]}"'
            for a in demo["fm"]
        ] or ["- none"]
        fix = ", ".join(
            f"{c['kind']} {c['target']}" if c["target"] else f"{c['kind']} {c['delta']:+g}"
            for c in demo["right_fix"]
        )
        lines += [
            "",
            f"Right answer: {demo['right_answer']}"
            + (f" (what-if: {fix})" if fix else "")
            + f". Recovery ({demo['measured_over']}): {fmt(demo['recovery'])}; over the area: "
            + f"{fmt(demo['area_recovery'])}.",
        ]
        lines += finding(kind, demo)
        if "wrong_fix" in demo:
            w = demo["wrong_fix"]
            lines += ["", f"Wrong fix, {w['change']}: recovery {w['recovery']}."]
        if "parameter_tries" in demo:
            lines += ["", "Every single bounded parameter change on the cell:", ""]
            lines += [
                f"- {t['change']}: recovery {fmt(t['recovery'])}" for t in demo["parameter_tries"]
            ]
    s = record["schedule"]
    lines += [
        "",
        "## Planted schedule (rule F3)",
        "",
        f"{s['faults']} faults over {len(s['per_week'])} weeks; quiet weeks {s['quiet_weeks']}; "
        f"{s['overlapping_pairs']} pairs of faults overlap in time.",
        "",
        "| Kind | Faults | Evaluated | Recovery min / median / max | Over the area |",
        "|---|---|---|---|---|",
        *[
            f"| {k} | {s['by_kind'][k]} | {v['evaluated']} | "
            f"{quartiles(v['recovery_min_median_max'])} | "
            f"{quartiles(v['area_recovery_min_median_max'])} |"
            for k, v in record["schedule_recovery"].items()
        ],
        "",
        "F1e counts the best single bounded parameter change; F1f has no parameter to change.",
        "Outages of GSM cells are not evaluated here: the recovery KPIs are LTE KPIs.",
        "",
        "| Week | " + " | ".join(s["per_week"]) + " |",
        "|---|" + "---|" * len(s["per_week"]),
        "| Faults | " + " | ".join(str(v) for v in s["per_week"].values()) + " |",
        "",
        "## Twelve weeks, network-wide LTE KPIs",
        "",
        "| KPI | Clean | With faults |",
        "|---|---|---|",
        *[
            f"| {k} | {fmt(record['twelve_weeks']['clean'][k])} | "
            f"{fmt(record['twelve_weeks']['with_faults'][k])} |"
            for k in KPI_ROWS
        ],
        "",
        "## Timing",
        "",
        "Measured on the build machine; varies run to run.",
        "",
        "| Step | Seconds |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in record["timing_s"].items()],
        "",
        "| Resource | Value |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in record["resources"].items()],
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    """Write the record, the Markdown report and the figure.

    Returns:
        The process exit code.
    """
    RESULTS.mkdir(exist_ok=True)
    started = time.perf_counter()
    record = build()
    # Peak resident set of this process (Linux reports kB).
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    record["resources"] = {
        "wall time of the whole report, s": round(time.perf_counter() - started, 1),
        "peak resident set, MB": round(peak_mb),
    }
    RECORD_JSON.write_text(json.dumps(record, indent=2) + "\n")
    RECORD_MD.write_text(render_markdown(json.loads(RECORD_JSON.read_text())))
    plot_recovery(json.loads(RECORD_JSON.read_text())["daily_series"], FIG_RECOVERY)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
