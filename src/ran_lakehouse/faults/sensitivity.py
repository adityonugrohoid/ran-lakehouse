"""What-if sensitivity (rule M6): how far one-step changes move the KPIs of
the changed cell and of the cells they touch.

Over a seeded sample of LTE cells of the demo network, each cell gets
+1 and -1 degree of tilt and +1 and -1 dB of power, one change at a time;
the next week is replayed before and after with common random numbers.
The report gives the distribution of the change of four KPIs, splits the
neighbours by how they relate to the changed cell, and works through two
cases: the largest cross-layer jump and the neighbour throughput drop seen
in the API smoke test. Synthetic network; the network is the clean demo
network (no planted fault present), so only the change moves the KPIs.

    uv run python -m ran_lakehouse.faults.sensitivity
"""

import json
import resource
import time
from pathlib import Path
from typing import Any

import numpy as np

from ran_lakehouse.faults.whatif import (
    TOTALS,
    Change,
    KpiTotals,
    apply_changes,
    replay,
    touched_cells,
)
from ran_lakehouse.model import NetworkModel, default_model
from ran_lakehouse.model.serving import (
    LAYER_MARGIN_DB,
    LAYER_MIN_RSRP_DBM,
    LAYER_SPREAD_DB,
    LTE_SUBSCRIBERS_PER_PERSON,
    SERVER_SPREAD_DB,
    lte_layer_weights,
)
from ran_lakehouse.world import build_world

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "whatif_sensitivity.json"
RECORD_MD = RESULTS / "whatif_sensitivity.md"
# The same report on the model before the soft assignment of rule M3 (hard
# best-server and layer thresholds), kept to compare against.
BASELINE_JSON = RESULTS / "whatif_sensitivity_hard_thresholds.json"

PROFILE = "demo"
SAMPLE_SEED = 20260930
SAMPLE_CELLS = 100
FIRST_DAY = 84  # the week after the demo run's twelve weeks of history
CHANGES = (("tilt", 1.0), ("tilt", -1.0), ("power", 1.0), ("power", -1.0))
THROUGHPUT = "DL IP throughput (kbit/s)"
PRB = "Mean DL PRB use (%)"
DROP = "E-RAB drop rate (%)"
ACCESS = "E-RAB accessibility (%)"
KPIS = (THROUGHPUT, PRB, DROP, ACCESS)
LARGE_SHARE = 0.10  # "more than about 10%" (the question asked)
# The worked cases: the neighbour drop seen in the API smoke test, and the
# largest cross-layer jump in the sample.
SMOKE_CASE = ("ENB0041_B3_1", "tilt", 1.0, "ENB0031_B3_2")


def per_cell(
    model: NetworkModel, columns: list[tuple[int, int]]
) -> tuple[np.ndarray, dict[str, np.ndarray], int]:
    """Replay the week and sum every KPI counter per LTE cell.

    Args:
        model: The network.
        columns: Relation columns (common to before and after).

    Returns:
        (cell index per column, counter name to per-column sums, periods).
    """
    sums: dict[str, np.ndarray] = {}
    periods = 0
    cells = np.zeros(0, dtype=int)
    for day in replay(model, FIRST_DAY, columns):
        for name in TOTALS:
            week = day.lte.values[name].sum(axis=0)
            sums[name] = week if name not in sums else sums[name] + week
        periods += day.lte.values["RRC.ConnMean"].shape[0]
        cells = day.lte.cells
    return cells, sums, periods


def cell_kpis(week: tuple[np.ndarray, dict[str, np.ndarray], int], cell: int) -> dict[str, float]:
    """One cell's KPIs from per-cell sums (the formulas of KpiTotals).

    Args:
        week: per_cell()'s result.
        cell: Global cell index.

    Returns:
        KPI name to value.
    """
    cells, sums, periods = week
    column = int(np.flatnonzero(cells == cell)[0])
    totals = KpiTotals(np.array([cell]))
    totals.sums = {name: float(sums[name][column]) for name in TOTALS}
    totals.periods = periods
    totals.cell_periods = periods
    return totals.kpis()


