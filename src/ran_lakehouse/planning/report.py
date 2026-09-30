"""Planning geography report (rules G1 to G7) for the demo profile.

Builds the expansion area's planning data (planning.build), writes the
gold tables to a warehouse, and records the counts, the parameters with
their labels and sources, and three review figures. Writes
results/planning.json, results/planning.md and the figures.
Run: `uv run python -m ran_lakehouse.planning.report --warehouse demo`.
"""

import argparse
import json
import resource
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LightSource

from ran_lakehouse.lake.catalog import connect
from ran_lakehouse.model import default_model
from ran_lakehouse.planning import backhaul, geography, radio, terrain
from ran_lakehouse.planning.build import Plan, build_plan, serves, tables, write_gold
from ran_lakehouse.world import build_world

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "planning.json"
RECORD_MD = RESULTS / "planning.md"
FIG_TERRAIN = RESULTS / "planning_terrain.png"
FIG_COVERAGE = RESULTS / "planning_coverage.png"
FIG_LOS = RESULTS / "planning_los.png"
PROFILE = "demo"
MAP_STEP_KM = 0.25  # coverage map spacing (ASSUMPTION)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
GRID_COLOR = "#eb6834"
FIBER_COLOR = "#7b3fbf"
SITE_COLOR = "#0b0b0b"
VILLAGE_COLOR = "#2a78d6"
COVERED_COLOR = "#1baf7a"


def spread(values: np.ndarray) -> list[float]:
    """Minimum, median and maximum.

    Args:
        values: Values.

    Returns:
        [min, median, max], rounded.
    """
    return [
        round(float(np.min(values)), 2),
        round(float(np.median(values)), 2),
        round(float(np.max(values)), 2),
    ]


def parameters() -> dict[str, Any]:
    """The planning constants with their labels.

    Returns:
        Name to value and label.
    """
    return {
        "relief_m (START)": list(terrain.RELIEF_M),
        "terrain spectral exponent (START)": terrain.SPECTRAL_BETA,
        "terrain grid km (ASSUMPTION)": terrain.STEP_KM,
        "persons per school (START)": geography.PERSONS_PER_SCHOOL,
        "school from persons (START)": geography.SCHOOL_FROM_PERSONS,
        "candidate sites (START)": geography.CANDIDATES,
        "candidate spacing km (START)": geography.MIN_SPACING_KM,
        "candidate near a village within km (START)": geography.NEAR_VILLAGE_KM,
        "mast height m (START)": radio.MAST_HEIGHT_M,
        "LTE B8 power dBm, N_RB (rule M)": [radio.LTE_POWER_DBM, radio.LTE_N_RB],
        "GSM 900 BCCH power dBm (rule M)": radio.GSM_POWER_DBM,
        "LTE RSRP threshold dBm (START)": radio.LTE_RSRP_THRESHOLD_DBM,
        "GSM RxLev threshold dBm (START)": radio.GSM_RXLEV_THRESHOLD_DBM,
        "indoor margin dB (ITU-R P.2109-2, traditional buildings)": radio.INDOOR_MARGIN_DB,
        "microwave GHz (START)": radio.MICROWAVE_GHZ,
        "Fresnel clearance used (START)": radio.FRESNEL_CLEARANCE,
        "hub zone km, hub antenna m, max hop km (START)": [
            backhaul.HUB_ZONE_KM,
            backhaul.HUB_ANTENNA_M,
            backhaul.MAX_HOP_KM,
        ],
        "grid power within km (START)": backhaul.GRID_POWER_KM,
        "satellite Mbps (START)": backhaul.SATELLITE_MBPS,
        "costs IDR (ASSUMPTION)": {
            "build base": geography.BUILD_BASE_IDR,
            "access road per km": geography.ACCESS_ROAD_IDR_PER_KM,
            "fiber per km": backhaul.FIBER_IDR_PER_KM,
            "microwave link": backhaul.MICROWAVE_LINK_IDR,
            "satellite terminal": backhaul.SATELLITE_TERMINAL_IDR,
            "satellite monthly": backhaul.SATELLITE_MONTHLY_IDR,
            "grid line per km": backhaul.GRID_LINE_IDR_PER_KM,
            "solar and battery": backhaul.SOLAR_IDR,
            "fiber monthly": backhaul.FIBER_MONTHLY_IDR,
            "microwave monthly": backhaul.MICROWAVE_MONTHLY_IDR,
            "grid power monthly": backhaul.GRID_POWER_MONTHLY_IDR,
            "solar monthly": backhaul.SOLAR_MONTHLY_IDR,
        },
    }


