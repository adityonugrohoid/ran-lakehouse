"""World report (rule W): counts, spacing and figures for review.

Writes results/world.json (the one record), results/world.md rendered from
it, and three small figures. Run: `uv run python -m ran_lakehouse.world.report`.
"""

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, LogNorm
from matplotlib.lines import Line2D

from ran_lakehouse.world import World, build_world
from ran_lakehouse.world import profiles as P
from ran_lakehouse.world.network import AREA_CLASSES, area_class_of
from ran_lakehouse.world.population import CLASS_SMOOTHING_KM

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS = REPO_ROOT / "results"
RECORD_JSON = RESULTS / "world.json"
RECORD_MD = RESULTS / "world.md"
FIG_MAP = RESULTS / "world_map.png"
FIG_POPULATION = RESULTS / "world_population.png"
FIG_ISD = RESULTS / "world_isd.png"

# Reference palette (light surface): categorical slots 1-3, sequential blue.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
VENDOR_COLOR = {"huawei": "#2a78d6", "nokia": "#eb6834"}
VENDOR_LABEL = {"huawei": "Huawei-style", "nokia": "Nokia-style"}
SEQUENTIAL_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
EXPANSION_SHADE = "#f0efec"
TECH_MARKER = {"LTE": "o", "LTE+GSM": "s", "GSM": "^"}


def technology_label(has_lte: bool, has_gsm: bool) -> str:
    """Name a site's technology combination.

    Args:
        has_lte: Site carries LTE.
        has_gsm: Site carries GSM.

    Returns:
        "LTE", "LTE+GSM" or "GSM".
    """
    if has_lte and has_gsm:
        return "LTE+GSM"
    return "LTE" if has_lte else "GSM"


def nearest_neighbour_km(world: World) -> dict[str, list[float]]:
    """Distance from each site to its nearest other site, by the site's area class.

    Args:
        world: The world.

    Returns:
        Distances in km per area class, sorted.
    """
    xy = np.array([(s.x_km, s.y_km) for s in world.sites])
    d = np.hypot(xy[:, None, 0] - xy[None, :, 0], xy[:, None, 1] - xy[None, :, 1])
    np.fill_diagonal(d, np.inf)
    nearest = d.min(axis=1)
    by_class: dict[str, list[float]] = {c: [] for c in AREA_CLASSES}
    for site, dist in zip(world.sites, nearest, strict=True):
        by_class[site.area_class].append(round(float(dist), 3))
    return {c: sorted(v) for c, v in by_class.items()}


def fingerprint(world: World) -> str:
    """Hash the sites, cells and population raster (rule W1 evidence).

    Args:
        world: The world.

    Returns:
        SHA-256 hex digest.
    """
    h = hashlib.sha256()
    h.update(repr(world.sites).encode())
    h.update(repr(world.cells).encode())
    h.update(np.round(world.population.persons, 6).tobytes())
    return h.hexdigest()


