"""Gold build and KPI catalog reports (rules L3, L5, D6, E4).

Builds gold from the demo run's 12 weeks of silver through dbt, one UTC day
at a time, and measures it; checks every daily KPI against the model's own
counters (lake.gold_check); and writes the KPI catalog. The planted
formula revision (rule D6) is public once applied; the planted faults stay
evaluation-only (rule A3), so the faulted-cell figure withholds the cell
and the date. Writes results/gold_build.{json,md}, results/kpi_catalog.
{json,md} and three figures.
Run: `uv run python -m ran_lakehouse.lake.gold_report --warehouse demo`
after the silver build.
"""

import argparse
import json
import resource
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib
import pyarrow as pa

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ran_lakehouse.collect.backfill import EMS_LIST
from ran_lakehouse.collect.delivery import plan_delivery
from ran_lakehouse.faults.plant import plan_faults
from ran_lakehouse.lake import gold
from ran_lakehouse.lake.catalog import pyiceberg as catalog
from ran_lakehouse.lake.gold import KPI_REVISION, GoldBuild, Target
from ran_lakehouse.lake.gold_check import (
    AVAIL_TOLERANCE_PCT,
    REL_TOLERANCE,
    compare,
    complete_days,
)
from ran_lakehouse.lake.kpi_catalog import KPIS, Kpi, revised
from ran_lakehouse.lake.silver import scalar
from ran_lakehouse.model import RUN_START, default_model
from ran_lakehouse.world import build_world

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "gold_build.json"
RECORD_MD = RESULTS / "gold_build.md"
CATALOG_JSON = RESULTS / "kpi_catalog.json"
CATALOG_MD = RESULTS / "kpi_catalog.md"
FIG_TRENDS = RESULTS / "gold_network_trends.png"
FIG_FAULT = RESULTS / "gold_faulted_cell.png"
FIG_WORST = RESULTS / "gold_worst_cells.png"
PROFILE = "demo"
WEEKS = 12
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
SERIES = ("#2a78d6", "#eb6834")
FAULT_SHADE = "#f3e3d9"
TABLES = (
    "cells",
    "lte_cell_15m",
    "lte_cell_60m",
    "gsm_cell_15m",
    *gold.KPI_MODELS,
    "worst_cells_week",
    "kpi_catalog",
    "loads",
)

NETWORK_LTE = """
SELECT CAST(period_start + INTERVAL 7 HOUR AS DATE) AS day,
    100 * (sum(rrc_succ) / sum(rrc_att)) * (sum(s1_succ) / sum(s1_att))
        * (sum(erab_succ) / sum(erab_att)) AS erab_acc,
    100 * sum(rrc_succ) / sum(rrc_att) AS rrc_v1,
    100 * (sum(rrc_succ) / sum(rrc_att)) * (sum(s1_succ) / sum(s1_att)) AS rrc_v2,
    100 * sum(erab_rel) / sum(erab_succ) AS erab_drop,
    sum(ip_vol_kbit) / sum(ip_time_ms) AS thp_mbit,
    sum(prb_pct * n_rb) / sum(n_rb) FILTER (WHERE prb_pct IS NOT NULL) AS prb
FROM lk.gold.lte_cell_15m JOIN lk.gold.cells USING (cell_name) GROUP BY 1 ORDER BY 1
"""
# Weekday against weekend (WIB) per area class, for the traffic-mix check.
MIX = """
WITH w AS (
    SELECT g.*, dayofweek(period_start + INTERVAL 7 HOUR) IN (0, 6) AS weekend,
        CAST(period_start + INTERVAL 7 HOUR AS DATE) AS day
    FROM lk.gold.lte_cell_15m g
)
SELECT c.area, w.weekend, sum(ip_vol_kbit) / sum(ip_time_ms) AS thp_mbit,
    sum(ip_vol_kbit) AS volume,
    sum(prb_pct * n_rb) / sum(n_rb) FILTER (WHERE prb_pct IS NOT NULL) AS prb
FROM w JOIN area_class c USING (cell_name) JOIN lk.gold.cells USING (cell_name)
WHERE w.day IN (SELECT day FROM complete)
GROUP BY ALL ORDER BY ALL
"""
MIX_CQI = """
SELECT c.area, dayofweek(period_start + INTERVAL 7 HOUR) IN (0, 6) AS weekend,
    sum(cqi_weighted) / sum(cqi_samples) AS cqi
FROM lk.gold.lte_cell_60m JOIN area_class c USING (cell_name)
WHERE CAST(period_start + INTERVAL 7 HOUR AS DATE) IN (SELECT day FROM complete)
GROUP BY ALL ORDER BY ALL
"""
MIX_CELLS = """
WITH per AS (
    SELECT cell_name, dayofweek(period_start + INTERVAL 7 HOUR) IN (0, 6) AS weekend,
        sum(ip_vol_kbit) / sum(ip_time_ms) AS thp, avg(prb_pct) AS prb
    FROM lk.gold.lte_cell_15m
    WHERE CAST(period_start + INTERVAL 7 HOUR AS DATE) IN (SELECT day FROM complete)
    GROUP BY ALL
)
SELECT count(*), count(*) FILTER (WHERE e.thp >= d.thp),
    count(*) FILTER (WHERE e.thp < d.thp AND e.prb < d.prb)
FROM per d JOIN per e ON d.cell_name = e.cell_name AND NOT d.weekend AND e.weekend
"""
NETWORK_GSM = """
SELECT CAST(period_start + INTERVAL 7 HOUR AS DATE) AS day,
    100 * (sum(tch_succ) / (sum(tch_req) - sum(tch_blocked)))
        * (sum(ia_succ) / sum(ia_att)) AS sas,
    100 * sum(tch_blocked) / sum(tch_req) AS tch_block
FROM lk.gold.gsm_cell_15m GROUP BY 1 ORDER BY 1
"""


