"""Network model report (rules M1-M5, M7): coverage, load and counters.

Builds the demo network, runs its 12 weeks of 15-minute counters in memory
and writes results/model.json (the one record), results/model.md rendered
from it, and five figures. Run: `uv run python -m ran_lakehouse.model.report`.
"""

import json
import time
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm

from ran_lakehouse.model import NetworkModel, default_model, simulate_days
from ran_lakehouse.model.erlang import erlang_b
from ran_lakehouse.model.serving import TA_BIN_EDGES_STEPS
from ran_lakehouse.world import build_world
from ran_lakehouse.world.network import AREA_CLASSES

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "model.json"
RECORD_MD = RESULTS / "model.md"
FIG_SERVER = RESULTS / "model_best_server.png"
FIG_SINR = RESULTS / "model_sinr.png"
FIG_DIURNAL = RESULTS / "model_diurnal.png"
FIG_PRB_THP = RESULTS / "model_prb_throughput.png"
FIG_BLOCKING = RESULTS / "model_gsm_blocking.png"

DEMO_WEEKS = 12  # rule W7
TINY_DAYS = 2  # rule W7
SAMPLE_DAYS = 7  # the first week is kept whole for scatter plots and checks
MAP_BAND = "B3"  # the layer on every LTE site

# Reference palette (light surface).
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")
SEQUENTIAL_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
DIVERGING = ["#e34948", "#f0efec", "#2a78d6"]