def greedy_cover(served: np.ndarray) -> int:
    """Candidates a greedy cover needs to serve every village some candidate serves.

    Args:
        served: Mask (candidates, villages).

    Returns:
        Candidates picked (an upper bound on the minimum cover).
    """
    left = served.any(axis=0)
    picked = 0
    while left.any():
        best = int(np.argmax((served & left[None, :]).sum(axis=1)))
        left &= ~served[best]
        picked += 1
    return picked


def counts(plan: Plan, data: dict[str, Any]) -> dict[str, Any]:
    """Counts for review.

    Args:
        plan: The plan.
        data: Its gold tables.

    Returns:
        The counts.
    """
    villages = data["villages"].to_pylist()
    population = np.array([v["population"] for v in villages])
    lte, gsm = plan.served["LTE"], plan.served["GSM"]
    outdoor_lte = lte >= radio.LTE_RSRP_THRESHOLD_DBM
    outdoor_gsm = gsm >= radio.GSM_RXLEV_THRESHOLD_DBM
    indoor_lte = serves(lte, radio.LTE_RSRP_THRESHOLD_DBM)
    indoor_gsm = serves(gsm, radio.GSM_RXLEV_THRESHOLD_DBM)
    rsrp = np.array([lv["lte_rsrp_dbm"] for lv in plan.levels])
    rxlev = np.array([lv["gsm_rxlev_dbm"] for lv in plan.levels])
    site_lte = serves(rsrp, radio.LTE_RSRP_THRESHOLD_DBM)
    site_gsm = serves(rxlev, radio.GSM_RXLEV_THRESHOLD_DBM)
    options = data["backhaul_power_options"].to_pylist()

    def option(kind: str) -> list[dict[str, Any]]:
        return [o for o in options if o["kind"] == kind]

    microwave = [o for o in option("microwave") if o["available"]]
    sites = data["candidate_sites"].to_pylist()
    return {
        "villages": len(villages),
        "persons": int(population.sum()),
        "schools": sum(v["schools"] for v in villages),
        "served_today": {
            "outdoor LTE (RSRP at threshold)": int(outdoor_lte.sum()),
            "outdoor GSM (RxLev at threshold)": int(outdoor_gsm.sum()),
            "indoor LTE (covered_today_lte)": int(indoor_lte.sum()),
            "indoor GSM (covered_today_gsm)": int(indoor_gsm.sum()),
            "persons without indoor LTE": int(population[~indoor_lte].sum()),
            "persons without indoor GSM": int(population[~indoor_gsm].sum()),
        },
        "candidates": len(sites),
        "candidates_qualifying": geography.qualifying(plan.terrain, plan.villages),
        "candidate_elevation_m": spread(np.array([s["elevation_m"] for s in sites])),
        "build_cost_idr": spread(np.array([s["build_cost_idr"] for s in sites])),
        "villages_served_by_some_candidate": {
            "LTE indoor": int(site_lte.any(axis=0).sum()),
            "GSM indoor": int(site_gsm.any(axis=0).sum()),
        },
        "villages_per_candidate_lte_indoor": spread(site_lte.sum(axis=1)),
        "greedy_cover_candidates": {
            "LTE indoor": greedy_cover(site_lte),
            "GSM indoor": greedy_cover(site_gsm),
        },
        "backhaul": {
            "fiber spur km": spread(np.array([o["distance_km"] for o in option("fiber")])),
            "microwave clear at 0.6 F1 (used)": len(microwave),
            "microwave clear at 1.0 F1 (P.530-19 2.2.2.1)": sum(
                o["clears_full_fresnel"] for o in option("microwave")
            ),
            "microwave hop km": spread(np.array([o["distance_km"] for o in microwave])),
            "satellite": len(option("satellite")),
            "hubs": len(plan.hubs),
        },
        "power": {
            "grid": sum(o["available"] for o in option("grid_power")),
            "solar": sum(o["available"] for o in option("solar_power")),
        },
    }