def style(ax: Any, title: str) -> None:
    """Shared axes style.

    Args:
        ax: Matplotlib axes.
        title: Title.
    """
    ax.set_title(title, color=INK, fontsize=10)
    ax.tick_params(colors=INK_SECONDARY, labelsize=8)
    ax.grid(axis="y", color="#e4e3df", linewidth=0.6)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def build(warehouse: str) -> dict[str, Any]:
    """Build gold and measure it.

    Args:
        warehouse: Warehouse with silver built and no gold yet.

    Returns:
        The record.

    Raises:
        RuntimeError: If gold was already built there.
    """
    target = Target("lake", warehouse)
    model = default_model(build_world(PROFILE))
    con = target.connect()
    has_gold = scalar(
        con,
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_catalog = 'lk' AND table_schema = 'gold' AND table_name = 'loads'",
    )
    if has_gold and scalar(con, f"SELECT count(*) FROM {gold.LOADS}"):
        raise RuntimeError(f"gold already built in {warehouse}; the report needs a fresh build")
    con.close()
    started = time.perf_counter()
    load_id = f"gold-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    stats = GoldBuild(target, load_id, KPI_REVISION).run()
    build_s = time.perf_counter() - started
    peak = {
        "python_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        "largest_dbt_run_mb": round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024),
    }
    con = target.connect()
    rows = {t: int(scalar(con, f"SELECT count(*) FROM lk.gold.{t}")) for t in TABLES}
    lake = catalog(warehouse)
    storage = {}
    for t in TABLES:
        tasks = list(lake.load_table(f"gold.{t}").scan().plan_files())
        storage[t] = {
            "data_files": len(tasks),
            "bytes": sum(task.file.file_size_in_bytes for task in tasks),
        }
    coverage = {
        tech: dict(
            zip(
                ("cell_days", "below_full_coverage", "with_suspect_values"),
                (
                    int(v)
                    for v in con.execute(
                        f"SELECT count(*), count(*) FILTER (WHERE coverage < 1), "
                        f"count(*) FILTER (WHERE suspect_share > 0) "
                        f"FROM lk.gold.{tech}_kpi_day WHERE kpi_id = '{kpi}'"
                    ).fetchone()
                    or ()
                ),
                strict=True,
            )
        )
        for tech, kpi in (("lte", "LTE_ERAB_ACC"), ("gsm", "GSM_SAS"))
    }
    worst = {
        kpi: {"weeks": int(weeks), "persistent_cell_weeks": int(n), "most_in_a_week": int(most)}
        for kpi, weeks, n, most in con.execute(
            "SELECT kpi_id || ' v' || formula_version, count(DISTINCT week_start), count(*), "
            "max(per_week) FROM (SELECT *, count(*) OVER (PARTITION BY week_start, kpi_id, "
            "formula_version) AS per_week FROM lk.gold.worst_cells_week) GROUP BY 1 ORDER BY 1"
        ).fetchall()
    }
    # Complete WIB days only: the run's last day is partial in gold.
    complete = complete_days(con)
    network_lte = [r for r in con.execute(NETWORK_LTE).fetchall() if r[0] in complete]
    network_gsm = [r for r in con.execute(NETWORK_GSM).fetchall() if r[0] in complete]
    mix = traffic_mix(con, model, complete)
    revision = con.execute(
        "SELECT kpi_id, from_version, to_version, effective_day, detail "
        "FROM lk.evaluation.kpi_revisions"
    ).fetchall()
    faults = plan_faults(model, WEEKS)
    plan = plan_delivery(model, EMS_LIST, 7 * WEEKS)
    started = time.perf_counter()
    check: dict[str, dict[str, Any]] = {}
    for week in range(WEEKS):
        for key, c in compare(con, model, faults, plan, range(7 * week, 7 * week + 7)).items():
            entry = check.setdefault(
                key, {"compared": 0, "agreeing": 0, "max_abs_diff": 0.0, "missing_in_gold": 0}
            )
            entry["compared"] += c.compared
            entry["agreeing"] += c.agreeing
            entry["missing_in_gold"] += c.missing_in_gold
            entry["max_abs_diff"] = max(entry["max_abs_diff"], c.max_abs_diff)
    check_s = time.perf_counter() - started
    plot_trends(network_lte, network_gsm, KPI_REVISION.effective_day)
    fault_note = plot_faulted_cell(con, model, faults)
    worst_note = plot_worst(con)
    con.close()
    return {
        "profile": PROFILE,
        "weeks": WEEKS,
        "revision": [
            {
                "kpi_id": k,
                "from_version": f,
                "to_version": t,
                "effective_day": d.isoformat(),
                "detail": text,
            }
            for k, f, t, d, text in revision
        ],
        "build": {
            "utc_days_built": stats["days"],
            "dbt_runs": stats["dbt_runs"],
            "dbt_runs_rerun_after_clock_step": stats["dbt_retries"],
            "seconds_days": round(stats["days_s"], 1),
            "seconds_reprocess": round(stats["reprocess_s"], 1),
            "seconds_total": round(build_s, 1),
        },
        "peak_rss": peak,
        "rows": rows,
        "storage": storage,
        "coverage": coverage,
        "worst_cells": worst,
        "persistence": {
            "n": gold.PERSISTENCE_N,
            "m": gold.PERSISTENCE_M,
            "min_coverage": gold.MIN_COVERAGE,
        },
        "network_days": {
            "first": network_lte[0][0].isoformat(),
            "last": network_lte[-1][0].isoformat(),
            "rrc_v1_mean": round(sum(r[2] for r in network_lte) / len(network_lte), 3),
            "rrc_v2_mean": round(sum(r[3] for r in network_lte) / len(network_lte), 3),
        },
        "model_check": {
            k: v | {"max_abs_diff": float(f"{v['max_abs_diff']:.3g}")} for k, v in check.items()
        },
        "model_check_tolerance": {
            "relative": REL_TOLERANCE,
            "availability_pct": round(AVAIL_TOLERANCE_PCT, 3),
        },
        "model_check_seconds": round(check_s, 1),
        "model_check_wib_days": len(complete),
        "figures": {"faulted_cell": fault_note, "worst_cells": worst_note},
        "weekend_traffic_mix": mix,
    }


