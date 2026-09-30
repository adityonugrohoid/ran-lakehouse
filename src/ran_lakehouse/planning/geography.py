"""Villages, utility lines and candidate sites of the expansion area (rules G3, G4).

Villages are the world's expansion villages (rule G1). The power grid and
the fiber route are synthetic lines from the served region's edge into the
expansion area. Candidate sites sit on local high points of the terrain
near villages, chosen greedily by the population around them.
"""

from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from ran_lakehouse.planning.terrain import Terrain
from ran_lakehouse.seeds import Purpose, rng
from ran_lakehouse.world import World

PERSONS_PER_SCHOOL = 1_500  # START
SCHOOL_FROM_PERSONS = 500  # a village above this size has at least one school (START)
# Utility lines: start at the served edge, then 1 km segments whose heading
# wanders (START).
LINE_SEGMENT_KM = 1.0
LINE_TURN_SD_DEG = 15.0
GRID_LINE_KM = 14.0  # power grid reach into the expansion area (START)
FIBER_ROUTE_KM = 9.0  # fiber reach (START)
# Candidate sites (START): a node is a local high point when it is the
# highest within HIGH_POINT_RADIUS_KM; a candidate needs a village within
# NEAR_VILLAGE_KM, candidates keep MIN_SPACING_KM apart, and they are taken
# greedily by the persons in villages within SCORE_RADIUS_KM.
CANDIDATES = 60
HIGH_POINT_RADIUS_KM = 0.5
NEAR_VILLAGE_KM = 3.0
MIN_SPACING_KM = 2.0
SCORE_RADIUS_KM = 5.0
# Build cost, IDR (ASSUMPTION): tower, civil works and radio equipment, plus
# an access road per km from the nearest village.
BUILD_BASE_IDR = 1_800_000_000
ACCESS_ROAD_IDR_PER_KM = 120_000_000


@dataclass(frozen=True)
class Village:
    """One village of the expansion area (rule G3).

    Attributes:
        village_id: Stable id.
        x_km: Centre x.
        y_km: Centre y.
        population: Persons.
        schools: Schools (START rule).
        elevation_m: Ground height at the centre.
    """

    village_id: int
    x_km: float
    y_km: float
    population: int
    schools: int
    elevation_m: float


@dataclass(frozen=True)
class Candidate:
    """One candidate site (rule G4); grid and fiber distances are added later.

    Attributes:
        site_id: Stable id, "C01" on.
        x_km: x.
        y_km: y.
        elevation_m: Ground height.
        nearest_village_km: Distance to the nearest village.
        persons_nearby: Persons in villages within SCORE_RADIUS_KM.
        build_cost_idr: Build cost (ASSUMPTION).
    """

    site_id: str
    x_km: float
    y_km: float
    elevation_m: float
    nearest_village_km: float
    persons_nearby: int
    build_cost_idr: int


def schools(population: int) -> int:
    """Schools of a village (START: one per PERSONS_PER_SCHOOL, at least one above 500).

    Args:
        population: Persons.

    Returns:
        Schools.
    """
    count = round(population / PERSONS_PER_SCHOOL)
    return max(count, 1) if population > SCHOOL_FROM_PERSONS else count


def villages(world: World, terrain: Terrain) -> list[Village]:
    """The expansion villages.

    Args:
        world: The world.
        terrain: The expansion terrain.

    Returns:
        Villages in id order.
    """
    out = []
    for s in world.population.settlements:
        if s.area != "expansion":
            continue
        out.append(
            Village(
                village_id=s.settlement_id,
                x_km=s.x_km,
                y_km=s.y_km,
                population=s.population,
                schools=schools(s.population),
                elevation_m=float(terrain.height_at(np.array(s.x_km), np.array(s.y_km))),
            )
        )
    return sorted(out, key=lambda v: v.village_id)


