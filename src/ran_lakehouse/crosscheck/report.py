"""Real-data cross-check (rule E2): shapes of the synthetic network against
live counters.

The real data: "Performance Management Counters from Live 5G, 4G and 2G
Radio Access Network" (Zenodo 10.5281/zenodo.17815388, CC BY 4.0), LTE 1800
and GSM 900 of Dataset_03, fetched by `ran_lakehouse.crosscheck.fetch` and
never committed. Only aggregated statistics and figures derived from it are
committed, with attribution.

What is compared, for the shapes only (absolute levels differ by design and
the real units are not given in the record's README):
- diurnal load: LTE RB utilization and GSM timeslot utilization by hour of
  day, each divided by its own daily mean; the real timestamps are UTC and
  the record does not state the local zone, so the real curve is shifted by
  the whole number of hours that best aligns it, and the shift is reported;
- data volume per connected user against PRB use: the real set has no
  active-time counter, so throughput itself cannot be formed from it; both
  sides use the same proxy, DL data volume per RRC user per 15-minute period,
  binned by PRB use and divided by its median;
- the spread of utilization over cells and periods (percentiles).
The real set carries no setup or success counters, so the success-rate
spread the spec names cannot be compared; the report says so.

Synthetic side: the clean demo network (no planted fault), days 0 to 83,
the same counters the lake holds (gold agrees with the model, rule E).

    uv run python -m ran_lakehouse.crosscheck.report
"""

import json
import resource
import time
from itertools import pairwise
from pathlib import Path
from typing import Any

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from ran_lakehouse.crosscheck.fetch import DEST, DOI, MEMBERS
from ran_lakehouse.model import default_model, simulate_days
from ran_lakehouse.world import build_world

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "real_data_crosscheck.json"
RECORD_MD = RESULTS / "real_data_crosscheck.md"
FIG_DIURNAL = RESULTS / "crosscheck_diurnal.png"
FIG_RATE = RESULTS / "crosscheck_rate_vs_prb.png"
FIG_SPREAD = RESULTS / "crosscheck_spread.png"

PROFILE = "demo"
DAYS = 84
PERIODS_PER_HOUR = 4
# Rows with fewer connected users carry too little traffic to form a rate
# (ASSUMPTION, the same on both sides).
MIN_USERS = 1.0
PRB_BINS = np.arange(0, 101, 10)
SPEARMAN_SAMPLE = 200_000
SAMPLE_SEED = 20261002
PERCENTILES = (10, 25, 50, 75, 90, 99)
CITATION = (
    "Peter Lehoczký, Matúš Turcsány, Laura Krajčovičová, Filip Zatroch, Marcel Kajan and "
    "Marek Galinski. Performance Management Counters from Live 5G, 4G and 2G Radio Access "
    "Network [Dataset]. Zenodo. https://doi.org/10.5281/zenodo.17815388 (2026). CC BY 4.0."
)
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
SYNTHETIC_COLOR = "#2a78d6"
REAL_COLOR = "#eb6834"