def traffic_mix(con: Any, model: Any, complete: set[Any]) -> dict[str, Any]:
    """Weekday against weekend per area class, and per cell (complete WIB days).

    Args:
        con: DuckDB with gold.
        model: The network (area class per cell).
        complete: Complete WIB days.

    Returns:
        Per class and day type: DL IP throughput, share of DL volume,
        N_RB-weighted PRB utilization and mean CQI; and the count of cells
        whose weekend throughput is at least their weekday one.
    """
    con.register(
        "area_class",
        pa.table(
            {
                "cell_name": [c.cell_name for c in model.world.cells],
                "area": [str(a) for a in model.state.area_class],
            }
        ),
    )
    con.register("complete", pa.table({"day": pa.array(sorted(complete), pa.date32())}))
    rows = con.execute(MIX).fetchall()
    cqi = {(a, w): v for a, w, v in con.execute(MIX_CQI).fetchall()}
    cells, not_lower, lower_both = con.execute(MIX_CELLS).fetchone() or (0, 0, 0)
    total = {w: sum(r[3] for r in rows if r[1] == w) for w in (False, True)}
    out: dict[str, Any] = {}
    for area, weekend, thp, volume, prb in rows:
        out.setdefault(area, {})["weekend" if weekend else "weekday"] = {
            "dl_ip_throughput_mbit_s": round(thp, 2),
            "share_of_dl_volume_pct": round(100 * volume / total[weekend], 1),
            "prb_utilization_pct": round(prb, 1),
            "mean_cqi": round(cqi[(area, weekend)], 2),
        }
    return {
        "by_area_class": out,
        "cells": int(cells),
        "cells_weekend_throughput_not_lower": int(not_lower),
        "cells_lower_throughput_and_lower_prb": int(lower_both),
    }