def utility_line(world: World, entity: int, length_km: float) -> np.ndarray:
    """A line from the served edge into the expansion area.

    Args:
        world: The world.
        entity: Seed entity (0 grid, 1 fiber).
        length_km: Length.

    Returns:
        Vertices (x, y) in km, shape (n, 2).
    """
    p = world.profile
    stream = rng(Purpose.PLANNING_LINES, entity)
    x, y = p.served_width_km, float(stream.uniform(0.3, 0.7) * p.height_km)
    heading = 90.0  # due east
    points = [(x, y)]
    for _ in range(round(length_km / LINE_SEGMENT_KM)):
        heading += float(stream.normal(0.0, LINE_TURN_SD_DEG))
        heading = float(np.clip(heading, 45.0, 135.0))
        x += LINE_SEGMENT_KM * float(np.sin(np.radians(heading)))
        y += LINE_SEGMENT_KM * float(np.cos(np.radians(heading)))
        y = float(np.clip(y, 0.5, p.height_km - 0.5))
        points.append((x, y))
    return np.array(points)


def distance_to_line(x_km: np.ndarray, y_km: np.ndarray, line: np.ndarray) -> np.ndarray:
    """Distance from points to a polyline.

    Args:
        x_km: Point x.
        y_km: Point y (same shape).
        line: Vertices, shape (n, 2).

    Returns:
        Distances, km.
    """
    px, py = np.asarray(x_km)[..., None], np.asarray(y_km)[..., None]
    ax, ay = line[:-1, 0], line[:-1, 1]
    bx, by = line[1:, 0], line[1:, 1]
    dx, dy = bx - ax, by - ay
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy), 0.0, 1.0)
    result: np.ndarray = np.hypot(px - (ax + t * dx), py - (ay + t * dy)).min(axis=-1)
    return result


def high_points(terrain: Terrain) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Nodes that are the highest within HIGH_POINT_RADIUS_KM.

    Args:
        terrain: The terrain.

    Returns:
        x, y and height of every local high point.
    """
    r = round(HIGH_POINT_RADIUS_KM / terrain.step_km)
    h = terrain.heights_m
    padded = np.pad(h, r, mode="edge")
    window_max = sliding_window_view(padded, (2 * r + 1, 2 * r + 1)).max(axis=(2, 3))
    rows, cols = np.nonzero(h >= window_max)
    return (
        terrain.x0_km + cols * terrain.step_km,
        terrain.y0_km + rows * terrain.step_km,
        h[rows, cols],
    )


def candidates(terrain: Terrain, village_list: list[Village]) -> list[Candidate]:
    """Candidate sites on local high points near villages.

    Args:
        terrain: The terrain.
        village_list: The villages.

    Returns:
        Up to CANDIDATES sites, the best-scored first.
    """
    x, y, h = high_points(terrain)
    vx = np.array([v.x_km for v in village_list])
    vy = np.array([v.y_km for v in village_list])
    vp = np.array([v.population for v in village_list])
    d = np.hypot(x[:, None] - vx[None, :], y[:, None] - vy[None, :])
    nearest = d.min(axis=1)
    score = (vp[None, :] * (d <= SCORE_RADIUS_KM)).sum(axis=1)
    order = [i for i in np.argsort(-score, kind="stable") if nearest[i] <= NEAR_VILLAGE_KM]
    chosen: list[int] = []
    for i in order:
        if all(np.hypot(x[i] - x[j], y[i] - y[j]) >= MIN_SPACING_KM for j in chosen):
            chosen.append(int(i))
        if len(chosen) == CANDIDATES:
            break
    return [
        Candidate(
            site_id=f"C{k + 1:02d}",
            x_km=round(float(x[i]), 3),
            y_km=round(float(y[i]), 3),
            elevation_m=round(float(h[i]), 1),
            nearest_village_km=round(float(nearest[i]), 3),
            persons_nearby=int(score[i]),
            build_cost_idr=BUILD_BASE_IDR + round(ACCESS_ROAD_IDR_PER_KM * float(nearest[i])),
        )
        for k, i in enumerate(chosen)
    ]
