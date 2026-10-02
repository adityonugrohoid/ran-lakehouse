"""Scale test report (rule E1): results/scale_test.md and .json from two run records.

The slice is the demo profile, the full run the scale profile, both through
`ranlake scale-test`; the lake of each run is read once more for the periods
each layer holds and for the time from file arrival to gold. Each run is one
sample. Synthetic network.
"""

import json
import math
from typing import Any

from ran_lakehouse.scale import PERIODS_PER_DAY, RECORD_JSON, RECORD_MD, TARGET_CELLS

GRACE_MIN = 30  # silver builds a UTC day this long after its end (lake.silver.GRACE)
PERCENTILES = {"min": 0.0, "median": 0.5, "p90": 0.9, "max": 1.0}
# The full scale run predates the spill sampling in ranlake scale-test; its
# silver spill was seen from outside the run, by control.
SPILL_NOT_RECORDED = (
    "not recorded in this run; observed externally at about 8.3 GB during silver "
    "(control's sample, 03:19 WIB)"
)


def lake_stats(warehouse: str) -> dict[str, Any]:
    """Periods per layer, and the wait from each file's arrival to its day's cutoff.

    The wait is measured for every PM file whose UTC day is complete in gold
    (all 96 periods): the cutoff (the day's end plus GRACE_MIN) minus the
    file's arrival, both in simulated time.

    Args:
        warehouse: Warehouse name.

    Returns:
        15-minute periods per layer, complete UTC days in gold, and the wait
        in seconds at PERCENTILES.

    Raises:
        RuntimeError: If gold holds no complete UTC day.
    """
    from ran_lakehouse.lake.catalog import connect

    con = connect(warehouse)
    queries = {
        "bronze": "SELECT count(DISTINCT period_start) FROM lk.bronze.pm_values "
        "WHERE period_end - period_start = INTERVAL 15 MINUTE",
        "silver": "SELECT count(DISTINCT period_start) FROM lk.silver.pm_measurements "
        "WHERE granularity_min = 15",
        "gold": "SELECT count(DISTINCT period_start) FROM lk.gold.lte_kpi_15m",
    }
    periods: dict[str, int] = {}
    for layer, sql in queries.items():
        found = con.execute(sql).fetchone()
        periods[layer] = 0 if found is None else int(found[0])
    kinds = dict(
        con.execute("SELECT kind, count(*) FROM lk.bronze.file_arrivals GROUP BY 1").fetchall()
    )
    derived = con.execute("SELECT count(*) FROM lk.silver.pm_measurements WHERE derived").fetchone()
    rows = con.execute(
        f"""WITH days AS (SELECT CAST(period_start AS DATE) AS d FROM lk.gold.lte_kpi_15m
                GROUP BY 1 HAVING count(DISTINCT period_start) = {PERIODS_PER_DAY}),
            files AS (SELECT f.arrival_time, CAST(min(p.period_start) AS DATE) AS d
                FROM lk.bronze.file_arrivals f JOIN lk.bronze.pm_values p USING (file_hash)
                WHERE f.kind = 'PM' AND f.loaded GROUP BY f.file_hash, f.arrival_time)
        SELECT (SELECT count(*) FROM days), date_diff('second', files.arrival_time,
            CAST(files.d AS TIMESTAMPTZ) + INTERVAL 1 DAY + INTERVAL {GRACE_MIN} MINUTE)
        FROM files JOIN days USING (d)"""
    ).fetchall()
    con.close()
    if not rows:
        raise RuntimeError(f"{warehouse}: gold holds no complete UTC day")
    waits = sorted(int(r[1]) for r in rows)
    return {
        "periods": periods,
        "files_by_kind": {str(k): int(v) for k, v in sorted(kinds.items())},
        "silver_derived_rows": 0 if derived is None else int(derived[0]),
        "complete_utc_days": int(rows[0][0]),
        "files_on_complete_days": len(waits),
        "wait_s": {k: waits[round(q * (len(waits) - 1))] for k, q in PERCENTILES.items()},
    }