def summarize(world: World) -> dict[str, Any]:
    """Count everything a reviewer checks in one world.

    Args:
        world: The world.

    Returns:
        The summary, JSON-ready.
    """
    profile = world.profile
    persons = world.population.persons
    served_cols = round(profile.served_width_km / P.RASTER_KM)
    served_classes = area_class_of(world.population.class_density[:, :served_cols])
    cell_km2 = P.RASTER_KM**2
    lte = [c for c in world.cells if c.technology == "LTE"]
    nn = nearest_neighbour_km(world)
    sites_by_vendor = Counter(s.vendor for s in world.sites)
    return {
        "fingerprint": fingerprint(world),
        "map_km": {
            "width": profile.width_km,
            "height": profile.height_km,
            "served_width": profile.served_width_km,
        },
        "population": {
            "total": round(float(persons.sum())),
            "served": round(float(persons[:, :served_cols].sum())),
            "expansion": round(float(persons[:, served_cols:].sum())),
            "settlements": {
                f"{area} {kind}": n
                for (area, kind), n in sorted(
                    Counter((s.area, s.kind) for s in world.population.settlements).items()
                )
            },
            "served_area_km2_by_class": {
                c: round(float((served_classes == c).sum() * cell_km2), 2) for c in AREA_CLASSES
            },
        },
        "sites": {
            "total": len(world.sites),
            "by_area_class": {c: sum(s.area_class == c for s in world.sites) for c in AREA_CLASSES},
            "by_vendor": dict(sorted(sites_by_vendor.items())),
            "huawei_share": round(sites_by_vendor["huawei"] / len(world.sites), 3),
            "vendor_split_x_km": profile.vendor_split_x_km,
            "urban_sites_nokia": sum(
                s.area_class == "urban" and s.vendor == "nokia" for s in world.sites
            ),
            "lte_layers_per_lte_site": round(
                sum(len(s.lte_bands) for s in world.sites) / sum(s.has_lte for s in world.sites),
                2,
            ),
            "by_technology": dict(
                sorted(Counter(technology_label(s.has_lte, s.has_gsm) for s in world.sites).items())
            ),
        },
        "cells": {
            "total": len(world.cells),
            "by_technology": dict(sorted(Counter(c.technology for c in world.cells).items())),
            "lte_share": round(len(lte) / len(world.cells), 3),
            "by_band": dict(sorted(Counter(c.band for c in world.cells).items())),
            "by_object_class": dict(sorted(Counter(c.object_class for c in world.cells).items())),
            "by_vendor": dict(
                sorted(
                    Counter(
                        next(s.vendor for s in world.sites if s.site_id == c.site_id)
                        for c in world.cells
                    ).items()
                )
            ),
        },
        "nearest_site_km": {
            c: {
                "sites": len(v),
                "p10": round(float(np.percentile(v, 10)), 2),
                "median": round(float(np.median(v)), 2),
                "p90": round(float(np.percentile(v, 90)), 2),
            }
            for c, v in nn.items()
            if v
        },
    }


def start_values() -> dict[str, Any]:
    """The START and ASSUMPTION parameters behind the worlds.

    Returns:
        Parameter name to value, JSON-ready.
    """
    return {
        "raster_km (W4, START)": P.RASTER_KM,
        "urban_min_density_per_km2 (START)": P.URBAN_MIN_DENSITY,
        "suburban_min_density_per_km2 (START)": P.SUBURBAN_MIN_DENSITY,
        "isd_km (START)": P.ISD_KM,
        "site_jitter_fraction (urban START, else ASSUMPTION)": P.SITE_JITTER_FRACTION,
        "map_edge_margin_km (START)": P.MAP_EDGE_MARGIN_KM,
        "min_spacing_fraction (ASSUMPTION)": P.MIN_SPACING_FRACTION,
        "class_smoothing_km (ASSUMPTION)": CLASS_SMOOTHING_KM,
        "sectors (W6, ASSUMPTION)": P.SECTORS,
        "azimuth_jitter_sd_deg (ASSUMPTION)": P.AZIMUTH_JITTER_SD_DEG,
        "gsm_cosite_share (START)": P.GSM_COSITE_SHARE,
        "lte_extra_layer_p (START; B3 on every LTE site)": P.LTE_EXTRA_LAYER_P,
        "demo vendor_split_x_km (START, design choice)": P.DEMO.vendor_split_x_km,
        "demo gsm_only_share_rural (START)": P.DEMO.gsm_only_share_rural,
        "demo rural density served/expansion per km2 (ASSUMPTION)": [
            P.DEMO.rural_density_served,
            P.DEMO.rural_density_expansion,
        ],
        "demo settlements (START)": {
            f"{c.kind}": [c.count, c.median_population]
            for c in (*P.DEMO.served_settlements, P.DEMO.expansion_villages)
            if c.kind != "village"
        }
        | {
            "served villages": [P.DEMO.served_settlements[-1].count],
            "expansion villages": [P.DEMO.expansion_villages.count],
        },
    }