def plot_trends(lte: list[tuple[Any, ...]], gsm: list[tuple[Any, ...]], revision: Any) -> None:
    """Daily network KPIs over the run (ratio of sums over all cells).

    Args:
        lte: Network LTE rows (NETWORK_LTE).
        gsm: Network GSM rows (NETWORK_GSM).
        revision: Revision day, marked on the RRC panel.
    """
    days = [r[0] for r in lte]
    panels: list[tuple[str, list[tuple[list[Any], list[Any], str | None]]]] = [
        ("LTE E-RAB accessibility (%)", [(days, [r[1] for r in lte], None)]),
        (
            "LTE RRC setup success (%)",
            [(days, [r[2] for r in lte], "v1"), (days, [r[3] for r in lte], "v2")],
        ),
        ("LTE E-RAB drop rate (%)", [(days, [r[4] for r in lte], None)]),
        ("LTE DL IP throughput (Mbit/s)", [(days, [r[5] for r in lte], None)]),
        ("LTE DL PRB utilization, N_RB-weighted (%)", [(days, [r[6] for r in lte], None)]),
        ("GSM service access success (%)", [([r[0] for r in gsm], [r[1] for r in gsm], None)]),
        ("GSM TCH blocking (%)", [([r[0] for r in gsm], [r[2] for r in gsm], None)]),
    ]
    fig, axes = plt.subplots(4, 2, figsize=(11, 10), facecolor=SURFACE)
    for ax, (title, series) in zip(axes.flat, panels, strict=False):
        ax.set_facecolor(SURFACE)
        for k, (x, y, label) in enumerate(series):
            ax.plot(x, y, color=SERIES[k], linewidth=1.6, label=label)
        if len(series) > 1:
            ax.axvline(revision, color=INK_SECONDARY, linewidth=0.8, linestyle="--")
            ax.legend(frameon=False, fontsize=8, title="formula", title_fontsize=8)
        style(ax, title)
        ax.tick_params(axis="x", labelrotation=30)
    axes.flat[-1].axis("off")
    fig.suptitle("Synthetic network: daily network KPIs from gold, 12 weeks (WIB days)", color=INK)
    fig.tight_layout()
    fig.savefig(FIG_TRENDS, facecolor=SURFACE, dpi=110)
    plt.close(fig)