LTE_PAIRS = (
    ("RRC.ConnEstabAtt.sum", "RRC.ConnEstabSucc.sum"),
    ("S1SIG.ConnEstabAtt", "S1SIG.ConnEstabSucc"),
    ("ERAB.EstabInitAttNbr.sum", "ERAB.EstabInitSuccNbr.sum"),
    ("HO.IntraFreqOutAtt", "HO.IntraFreqOutSucc"),
)
GSM_PAIRS = (
    ("attTCHSeizures", "succTCHSeizures"),
    ("attImmediateAssingProcs", "succImmediateAssingProcs"),
    ("attOutgoingInternalInterCellHDOs", "succOutgoingInternalInterCellHDOs"),
)


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman rank correlation (average ranks are not used for ties).

    Args:
        a: First sample.
        b: Second sample, same length.

    Returns:
        The rank correlation.
    """
    ra = np.argsort(np.argsort(a, kind="stable"), kind="stable").astype(float)
    rb = np.argsort(np.argsort(b, kind="stable"), kind="stable").astype(float)
    return float(np.corrcoef(ra, rb)[0, 1])


def coverage_summary(model: NetworkModel) -> dict[str, Any]:
    """Coverage and quality per layer.

    Args:
        model: The network model.

    Returns:
        Band to statistics.
    """
    persons = model.grid.persons
    out = {}
    for band, layer in sorted(model.coverage.items()):
        served = layer.best >= 0
        out[band] = {
            "technology": layer.technology,
            "cells": int((model.state.band == band).sum()),
            "population_covered_share": round(float(persons[served].sum() / persons.sum()), 3),
            "level_dbm_p10_p50_p90": [
                round(float(v), 1)
                for v in np.percentile(layer.best_level_dbm[served], [10, 50, 90])
            ],
            "sinr_db_p10_p50_p90": [
                round(float(v), 1) for v in np.percentile(layer.sinr_db[served], [10, 50, 90])
            ],
        }
    return out


class Accumulator:
    """Streams 12 weeks of counters into totals, curves and invariant counts."""

    def __init__(self, model: NetworkModel) -> None:
        """Start empty totals.

        Args:
            model: The network model.
        """
        self.model = model
        self.lte_totals: dict[str, float] = {}
        self.gsm_totals: dict[str, float] = {}
        self.violations: dict[str, int] = {}
        self.prb_sum = np.zeros((len(AREA_CLASSES), 7 * 96))
        self.prb_count = np.zeros((len(AREA_CLASSES), 7 * 96))
        self.cell_periods = 0
        self.periods = 0

    def add(self, lte: dict[str, np.ndarray], gsm: dict[str, np.ndarray], day: int) -> None:
        """Add one day.

        Args:
            lte: LTE counters of the day.
            gsm: GSM counters of the day.
            day: Day index.
        """
        for name, value in lte.items():
            self.lte_totals[name] = self.lte_totals.get(name, 0.0) + float(value.sum())
        for name, value in gsm.items():
            self.gsm_totals[name] = self.gsm_totals.get(name, 0.0) + float(value.sum())
        for att, succ in LTE_PAIRS:
            self.count(f"{succ} > {att}", int((lte[succ] > lte[att]).sum()))
        for att, succ in GSM_PAIRS:
            self.count(f"{succ} > {att}", int((gsm[succ] > gsm[att]).sum()))
        prb = lte["RRU.PrbTotDl"]
        self.count("RRU.PrbTotDl outside 0-100", int(((prb < 0) | (prb > 100)).sum()))
        negative = sum(int((v < 0).sum()) for v in (*lte.values(), *gsm.values()))
        self.count("negative counter values", negative)
        timeless = (lte["DRB.IPVolDl.sum"] > 0) & (lte["DRB.IPTimeDl.sum"] <= 0)
        self.count("DRB.IPVolDl > 0 with DRB.IPTimeDl = 0", int(timeless.sum()))
        state = self.model.state
        lte_cells = np.flatnonzero(state.technology == "LTE")
        offset = (day % 7) * 96
        for k, area in enumerate(AREA_CLASSES):
            cols = state.area_class[lte_cells] == area
            self.prb_sum[k, offset : offset + 96] += prb[:, cols].sum(axis=1)
            self.prb_count[k, offset : offset + 96] += cols.sum()
        self.cell_periods += prb.size + gsm["attTCHSeizures"].size
        self.periods += prb.shape[0]

    def count(self, name: str, n: int) -> None:
        """Add to a violation count.

        Args:
            name: Invariant name.
            n: Violations found.
        """
        self.violations[name] = self.violations.get(name, 0) + n


def kpis(acc: Accumulator) -> dict[str, float]:
    """Network-level KPIs over the whole run (ratio of sums, rule L3).

    Args:
        acc: The totals.

    Returns:
        KPI name to value.
    """
    t = acc.lte_totals
    g = acc.gsm_totals
    n_lte = int((acc.model.state.technology == "LTE").sum())
    rrc = t["RRC.ConnEstabSucc.sum"] / t["RRC.ConnEstabAtt.sum"]
    s1 = t["S1SIG.ConnEstabSucc"] / t["S1SIG.ConnEstabAtt"]
    erab = t["ERAB.EstabInitSuccNbr.sum"] / t["ERAB.EstabInitAttNbr.sum"]
    period_time = acc.periods * 900.0 * n_lte
    blocked = g["attTCHSeizuresMeetingTCHBlockedState"]
    abnormal = (
        g["nbrOfLostRadioLinksTCH"]
        + g["unsuccHDOsWithReconnection"]
        + g["unsuccHDOsWithLossOfConnection"]
    )
    return {
        "LTE E-RAB accessibility, TS 32.450 6.1.1 (%)": round(100 * rrc * s1 * erab, 3),
        "LTE RRC setup success rate, operator-defined (%)": round(100 * rrc, 3),
        "LTE E-RAB retainability R2, TS 32.450 6.2.1 (releases per session hour)": round(
            3600 * t["ERAB.RelActNbr.sum"] / t["ERAB.SessionTimeUE"], 4
        ),
        "LTE E-RAB drop rate, operator-defined (%)": round(
            100 * t["ERAB.RelActNbr.sum"] / t["ERAB.EstabInitSuccNbr.sum"], 3
        ),
        "LTE DL IP throughput, TS 32.450 6.3.1 (kbit/s)": round(
            1000 * t["DRB.IPVolDl.sum"] / t["DRB.IPTimeDl.sum"], 1
        ),
        "LTE cell availability, TS 32.450 6.4.1 (%)": round(
            100 * (period_time - t["RRU.CellUnavailableTime.sum"]) / period_time, 3
        ),
        "LTE intra-frequency handover success, HO.IntraFreqOut* (%)": round(
            100 * t["HO.IntraFreqOutSucc"] / t["HO.IntraFreqOutAtt"], 3
        ),
        "LTE mean DL PRB use, operator-defined (%)": round(
            t["RRU.PrbTotDl"] / (acc.periods * n_lte), 2
        ),
        "GSM service access success, TS 32.410 7.4 (%)": round(
            100
            * g["succTCHSeizures"]
            / g["attTCHSeizures"]
            * g["succImmediateAssingProcs"]
            / g["attImmediateAssingProcs"],
            3,
        ),
        "GSM TCH blocking, vendor-style (%)": round(
            100 * blocked / (blocked + g["attTCHSeizures"]), 3
        ),
        "GSM handover success per cell, TS 32.410 9.5 (%)": round(
            100 * g["succOutgoingInternalInterCellHDOs"] / g["attOutgoingInternalInterCellHDOs"],
            3,
        ),
        "GSM abnormal release rate, TS 32.410 8.2 without intra-cell terms (%)": round(
            100 * abnormal / (g["succTCHSeizures"] + g["succIncomingInternalInterCellHDOs"]), 3
        ),
    }


def relationship_checks(model: NetworkModel, sample: list[Any]) -> list[dict[str, Any]]:
    """Checks that counters move the way the model says they must.

    Args:
        model: The network model.
        sample: The first week's days.

    Returns:
        One entry per check: name, statistic, value, expectation, pass.
    """
    lte = {k: np.concatenate([d.lte.values[k] for d in sample]) for k in sample[0].lte.values}
    gsm = {k: np.concatenate([d.gsm.values[k] for d in sample]) for k in sample[0].gsm.values}
    state = model.state
    lte_cells = sample[0].lte.cells
    serving = model.serving

    busy = lte["DRB.IPVolDl.sum"] > 0
    thp = lte["DRB.IPVolDl.sum"][busy] / lte["DRB.IPTimeDl.sum"][busy] * 1000.0
    rho_thp = spearman(lte["RRU.PrbTotDl"][busy], thp)

    def rate(succ: str, att: str, mask: np.ndarray) -> float:
        return float(lte[succ][mask].sum() / max(lte[att][mask].sum(), 1.0))

    prb = lte["RRU.PrbTotDl"]
    rrc_low = rate("RRC.ConnEstabSucc.sum", "RRC.ConnEstabAtt.sum", prb < 80)
    rrc_high = rate("RRC.ConnEstabSucc.sum", "RRC.ConnEstabAtt.sum", prb >= 95)

    drop = lte["ERAB.RelActNbr.sum"].sum(0) / np.maximum(lte["ERAB.EstabInitSuccNbr.sum"].sum(0), 1)
    rho_drop = spearman(serving.edge_share[lte_cells], drop)

    cqi = lte["CARR.WBCQIDist.Bin"].sum(axis=0)
    mean_cqi = (cqi * np.arange(16)).sum(axis=1) / np.maximum(cqi.sum(axis=1), 1)
    rho_cqi = spearman(serving.mean_sinr_db[lte_cells], mean_cqi)

    centres = (np.array(TA_BIN_EDGES_STEPS[:-1]) + np.array(TA_BIN_EDGES_STEPS[1:])) / 2.0
    centres[-1] = TA_BIN_EDGES_STEPS[-2]
    mean_ta = (serving.ta_share[lte_cells] * centres).sum(axis=1)
    ta_by_class = [
        float(np.median(mean_ta[state.area_class[lte_cells] == a])) for a in AREA_CLASSES
    ]

    rho_conn = spearman(lte["RRC.ConnMean"].ravel(), prb.ravel())

    occupancy = gsm["meanNbrOfBusyTCHs"] / np.maximum(gsm["nbrOfAvailableTCHs"], 1)
    blocked = gsm["attTCHSeizuresMeetingTCHBlockedState"]
    calls = gsm["attTCHSeizures"] + blocked
    high = occupancy >= 0.8
    low = occupancy < 0.5
    block_high = float(blocked[high].sum() / max(calls[high].sum(), 1.0))
    block_low = float(blocked[low].sum() / max(calls[low].sum(), 1.0))

    return [
        check("IP throughput falls as PRB use rises", "Spearman", rho_thp, "< 0", rho_thp < 0),
        check(
            "RRC setup success lower at PRB >= 95% than below 80%",
            "success rate below 80% minus at >= 95%",
            rrc_low - rrc_high,
            "> 0",
            rrc_low > rrc_high,
        ),
        check(
            "E-RAB drop rate rises with cell-edge share", "Spearman", rho_drop, "> 0", rho_drop > 0
        ),
        check("Mean CQI rises with mean SINR", "Spearman", rho_cqi, "> 0", rho_cqi > 0),
        check(
            "Median TA distance grows urban < suburban < rural",
            "median mean TA step per class",
            ta_by_class,
            "increasing",
            ta_by_class[0] < ta_by_class[1] < ta_by_class[2],
        ),
        check("PRB use rises with connected users", "Spearman", rho_conn, "> 0", rho_conn > 0),
        check(
            "TCH blocking higher at TCH occupancy >= 0.8 than below 0.5",
            "blocking share at >= 0.8 minus below 0.5",
            block_high - block_low,
            "> 0",
            block_high > block_low,
        ),
    ]


def check(name: str, statistic: str, value: Any, expected: str, passed: Any) -> dict[str, Any]:
    """One relationship check.

    Args:
        name: What must hold.
        statistic: How it is measured.
        value: The measured value.
        expected: The expectation.
        passed: Whether it held.

    Returns:
        The check, JSON-ready.
    """
    shown = [round(v, 3) for v in value] if isinstance(value, list) else round(float(value), 4)
    return {
        "check": name,
        "statistic": statistic,
        "value": shown,
        "expected": expected,
        "pass": bool(passed),
    }


def build() -> tuple[dict[str, Any], NetworkModel, list[Any], Accumulator]:
    """Run the demo and tiny profiles and build the record.

    Returns:
        The record, the demo model, the first week's days and the totals.
    """
    t0 = time.perf_counter()
    demo = default_model(build_world("demo"))
    t_build = time.perf_counter() - t0
    acc = Accumulator(demo)
    sample: list[Any] = []
    t0 = time.perf_counter()
    for day in simulate_days(demo, 0, 7 * DEMO_WEEKS):
        acc.add(day.lte.values, day.gsm.values, day.index)
        if day.index < SAMPLE_DAYS:
            sample.append(day)
    t_run = time.perf_counter() - t0
    t0 = time.perf_counter()
    tiny = default_model(build_world("tiny"))
    tiny_days = list(simulate_days(tiny, 0, TINY_DAYS))
    t_tiny = time.perf_counter() - t0
    state = demo.state
    record = {
        "demo": {
            "cells": {
                "LTE": int((state.technology == "LTE").sum()),
                "GSM": int((state.technology == "GSM").sum()),
            },
            "weeks": DEMO_WEEKS,
            "cell_periods": acc.cell_periods,
            "gsm_trx": {
                str(k): int(v)
                for k, v in zip(
                    *np.unique(state.trx[state.technology == "GSM"], return_counts=True),
                    strict=True,
                )
            },
            "unserved_persons": round(demo.serving.unserved_persons),
            "neighbour_relations": int(demo.serving.relations[0].size),
            "coverage": coverage_summary(demo),
            "kpis": kpis(acc),
            "invariant_violations": acc.violations,
            "relationship_checks": relationship_checks(demo, sample),
        },
        "tiny": {
            "cells": len(tiny.state.cell_names),
            "days": TINY_DAYS,
            "rrc_attempts": int(sum(d.lte.values["RRC.ConnEstabAtt.sum"].sum() for d in tiny_days)),
        },
        "timing_s": {
            "demo model build (coverage and serving)": round(t_build, 1),
            f"demo {DEMO_WEEKS} weeks of counters": round(t_run, 1),
            f"tiny build and {TINY_DAYS} days": round(t_tiny, 1),
        },
    }
    return record, demo, sample, acc


def raster(model: NetworkModel, values: np.ndarray) -> np.ndarray:
    """Place per-point values back on the served-region raster.

    Args:
        model: The network model.
        values: One value per grid point.

    Returns:
        2D array, row 0 at the south edge.
    """
    rows, cols = model.grid.raster_index
    out = np.full((rows.max() + 1, cols.max() + 1), np.nan)
    out[rows, cols] = values
    return out


def style(ax: Any, title: str) -> None:
    """Shared axes style.

    Args:
        ax: Matplotlib axes.
        title: Title.
    """
    ax.set_title(title, color=INK)
    ax.tick_params(colors=INK_SECONDARY)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def plot_server(model: NetworkModel, path: Path) -> None:
    """Best-server RSRP of the map layer with server boundaries.

    Args:
        model: The network model.
        path: PNG path.
    """
    layer = model.coverage[MAP_BAND]
    level = raster(model, layer.best_level_dbm)
    best = raster(model, layer.best.astype(float))
    edge = np.zeros(best.shape, dtype=bool)
    edge[:, 1:] |= best[:, 1:] != best[:, :-1]
    edge[1:, :] |= best[1:, :] != best[:-1, :]
    extent = (0, model.world.profile.served_width_km, 0, model.world.profile.height_km)
    fig, ax = plt.subplots(figsize=(7.5, 6.3), dpi=100, facecolor=SURFACE)
    cmap = LinearSegmentedColormap.from_list("seq", SEQUENTIAL_BLUE)
    image = ax.imshow(level, origin="lower", extent=extent, cmap=cmap, vmin=-120, vmax=-60)
    ax.imshow(
        np.where(edge, 1.0, np.nan),
        origin="lower",
        extent=extent,
        cmap=LinearSegmentedColormap.from_list("ink", [INK, INK]),
        interpolation="nearest",
        alpha=0.3,
    )
    bar = fig.colorbar(image, ax=ax, shrink=0.8)
    bar.set_label("best-server RSRP, dBm", color=INK_SECONDARY)
    ax.set_xlabel("x, km", color=INK_SECONDARY)
    ax.set_ylabel("y, km", color=INK_SECONDARY)
    style(ax, f"{MAP_BAND} best-server RSRP and server boundaries (synthetic)")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def plot_sinr(model: NetworkModel, path: Path) -> None:
    """SINR of the map layer at the reference load.

    Args:
        model: The network model.
        path: PNG path.
    """
    sinr = raster(model, np.clip(model.coverage[MAP_BAND].sinr_db, -10, 25))
    extent = (0, model.world.profile.served_width_km, 0, model.world.profile.height_km)
    fig, ax = plt.subplots(figsize=(7.5, 6.3), dpi=100, facecolor=SURFACE)
    cmap = LinearSegmentedColormap.from_list("div", DIVERGING)
    image = ax.imshow(
        sinr, origin="lower", extent=extent, cmap=cmap, norm=TwoSlopeNorm(0.0, -10.0, 25.0)
    )
    bar = fig.colorbar(image, ax=ax, shrink=0.8)
    bar.set_label("SINR, dB (clipped to -10..25)", color=INK_SECONDARY)
    ax.set_xlabel("x, km", color=INK_SECONDARY)
    ax.set_ylabel("y, km", color=INK_SECONDARY)
    style(ax, f"{MAP_BAND} SINR at 50% reference load (synthetic)")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def plot_diurnal(acc: Accumulator, path: Path) -> None:
    """Mean PRB use by hour of week and area class.

    Args:
        acc: The totals.
        path: PNG path.
    """
    hours = np.arange(7 * 96) / 4.0
    fig, ax = plt.subplots(figsize=(10, 3.6), dpi=100, facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    for k, area in enumerate(AREA_CLASSES):
        mean = acc.prb_sum[k] / np.maximum(acc.prb_count[k], 1)
        ax.plot(hours, mean, color=SERIES[k], lw=2, label=area)
    ax.set_xticks(np.arange(0, 7 * 24 + 1, 24))
    ax.set_xticklabels(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", ""])
    ax.set_ylabel("mean DL PRB use, %", color=INK_SECONDARY)
    ax.grid(axis="y", color="#e6e5e0", lw=0.8)
    ax.legend(frameon=False, loc="upper left")
    style(ax, "Load by hour of week, 12-week mean (synthetic, demo profile)")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def plot_prb_throughput(sample: list[Any], path: Path) -> None:
    """IP throughput against PRB use, one week of cell-periods.

    Args:
        sample: The first week's days.
        path: PNG path.
    """
    vol = np.concatenate([d.lte.values["DRB.IPVolDl.sum"] for d in sample]).ravel()
    tim = np.concatenate([d.lte.values["DRB.IPTimeDl.sum"] for d in sample]).ravel()
    prb = np.concatenate([d.lte.values["RRU.PrbTotDl"] for d in sample]).ravel()
    busy = vol > 0
    thp = vol[busy] / tim[busy] * 1000.0 / 1000.0
    fig, ax = plt.subplots(figsize=(7, 4.6), dpi=100, facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    cmap = LinearSegmentedColormap.from_list("seq", SEQUENTIAL_BLUE)
    hb = ax.hexbin(prb[busy], thp, gridsize=40, cmap=cmap, bins="log", mincnt=1, linewidths=0)
    bar = fig.colorbar(hb, ax=ax)
    bar.set_label("cell-periods (log)", color=INK_SECONDARY)
    ax.set_xlabel("RRU.PrbTotDl, %", color=INK_SECONDARY)
    ax.set_ylabel("DL IP throughput, Mbit/s", color=INK_SECONDARY)
    style(ax, "IP throughput against PRB use, one week (synthetic)")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def plot_blocking(sample: list[Any], path: Path) -> None:
    """GSM TCH blocking against offered traffic, with Erlang B curves.

    Args:
        sample: The first week's days.
        path: PNG path.
    """
    g = {k: np.concatenate([d.gsm.values[k] for d in sample]).ravel() for k in sample[0].gsm.values}
    calls = g["attTCHSeizures"] + g["attTCHSeizuresMeetingTCHBlockedState"]
    active = calls > 0
    n_tch = g["nbrOfAvailableTCHs"][active]
    blocking = g["attTCHSeizuresMeetingTCHBlockedState"][active] / calls[active]
    # Offered traffic estimated from carried traffic and the measured
    # blocking: A = carried / (1 - B).
    offered = g["meanNbrOfBusyTCHs"][active] / np.maximum(1.0 - blocking, 0.05)
    common = [
        int(v)
        for v, _ in sorted(
            zip(*np.unique(n_tch, return_counts=True), strict=True), key=lambda p: -p[1]
        )[:3]
    ]
    common.sort()
    fig, ax = plt.subplots(figsize=(7, 4.6), dpi=100, facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    for k, n in enumerate(common):
        pick = n_tch == n
        ax.scatter(offered[pick], blocking[pick], s=8, color=SERIES[k], alpha=0.35, linewidths=0)
        a = np.linspace(0, max(float(offered[pick].max()), 1.0), 200)
        ax.plot(
            a, erlang_b(a, np.full(a.shape, n)), color=SERIES[k], lw=2, label=f"{n} TCH, Erlang B"
        )
    ax.set_xlabel("offered traffic, Erlang", color=INK_SECONDARY)
    ax.set_ylabel("TCH blocking share", color=INK_SECONDARY)
    ax.legend(frameon=False, loc="upper left")
    style(ax, "GSM blocking against offered traffic (synthetic)")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def render_markdown(record: dict[str, Any]) -> str:
    """Render the report from the record.

    Args:
        record: The record as loaded from JSON.

    Returns:
        The Markdown report.
    """
    demo = record["demo"]
    lines = [
        "# Network model report",
        "",
        "Synthetic network (rules M1-M5): coverage, load and 15-minute counters of the demo",
        "profile, generated in memory. Stated simplifications (rule M7): no terrain in the served",
        "region, no scheduler, fading or mobility traces; interference at a 50% reference load.",
        "Written by `python -m ran_lakehouse.model.report` from `model.json`.",
        "",
        f"Demo: {demo['cells']['LTE']} LTE and {demo['cells']['GSM']} GSM cells, "
        f"{demo['weeks']} weeks, {demo['cell_periods']:,} cell-periods; "
        f"{demo['neighbour_relations']} neighbour relations; "
        f"{demo['unserved_persons']} persons without LTE coverage.",
        "",
        "![Best-server RSRP](model_best_server.png)",
        "",
        "![SINR](model_sinr.png)",
        "",
        "![Load by hour of week](model_diurnal.png)",
        "",
        "![IP throughput against PRB use](model_prb_throughput.png)",
        "",
        "![GSM blocking against offered traffic](model_gsm_blocking.png)",
        "",
        "The blocking figure shows the three commonest TCH counts, one week of cell-periods.",
        "",
        "## Coverage per layer",
        "",
        "| Band | Cells | Population covered | Level p10 / p50 / p90, dBm "
        "| SINR p10 / p50 / p90, dB |",
        "|---|---|---|---|---|",
        *[
            f"| {b} | {c['cells']} | {c['population_covered_share']} | "
            f"{' / '.join(str(v) for v in c['level_dbm_p10_p50_p90'])} | "
            f"{' / '.join(str(v) for v in c['sinr_db_p10_p50_p90'])} |"
            for b, c in demo["coverage"].items()
        ],
        "",
        "Level is RSRP for LTE and RxLev for GSM; GSM SINR is carrier to interference plus noise.",
        "",
        "## KPIs over the 12 weeks (ratio of sums)",
        "",
        "| KPI | Value |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in demo["kpis"].items()],
        "",
        "## Counter invariants over every cell-period",
        "",
        "| Invariant broken | Cell-periods |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in demo["invariant_violations"].items()],
        "",
        "## Counter relationships, first week",
        "",
        "| Check | Statistic | Value | Expected | Result |",
        "|---|---|---|---|---|",
        *[
            f"| {c['check']} | {c['statistic']} | {c['value']} | {c['expected']} | "
            f"{'pass' if c['pass'] else 'FAIL'} |"
            for c in demo["relationship_checks"]
        ],
        "",
        "## GSM transceivers",
        "",
        "Dimensioned per cell for 2% blocking at the busy hour (Erlang B), 1 to 12 TRX:",
        "",
        "| TRX | Cells |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in demo["gsm_trx"].items()],
        "",
        "## Timing",
        "",
        "Measured on the build machine; varies run to run.",
        "",
        "| Step | Seconds |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in record["timing_s"].items()],
        "",
        f"Tiny profile: {record['tiny']['cells']} cells, {record['tiny']['days']} days, "
        f"{record['tiny']['rrc_attempts']} RRC setup attempts.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    """Write the record, the Markdown report and the figures.

    Returns:
        The process exit code.
    """
    RESULTS.mkdir(exist_ok=True)
    record, demo, sample, acc = build()
    RECORD_JSON.write_text(json.dumps(record, indent=2) + "\n")
    RECORD_MD.write_text(render_markdown(json.loads(RECORD_JSON.read_text())))
    plot_server(demo, FIG_SERVER)
    plot_sinr(demo, FIG_SINR)
    plot_diurnal(acc, FIG_DIURNAL)
    plot_prb_throughput(sample, FIG_PRB_THP)
    plot_blocking(sample, FIG_BLOCKING)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