def relation(model: NetworkModel, changed: int, other: int) -> str:
    """How a touched cell relates to the changed one.

    Args:
        model: The network.
        changed: Changed cell.
        other: Touched cell.

    Returns:
        "same band", "other band, same site" or "other band, other site".
    """
    if model.state.band[changed] == model.state.band[other]:
        return "same band"
    if model.state.site_ids[changed] == model.state.site_ids[other]:
        return "other band, same site"
    return "other band, other site"


def cases(base: NetworkModel, sample: np.ndarray) -> list[dict[str, Any]]:
    """Every (change, touched LTE cell) with its KPIs before and after.

    Args:
        base: The network.
        sample: Changed cells.

    Returns:
        One row per changed or touched LTE cell per change.
    """
    columns = sorted(base.neighbours)
    before = per_cell(base, columns)
    rows = []
    for cell in sample:
        for kind, delta in CHANGES:
            after_model = apply_changes(base, [Change(kind, int(cell), delta, -1)])
            after = per_cell(after_model, columns)
            for other in touched_cells(base, after_model, {int(cell)}):
                if base.state.technology[other] != "LTE":
                    continue
                b, a = cell_kpis(before, int(other)), cell_kpis(after, int(other))
                rows.append(
                    {
                        "changed_cell": base.state.cell_names[cell],
                        "change": f"{kind} {delta:+g}",
                        "cell": base.state.cell_names[other],
                        "is_changed": int(other) == int(cell),
                        "relation": relation(base, int(cell), int(other)),
                        "subscribers": [
                            float(base.serving.subscribers[other]),
                            float(after_model.serving.subscribers[other]),
                        ],
                        "before": {k: b[k] for k in KPIS},
                        "after": {k: a[k] for k in KPIS},
                    }
                )
    return rows


def spread(values: list[float]) -> dict[str, float] | None:
    """Median, p90, p99 and max of absolute changes.

    Args:
        values: Changes (NaN dropped).

    Returns:
        The four and the count, or None with no values.
    """
    v = np.abs(np.array([x for x in values if not np.isnan(x)]))
    if not len(v):
        return None
    return {
        "median": round(float(np.median(v)), 2),
        "p90": round(float(np.percentile(v, 90)), 2),
        "p99": round(float(np.percentile(v, 99)), 2),
        "max": round(float(v.max()), 2),
        "count": len(v),
    }


def relative(row: dict[str, Any], kpi: str) -> float:
    """Relative change of a KPI, percent.

    Args:
        row: A case.
        kpi: KPI name.

    Returns:
        100 * (after - before) / before; NaN for a zero or missing base.
    """
    b, a = row["before"][kpi], row["after"][kpi]
    if b is None or a is None or np.isnan(b) or np.isnan(a) or b == 0:
        return float("nan")
    return float(100.0 * (a - b) / b)