def plot_faulted_cell(con: Any, model: Any, faults: list[Any]) -> str:
    """Hourly KPIs of one cell around a planted fault, cell and date withheld.

    Args:
        con: DuckDB with gold.
        model: The network.
        faults: The fault schedule.

    Returns:
        What the figure shows, without the cell or the date.

    Raises:
        RuntimeError: If no suitable fault exists.
    """
    run_end = RUN_START + timedelta(days=7 * WEEKS)
    pick = next(
        (
            f
            for f in faults
            if f.kind == "F1e"
            and model.world.cells[f.cell].technology == "LTE"
            and f.start - timedelta(days=2) >= RUN_START
            and f.end + timedelta(days=2) <= run_end
        ),
        None,
    )
    if pick is None:
        raise RuntimeError("no LTE F1e fault with two clean days either side")
    cell = model.world.cells[pick.cell].cell_name
    start_utc = pick.start.replace(tzinfo=UTC) - timedelta(hours=7)
    end_utc = pick.end.replace(tzinfo=UTC) - timedelta(hours=7)
    lo, hi = start_utc - timedelta(days=2), end_utc + timedelta(days=2)
    rows = con.execute(
        "SELECT period_start, kpi_id, value FROM lk.gold.lte_kpi_hour "
        "WHERE cell_name = ? AND formula_version = 1 AND kpi_id IN "
        "('LTE_RRC_SSR', 'LTE_ERAB_DROP') AND period_start >= ? AND period_start < ? "
        "ORDER BY period_start",
        [cell, lo, hi],
    ).fetchall()
    fig, axes = plt.subplots(2, 1, figsize=(10, 5.5), sharex=True, facecolor=SURFACE)
    duration = (end_utc - start_utc).total_seconds() / 3600
    for ax, (kpi, title) in zip(
        axes,
        (("LTE_RRC_SSR", "RRC setup success (%)"), ("LTE_ERAB_DROP", "E-RAB drop rate (%)")),
        strict=True,
    ):
        ax.set_facecolor(SURFACE)
        hours = [(t - start_utc).total_seconds() / 3600 for t, k, _ in rows if k == kpi]
        values = [v for _, k, v in rows if k == kpi]
        ax.axvspan(0, duration, color=FAULT_SHADE, label="fault present")
        ax.plot(hours, values, color=SERIES[0], linewidth=1.3)
        style(ax, title)
    axes[0].legend(frameon=False, fontsize=8)
    axes[1].set_xlabel("hours from the fault's start", color=INK_SECONDARY)
    fig.suptitle(
        "Synthetic network: one planted uplink-interference fault (F1e), hourly gold KPIs;\n"
        "cell and date withheld (rule A3)",
        color=INK,
        fontsize=10,
    )
    fig.tight_layout()
    fig.savefig(FIG_FAULT, facecolor=SURFACE, dpi=110)
    plt.close(fig)
    return f"one planted F1e fault lasting {duration:.0f} h, two days either side"