def hillshade(ax: Any, plan: Plan) -> None:
    """Terrain as a shaded relief map.

    Args:
        ax: Axes.
        plan: The plan.
    """
    t = plan.terrain
    rows, cols = t.heights_m.shape
    extent = (t.x0_km, t.x0_km + (cols - 1) * t.step_km, t.y0_km, t.y0_km + (rows - 1) * t.step_km)
    light = LightSource(azdeg=315, altdeg=45)
    rgb = light.shade(
        t.heights_m,
        cmap=plt.get_cmap("terrain"),
        vert_exag=0.05,
        blend_mode="soft",
        dx=t.step_km * 1000,
        dy=t.step_km * 1000,
        vmin=terrain.RELIEF_M[0] - 150,
        vmax=terrain.RELIEF_M[1] + 50,
    )
    ax.imshow(rgb, origin="lower", extent=extent)


def plot_terrain(plan: Plan, data: dict[str, Any]) -> None:
    """Terrain with villages, candidates, hubs, grid line and fiber route.

    Args:
        plan: The plan.
        data: Its gold tables.
    """
    fig, ax = plt.subplots(figsize=(6.4, 9.6), facecolor=SURFACE)
    hillshade(ax, plan)
    villages = data["villages"].to_pylist()
    for covered, color, label in (
        (False, VILLAGE_COLOR, "village, no indoor GSM today"),
        (True, COVERED_COLOR, "village, indoor GSM today"),
    ):
        pick = [v for v in villages if v["covered_today_gsm"] == covered]
        ax.scatter(
            [v["x_km"] for v in pick],
            [v["y_km"] for v in pick],
            s=[4 + v["population"] / 120 for v in pick],
            color=color,
            edgecolor="white",
            linewidth=0.5,
            label=label,
            zorder=3,
        )
    ax.scatter(
        [c.x_km for c in plan.candidates],
        [c.y_km for c in plan.candidates],
        marker="^",
        s=36,
        color=SITE_COLOR,
        label="candidate site",
        zorder=4,
    )
    ax.plot(
        plan.grid_line[:, 0],
        plan.grid_line[:, 1],
        color=GRID_COLOR,
        linewidth=2,
        label="power grid line",
    )
    ax.plot(
        plan.fiber_route[:, 0],
        plan.fiber_route[:, 1],
        color=FIBER_COLOR,
        linewidth=2,
        linestyle="--",
        label="fiber route",
    )
    ax.scatter(
        [h[1] for h in plan.hubs],
        [h[2] for h in plan.hubs],
        marker="s",
        s=24,
        color=INK_SECONDARY,
        label="microwave hub (served site)",
        zorder=4,
    )
    ax.set_xlim(
        plan.hubs[0][1] - 1 if plan.hubs else plan.terrain.x0_km,
        plan.terrain.x0_km + (plan.terrain.heights_m.shape[1] - 1) * plan.terrain.step_km,
    )
    ax.set_xlabel("x (km)", color=INK_SECONDARY)
    ax.set_ylabel("y (km)", color=INK_SECONDARY)
    ax.set_title(
        "Synthetic expansion area: terrain (200-600 m), villages,\n"
        "candidate sites and utility lines",
        color=INK,
        fontsize=10,
    )
    ax.legend(loc="lower right", fontsize=7, framealpha=0.9)
    fig.tight_layout()
    fig.savefig(FIG_TERRAIN, facecolor=SURFACE, dpi=110)
    plt.close(fig)