def absolute(row: dict[str, Any], kpi: str) -> float:
    """Absolute change of a KPI.

    Args:
        row: A case.
        kpi: KPI name.

    Returns:
        after - before; NaN when either is missing.
    """
    b, a = row["before"][kpi], row["after"][kpi]
    if b is None or a is None or np.isnan(b) or np.isnan(a):
        return float("nan")
    return float(a - b)


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Distributions of one group of cases.

    Args:
        rows: Cases.

    Returns:
        Per KPI: relative (%) and absolute spread, and the share of cases
        moved by more than LARGE_SHARE; the subscriber shift likewise.
    """
    out: dict[str, Any] = {"cases": len(rows)}
    for kpi in KPIS:
        rel = [relative(r, kpi) for r in rows]
        finite = [x for x in rel if not np.isnan(x)]
        out[kpi] = {
            "relative, %": spread(rel),
            "absolute": spread([absolute(r, kpi) for r in rows]),
            "share moved > 10%": round(
                float(np.mean([abs(x) > 100 * LARGE_SHARE for x in finite])), 3
            )
            if finite
            else None,
        }
    subs = [
        100.0 * (r["subscribers"][1] - r["subscribers"][0]) / max(r["subscribers"][0], 1.0)
        for r in rows
    ]
    out["subscribers, relative %"] = spread(subs)
    return out


def smoke_case(base: NetworkModel) -> dict[str, Any]:
    """The neighbour throughput drop seen in the API smoke test, explained.

    Args:
        base: The network.

    Returns:
        What moved: points and persons handed over, their SINR, the
        neighbour's users, spectral efficiency, edge share and busy-period
        PRB use, before and after.
    """
    names = list(base.state.cell_names)
    changed_name, kind, delta, neighbour_name = SMOKE_CASE
    changed, neighbour = names.index(changed_name), names.index(neighbour_name)
    after = apply_changes(base, [Change(kind, changed, delta, -1)])
    layer_b, layer_a = (
        base.coverage[base.state.band[changed]],
        after.coverage[base.state.band[changed]],
    )
    gained = (layer_b.best != neighbour) & (layer_a.best == neighbour)
    columns = sorted(base.neighbours)
    load = {}
    for label, model in (("before", base), ("after", after)):
        prb = []
        for day in replay(model, FIRST_DAY, columns):
            column = int(np.flatnonzero(day.lte.cells == neighbour)[0])
            prb.append(day.lte.values["RRU.PrbTotDl"][:, column])
        p = np.concatenate(prb)
        load[label] = {
            "PRB use mean, %": round(float(p.mean()), 1),
            "PRB use p95, %": round(float(np.percentile(p, 95)), 1),
            "PRB use max, %": round(float(p.max()), 1),
        }
    kpis_b = cell_kpis(per_cell(base, columns), neighbour)
    kpis_a = cell_kpis(per_cell(after, columns), neighbour)
    s_b, s_a = base.serving, after.serving
    return {
        "change": f"{changed_name} {kind} {delta:+g}",
        "neighbour cell": neighbour_name,
        "grid points handed to the neighbour": int(gained.sum()),
        "persons at them": round(float(base.persons[gained].sum())),
        "their SINR before, dB (median)": round(float(np.median(layer_b.sinr_db[gained])), 1),
        "their SINR after, dB (median)": round(float(np.median(layer_a.sinr_db[gained])), 1),
        "neighbour state": {
            "subscribers": [
                round(float(s_b.subscribers[neighbour])),
                round(float(s_a.subscribers[neighbour])),
            ],
            "spectral efficiency, bit/s/Hz": [
                round(float(s_b.spectral_efficiency[neighbour]), 3),
                round(float(s_a.spectral_efficiency[neighbour]), 3),
            ],
            "edge share": [
                round(float(s_b.edge_share[neighbour]), 3),
                round(float(s_a.edge_share[neighbour]), 3),
            ],
            "DL IP throughput, kbit/s": [round(kpis_b[THROUGHPUT]), round(kpis_a[THROUGHPUT])],
            "load": load,
        },
    }


def layer_case(base: NetworkModel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The largest co-sited cross-layer subscriber jump, explained.

    Args:
        base: The network.
        rows: The cases.

    Returns:
        The change, the jump, and whether any point changed best server
        inside the changed cell's own layer.
    """
    co_sited = [r for r in rows if r["relation"] == "other band, same site"]
    top = max(co_sited, key=lambda r: abs(r["subscribers"][1] - r["subscribers"][0]))
    names = list(base.state.cell_names)
    changed = names.index(top["changed_cell"])
    kind, delta = top["change"].split()
    after = apply_changes(base, [Change(kind, changed, float(delta), -1)])
    band = base.state.band[changed]
    moved = int((base.coverage[band].best != after.coverage[band].best).sum())
    other = names.index(top["cell"])
    bandwidth = {
        str(b): float(w)
        for b, w in zip(base.state.band, base.state.bandwidth_mhz, strict=True)
        if w
    }
    layers_b = [c for c in base.coverage.values() if c.technology == "LTE"]
    layers_a = [c for c in after.coverage.values() if c.technology == "LTE"]
    k = [c.band for c in layers_b].index(base.state.band[other])
    share_b = lte_layer_weights(layers_b, bandwidth)[k]
    share_a = lte_layer_weights(layers_a, bandwidth)[k]
    served = layers_b[k].best == other
    switched = served & (share_a != share_b)
    gain = float((base.persons * (share_a - share_b))[served].sum()) * LTE_SUBSCRIBERS_PER_PERSON
    return {
        "change": f"{top['changed_cell']} {top['change']}",
        "co-sited cell": top["cell"],
        "its subscribers": [round(top["subscribers"][0]), round(top["subscribers"][1])],
        "its DL IP throughput, kbit/s": [
            round(top["before"][THROUGHPUT]),
            round(top["after"][THROUGHPUT]),
        ],
        "own-layer points changing best server": moved,
        "points of the co-sited cell whose layer share changed": int(switched.sum()),
        "its layer share at them, median": [
            round(float(np.median(share_b[switched])), 3),
            round(float(np.median(share_a[switched])), 3),
        ],
        "subscribers the share change moves": round(gain),
    }