def derive(run: dict[str, Any], lake: dict[str, Any]) -> dict[str, Any]:
    """Per complete UTC day figures of one run.

    Every stage time is scaled to one complete UTC day of 96 periods by the
    periods that stage processed (silver and gold also built the partial day
    before the first complete one).

    Args:
        run: A run record from ran_lakehouse.scale.scale_run.
        lake: lake_stats() of its warehouse.

    Returns:
        Seconds, rows and files per complete UTC day, storage per cell-day,
        seconds per period, and the file arrival to gold latency in minutes.
    """
    cells = run["network"]["cells"]
    collect = run["stages"]["collect"]
    timing = collect["output"]["timing_s"]
    files = collect["output"]["collector"]["deliveries"]
    layers = run["layers"]
    periods = lake["periods"]
    seconds = {
        "simulate": timing["simulate"],
        "render": timing["render"],
        "collect": timing["collect"],
        "silver": run["stages"]["silver"]["wall_s"],
        "gold": run["stages"]["gold"]["wall_s"],
    }
    processed = {
        "simulate": periods["bronze"],
        "render": periods["bronze"],
        "collect": periods["bronze"],
        "silver": periods["silver"],
        "gold": periods["gold"],
    }
    per_period = {k: seconds[k] / processed[k] for k in seconds}
    per_day = {k: round(v * PERIODS_PER_DAY, 1) for k, v in per_period.items()}
    build = per_day["silver"] + per_day["gold"]
    layer_days = {layer: periods[layer] / PERIODS_PER_DAY for layer in ("bronze", "silver", "gold")}
    return {
        "cells": cells,
        "seconds_per_utc_day": per_day,
        "seconds_per_period": {k: round(v, 3) for k, v in per_period.items()},
        "files_per_utc_day": round(files / layer_days["bronze"]),
        "files_per_s": round(files / (timing["render"] + timing["collect"]), 2),
        "rows_per_utc_day": {
            layer: round(layers[layer]["rows"] / layer_days[layer]) for layer in layer_days
        },
        "rows_per_s": {
            "bronze": round(layers["bronze"]["rows"] / timing["collect"]),
            "silver": round(layers["silver"]["rows"] / seconds["silver"]),
            "gold": round(layers["gold"]["rows"] / seconds["gold"]),
        },
        "storage_bytes_per_cell_day": {
            layer: round(layers[layer]["bytes"] / cells / layer_days[layer]) for layer in layer_days
        },
        "latency_min": {k: round((v + build) / 60.0, 1) for k, v in lake["wait_s"].items()},
        "build_s_per_utc_day": round(build, 1),
    }


def power_law(x0: float, y0: float, x1: float, y1: float, x: float) -> tuple[float, float]:
    """A power law y = a * x ** b through two points, evaluated at x.

    Args:
        x0: First size.
        y0: First value.
        x1: Second size.
        y1: Second value.
        x: Size to evaluate at.

    Returns:
        (exponent b, value at x).
    """
    b = math.log(y1 / y0) / math.log(x1 / x0)
    return b, y1 * (x / x1) ** b


def extrapolate(
    slice_run: dict[str, Any], full: dict[str, Any], d: dict[str, Any]
) -> dict[str, Any]:
    """The full run at TARGET_CELLS cells.

    Terms that grow with cells (rows, files, bytes): the full run's per
    complete UTC day figure times the cell factor. The network build grows
    with grid points times cells per band (both grow with the network): a
    power law in cells through the slice and the full run.

    Args:
        slice_run: The slice run record.
        full: The full run record.
        d: derive() of the full run.

    Returns:
        The factor, per-day figures at the target and the network fit.
    """
    cells = full["network"]["cells"]
    factor = TARGET_CELLS / cells
    b_t, wall = power_law(
        slice_run["network"]["cells"],
        slice_run["network_build"]["wall_s"],
        cells,
        full["network_build"]["wall_s"],
        TARGET_CELLS,
    )
    b_m, peak = power_law(
        slice_run["network"]["cells"],
        slice_run["network_build"]["peak_rss_mb"],
        cells,
        full["network_build"]["peak_rss_mb"],
        TARGET_CELLS,
    )
    b_p, points = power_law(
        slice_run["network"]["cells"],
        slice_run["network"]["grid_points"],
        cells,
        full["network"]["grid_points"],
        TARGET_CELLS,
    )
    return {
        "target_cells": TARGET_CELLS,
        "factor": round(factor, 2),
        "hours_per_utc_day": {
            k: round(v * factor / 3600.0, 2) for k, v in d["seconds_per_utc_day"].items()
        },
        # One type B file per EMS and period: the count does not grow with cells.
        "files_per_utc_day": d["files_per_utc_day"],
        "rows_per_utc_day": {k: round(v * factor) for k, v in d["rows_per_utc_day"].items()},
        "storage_gb_per_utc_day": {
            k: round(v * TARGET_CELLS / 1e9, 2) for k, v in d["storage_bytes_per_cell_day"].items()
        },
        "network_build": {
            "grid_points_exponent": round(b_p, 2),
            "grid_points": round(points),
            "time_exponent": round(b_t, 2),
            "hours": round(wall / 3600.0, 1),
            "memory_exponent": round(b_m, 2),
            "peak_gb": round(peak / 1024.0, 1),
        },
    }