def plot_coverage(plan: Plan) -> dict[str, Any]:
    """Best outdoor LTE RSRP from all candidates over the expansion area.

    Args:
        plan: The plan.

    Returns:
        Share of the area with indoor LTE service from some candidate.
    """
    t = plan.terrain
    x_max = t.x0_km + (t.heights_m.shape[1] - 1) * t.step_km
    y_max = t.y0_km + (t.heights_m.shape[0] - 1) * t.step_km
    xs = np.arange(t.x0_km + MAP_STEP_KM / 2, x_max, MAP_STEP_KM)
    ys = np.arange(t.y0_km + MAP_STEP_KM / 2, y_max, MAP_STEP_KM)
    gx, gy = np.meshgrid(xs, ys)
    best = np.full(gx.size, -np.inf)
    for c in plan.candidates:
        best = np.maximum(
            best, radio.site_levels(t, c.x_km, c.y_km, gx.ravel(), gy.ravel())["lte_rsrp_dbm"]
        )
    grid = best.reshape(gx.shape)
    fig, ax = plt.subplots(figsize=(6.4, 9.6), facecolor=SURFACE)
    image = ax.imshow(
        grid,
        origin="lower",
        extent=(xs[0], xs[-1], ys[0], ys[-1]),
        cmap="Blues",
        vmin=-130,
        vmax=-60,
    )
    service = radio.LTE_RSRP_THRESHOLD_DBM + radio.INDOOR_MARGIN_DB
    ax.contour(
        gx,
        gy,
        grid,
        levels=[radio.LTE_RSRP_THRESHOLD_DBM, service],
        colors=[INK_SECONDARY, INK],
        linewidths=[0.8, 1.2],
        linestyles=["--", "-"],
    )
    ax.scatter(
        [c.x_km for c in plan.candidates],
        [c.y_km for c in plan.candidates],
        marker="^",
        s=18,
        color=SITE_COLOR,
    )
    fig.colorbar(image, ax=ax, shrink=0.6, label="best outdoor LTE RSRP (dBm)")
    ax.set_xlabel("x (km)", color=INK_SECONDARY)
    ax.set_ylabel("y (km)", color=INK_SECONDARY)
    ax.set_title(
        f"Synthetic: all {len(plan.candidates)} candidates, LTE B8, Hata + Bullington\n"
        f"solid: indoor service ({service:g} dBm), dashed: outdoor threshold "
        f"({radio.LTE_RSRP_THRESHOLD_DBM:g} dBm)",
        color=INK,
        fontsize=9,
    )
    fig.tight_layout()
    fig.savefig(FIG_COVERAGE, facecolor=SURFACE, dpi=110)
    plt.close(fig)
    return {
        "area_share_indoor_lte": round(float((grid >= service).mean()), 3),
        "area_share_outdoor_lte": round(float((grid >= radio.LTE_RSRP_THRESHOLD_DBM).mean()), 3),
    }