def comparison(record: dict[str, Any]) -> dict[str, Any]:
    """The figures compared before and after the soft assignment.

    Args:
        record: A sensitivity record.

    Returns:
        Neighbour throughput and co-sited layer subscribers (relative change,
        with the share moved by more than 10%), and the changed cell's
        subscribers.
    """
    neighbours = record["touched neighbours"]
    co_sited = record["neighbours by relation"]["other band, same site"]
    return {
        "neighbour throughput, relative %": neighbours[THROUGHPUT]["relative, %"],
        "neighbour throughput, share moved > 10%": neighbours[THROUGHPUT]["share moved > 10%"],
        "co-sited layer subscribers, relative %": co_sited["subscribers, relative %"],
        "changed cell subscribers, relative %": record["changed cell"]["subscribers, relative %"],
        "smoke case neighbour": record["smoke case"]["neighbour state"],
    }


def build() -> dict[str, Any]:
    """The record.

    Returns:
        Settings, distributions and the worked cases.
    """
    base = default_model(build_world(PROFILE))
    lte = np.flatnonzero(base.state.technology == "LTE")
    sample = np.sort(np.random.default_rng(SAMPLE_SEED).choice(lte, SAMPLE_CELLS, replace=False))
    rows = cases(base, sample)
    neighbours = [r for r in rows if not r["is_changed"]]
    return {
        "profile": PROFILE,
        "network": "clean demo network (no planted fault present)",
        "replay days": [FIRST_DAY, FIRST_DAY + 6],
        "sample": {"seed": SAMPLE_SEED, "LTE cells": SAMPLE_CELLS, "of LTE cells": len(lte)},
        "changes per cell": [f"{k} {d:+g}" for k, d in CHANGES],
        "layer split": {
            "margin to the strongest layer, dB": LAYER_MARGIN_DB,
            "minimum RSRP, dBm": LAYER_MIN_RSRP_DBM,
            "logistic scale, dB": LAYER_SPREAD_DB,
            "server split logistic scale, dB": SERVER_SPREAD_DB,
        },
        "changed cell": summary([r for r in rows if r["is_changed"]]),
        "touched neighbours": summary(neighbours),
        "neighbours by relation": {
            rel: summary([r for r in neighbours if r["relation"] == rel])
            for rel in ("same band", "other band, same site", "other band, other site")
        },
        "neighbours by change": {
            f"{k} {d:+g}": summary([r for r in neighbours if r["change"] == f"{k} {d:+g}"])
            for k, d in CHANGES
        },
        "smoke case": smoke_case(base),
        "layer case": layer_case(base, rows),
        "before": comparison(json.loads(BASELINE_JSON.read_text())),
    }