LTE_CSV = DEST / MEMBERS[0]
GSM_CSV = DEST / MEMBERS[1]


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman rank correlation (ties get the mean rank).

    Args:
        x: Values.
        y: Values.

    Returns:
        The correlation.
    """

    def ranks(v: np.ndarray) -> np.ndarray:
        order = np.argsort(v, kind="stable")
        r = np.empty(len(v))
        r[order] = np.arange(len(v), dtype=float)
        _, inverse, counts = np.unique(v, return_inverse=True, return_counts=True)
        sums = np.bincount(inverse, r)
        return np.asarray(sums[inverse] / counts[inverse])

    return float(np.corrcoef(ranks(x), ranks(y))[0, 1])


def best_shift(real: np.ndarray, synthetic: np.ndarray) -> tuple[int, float]:
    """The whole-hour shift of the real curve that best matches the synthetic one.

    Args:
        real: 24 hourly values, real (UTC hours).
        synthetic: 24 hourly values, synthetic (local hours).

    Returns:
        (shift s, Pearson r at s): real hour h aligns with synthetic hour
        (h + s) mod 24.
    """
    scores = [float(np.corrcoef(np.roll(real, s), synthetic)[0, 1]) for s in range(24)]
    s = int(np.argmax(scores))
    return s, scores[s]


def binned_medians(prb: np.ndarray, value: np.ndarray) -> list[float | None]:
    """Median of a value per PRB-use bin, divided by its overall median.

    Args:
        prb: PRB use, percent.
        value: The value.

    Returns:
        One entry per bin of PRB_BINS; None for an empty bin.
    """
    overall = float(np.median(value))
    out: list[float | None] = []
    for lo, hi in pairwise(PRB_BINS):
        inside = value[(prb >= lo) & (prb < hi if hi < 100 else prb <= hi)]
        out.append(round(float(np.median(inside)) / overall, 4) if len(inside) else None)
    return out


def percentiles(values: np.ndarray) -> dict[str, float]:
    """Percentiles of a sample.

    Args:
        values: Values.

    Returns:
        "p10" and so on to "p99".
    """
    return {f"p{p}": round(float(np.percentile(values, p)), 2) for p in PERCENTILES}


def hourly_shape(hours: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Mean by hour of day divided by the daily mean.

    Args:
        hours: Hour of day per row.
        values: Value per row.

    Returns:
        24 values.
    """
    sums = np.bincount(hours, values, 24)
    counts = np.bincount(hours, minlength=24)
    mean = sums / np.maximum(counts, 1)
    shape: np.ndarray = mean / mean.mean()
    return shape