def plot_map(world: World, path: Path) -> None:
    """Sites coloured by vendor, shaped by technology, on the map.

    Args:
        world: The world.
        path: PNG output path.
    """
    p = world.profile
    fig, ax = plt.subplots(figsize=(9, 6.3), dpi=100, facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    ax.axvspan(p.served_width_km, p.width_km, color=EXPANSION_SHADE, zorder=0)
    ax.axvline(p.vendor_split_x_km, color=INK_SECONDARY, lw=1, ls=(0, (4, 3)), zorder=1)
    villages = [s for s in world.population.settlements if s.area == "expansion"]
    ax.scatter(
        [v.x_km for v in villages],
        [v.y_km for v in villages],
        s=6,
        color=INK_SECONDARY,
        zorder=2,
    )
    for vendor in ("huawei", "nokia"):
        for tech, marker in TECH_MARKER.items():
            pts = [
                s
                for s in world.sites
                if s.vendor == vendor and technology_label(s.has_lte, s.has_gsm) == tech
            ]
            ax.scatter(
                [s.x_km for s in pts],
                [s.y_km for s in pts],
                s=34,
                marker=marker,
                color=VENDOR_COLOR[vendor],
                edgecolor=SURFACE,
                linewidth=1.2,
                zorder=3,
            )
    handles = [
        Line2D([], [], ls="", marker="o", color=VENDOR_COLOR[v], label=VENDOR_LABEL[v], ms=7)
        for v in ("huawei", "nokia")
    ] + [
        Line2D([], [], ls="", marker=m, color=INK_SECONDARY, label=t, ms=7)
        for t, m in TECH_MARKER.items()
    ]
    handles.append(Line2D([], [], ls="", marker="o", color=INK_SECONDARY, ms=3, label="village"))
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.01, 1.0), frameon=False)
    ax.text(
        p.served_width_km + 0.5,
        p.height_km - 1.5,
        "expansion area",
        color=INK_SECONDARY,
        bbox={"facecolor": EXPANSION_SHADE, "edgecolor": "none", "pad": 1.5},
        zorder=4,
    )
    ax.text(p.vendor_split_x_km + 0.4, p.height_km - 1.5, "vendor split", color=INK_SECONDARY)
    style_axes(ax, p, "Sites by vendor and technology (synthetic, demo profile)")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def plot_population(world: World, path: Path) -> None:
    """Population density heatmap with the sites on top.

    Args:
        world: The world.
        path: PNG output path.
    """
    p = world.profile
    density = world.population.persons / P.RASTER_KM**2
    fig, ax = plt.subplots(figsize=(9, 6.3), dpi=100, facecolor=SURFACE)
    cmap = LinearSegmentedColormap.from_list("seq_blue", SEQUENTIAL_BLUE)
    image = ax.imshow(
        density,
        origin="lower",
        extent=(0, p.width_km, 0, p.height_km),
        cmap=cmap,
        norm=LogNorm(vmin=5, vmax=float(density.max())),
        interpolation="nearest",
    )
    ax.scatter(
        [s.x_km for s in world.sites],
        [s.y_km for s in world.sites],
        s=9,
        color=INK,
        edgecolor=SURFACE,
        linewidth=0.6,
    )
    ax.axvline(p.served_width_km, color=INK_SECONDARY, lw=1, ls=(0, (4, 3)))
    bar = fig.colorbar(image, ax=ax, shrink=0.8)
    bar.set_label("persons per km2 (log scale)", color=INK_SECONDARY)
    style_axes(ax, p, "Population density and sites (synthetic, demo profile)")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def plot_isd(world: World, path: Path) -> None:
    """Histogram of nearest-site distance, one panel per area class.

    Args:
        world: The world.
        path: PNG output path.
    """
    nn = nearest_neighbour_km(world)
    fig, axes = plt.subplots(1, 3, figsize=(10, 3.2), dpi=100, facecolor=SURFACE)
    for ax, c in zip(axes, AREA_CLASSES, strict=True):
        ax.set_facecolor(SURFACE)
        values = nn[c]
        bins = np.linspace(0, max(values) * 1.05, 16)
        ax.hist(values, bins=bins, color=VENDOR_COLOR["huawei"], edgecolor=SURFACE, linewidth=2)
        ax.axvline(P.ISD_KM[c], color=INK, lw=1, ls=(0, (4, 3)))
        ax.set_title(f"{c} ({len(values)} sites), target {P.ISD_KM[c]} km", color=INK, fontsize=10)
        ax.set_xlabel("distance to nearest site, km", color=INK_SECONDARY)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(colors=INK_SECONDARY)
    axes[0].set_ylabel("sites", color=INK_SECONDARY)
    fig.suptitle("Nearest-site distance by area class (synthetic, demo profile)", color=INK)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def style_axes(ax: Any, profile: P.Profile, title: str) -> None:
    """Apply the shared map axes style.

    Args:
        ax: Matplotlib axes.
        profile: The world profile, for the extent.
        title: Axes title.
    """
    ax.set_xlim(0, profile.width_km)
    ax.set_ylim(0, profile.height_km)
    ax.set_aspect("equal")
    ax.set_xlabel("x, km", color=INK_SECONDARY)
    ax.set_ylabel("y, km", color=INK_SECONDARY)
    ax.set_title(title, color=INK)
    ax.tick_params(colors=INK_SECONDARY)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def build_record() -> dict[str, Any]:
    """Build both profiles and summarize them.

    Returns:
        The record, JSON-ready.
    """
    return {
        "start_values": start_values(),
        "profiles": {name: summarize(build_world(name)) for name in ("demo", "tiny")},
    }