def fmt(s: dict[str, float] | None) -> str:
    """A spread as table cells.

    Args:
        s: spread()'s result.

    Returns:
        "median | p90 | p99 | max".
    """
    if s is None:
        return "n/a | n/a | n/a | n/a"
    return f"{s['median']:g} | {s['p90']:g} | {s['p99']:g} | {s['max']:g}"


def table(group: dict[str, Any]) -> list[str]:
    """One group's KPI table.

    Args:
        group: summary()'s result.

    Returns:
        Markdown lines.
    """
    lines = [
        "| KPI | relative change %: median, p90, p99, max | absolute change: median, p90, p99, max "
        "| share moved > 10% |",
        "|---|---|---|---|",
    ]
    for kpi in KPIS:
        k = group[kpi]
        share = "n/a" if k["share moved > 10%"] is None else f"{100 * k['share moved > 10%']:.1f}%"
        lines.append(
            f"| {kpi} | {fmt(k['relative, %']).replace(' | ', ', ')} "
            f"| {fmt(k['absolute']).replace(' | ', ', ')} | {share} |"
        )
    subs = group["subscribers, relative %"]
    lines.append(f"| subscribers | {fmt(subs).replace(' | ', ', ')} | | |")
    return lines


def plural(n: int, word: str) -> str:
    """A count with its noun.

    Args:
        n: Count.
        word: Singular noun.

    Returns:
        "1 point", "3 points".
    """
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def routine(kpi: dict[str, Any]) -> str:
    """Whether one-step changes routinely move neighbours by more than 10%.

    Args:
        kpi: A KPI's summary for the neighbours.

    Returns:
        A sentence judged on the p90 of the relative change.
    """
    if kpi["relative, %"]["p90"] > 100 * LARGE_SHARE:
        return "One-step changes routinely move neighbours by more than 10% (p90 above 10%)."
    return (
        "One-step changes do not routinely move neighbours by more than 10% (p90 below 10%), "
        "but the tail is long."
    )


def verdict(before: dict[str, Any], after: dict[str, Any]) -> str:
    """What the soft assignment did to the tail, judged on the figures.

    Args:
        before: comparison() of the hard-threshold record.
        after: comparison() of this record.

    Returns:
        A paragraph.
    """
    thr_b = before["neighbour throughput, relative %"]
    thr_a = after["neighbour throughput, relative %"]
    sub_b = before["co-sited layer subscribers, relative %"]
    sub_a = after["co-sited layer subscribers, relative %"]
    shrank = thr_a["p99"] < thr_b["p99"] and sub_a["p99"] < sub_b["p99"]
    return (
        f"The tail {'shrank' if shrank else 'did not shrink'}: neighbour throughput p99 "
        f"{thr_b['p99']:g}% to {thr_a['p99']:g}% (max {thr_b['max']:g}% to {thr_a['max']:g}%), "
        f"co-sited layer subscribers p99 {sub_b['p99']:g}% to {sub_a['p99']:g}% (max "
        f"{sub_b['max']:g}% to {sub_a['max']:g}%). The medians move from {thr_b['median']:g}% "
        f"to {thr_a['median']:g}% (neighbour throughput) and from "
        f"{before['changed cell subscribers, relative %']['median']:g}% to "
        f"{after['changed cell subscribers, relative %']['median']:g}% (the changed cell's "
        "subscribers)."
    )