def plot_worst(con: Any) -> str:
    """The worst-cell ranking of the week and KPI with the most persistent cells.

    Args:
        con: DuckDB with gold.

    Returns:
        Which KPI and week the figure shows.

    Raises:
        RuntimeError: If the ranking is empty.
    """
    top = con.execute(
        "SELECT kpi_id, formula_version, week_start, count(*) AS n "
        "FROM lk.gold.worst_cells_week GROUP BY ALL ORDER BY n DESC, kpi_id, week_start LIMIT 1"
    ).fetchone()
    if top is None:
        raise RuntimeError("the worst-cell ranking is empty")
    kpi, version, week, _ = top
    rows = con.execute(
        "SELECT cell_name, breach_days, week_value, breach_threshold, better FROM "
        "lk.gold.worst_cells_week WHERE kpi_id = ? AND formula_version = ? AND week_start = ? "
        "ORDER BY rank LIMIT 10",
        [kpi, version, week],
    ).fetchall()
    threshold, better = rows[0][3], rows[0][4]
    fig, ax = plt.subplots(figsize=(9, 4.8), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    names = [r[0] for r in rows][::-1]
    values = [r[2] for r in rows][::-1]
    ax.scatter(values, names, color=SERIES[0], s=36, zorder=3)
    ax.hlines(names, [threshold] * len(names), values, color=SERIES[0], linewidth=1.2)
    ax.axvline(threshold, color=INK_SECONDARY, linewidth=0.9, linestyle="--")
    for y, (value, days) in enumerate(zip(values, [r[1] for r in rows][::-1], strict=True)):
        ax.annotate(
            f"{days} of {gold.PERSISTENCE_M} days",
            (value, y),
            xytext=(-8 if better == "higher" else 8, 0),
            textcoords="offset points",
            ha="right" if better == "higher" else "left",
            va="center",
            fontsize=8,
            color=INK,
        )
    span = max(abs(v - threshold) for v in values)
    if better == "higher":
        ax.set_xlim(threshold - 1.6 * span, threshold + 0.2 * span)
    else:
        ax.set_xlim(threshold - 0.2 * span, threshold + 1.6 * span)
    ax.set_xlabel(
        f"week value; dashed: daily breach threshold {threshold:g} (START); ranked by breach "
        f"days, then distance past the threshold",
        color=INK_SECONDARY,
    )
    style(ax, f"Synthetic network: worst cells, {kpi} v{version}, week of {week.isoformat()} (WIB)")
    ax.grid(axis="x", color="#e4e3df", linewidth=0.6)
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    fig.savefig(FIG_WORST, facecolor=SURFACE, dpi=110)
    plt.close(fig)
    return f"{kpi} v{version}, week of {week.isoformat()}, top {len(rows)} persistent cells"


def catalog_record() -> dict[str, Any]:
    """The KPI catalog as a record, with the revised version.

    Returns:
        The record.
    """
    entries: list[Kpi] = [
        *KPIS,
        revised(KPI_REVISION.kpi_id, KPI_REVISION.version, KPI_REVISION.effective_day),
    ]
    return {
        "kpis": [
            {
                "kpi_id": k.kpi_id,
                "formula_version": k.formula_version,
                "name": k.name,
                "technology": k.technology,
                "formula": k.formula,
                "source": k.source,
                "unit": k.unit,
                "granularities": list(k.granularities),
                "vendors": list(k.vendors),
                "better": k.better,
                "breach_threshold": k.breach_threshold,
                "effective_from": None
                if k.effective_from is None
                else k.effective_from.isoformat(),
                "operators_differ": k.operators_differ,
            }
            for k in entries
        ],
        "persistence": {"n": gold.PERSISTENCE_N, "m": gold.PERSISTENCE_M},
        "min_coverage": gold.MIN_COVERAGE,
    }


def render_catalog(record: dict[str, Any]) -> str:
    """Render the KPI catalog report.

    Args:
        record: The catalog record as loaded from JSON.

    Returns:
        The Markdown report.
    """
    lines = [
        "# KPI catalog",
        "",
        "Gold KPIs of the synthetic network (rules L3, E4). Every value is a ratio of sums over",
        "the window's reported periods, never an average of ratios; a zero denominator gives",
        "no value (NULL), never 0. Each value carries its coverage (reported / expected",
        "15-minute periods; hourly periods for the CQI KPI) and its suspect share (suspect /",
        "reported periods), so gaps (rule D3) and suspect data (rule D4) stay visible.",
        "Granularities: 15 minutes and hours in UTC, days and weeks (from Monday) in WIB.",
        "Values are per cell. Over several cells, ratio KPIs sum their counters first; PRB",
        "utilization is weighted by each cell's N_RB (gold.cells, TS 36.101 Table 5.6-1).",
        "Counter names: TS 32.425 (LTE) and TS 52.402 (GSM). Breach thresholds drive the",
        "weekly worst-cell ranking (rule L5): a day is judged when its coverage is at least",
        f"{record['min_coverage']}; a cell is persistent when it breaches on "
        f"{record['persistence']['n']} of the week's {record['persistence']['m']} days. "
        "Thresholds, N and the coverage floor are START values.",
        "Written by `python -m ran_lakehouse.lake.gold_report` from `kpi_catalog.json`.",
        "",
        "| Id | v | Name | Unit | Source | Granularities | Vendors | Breach (START) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for k in record["kpis"]:
        breach = (
            ""
            if k["breach_threshold"] is None
            else (f"{'below' if k['better'] == 'higher' else 'above'} {k['breach_threshold']:g}")
        )
        lines.append(
            f"| {k['kpi_id']} | {k['formula_version']} | {k['name']} | {k['unit']} | "
            f"{k['source']} | {', '.join(k['granularities'])} | {', '.join(k['vendors'])} | "
            f"{breach} |"
        )
    lines += ["", "## Formulas and where operators differ", ""]
    for k in record["kpis"]:
        since = f" Current from {k['effective_from']} (WIB)." if k["effective_from"] else ""
        lines += [
            f"### {k['kpi_id']} v{k['formula_version']}: {k['name']}",
            "",
            f"`{k['formula']}`",
            "",
            f"{k['operators_differ']}{since}",
            "",
        ]
    return "\n".join(lines)


def network_throughput(by_area_class: dict[str, Any], day: str) -> float:
    """Network DL IP throughput of a day type from the area-class rows.

    Throughput is volume over active time, so the network figure is the
    volume-share-weighted harmonic mean of the classes.

    Args:
        by_area_class: weekend_traffic_mix["by_area_class"].
        day: "weekday" or "weekend".

    Returns:
        Mbit/s.
    """
    time = sum(
        v[day]["share_of_dl_volume_pct"] / v[day]["dl_ip_throughput_mbit_s"]
        for v in by_area_class.values()
    )
    share = sum(v[day]["share_of_dl_volume_pct"] for v in by_area_class.values())
    return float(share / time)


def network_sentence(by_area_class: dict[str, Any]) -> str:
    """Whether network throughput is lower at weekends, with the figures.

    Args:
        by_area_class: weekend_traffic_mix["by_area_class"].

    Returns:
        A sentence.
    """
    weekday = network_throughput(by_area_class, "weekday")
    weekend = network_throughput(by_area_class, "weekend")
    lower = "lower" if weekend < weekday else "not lower"
    return (
        f"Network DL IP throughput is {lower} at weekends ({weekend:.1f} against {weekday:.1f} "
        "Mbit/s on weekdays, from the rows below)."
    )


def render_markdown(record: dict[str, Any]) -> str:
    """Render the gold build report.

    Args:
        record: The record as loaded from JSON.

    Returns:
        The Markdown report.
    """
    b = record["build"]
    total_bytes = sum(s["bytes"] for s in record["storage"].values())
    check_ok = all(v["agreeing"] == v["compared"] for v in record["model_check"].values())
    lines = [
        "# Gold build report",
        "",
        f"Synthetic network, {record['profile']} profile. {record['weeks']} weeks of silver built",
        "into gold KPIs by the dbt project (incremental merge models keyed by formula version,",
        "rule S2), one UTC day at a time, recomputing the WIB days and weeks each day touches",
        "(rules L3, L5, D6). Formulas: `kpi_catalog.md`. Written by",
        "`python -m ran_lakehouse.lake.gold_report` from `gold_build.json`.",
        "",
        "![Daily network KPIs](gold_network_trends.png)",
        "",
        "## Formula revision (rule D6)",
        "",
    ]
    for r in record["revision"]:
        lines += [
            f"{r['kpi_id']} v{r['from_version']} to v{r['to_version']}, current from "
            f"{r['effective_day']} (WIB): {r['detail']}. Both versions are kept for the whole",
            f"history. Network daily mean over the run: v1 {record['network_days']['rrc_v1_mean']}"
            f" %, v2 {record['network_days']['rrc_v2_mean']} %.",
        ]
    lines += [
        "",
        "## Agreement with the model",
        "",
        "Every daily KPI recomputed from the simulator's own counters (same faults) against",
        "gold, per cell and WIB day, leaving out cell-days a planted delivery anomaly changed",
        "(D2 conflict, D3, D4). Tolerance: relative "
        f"{record['model_check_tolerance']['relative']:g}; availability "
        f"{record['model_check_tolerance']['availability_pct']} percentage points (10 s samples).",
        f"WIB days compared: {record['model_check_wib_days']} of {7 * record['weeks']}: the",
        "run's last WIB day stays partial in gold (silver builds a UTC day only after it ends).",
        f"All agree: {'yes' if check_ok else 'NO'}.",
        "",
        "| KPI | Cell-days compared | Agreeing | Largest difference | Missing in gold |",
        "|---|---|---|---|---|",
        *[
            f"| {k} | {v['compared']:,} | {v['agreeing']:,} | {v['max_abs_diff']:g} | "
            f"{v['missing_in_gold']} |"
            for k, v in record["model_check"].items()
        ],
        "",
        "## Coverage and suspect data",
        "",
        "| Daily KPI rows | Cell-days | Coverage below 1 | With suspect values |",
        "|---|---|---|---|",
        *[
            f"| {tech.upper()} | {c['cell_days']:,} | {c['below_full_coverage']:,} | "
            f"{c['with_suspect_values']:,} |"
            for tech, c in record["coverage"].items()
        ],
        "",
        "## Worst cells (rule L5)",
        "",
        f"Persistent: breach on {record['persistence']['n']} of {record['persistence']['m']} "
        f"days of a WIB week, days judged at coverage {record['persistence']['min_coverage']} "
        "or more (START).",
        "",
        "![Worst cells](gold_worst_cells.png)",
        "",
        f"The figure: {record['figures']['worst_cells']}.",
        "",
        "| KPI | Weeks with a persistent cell | Persistent cell-weeks | Most in one week |",
        "|---|---|---|---|",
        *[
            f"| {k} | {v['weeks']} | {v['persistent_cell_weeks']} | {v['most_in_a_week']} |"
            for k, v in record["worst_cells"].items()
        ],
        "",
        "## Weekend throughput: a traffic-mix effect",
        "",
        network_sentence(record["weekend_traffic_mix"]["by_area_class"]) + " Cell by",
        f"cell it is not: {record['weekend_traffic_mix']['cells_weekend_throughput_not_lower']:,}"
        f" of {record['weekend_traffic_mix']['cells']:,} LTE cells have a weekend throughput at",
        "least their weekday one, and "
        f"{record['weekend_traffic_mix']['cells_lower_throughput_and_lower_prb']} have both lower "
        "throughput and lower PRB use.",
        "At weekends traffic moves from the urban business areas, where throughput is highest, to",
        "residential and suburban cells, so the network mean falls (complete WIB days,",
        "weekday against Saturday and Sunday):",
        "",
        "| Area class | Day | DL IP throughput (Mbit/s) | Share of DL volume (%) | "
        "PRB utilization, N_RB-weighted (%) | Mean CQI |",
        "|---|---|---|---|---|---|",
        *[
            f"| {area} | {day} | {v['dl_ip_throughput_mbit_s']} | "
            f"{v['share_of_dl_volume_pct']} | {v['prb_utilization_pct']} | {v['mean_cqi']} |"
            for area, days in record["weekend_traffic_mix"]["by_area_class"].items()
            for day, v in sorted(days.items())
        ],
        "",
        "## A faulted cell",
        "",
        "![Faulted cell](gold_faulted_cell.png)",
        "",
        f"{record['figures']['faulted_cell']}; the planted schedule is evaluation-only (rule",
        "A3), so the cell and the date are withheld.",
        "",
        "## Gold tables",
        "",
        "| Table | Rows | Data files | MB |",
        "|---|---|---|---|",
        *[
            f"| gold.{t} | {record['rows'][t]:,} | {s['data_files']:,} | {s['bytes'] / 1e6:,.1f} |"
            for t, s in record["storage"].items()
        ],
        f"| total | | | {total_bytes / 1e6:,.1f} |",
        "",
        "## Cost",
        "",
        "Measured on the build machine; varies run to run. Each dbt run is its own process.",
        "",
        "| Step | Value |",
        "|---|---|",
        f"| UTC days built | {b['utc_days_built']} |",
        f"| dbt runs | {b['dbt_runs']} |",
        f"| dbt runs rerun after a wall-clock step | {b['dbt_runs_rerun_after_clock_step']} |",
        f"| seconds, days | {b['seconds_days']:,} |",
        f"| seconds, revision reprocessing | {b['seconds_reprocess']:,} |",
        f"| seconds, build total | {b['seconds_total']:,} |",
        f"| seconds, model check | {record['model_check_seconds']:,} |",
        f"| peak RSS, Python (MB) | {record['peak_rss']['python_mb']:,} |",
        f"| peak RSS, largest dbt run (MB) | {record['peak_rss']['largest_dbt_run_mb']:,} |",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Build gold, then write the records, reports and figures.

    Args:
        argv: Command-line arguments, or None for sys.argv.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse", required=True, help="warehouse with silver built")
    args = parser.parse_args(argv)
    RESULTS.mkdir(exist_ok=True)
    CATALOG_JSON.write_text(json.dumps(catalog_record(), indent=2) + "\n")
    CATALOG_MD.write_text(render_catalog(json.loads(CATALOG_JSON.read_text())) + "\n")
    RECORD_JSON.write_text(json.dumps(build(args.warehouse), indent=2) + "\n")
    RECORD_MD.write_text(render_markdown(json.loads(RECORD_JSON.read_text())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