def table(rows: dict[str, Any], key: str, value: str) -> list[str]:
    """Render a two-column Markdown table.

    Args:
        rows: Key to value.
        key: Header of the key column.
        value: Header of the value column.

    Returns:
        Table lines.
    """
    return [f"| {key} | {value} |", "|---|---|", *[f"| {k} | {v} |" for k, v in rows.items()]]


def notes(record: dict[str, Any]) -> list[str]:
    """Observations for the reviewer, with every number taken from the record.

    Args:
        record: The record as loaded from JSON.

    Returns:
        Note paragraphs.
    """
    demo = record["profiles"]["demo"]
    tiny = record["profiles"]["tiny"]
    start = record["start_values"]
    isd = start["isd_km (START)"]
    jitter = start["site_jitter_fraction (urban START, else ASSUMPTION)"]
    jitter_text = ", ".join(f"{c} {round(100 * v)}%" for c, v in jitter.items())
    smoothing = start["class_smoothing_km (ASSUMPTION)"]
    spacing = start["min_spacing_fraction (ASSUMPTION)"]
    nn = demo["nearest_site_km"]
    ratios = ", ".join(
        f"{c} {nn[c]['median']} of {isd[c]} km ({nn[c]['median'] / isd[c]:.2f})" for c in nn
    )
    sites = demo["sites"]
    gsm_sites = sites["by_technology"].get("LTE+GSM", 0) + sites["by_technology"].get("GSM", 0)
    layers = sites["lte_layers_per_lte_site"]
    return [
        "Inter-site distance (ISD) means the lattice spacing: sites sit on a hexagonal lattice "
        "spaced at the START ISD of their area class, each jittered in x and y by up to a "
        f"fraction of it ({jitter_text}), so the distance to the nearest site runs below the "
        f"lattice spacing. Median nearest-site distance against lattice ISD: {ratios}.",
        f"Every site stays at least {start['map_edge_margin_km (START)']} km inside the map "
        "edge; lattice points jittered outside that margin are dropped.",
        f"Area classes are judged on density smoothed over {smoothing} km (ASSUMPTION), so a "
        "village reads as rural and a town as a whole; a site closer than "
        f"{spacing} of its class ISD to a "
        "denser class's site is dropped (ASSUMPTION).",
        f"Vendor regions: sites west of x = {sites['vendor_split_x_km']} km are Huawei-style "
        f"({sites['huawei_share']} of sites), the rest Nokia-style; {sites['urban_sites_nokia']} "
        f"of {sites['by_area_class']['urban']} urban sites fall east of the split, on the "
        "eastern edges of the cities. The split is a design choice (rule P5), and the border "
        "through a city is intended: cells on an inter-vendor border are a real optimization "
        "pain point.",
        f"Technology (rule W6, shares of cells): LTE {demo['cells']['lte_share']} of cells; GSM "
        f"on {gsm_sites} of {sites['total']} sites, {sites['by_technology'].get('GSM', 0)} of "
        f"them GSM-only rural sites; {layers} LTE layers per LTE site on average.",
        f"Tiny profile (rule W7): {tiny['sites']['total']} sites and "
        f"{tiny['cells']['total']} cells from the same generator and band rules.",
        "Names: DNs follow TS 32.300 V19.0.0 clause 7. Class names are the XML solution-set "
        "spellings (TS 28.659 V20.0.0 for E-UTRAN; TS 28.656 V19.0.0 for GERAN: BssFunction, "
        "BtsSiteMgr, GsmCell), where the TS 28.655 information model writes BSSFunction, "
        "BTSSiteMgr, GSMCell.",
    ]