def plot_los(plan: Plan, data: dict[str, Any]) -> dict[str, Any]:
    """The longest clear microwave hop: its profile and Fresnel zone.

    Args:
        plan: The plan.
        data: Its gold tables.

    Returns:
        Which hop the figure shows.

    Raises:
        RuntimeError: If no candidate has a clear hop.
    """
    hops = [
        o
        for o in data["backhaul_power_options"].to_pylist()
        if o["kind"] == "microwave" and o["available"]
    ]
    if not hops:
        raise RuntimeError("no candidate has a clear microwave hop")
    hop = max(hops, key=lambda o: o["distance_km"])
    site = next(c for c in plan.candidates if c.site_id == hop["site_id"])
    hub = next(h for h in plan.hubs if h[0] == hop["detail"])
    _, p = radio.line_of_sight(
        plan.terrain,
        (site.x_km, site.y_km, radio.MAST_HEIGHT_M),
        (hub[1], hub[2], backhaul.HUB_ANTENNA_M),
        radio.MICROWAVE_GHZ,
        radio.FRESNEL_CLEARANCE,
    )
    d = p["distance_km"]
    fig, ax = plt.subplots(figsize=(10, 4.2), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    ax.fill_between(d, 0, p["ground_with_bulge_m"], color="#c9b99a", label="ground + earth bulge")
    ax.plot(d, p["path_m"], color=INK, linewidth=1.2, label="direct path")
    ax.fill_between(
        d,
        p["path_m"] - p["fresnel_radius_m"],
        p["path_m"] + p["fresnel_radius_m"],
        color=VILLAGE_COLOR,
        alpha=0.15,
        label="first Fresnel zone",
    )
    ax.plot(
        d,
        p["path_m"] - radio.FRESNEL_CLEARANCE * p["fresnel_radius_m"],
        color=VILLAGE_COLOR,
        linewidth=1,
        linestyle="--",
        label=f"{radio.FRESNEL_CLEARANCE:g} F1 clearance line",
    )
    ax.set_ylim(
        float(np.min(p["ground_with_bulge_m"])) - 40,
        float(np.max(p["path_m"] + p["fresnel_radius_m"])) + 30,
    )
    ax.set_xlabel("distance from the candidate (km)", color=INK_SECONDARY)
    ax.set_ylabel("height above sea level (m)", color=INK_SECONDARY)
    ax.set_title(
        f"Synthetic: microwave hop {site.site_id} to {hub[0]}, "
        f"{hop['distance_km']:.1f} km at {radio.MICROWAVE_GHZ:g} GHz, k = 4/3",
        color=INK,
        fontsize=10,
    )
    ax.legend(fontsize=8, loc="upper right")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    fig.savefig(FIG_LOS, facecolor=SURFACE, dpi=110)
    plt.close(fig)
    return {
        "site": site.site_id,
        "hub": hub[0],
        "hop_km": hop["distance_km"],
        "clears_full_fresnel": hop["clears_full_fresnel"],
    }


def build(warehouse: str) -> dict[str, Any]:
    """Build the plan, write gold, and make the record and figures.

    Args:
        warehouse: Warehouse to write the planning gold tables to.

    Returns:
        The record.
    """
    started = time.perf_counter()
    model = default_model(build_world(PROFILE))
    plan = build_plan(model)
    data = tables(plan)
    built_s = time.perf_counter() - started
    con = connect(warehouse)
    write_gold(con, data)
    con.close()
    plot_terrain(plan, data)
    area = plot_coverage(plan)
    los = plot_los(plan, data)
    return {
        "profile": PROFILE,
        "sources": {
            "diffraction": radio.BULLINGTON_SOURCE,
            "fresnel": radio.FRESNEL_SOURCE,
            "clearance": radio.CLEARANCE_SOURCE,
            "thresholds": radio.THRESHOLD_CONTEXT,
            "indoor margin": radio.INDOOR_MARGIN_SOURCE,
            "ITU-R P.2109-2 median at 0.9 GHz (dB)": radio.P2109_MEDIAN_DB,
        },
        "parameters": parameters(),
        "counts": counts(plan, data),
        "coverage_area": area,
        "los_example": los,
        "rows": {name: table.num_rows for name, table in data.items()},
        "timing_s": {
            "network model and plan": round(built_s, 1),
            "total": round(time.perf_counter() - started, 1),
        },
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
    }


def text(value: Any) -> str:
    """A record value as report text.

    Args:
        value: A number, string, list or mapping.

    Returns:
        The text: lists joined by " / ", mappings as "key value" pairs.
    """
    if isinstance(value, dict):
        return "; ".join(f"{k} {text(v)}" for k, v in value.items())
    if isinstance(value, list):
        return " / ".join(text(v) for v in value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"{value:,}" if isinstance(value, int) else f"{value:g}"
    return str(value)


def render_markdown(record: dict[str, Any]) -> str:
    """Render the report.

    Args:
        record: The record as loaded from JSON.

    Returns:
        The Markdown report.
    """
    c = record["counts"]
    s = c["served_today"]
    lines = [
        "# Planning geography report",
        "",
        f"Synthetic expansion area of the {record['profile']} profile (rules G1 to G7): terrain,",
        "villages, candidate sites, coverage and backhaul, all generated from the world's seed.",
        "Written by `python -m ran_lakehouse.planning.report` from `planning.json`.",
        "",
        "![Terrain](planning_terrain.png)",
        "",
        "## Service today (rule G1)",
        "",
        "The expansion area has little or no usable indoor service today; some villages see",
        "weak outdoor coverage from the served edge. The served cells' levels at each village",
        "use the model's Hata plus the same Bullington diffraction over the terrain as the",
        "candidates. Service coverage is indoor: the outdoor level must clear the threshold by",
        "the indoor margin ("
        f"{record['parameters']['indoor margin dB (ITU-R P.2109-2, traditional buildings)']:g}"
        " dB, the ITU-R P.2109-2 median for traditional buildings; village housing is",
        "modelled as that class, ASSUMPTION).",
        "",
        "| Villages | Count |",
        "|---|---|",
        f"| all | {c['villages']} ({c['persons']:,} persons, {c['schools']} schools) |",
        *[f"| {k} | {v:,} |" for k, v in s.items()],
        "",
        "## Candidates and coverage (rules G4, G5)",
        "",
        f"{c['candidates_qualifying']} hilltops qualify and {c['candidates']} are kept (cap "
        f"{record['parameters']['candidate sites (START)']}, by persons nearby): each village's"
        " highest hilltop within "
        f"{record['parameters']['candidate near a village within km (START)']:g} km, kept "
        f"{record['parameters']['candidate spacing km (START)']:g} km apart, so they cluster",
        "where villages do. A greedy cover serves every village some candidate serves with "
        f"{c['greedy_cover_candidates']['LTE indoor']} candidates (LTE indoor) or "
        f"{c['greedy_cover_candidates']['GSM indoor']} (GSM indoor); elevation "
        f"{c['candidate_elevation_m'][0]:g} to {c['candidate_elevation_m'][2]:g} m; build cost "
        f"{c['build_cost_idr'][0]:,.0f} to {c['build_cost_idr'][2]:,.0f} IDR (ASSUMPTION).",
        "",
        "![Coverage](planning_coverage.png)",
        "",
        "| Coverage from candidates | Value |",
        "|---|---|",
        f"| villages with indoor LTE from some candidate | "
        f"{c['villages_served_by_some_candidate']['LTE indoor']} |",
        f"| villages with indoor GSM from some candidate | "
        f"{c['villages_served_by_some_candidate']['GSM indoor']} |",
        f"| villages per candidate, indoor LTE (min / median / max) | "
        f"{' / '.join(f'{v:g}' for v in c['villages_per_candidate_lte_indoor'])} |",
        f"| area share with indoor LTE from all candidates | "
        f"{record['coverage_area']['area_share_indoor_lte']:.1%} |",
        f"| area share with outdoor LTE from all candidates | "
        f"{record['coverage_area']['area_share_outdoor_lte']:.1%} |",
        "",
        "## Backhaul and power (rule G6)",
        "",
        "![Line of sight](planning_los.png)",
        "",
        f"The figure: the longest clear hop, {record['los_example']['site']} to "
        f"{record['los_example']['hub']}, {record['los_example']['hop_km']:.1f} km"
        f"{'' if record['los_example']['clears_full_fresnel'] else ' (not clear at 1.0 F1)'}.",
        "The gold table uses the 0.6 F1 clearance; the 1.0 F1 count is beside it.",
        "",
        "| Option (spreads are min / median / max) | Value |",
        "|---|---|",
        *[f"| {k} | {text(v)} |" for k, v in c["backhaul"].items()],
        *[f"| power: {k} | {v} |" for k, v in c["power"].items()],
        "",
        "## Sources",
        "",
        *[f"- {k}: {text(v)}" for k, v in record["sources"].items()],
        "",
        "## Parameters",
        "",
        "| Parameter | Value |",
        "|---|---|",
        *[f"| {k} | {text(v)} |" for k, v in record["parameters"].items()],
        "",
        "## Gold tables (rule G7)",
        "",
        "| Table | Rows |",
        "|---|---|",
        *[f"| gold.{k} | {v:,} |" for k, v in record["rows"].items()],
        "",
        "## Cost",
        "",
        "| Step | Value |",
        "|---|---|",
        *[f"| seconds, {k} | {v} |" for k, v in record["timing_s"].items()],
        f"| peak RSS (MB) | {record['peak_rss_mb']:,} |",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Build, then write the record, report and figures.

    Args:
        argv: Command-line arguments, or None for sys.argv.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse", required=True, help="warehouse for the gold tables")
    args = parser.parse_args(argv)
    RESULTS.mkdir(exist_ok=True)
    RECORD_JSON.write_text(json.dumps(build(args.warehouse), indent=2) + "\n")
    RECORD_MD.write_text(render_markdown(json.loads(RECORD_JSON.read_text())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
