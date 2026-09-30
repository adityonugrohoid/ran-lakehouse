"""Backhaul and power options per candidate site (rule G6).

- fiber: a spur from the synthetic fiber route, priced per km;
- microwave: one hop to a hub, a served site within HUB_ZONE_KM of the
  served region's edge, nearest first, the first with line of sight that
  clears the first Fresnel zone as radio.line_of_sight checks;
- satellite: always available, SATELLITE_MBPS, a terminal and a monthly fee;
- power: the grid when the synthetic grid line is within GRID_POWER_KM
  (a line extension priced per km), else a solar and battery system.

All costs are IDR, ASSUMPTION; distances and caps are START.
"""

from dataclasses import dataclass

import numpy as np

from ran_lakehouse.planning.geography import Candidate, distance_to_line
from ran_lakehouse.planning.radio import (
    FRESNEL_CLEARANCE,
    FULL_CLEARANCE,
    MAST_HEIGHT_M,
    MICROWAVE_GHZ,
    line_of_sight,
)
from ran_lakehouse.planning.terrain import Terrain
from ran_lakehouse.world import World

HUB_ZONE_KM = 4.0  # served sites this close to the edge can be hubs (START)
HUB_ANTENNA_M = 40.0  # the served rural antenna height (rule M)
MAX_HOP_KM = 30.0  # longest microwave hop considered (START)
MICROWAVE_MBPS = 400.0  # one 8 GHz hop, 56 MHz channel (ASSUMPTION)
FIBER_MBPS = 10_000.0  # ASSUMPTION
SATELLITE_MBPS = 8.0  # START
GRID_POWER_KM = 5.0  # START
FIBER_IDR_PER_KM = 150_000_000
MICROWAVE_LINK_IDR = 350_000_000
SATELLITE_TERMINAL_IDR = 150_000_000
SATELLITE_MONTHLY_IDR = 25_000_000
GRID_LINE_IDR_PER_KM = 250_000_000
SOLAR_IDR = 650_000_000


@dataclass(frozen=True)
class Option:
    """One backhaul or power option of a candidate site.

    Attributes:
        site_id: Candidate site.
        kind: "fiber", "microwave", "satellite", "grid_power" or "solar_power".
        available: Whether the option can be used.
        distance_km: Spur, hop or line length (0 where none).
        capacity_mbps: Backhaul capacity (0 for power).
        capex_idr: One-time cost.
        monthly_idr: Recurring cost.
        detail: Hub name for microwave, why an option is unavailable.
        clears_full_fresnel: Microwave only: a hub within reach also clears
            the whole first Fresnel zone (FULL_CLEARANCE); False otherwise.
    """

    site_id: str
    kind: str
    available: bool
    distance_km: float
    capacity_mbps: float
    capex_idr: int
    monthly_idr: int
    detail: str
    clears_full_fresnel: bool


def hubs(world: World) -> list[tuple[str, float, float]]:
    """Served sites near the served region's edge.

    Args:
        world: The world.

    Returns:
        (name, x_km, y_km) per hub.
    """
    edge = world.profile.served_width_km
    return [(s.name, s.x_km, s.y_km) for s in world.sites if s.x_km >= edge - HUB_ZONE_KM]


def microwave(
    terrain: Terrain, site: Candidate, hub_list: list[tuple[str, float, float]], clearance: float
) -> tuple[str, float] | None:
    """The nearest hub with a clear microwave path.

    Args:
        terrain: The terrain.
        site: The candidate.
        hub_list: Hubs.
        clearance: Fraction of the first Fresnel zone to keep clear.

    Returns:
        (hub name, hop km), or None when no hub within MAX_HOP_KM is clear.
    """
    by_distance = sorted(hub_list, key=lambda h: np.hypot(h[1] - site.x_km, h[2] - site.y_km))
    for name, x, y in by_distance:
        hop = float(np.hypot(x - site.x_km, y - site.y_km))
        if hop > MAX_HOP_KM:
            return None
        clear, _ = line_of_sight(
            terrain,
            (site.x_km, site.y_km, MAST_HEIGHT_M),
            (x, y, HUB_ANTENNA_M),
            MICROWAVE_GHZ,
            clearance,
        )
        if clear:
            return name, hop
    return None


def options(
    terrain: Terrain,
    world: World,
    sites: list[Candidate],
    grid_line: np.ndarray,
    fiber_route: np.ndarray,
) -> list[Option]:
    """Every backhaul and power option of every candidate.

    Args:
        terrain: The terrain.
        world: The world (hubs).
        sites: Candidates.
        grid_line: Power grid vertices.
        fiber_route: Fiber route vertices.

    Returns:
        Five options per site.
    """
    hub_list = hubs(world)
    out = []
    for s in sites:
        fiber_km = float(distance_to_line(np.array(s.x_km), np.array(s.y_km), fiber_route))
        grid_km = float(distance_to_line(np.array(s.x_km), np.array(s.y_km), grid_line))
        hop = microwave(terrain, s, hub_list, FRESNEL_CLEARANCE)
        strict = microwave(terrain, s, hub_list, FULL_CLEARANCE)
        on_grid = grid_km <= GRID_POWER_KM
        out += [
            Option(
                s.site_id,
                "fiber",
                True,
                round(fiber_km, 3),
                FIBER_MBPS,
                round(FIBER_IDR_PER_KM * fiber_km),
                0,
                "",
                False,
            ),
            Option(
                s.site_id,
                "microwave",
                hop is not None,
                round(hop[1], 3) if hop else 0.0,
                MICROWAVE_MBPS if hop else 0.0,
                MICROWAVE_LINK_IDR if hop else 0,
                0,
                hop[0] if hop else f"no hub within {MAX_HOP_KM:g} km clears the Fresnel zone",
                strict is not None,
            ),
            Option(
                s.site_id,
                "satellite",
                True,
                0.0,
                SATELLITE_MBPS,
                SATELLITE_TERMINAL_IDR,
                SATELLITE_MONTHLY_IDR,
                "",
                False,
            ),
            Option(
                s.site_id,
                "grid_power",
                on_grid,
                round(grid_km, 3),
                0.0,
                round(GRID_LINE_IDR_PER_KM * grid_km) if on_grid else 0,
                0,
                "" if on_grid else f"grid line {grid_km:.1f} km away, beyond {GRID_POWER_KM:g} km",
                False,
            ),
            Option(
                s.site_id,
                "solar_power",
                not on_grid,
                0.0,
                0.0,
                SOLAR_IDR if not on_grid else 0,
                0,
                "" if not on_grid else "grid power within reach",
                False,
            ),
        ]
    return out