def render_markdown(record: dict[str, Any]) -> str:
    """Render the report from the record.

    Args:
        record: The record as loaded from JSON.

    Returns:
        The Markdown report.
    """
    demo = record["profiles"]["demo"]
    tiny = record["profiles"]["tiny"]
    lines = [
        "# World report",
        "",
        "Synthetic world (rule W): the map, population, sites and cells are generated from seeds",
        "(rule W1); no real operator, network, site, city or person, and no real coordinates",
        "(rule W2). Written by `python -m ran_lakehouse.world.report` from `world.json`.",
        "",
        "## Demo profile",
        "",
        f"Map {demo['map_km']['width']} x {demo['map_km']['height']} km; served region "
        f"x < {demo['map_km']['served_width']} km, expansion area east of it (rule W3).",
        "",
        "![Sites by vendor and technology](world_map.png)",
        "",
        "![Population density and sites](world_population.png)",
        "",
        "![Nearest-site distance by area class](world_isd.png)",
        "",
        "### Population",
        "",
        *table(
            {
                "total": demo["population"]["total"],
                "served region": demo["population"]["served"],
                "expansion area": demo["population"]["expansion"],
                **demo["population"]["settlements"],
            },
            "Item",
            "Value",
        ),
        "",
        *table(demo["population"]["served_area_km2_by_class"], "Served area class", "km2"),
        "",
        "### Sites",
        "",
        *table(
            {
                "total": demo["sites"]["total"],
                **{f"area class {k}": v for k, v in demo["sites"]["by_area_class"].items()},
                **{f"vendor {k}": v for k, v in demo["sites"]["by_vendor"].items()},
                "Huawei-style share": demo["sites"]["huawei_share"],
                **{f"technology {k}": v for k, v in demo["sites"]["by_technology"].items()},
            },
            "Item",
            "Sites",
        ),
        "",
        "### Cells",
        "",
        *table(
            {
                "total": demo["cells"]["total"],
                **{f"technology {k}": v for k, v in demo["cells"]["by_technology"].items()},
                "LTE share of cells": demo["cells"]["lte_share"],
                **{f"band {k}": v for k, v in demo["cells"]["by_band"].items()},
                **{f"class {k}": v for k, v in demo["cells"]["by_object_class"].items()},
                **{f"vendor {k}": v for k, v in demo["cells"]["by_vendor"].items()},
            },
            "Item",
            "Cells",
        ),
        "",
        "### Nearest-site distance, km",
        "",
        "| Area class | Sites | p10 | Median | p90 |",
        "|---|---|---|---|---|",
        *[
            f"| {c} | {v['sites']} | {v['p10']} | {v['median']} | {v['p90']} |"
            for c, v in demo["nearest_site_km"].items()
        ],
        "",
        "## Tiny profile",
        "",
        f"Map {tiny['map_km']['width']} x {tiny['map_km']['height']} km; "
        f"{tiny['sites']['total']} sites, {tiny['cells']['total']} cells "
        f"({', '.join(f'{k} {v}' for k, v in tiny['cells']['by_band'].items())}).",
        "",
        "## Notes",
        "",
        *[f"- {n}" for n in notes(record)],
        "",
        "## Determinism",
        "",
        "SHA-256 over sites, cells and the population raster (rule W1); a test rebuilds both",
        "profiles and compares.",
        "",
        *table({k: v["fingerprint"] for k, v in record["profiles"].items()}, "Profile", "SHA-256"),
        "",
        "## START and ASSUMPTION values",
        "",
        *table({k: json.dumps(v) for k, v in record["start_values"].items()}, "Parameter", "Value"),
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    """Write the record, the Markdown report and the figures.

    Returns:
        The process exit code.
    """
    RESULTS.mkdir(exist_ok=True)
    record = build_record()
    RECORD_JSON.write_text(json.dumps(record, indent=2) + "\n")
    RECORD_MD.write_text(render_markdown(json.loads(RECORD_JSON.read_text())))
    demo = build_world("demo")
    plot_map(demo, FIG_MAP)
    plot_population(demo, FIG_POPULATION)
    plot_isd(demo, FIG_ISD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