def record(
    slice_run: dict[str, Any],
    full: dict[str, Any],
    slice_lake: dict[str, Any],
    full_lake: dict[str, Any],
) -> dict[str, Any]:
    """The report record.

    Args:
        slice_run: The slice run record.
        full: The full run record.
        slice_lake: lake_stats() of the slice warehouse.
        full_lake: lake_stats() of the full warehouse.

    Returns:
        Both runs, their lake statistics, derived figures and the extrapolation.
    """
    d_full = derive(full, full_lake)
    return {
        "runs": {"slice": slice_run, "full": full},
        "lake": {"slice": slice_lake, "full": full_lake},
        "derived": {"slice": derive(slice_run, slice_lake), "full": d_full},
        "extrapolation": extrapolate(slice_run, full, d_full),
    }


def spill(name: str, stage: dict[str, Any]) -> str:
    """A stage's peak spill, or why it is missing.

    Args:
        name: Stage name.
        stage: The stage of a run record.

    Returns:
        Text for the table.
    """
    if "peak_spill_mb" in stage:
        return f"{stage['peak_spill_mb']:,}"
    return SPILL_NOT_RECORDED if name == "silver" else "not recorded in this run"


def stage_rows(run: dict[str, Any]) -> list[str]:
    """Wall time, peak memory and peak spill per stage process of one run.

    Args:
        run: A run record.

    Returns:
        Table rows.
    """
    timing = run["stages"]["collect"]["output"]["timing_s"]
    rows = [
        f"| network build | {run['network_build']['wall_s']:,.1f} | "
        f"{run['network_build']['peak_rss_mb']:,} | |"
    ]
    rows += [
        f"| {stage} | {v['wall_s']:,.1f} | {v['peak_rss_mb']:,} | {spill(stage, v)} |"
        for stage, v in run["stages"].items()
    ]
    rows.append(
        f"| of which collect's own work (simulate {timing['simulate']:,.1f} s, render "
        f"{timing['render']:,.1f} s, deliver and collect {timing['collect']:,.1f} s) | "
        f"{timing['simulate'] + timing['render'] + timing['collect']:,.1f} | | |"
    )
    return rows