def real_side(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Statistics of the real data.

    Args:
        con: DuckDB.

    Returns:
        Hourly shapes, the rate proxy against PRB use, spreads and sizes.
    """
    lte = f"read_csv('{LTE_CSV}')"
    gsm = f"read_csv('{GSM_CSV}')"
    lte_rows = con.execute(
        f"""SELECT hour(Timestamp) AS h, "4G RB utilization" AS prb,
            "4G data volume DL" AS volume, "4G RRC users" AS users
        FROM {lte} WHERE "4G RB utilization" IS NOT NULL"""
    ).fetchnumpy()
    hours = lte_rows["h"].astype(int)
    prb = lte_rows["prb"].astype(float)
    volume = lte_rows["volume"].astype(float)
    users = lte_rows["users"].astype(float)
    gsm_rows = con.execute(
        f"""SELECT hour(Timestamp) AS h, "2G TS utilization" AS util FROM {gsm}
        WHERE "2G TS utilization" IS NOT NULL"""
    ).fetchnumpy()
    gsm_hours = gsm_rows["h"].astype(int)
    gsm_util = gsm_rows["util"].astype(float)
    sizes = con.execute(
        f"""SELECT (SELECT count(*) FROM {lte}), (SELECT count(DISTINCT "Base station" || '/' ||
        Sector) FROM {lte}), (SELECT min(Timestamp) FROM {lte}), (SELECT max(Timestamp) FROM {lte}),
        (SELECT count(*) FROM {gsm}), (SELECT count(DISTINCT "Base station" || '/' || Sector)
        FROM {gsm})"""
    ).fetchone()
    if sizes is None:
        raise RuntimeError("the real files returned no rows")
    return {
        "lte_rows": int(sizes[0]),
        "lte_sectors": int(sizes[1]),
        "first": str(sizes[2]),
        "last": str(sizes[3]),
        "gsm_rows": int(sizes[4]),
        "gsm_sectors": int(sizes[5]),
        "lte_prb_shape": hourly_shape(hours, prb),
        "gsm_util_shape": hourly_shape(gsm_hours, gsm_util),
        "rate": rate_stats(prb, volume, users),
        "lte_prb_spread": percentiles(prb),
        "gsm_util_spread": percentiles(gsm_util),
        "rows_with_rb_utilization": len(prb),
    }


def rate_stats(prb: np.ndarray, volume: np.ndarray, users: np.ndarray) -> dict[str, Any]:
    """DL volume per connected user against PRB use.

    Args:
        prb: PRB use, percent.
        volume: DL volume per period.
        users: Mean connected (RRC) users per period.

    Returns:
        Binned medians (relative), the Spearman correlation on a sample,
        and the rows used.
    """
    keep = (users >= MIN_USERS) & np.isfinite(volume) & np.isfinite(prb)
    rate = volume[keep] / users[keep]
    p = prb[keep]
    rng = np.random.default_rng(SAMPLE_SEED)
    pick = rng.choice(len(p), min(SPEARMAN_SAMPLE, len(p)), replace=False)
    return {
        "binned_relative_median": binned_medians(p, rate),
        "spearman": round(spearman(p[pick], rate[pick]), 4),
        "rows": int(keep.sum()),
    }


def synthetic_side() -> dict[str, Any]:
    """Statistics of the clean demo network over DAYS days.

    Returns:
        The same statistics as real_side, plus the model's own throughput
        against PRB use for reference.
    """
    model = default_model(build_world(PROFILE))
    hours_l, prb_l, vol_l, users_l, thr_l = [], [], [], [], []
    gsm_hours, gsm_util = [], []
    for day in simulate_days(model, 0, DAYS):
        v = day.lte.values
        periods, cells = v["RRU.PrbTotDl"].shape
        hour = np.repeat(np.arange(periods) // PERIODS_PER_HOUR, cells)
        hours_l.append(hour)
        prb_l.append(v["RRU.PrbTotDl"].ravel())
        vol_l.append(v["DRB.IPVolDl.sum"].ravel())
        users_l.append(v["RRC.ConnMean"].ravel())
        time_ms = v["DRB.IPTimeDl.sum"].ravel()
        thr_l.append(
            np.where(time_ms > 0, v["DRB.IPVolDl.sum"].ravel() / np.maximum(time_ms, 1e-9), np.nan)
        )
        g = day.gsm.values
        g_periods, g_cells = g["meanNbrOfBusyTCHs"].shape
        gsm_hours.append(np.repeat(np.arange(g_periods) // PERIODS_PER_HOUR, g_cells))
        gsm_util.append(
            (100.0 * g["meanNbrOfBusyTCHs"] / np.maximum(g["nbrOfAvailableTCHs"], 1)).ravel()
        )
    hours = np.concatenate(hours_l).astype(int)
    prb = np.concatenate(prb_l).astype(float)
    volume = np.concatenate(vol_l).astype(float)
    users = np.concatenate(users_l).astype(float)
    throughput = np.concatenate(thr_l).astype(float)
    g_hours = np.concatenate(gsm_hours).astype(int)
    g_util = np.concatenate(gsm_util).astype(float)
    served = np.isfinite(throughput)
    rng = np.random.default_rng(SAMPLE_SEED)
    pick = rng.choice(int(served.sum()), min(SPEARMAN_SAMPLE, int(served.sum())), replace=False)
    return {
        "lte_cells": int(
            len(model.state.cell_names) - int((model.state.technology == "GSM").sum())
        ),
        "gsm_cells": int((model.state.technology == "GSM").sum()),
        "lte_prb_shape": hourly_shape(hours, prb),
        "gsm_util_shape": hourly_shape(g_hours, g_util),
        "rate": rate_stats(prb, volume, users),
        "throughput_vs_prb_spearman": round(
            spearman(prb[served][pick], throughput[served][pick]), 4
        ),
        "lte_prb_spread": percentiles(prb),
        "gsm_util_spread": percentiles(g_util),
    }


def build() -> dict[str, Any]:
    """The record.

    Returns:
        Both sides' statistics, the alignment shifts and sizes.

    Raises:
        RuntimeError: If the real files are missing (run the fetch first) or empty.
    """
    for path in (LTE_CSV, GSM_CSV):
        if not path.exists():
            raise RuntimeError(f"{path} is missing; run python -m ran_lakehouse.crosscheck.fetch")
    con = duckdb.connect()
    con.execute("SET memory_limit = '1GB'")
    real = real_side(con)
    synthetic = synthetic_side()
    shifts = {}
    for key in ("lte_prb_shape", "gsm_util_shape"):
        s, r = best_shift(real[key], synthetic[key])
        shifts[key] = {"shift_h": s, "pearson_r": round(r, 4)}
        real[key] = [round(float(x), 4) for x in np.roll(real[key], s)]
        synthetic[key] = [round(float(x), 4) for x in synthetic[key]]
    return {
        "doi": DOI,
        "citation": CITATION,
        "real_files": [str(p.relative_to(DEST)) for p in (LTE_CSV, GSM_CSV)],
        "synthetic": {"profile": PROFILE, "days": DAYS, "network": "clean (no planted fault)"}
        | synthetic,
        "real": real,
        "alignment": shifts,
        "min_users": MIN_USERS,
        "prb_bins": [int(b) for b in PRB_BINS],
    }


def style(ax: Any, title: str, xlabel: str, ylabel: str) -> None:
    """House style for one panel.

    Args:
        ax: Axes.
        title: Title.
        xlabel: X label.
        ylabel: Y label.
    """
    ax.set_title(title, color=INK, fontsize=11, loc="left")
    ax.set_xlabel(xlabel, color=INK_SECONDARY)
    ax.set_ylabel(ylabel, color=INK_SECONDARY)
    ax.tick_params(colors=INK_SECONDARY)
    ax.grid(color="#e4e3df", linewidth=0.6)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def plot(record: dict[str, Any]) -> None:
    """Write the three figures.

    Args:
        record: The record.
    """
    hours = np.arange(24)
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), sharey=True)
    for ax, key, title in (
        (axes[0], "lte_prb_shape", "LTE PRB use by hour"),
        (axes[1], "gsm_util_shape", "GSM timeslot use by hour"),
    ):
        shift = record["alignment"][key]["shift_h"]
        ax.plot(hours, record["synthetic"][key], color=SYNTHETIC_COLOR, lw=2, label="synthetic")
        ax.plot(
            hours, record["real"][key], color=REAL_COLOR, lw=2, label=f"real, shifted {shift} h"
        )
        style(ax, title, "hour of day (synthetic local time)", "relative to daily mean")
        ax.legend(frameon=False)
    fig.suptitle(
        "Synthetic data against live counters (Zenodo 10.5281/zenodo.17815388)",
        x=0.01,
        ha="left",
        fontsize=10,
        color=INK_SECONDARY,
    )
    fig.tight_layout()
    fig.savefig(FIG_DIURNAL, dpi=150)
    plt.close(fig)

    centres = (PRB_BINS[:-1] + PRB_BINS[1:]) / 2
    fig, ax = plt.subplots(figsize=(6, 3.6))
    for side, color in (("synthetic", SYNTHETIC_COLOR), ("real", REAL_COLOR)):
        values = [
            np.nan if v is None else v for v in record[side]["rate"]["binned_relative_median"]
        ]
        ax.plot(centres, values, color=color, lw=2, marker="o", ms=5, label=side)
    style(ax, "DL volume per connected user against PRB use", "PRB use (%)", "median, relative")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(FIG_RATE, dpi=150)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    labels = [f"p{p}" for p in PERCENTILES]
    x = np.arange(len(labels))
    for ax, key, title in (
        (axes[0], "lte_prb_spread", "LTE PRB use over cells and periods"),
        (axes[1], "gsm_util_spread", "GSM timeslot use over cells and periods"),
    ):
        for side, color, dx in (("synthetic", SYNTHETIC_COLOR, -0.2), ("real", REAL_COLOR, 0.2)):
            ax.bar(
                x + dx, [record[side][key][k] for k in labels], width=0.38, color=color, label=side
            )
        ax.set_xticks(x, labels)
        style(ax, title, "percentile", "utilization (%)")
        ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(FIG_SPREAD, dpi=150)
    plt.close(fig)


def row(values: list[float | None]) -> str:
    """Values as table cells.

    Args:
        values: Values.

    Returns:
        Cells joined by " | ".
    """
    return " | ".join("n/a" if v is None else f"{v:g}" for v in values)


def first_last(values: list[float | None]) -> tuple[float, float]:
    """The first and last filled bins.

    Args:
        values: Binned values, None for empty bins.

    Returns:
        (first, last).
    """
    filled = [v for v in values if v is not None]
    return filled[0], filled[-1]


def findings(record: dict[str, Any]) -> list[str]:
    """What the numbers say, one bullet per comparison.

    Args:
        record: The record.

    Returns:
        Markdown bullets.
    """
    s, r, a = record["synthetic"], record["real"], record["alignment"]
    s_lo, s_hi = first_last(s["rate"]["binned_relative_median"])
    r_lo, r_hi = first_last(r["rate"]["binned_relative_median"])
    return [
        f"- Diurnal load: after the shift the shapes agree with Pearson r "
        f"{a['lte_prb_shape']['pearson_r']:g} (LTE) and {a['gsm_util_shape']['pearson_r']:g} "
        "(GSM).",
        f"- Volume per connected user: from the lowest to the highest PRB-use bin the real "
        f"median goes from {r_lo:g} to {r_hi:g} times its overall median, the synthetic from "
        f"{s_lo:g} to {s_hi:g}. In the model every connected user asks for the same demand "
        "(rule M4, ASSUMPTION), so PRB use rises only with the number of users; in the live "
        "network heavier users also drive PRB use. This is a gap of the model, stated, not "
        "changed here.",
        f"- Spread: median LTE PRB use is {s['lte_prb_spread']['p50']:g}% synthetic against "
        f"{r['lte_prb_spread']['p50']:g}% real, p99 {s['lte_prb_spread']['p99']:g}% against "
        f"{r['lte_prb_spread']['p99']:g}%; GSM timeslot use p10 is "
        f"{s['gsm_util_spread']['p10']:g}% synthetic against {r['gsm_util_spread']['p10']:g}% "
        "real (the live network shows a floor the model does not).",
    ]


def render_markdown(record: dict[str, Any]) -> str:
    """The report.

    Args:
        record: The record.

    Returns:
        Markdown.
    """
    s, r = record["synthetic"], record["real"]
    a = record["alignment"]
    bins = record["prb_bins"]
    bin_labels = [f"{lo}-{hi}" for lo, hi in pairwise(bins)]
    labels = [f"p{p}" for p in PERCENTILES]
    lines = [
        "# Real-data cross-check",
        "",
        'The synthetic network (rule E2) against live counters: "Performance Management '
        f'Counters from Live 5G, 4G and 2G Radio Access Network", Zenodo {record["doi"]}, '
        "CC BY 4.0. Written by `python -m ran_lakehouse.crosscheck.report` from "
        "`real_data_crosscheck.json`; the real data is fetched by "
        "`python -m ran_lakehouse.crosscheck.fetch` and never committed. Only aggregated "
        "statistics and figures derived from it are here.",
        "",
        f"Real: {', '.join(record['real_files'])}; LTE {r['lte_rows']:,} rows from "
        f"{r['lte_sectors']} sectors, GSM {r['gsm_rows']:,} rows from {r['gsm_sectors']} "
        f"sectors, {r['first']} to {r['last']} (UTC). Synthetic: the {s['profile']} profile, "
        f"{s['network']}, days 0 to {s['days'] - 1}, {s['lte_cells']} LTE and "
        f"{s['gsm_cells']} GSM cells.",
        "",
        "Shapes only: absolute levels differ by design (a different network, mix and "
        "market) and the real record does not give its units in its README.",
        "",
        "## Diurnal load",
        "",
        "Mean by hour of day divided by the daily mean. The real timestamps are UTC and the "
        "record does not state the local zone, so the real curve is shifted by the whole "
        "number of hours that best matches the synthetic one (WIB local time).",
        "",
        "| Series | Shift (h) | Pearson r after the shift |",
        "|---|---|---|",
        f"| LTE PRB use | {a['lte_prb_shape']['shift_h']} | {a['lte_prb_shape']['pearson_r']:g} |",
        f"| GSM timeslot use | {a['gsm_util_shape']['shift_h']} | "
        f"{a['gsm_util_shape']['pearson_r']:g} |",
        "",
        "![Diurnal load](crosscheck_diurnal.png)",
        "",
        "## Data volume per connected user against PRB use",
        "",
        "The real set has no active-time counter, so throughput (volume over active time, "
        "the KPI the lake computes) cannot be formed from it. Both sides use the same "
        "proxy instead: DL data volume per mean RRC-connected user in a 15-minute period, "
        f"over periods with at least {record['min_users']:g} connected user, as a median per "
        "PRB-use bin divided by the overall median.",
        "",
        f"| PRB use (%) | {' | '.join(bin_labels)} |",
        f"|---|{'---|' * len(bin_labels)}",
        f"| synthetic | {row(s['rate']['binned_relative_median'])} |",
        f"| real | {row(r['rate']['binned_relative_median'])} |",
        "",
        f"Spearman correlation of the proxy with PRB use: synthetic {s['rate']['spearman']:g}, "
        f"real {r['rate']['spearman']:g} (on a sample of up to {SPEARMAN_SAMPLE:,} periods each). "
        "For reference, the synthetic network's own DL IP throughput against PRB use: "
        f"{s['throughput_vs_prb_spearman']:g}.",
        "",
        "![Rate against PRB use](crosscheck_rate_vs_prb.png)",
        "",
        "## Spread of utilization",
        "",
        "Percentiles over every cell and 15-minute period.",
        "",
        f"| Series | {' | '.join(labels)} |",
        f"|---|{'---|' * len(labels)}",
        f"| LTE PRB use, synthetic | {row([s['lte_prb_spread'][k] for k in labels])} |",
        f"| LTE PRB use, real | {row([r['lte_prb_spread'][k] for k in labels])} |",
        f"| GSM timeslot use, synthetic | {row([s['gsm_util_spread'][k] for k in labels])} |",
        f"| GSM timeslot use, real | {row([r['gsm_util_spread'][k] for k in labels])} |",
        "",
        "![Spread of utilization](crosscheck_spread.png)",
        "",
        "## What the comparison shows",
        "",
        *findings(record),
        "",
        "## Not compared",
        "",
        "Success-rate spread: the real set carries no setup, success or drop counters for LTE "
        "or GSM, so it cannot be compared with this data.",
        "",
        "## Attribution",
        "",
        record["citation"],
        "",
        "Changes: the counters were aggregated into the statistics and figures above; no row "
        "of the data is reproduced.",
    ]
    res = record.get("resources")
    if res:
        lines += [
            "",
            "## Cost",
            "",
            f"Report run {res['wall time, s']:g} s, peak resident set "
            f"{res['peak resident set, MB']} MB.",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    """Write the record, the report and the figures.

    Returns:
        The process exit code.
    """
    RESULTS.mkdir(exist_ok=True)
    started = time.perf_counter()
    record = build()
    record["resources"] = {
        "wall time, s": round(time.perf_counter() - started, 1),
        # Peak resident set of this process (Linux reports kB).
        "peak resident set, MB": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0),
    }
    RECORD_JSON.write_text(json.dumps(record, indent=2) + "\n")
    loaded = json.loads(RECORD_JSON.read_text())
    RECORD_MD.write_text(render_markdown(loaded))
    plot(loaded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