def render_markdown(record: dict[str, Any]) -> str:
    """The report.

    Args:
        record: The record.

    Returns:
        Markdown.
    """
    n = record["touched neighbours"]
    c = record["changed cell"]
    thr = n[THROUGHPUT]
    smoke = record["smoke case"]
    nb = smoke["neighbour state"]
    se = nb["spectral efficiency, bit/s/Hz"]
    load_b, load_a = nb["load"]["before"], nb["load"]["after"]
    layer = record["layer case"]
    before = record["before"]
    after = comparison(record)
    hard = before["smoke case neighbour"]
    lines = [
        "# What-if sensitivity",
        "",
        f"Synthetic network, {record['network']}; written by "
        "`python -m ran_lakehouse.faults.sensitivity` from `whatif_sensitivity.json` (rule M6).",
        "",
        f"{record['sample']['LTE cells']} of the {record['sample']['of LTE cells']} LTE cells "
        f"(seed {record['sample']['seed']}) each got {', '.join(record['changes per cell'])}, "
        f"one change at a time; days {record['replay days'][0]} to {record['replay days'][1]} "
        "were replayed before and after with common random numbers. A neighbour is every "
        "LTE cell the change touches (rule M6). Changes are absolute values of after minus "
        "before; relative changes are against the before value.",
        "",
        f"## Changed cell ({c['cases']} cases)",
        "",
        *table(c),
        "",
        f"## Touched neighbours ({n['cases']} cases)",
        "",
        *table(n),
        "",
        f"Neighbour throughput moves by a median {thr['relative, %']['median']:g}% and a p90 of "
        f"{thr['relative, %']['p90']:g}%; {100 * thr['share moved > 10%']:.1f}% of neighbour "
        f"cases move by more than 10%. {routine(thr)}",
        "",
        "By how the neighbour relates to the changed cell (DL IP throughput):",
        "",
        "| Relation | cases | relative change %: median, p90, p99, max | share moved > 10% |",
        "|---|---|---|---|",
        *[
            f"| {rel} | {g['cases']} | {fmt(g[THROUGHPUT]['relative, %']).replace(' | ', ', ')} "
            f"| {100 * g[THROUGHPUT]['share moved > 10%']:.1f}% |"
            for rel, g in record["neighbours by relation"].items()
        ],
        "",
        "By change (DL IP throughput of neighbours):",
        "",
        "| Change | cases | relative change %: median, p90, p99, max | share moved > 10% |",
        "|---|---|---|---|",
        *[
            f"| {ch} | {g['cases']} | {fmt(g[THROUGHPUT]['relative, %']).replace(' | ', ', ')} "
            f"| {100 * g[THROUGHPUT]['share moved > 10%']:.1f}% |"
            for ch, g in record["neighbours by change"].items()
        ],
        "",
        "## Why a neighbour loses throughput: the API smoke case",
        "",
        f"`{smoke['change']}` (the API smoke test's change) makes `{smoke['neighbour cell']}` "
        f"the best server at {smoke['grid points handed to the neighbour']} grid points "
        f"({smoke['persons at them']} persons, median SINR "
        f"{smoke['their SINR before, dB (median)']:g} dB before and "
        f"{smoke['their SINR after, dB (median)']:g} dB after); under rule M3 their users move "
        "in part, by the logistic split. The neighbour's subscribers go from "
        f"{nb['subscribers'][0]} to {nb['subscribers'][1]}, its mean spectral efficiency from "
        f"{se[0]:g} to {se[1]:g} bit/s/Hz, its edge share from {nb['edge share'][0]:g} to "
        f"{nb['edge share'][1]:g}, and its PRB use "
        f"{load_b['PRB use mean, %']:g}% to {load_a['PRB use mean, %']:g}% on average, "
        f"{load_b['PRB use p95, %']:g}% to {load_a['PRB use p95, %']:g}% at p95 and "
        f"{load_b['PRB use max, %']:g}% to {load_a['PRB use max, %']:g}% at the busiest period; "
        f"its DL IP throughput goes from {nb['DL IP throughput, kbit/s'][0]} to "
        f"{nb['DL IP throughput, kbit/s'][1]} kbit/s. With hard thresholds the same change took "
        f"it from {hard['subscribers'][0]} to {hard['subscribers'][1]} subscribers and from "
        f"{hard['DL IP throughput, kbit/s'][0]} to {hard['DL IP throughput, kbit/s'][1]} "
        f"kbit/s (PRB p95 {hard['load']['before']['PRB use p95, %']:g}% to "
        f"{hard['load']['after']['PRB use p95, %']:g}%). Per-user throughput is capacity times "
        "(1 - load) (rule M4, processor sharing) and the KPI is volume over active time, so "
        "it weighs the busy periods, where an added edge user costs the most.",
        "",
        "## Largest co-sited layer shift",
        "",
        f"`{layer['change']}` moves "
        f"{plural(layer['own-layer points changing best server'], 'point')} "
        "to another best server inside its own layer; the co-sited "
        f"`{layer['co-sited cell']}` goes from {layer['its subscribers'][0]} to "
        f"{layer['its subscribers'][1]} subscribers (throughput "
        f"{layer['its DL IP throughput, kbit/s'][0]} to {layer['its DL IP throughput, kbit/s'][1]} "
        f"kbit/s). The layer share moves {layer['subscribers the share change moves']} "
        "subscribers onto it, at "
        f"{layer['points of the co-sited cell whose layer share changed']} points where its "
        f"share went from {layer['its layer share at them, median'][0]:g} to "
        f"{layer['its layer share at them, median'][1]:g} (median). "
        "At each grid point, users split over the LTE layers in proportion to bandwidth, "
        "each layer weighted by logistics (scale "
        f"{record['layer split']['logistic scale, dB']:g} dB) in how far its RSRP sits inside "
        f"the {record['layer split']['margin to the strongest layer, dB']:g} dB margin of the "
        "strongest layer and above the "
        f"{record['layer split']['minimum RSRP, dBm']:g} dBm floor; within a layer, users split "
        "between the best and second server by a logistic (scale "
        f"{record['layer split']['server split logistic scale, dB']:g} dB) in their level "
        "difference (rule M3, ASSUMPTION).",
        "",
        "## Before and after the soft assignment",
        "",
        "Before: the same report on the model with hard thresholds (one best server per point, "
        "a layer's share switched on or off at the margin and the floor), from "
        "`whatif_sensitivity_hard_thresholds.json`. After: this report (rule M3 soft "
        "assignment).",
        "",
        "| Figure | before: median, p90, p99, max | after: median, p90, p99, max |",
        "|---|---|---|",
        *[
            f"| {name} | {fmt(before[name]).replace(' | ', ', ')} "
            f"| {fmt(after[name]).replace(' | ', ', ')} |"
            for name in (
                "neighbour throughput, relative %",
                "co-sited layer subscribers, relative %",
                "changed cell subscribers, relative %",
            )
        ],
        "",
        "Neighbour cases moved by more than 10% in throughput: "
        f"{100 * before['neighbour throughput, share moved > 10%']:.1f}% before, "
        f"{100 * after['neighbour throughput, share moved > 10%']:.1f}% after.",
        "",
        "## Finding",
        "",
        verdict(before, after),
    ]
    res = record.get("resources")
    if res:
        lines += [
            "",
            "## Cost",
            "",
            f"Report run {res['wall time of the whole report, s']:g} s, peak resident set "
            f"{res['peak resident set, MB']} MB.",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    """Write the record and the report.

    Returns:
        The process exit code.
    """
    RESULTS.mkdir(exist_ok=True)
    started = time.perf_counter()
    record = build()
    record["resources"] = {
        "wall time of the whole report, s": round(time.perf_counter() - started, 1),
        # Peak resident set of this process (Linux reports kB).
        "peak resident set, MB": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0),
    }
    RECORD_JSON.write_text(json.dumps(record, indent=2) + "\n")
    RECORD_MD.write_text(render_markdown(json.loads(RECORD_JSON.read_text())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