def render_markdown(rec: dict[str, Any]) -> str:
    """The report.

    Args:
        rec: The record.

    Returns:
        Markdown.
    """
    s, f = rec["runs"]["slice"], rec["runs"]["full"]
    ds, df = rec["derived"]["slice"], rec["derived"]["full"]
    lf = rec["lake"]["full"]
    e = rec["extrapolation"]
    n = e["network_build"]
    order = ("simulate", "render", "collect", "silver", "gold")
    lines = [
        "# Scale test",
        "",
        "A synthetic network of about 10,000 cells through the whole pipeline, measured "
        "stage by stage on the build machine (rule E1). Written by "
        "`python -m ran_lakehouse.scale report` from `scale_test.json`; the runs come from "
        "`ranlake scale-test`. One run each, so every figure is one sample: no spread, and "
        "no memory target is claimed.",
        "",
        "| Run | Profile | Cells | LTE / GSM | Grid points | Days simulated | Total wall (s) |",
        "|---|---|---|---|---|---|---|",
        *[
            f"| {name} | {r['profile']} | {r['network']['cells']:,} | "
            f"{r['network']['lte_cells']:,} / {r['network']['gsm_cells']:,} | "
            f"{r['network']['grid_points']:,} | {r['days']} | {r['wall_s']:,.1f} |"
            for name, r in (("slice", s), ("full", f))
        ],
        "",
        "## Why two simulated days",
        "",
        "The network's days are WIB days (UTC+7), and silver and gold build UTC days, each "
        f"only after its cutoff, the UTC day's end plus {GRACE_MIN} minutes. One simulated "
        "WIB day covers the last 7 hours of one UTC day and the first 17 of the next, so "
        "after one day no UTC day is complete: silver and gold would process only a "
        "partial one. The full run simulates two days, so that one complete UTC day goes "
        f"from files to gold ({lf['complete_utc_days']} complete UTC day in gold); every "
        "rate below "
        "is stated per complete UTC day of 96 periods, scaled by the periods each stage "
        "actually processed.",
        "",
        "## Per stage, full run",
        "",
        "Each stage runs in a process of its own; peak resident set from os.wait4, peak "
        "DuckDB spill folder size sampled every second. The collect stage builds the "
        "network again before it simulates, renders, delivers and collects, so its wall "
        "time holds a second network build; its own work is the last row. The network "
        "build is a once-per-network cost and is counted once below.",
        "",
        "| Stage process | Wall (s) | Peak RSS (MB) | Peak spill (MB) |",
        "|---|---|---|---|",
        *stage_rows(f),
        "",
        f"Silver is the bottleneck at scale: {f['stages']['silver']['wall_s']:,.0f} s for "
        f"{f['layers']['silver']['rows']:,} rows, against "
        f"{f['stages']['gold']['wall_s']:,.0f} s for gold and "
        f"{f['stages']['collect']['output']['timing_s']['collect']:,.0f} s for collecting "
        "the same data. A reading of the code, not verified: silver stages each partition "
        "(one UTC day of one EMS) as DuckDB temporary tables, the bronze rows with every "
        "column and then the mapped rows with every column, and joins the bronze rows to "
        "themselves to find changed redeliveries; at the 1 GB memory limit those tables "
        "live in the spill folder. The slice's silver spilled "
        f"{s['stages']['silver'].get('peak_spill_mb', 0):,} MB for "
        f"{s['layers']['silver']['rows']:,} rows. Making silver stream or narrow its "
        "staging is an open item, not built here.",
        "",
        "## Per stage, slice",
        "",
        "| Stage process | Wall (s) | Peak RSS (MB) | Peak spill (MB) |",
        "|---|---|---|---|",
        *stage_rows(s),
        "",
        "## Per complete UTC day",
        "",
        "| Figure | Slice (demo) | Full (scale) |",
        "|---|---|---|",
        *[
            f"| {k}, seconds | {ds['seconds_per_utc_day'][k]:,.1f} | "
            f"{df['seconds_per_utc_day'][k]:,.1f} |"
            for k in order
        ],
        f"| files delivered | {ds['files_per_utc_day']:,} | {df['files_per_utc_day']:,} |",
        f"| files per second (render and collect) | {ds['files_per_s']:g} | "
        f"{df['files_per_s']:g} |",
        *[
            f"| {layer} rows | {ds['rows_per_utc_day'][layer]:,} | "
            f"{df['rows_per_utc_day'][layer]:,} |"
            for layer in ("bronze", "silver", "gold")
        ],
        *[
            f"| {layer} rows per second | {ds['rows_per_s'][layer]:,} | "
            f"{df['rows_per_s'][layer]:,} |"
            for layer in ("bronze", "silver", "gold")
        ],
        *[
            f"| {layer} storage per cell-day, bytes | "
            f"{ds['storage_bytes_per_cell_day'][layer]:,} | "
            f"{df['storage_bytes_per_cell_day'][layer]:,} |"
            for layer in ("bronze", "silver", "gold")
        ],
        "",
        "Files: per UTC day, 2 EMS times 96 periods of PM files (one type B file per EMS "
        "and period) plus one CM snapshot, one CM change log and one FM export per EMS, "
        "so 198 a day is 192 PM files and 6 others; "
        "in the full run's two days, "
        + ", ".join(f"{v} {k}" for k, v in lf["files_by_kind"].items())
        + ". Silver holds more rows than bronze because it adds the 3GPP measurements "
        "derived from vendor-style counters (PRB use and cell unavailable time, flagged "
        f"derived): {lf['silver_derived_rows']:,} rows in the full run.",
        "",
        "## Latency per period, file arrival to gold",
        "",
        "Computed, not observed end to end: from the simulated arrival time of each file, "
        "the cutoff rule and the measured silver and gold times; the run itself was a "
        "batch, so no wall clock saw a file arrive and its KPIs appear. "
        "Silver builds a UTC day after its cutoff and gold builds it next, so a period's "
        "KPIs are available in gold at the cutoff plus the silver and gold time of the day "
        f"({df['build_s_per_utc_day']:,.1f} s in the full run). For each of the "
        f"{lf['files_on_complete_days']:,} PM files of the complete UTC days (PM only; "
        "CM and FM files do not feed gold KPIs), from its "
        "(simulated) arrival to gold:",
        "",
        "| Run | min | median | p90 | max |",
        "|---|---|---|---|---|",
        *[
            f"| {name} | "
            + " | ".join(f"{d['latency_min'][k]:,.1f} min" for k in ("min", "median", "p90", "max"))
            + " |"
            for name, d in (("slice", ds), ("full", df))
        ],
        "",
        "The wait for the cutoff dominates (a file of the day's first period waits almost a "
        "day); the processing itself is the per-period cost below. A live run that needs "
        "fresher gold would build silver per hour instead of per day; not measured here.",
        "",
        "| Stage | Slice, s per period | Full, s per period |",
        "|---|---|---|",
        *[
            f"| {k} | {ds['seconds_per_period'][k]:g} | {df['seconds_per_period'][k]:g} |"
            for k in order
        ],
        "",
        f"## Extrapolation to {e['target_cells']:,} cells",
        "",
        "Method. Each term is placed by what it grows with:",
        "",
        "- with the cells (rows, files, bytes, and the time of simulate, render, collect, "
        "silver and gold that handles them, silver at its time per row in this run): the "
        "full run's figure per complete UTC day "
        f"times {e['factor']:g}, the target over the full run's cells, on one machine with "
        "one process per stage, as measured; the network build is not part of these "
        "per-day figures;",
        "- with the grid points and the cells per band together (the network build: "
        "coverage computes every grid point against every cell of its band): a power law in "
        "cells through the slice and the full run, two points. The grid grows with the "
        f"served area (exponent {n['grid_points_exponent']:g} in cells here) and the cells "
        "with area and density.",
        "",
        "The network build is a one-off per network in principle (it is deterministic from "
        "the profile), but the current code builds it again in every process that needs it "
        "(each backfill, run, serve and scale-test stage): per run as built.",
        "",
        "Assumptions: the scale profile's density and band mix, the same machine, no "
        "parallelism, and silver and gold staying linear in rows.",
        "",
        "| Term | Grows with | At the target |",
        "|---|---|---|",
        *[
            f"| {k}, hours per UTC day | cells | {v:,.2f} |"
            for k, v in e["hours_per_utc_day"].items()
        ],
        f"| files delivered per UTC day | EMS and periods, not cells (one type B file per "
        f"EMS and period; each file grows with its cells) | {e['files_per_utc_day']:,} |",
        *[
            f"| {layer} rows per UTC day | cells | {v:,} |"
            for layer, v in e["rows_per_utc_day"].items()
        ],
        *[
            f"| {layer} storage, GB per UTC day | cells | {v:,.2f} |"
            for layer, v in e["storage_gb_per_utc_day"].items()
        ],
        f"| grid points | served area | {n['grid_points']:,} |",
        f"| network build, hours (exponent {n['time_exponent']:g}) | grid points and cells "
        f"per band | {n['hours']:,.1f} (rough: two points) |",
        f"| network build, peak GB (exponent {n['memory_exponent']:g}) | grid points and "
        f"cells per band | {n['peak_gb']:,.1f} (rough: two points) |",
        "",
        "What would have to change at that size (not measured):",
        "",
        "- the network build: keep only the cells within reach of each grid point (a "
        "spatial index), build per region in parallel, and build it once per network "
        "instead of once per process;",
        "- collect: one collector process per EMS or per region in parallel; a type B file "
        "per EMS and period grows with its cells, so the file count stays low and file "
        "size grows;",
        "- silver and gold: partitions are already one UTC day per EMS; at that size they "
        "run in parallel, or on a distributed engine (Spark or Trino over the same Iceberg "
        "tables) instead of one DuckDB process;",
        "- storage grows linearly with the cells, which object storage and the catalog take as is.",
    ]
    return "\n".join(lines) + "\n"


def write(slice_run: dict[str, Any], full: dict[str, Any]) -> None:
    """Read both lakes once more, then write the record and the report.

    Args:
        slice_run: The slice run record.
        full: The full run record.
    """
    rec = record(slice_run, full, lake_stats(slice_run["warehouse"]), lake_stats(full["warehouse"]))
    RECORD_JSON.write_text(json.dumps(rec, indent=2) + "\n")
    RECORD_MD.write_text(render_markdown(json.loads(RECORD_JSON.read_text())))
