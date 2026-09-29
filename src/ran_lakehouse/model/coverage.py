"""Coverage on the grid (rule M2): best server and signal quality per layer.

A layer is one band. For every point of the population raster in the
served region, and every cell of the layer that is in service, the model
computes received power from transmit power, antenna gain and path loss
(radio.py), keeps the best and second-best server, and computes SINR
against the other cells of the layer. No terrain in the served region and
no fading (rule M7).
"""

from dataclasses import dataclass

import numpy as np

from ran_lakehouse.model.cells import CellState, band_centre_mhz
from ran_lakehouse.model.radio import (
    UE_HEIGHT_M,
    antenna_gain_db,
    hata_path_loss_db,
    noise_per_re_dbm,
)
from ran_lakehouse.world import World
from ran_lakehouse.world.network import area_class_of
from ran_lakehouse.world.profiles import SUBURBAN_MIN_DENSITY, URBAN_MIN_DENSITY

# Interference is computed with every other cell of the layer loaded to this
# share of its resources (ASSUMPTION; the model has no scheduler, rule M7).
LTE_REFERENCE_LOAD = 0.5
# GSM co-channel interference: frequency reuse of 12 spreads each carrier
# over 1/12 of the neighbours (ASSUMPTION).
GSM_REUSE_FACTOR = 12.0
GSM_NOISE_DBM = -174.0 + 10.0 * np.log10(200_000.0) + 9.0  # 200 kHz carrier
# Minimum best-server level for a point to count as covered on a layer:
# LTE RSRP and GSM RxLev, dBm (ASSUMPTION).
LTE_MIN_RSRP_DBM = -120.0
GSM_MIN_RXLEV_DBM = -104.0
# A second server within this margin of the best is a handover neighbour
# (ASSUMPTION).
NEIGHBOUR_MARGIN_DB = 6.0
POINT_CHUNK = 4096


@dataclass(frozen=True)
class Grid:
    """Points of the population raster inside the served region.

    Attributes:
        x_km: Point x.
        y_km: Point y.
        persons: Persons at each point.
        area_class: Area class per point (urban, suburban, rural).
        urban_weight: Weight of the urban path-loss environment per point.
        suburban_weight: Weight of the suburban environment per point.
        raster_index: (row, col) of each point in the population raster.
    """

    x_km: np.ndarray
    y_km: np.ndarray
    persons: np.ndarray
    area_class: np.ndarray
    urban_weight: np.ndarray
    suburban_weight: np.ndarray
    raster_index: tuple[np.ndarray, np.ndarray]


@dataclass(frozen=True)
class LayerCoverage:
    """Best server and signal quality of one layer at every grid point.

    Attributes:
        band: Band of the layer.
        technology: "LTE" or "GSM".
        best: Global cell index of the best server, -1 where not covered.
        best_level_dbm: RSRP (LTE) or RxLev (GSM) of the best server.
        second: Global cell index of the second server, -1 if none.
        second_level_dbm: Level of the second server.
        sinr_db: Signal to interference plus noise ratio.
        distance_km: Distance to the best server.
    """

    band: str
    technology: str
    best: np.ndarray
    best_level_dbm: np.ndarray
    second: np.ndarray
    second_level_dbm: np.ndarray
    sinr_db: np.ndarray
    distance_km: np.ndarray


def environment_weights(density: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Urban and suburban path-loss weights from smoothed density.

    The urban weight rises linearly from the suburban to the urban density
    threshold (rule W class thresholds); the suburban weight rises from
    half the suburban threshold to the suburban threshold, then gives way
    to urban (ASSUMPTION).

    Args:
        density: Smoothed density, persons per km2.

    Returns:
        Urban weight and suburban weight per point.
    """
    urban = np.clip(
        (density - SUBURBAN_MIN_DENSITY) / (URBAN_MIN_DENSITY - SUBURBAN_MIN_DENSITY), 0.0, 1.0
    )
    low = SUBURBAN_MIN_DENSITY / 2.0
    built = np.clip((density - low) / (SUBURBAN_MIN_DENSITY - low), 0.0, 1.0)
    return urban, built * (1.0 - urban)


def build_grid(world: World) -> Grid:
    """Points of the served region with their persons and area class.

    Args:
        world: The world.

    Returns:
        The grid.
    """
    pop = world.population
    ny = pop.persons.shape[0]
    cols = round(world.profile.served_width_km / pop.raster_km)
    row_grid, col_grid = np.meshgrid(np.arange(ny), np.arange(cols), indexing="ij")
    rows, cs = row_grid.ravel(), col_grid.ravel()
    urban_w, suburban_w = environment_weights(pop.class_density[rows, cs])
    return Grid(
        x_km=(cs + 0.5) * pop.raster_km,
        y_km=(rows + 0.5) * pop.raster_km,
        persons=pop.persons[rows, cs],
        area_class=area_class_of(pop.class_density[rows, cs]),
        urban_weight=urban_w,
        suburban_weight=suburban_w,
        raster_index=(rows, cs),
    )


def received_dbm(
    state: CellState, cells: np.ndarray, grid: Grid, points: slice, band: str
) -> tuple[np.ndarray, np.ndarray]:
    """Received level from a set of cells at a chunk of points.

    Args:
        state: Cell parameters.
        cells: Global indices of the cells.
        grid: The grid.
        points: Chunk of grid points.
        band: Band of the cells.

    Returns:
        Level per (point, cell) in dBm (RSRP per resource element for LTE,
        carrier level for GSM) and distance per (point, cell) in km.
    """
    px = grid.x_km[points][:, None]
    py = grid.y_km[points][:, None]
    dx = px - state.x_km[cells][None, :]
    dy = py - state.y_km[cells][None, :]
    d_km = np.hypot(dx, dy)
    bearing = np.degrees(np.arctan2(dx, dy))
    off_azimuth = bearing - state.azimuth_deg[cells][None, :]
    height = state.height_m[cells][None, :]
    elevation = np.degrees(np.arctan2(height - UE_HEIGHT_M, np.maximum(d_km, 0.001) * 1000.0))
    off_tilt = elevation - state.tilt_deg[cells][None, :]
    gain = antenna_gain_db(off_azimuth, off_tilt)
    loss = hata_path_loss_db(
        band_centre_mhz(band),
        d_km,
        height,
        grid.urban_weight[points][:, None],
        grid.suburban_weight[points][:, None],
    )
    power = state.power_dbm[cells][None, :]
    if state.technology[cells[0]] == "LTE":
        # RSRP: total power spread evenly over 12 subcarriers per resource
        # block (TS 36.214 V19.0.0 clause 5.1.1 defines RSRP per resource
        # element; equal power per element is ASSUMPTION).
        power = power - 10.0 * np.log10(12.0 * state.n_rb[cells][None, :])
    return power + gain - loss, d_km


def layer_coverage(state: CellState, grid: Grid, band: str) -> LayerCoverage:
    """Best server and SINR of one band at every grid point.

    Args:
        state: Cell parameters.
        grid: The grid.
        band: The band.

    Returns:
        The layer coverage.

    Raises:
        ValueError: If the band has no cell in service.
    """
    cells = np.flatnonzero((state.band == band) & ~state.down)
    if cells.size == 0:
        raise ValueError(f"band {band} has no cell in service")
    technology = str(state.technology[cells[0]])
    n = grid.x_km.size
    best = np.full(n, -1)
    second = np.full(n, -1)
    best_level = np.full(n, -np.inf)
    second_level = np.full(n, -np.inf)
    sinr = np.full(n, -np.inf)
    distance = np.full(n, np.nan)
    for start in range(0, n, POINT_CHUNK):
        chunk = slice(start, min(start + POINT_CHUNK, n))
        level, d_km = received_dbm(state, cells, grid, chunk, band)
        # Stable sort: exact ties (co-sited sectors at the antenna floor) go to
        # the lowest cell index on every CPU.
        order = np.argsort(-level, axis=1, kind="stable")
        top = order[:, 0]
        rows = np.arange(level.shape[0])
        s_dbm = level[rows, top]
        linear = 10.0 ** (level / 10.0)
        others = linear.sum(axis=1) - linear[rows, top]
        if technology == "LTE":
            interference = LTE_REFERENCE_LOAD * others
            noise = 10.0 ** (noise_per_re_dbm() / 10.0)
            minimum = LTE_MIN_RSRP_DBM
        else:
            interference = others / GSM_REUSE_FACTOR
            noise = 10.0 ** (GSM_NOISE_DBM / 10.0)
            minimum = GSM_MIN_RXLEV_DBM
        covered = s_dbm >= minimum
        best[chunk] = np.where(covered, cells[top], -1)
        best_level[chunk] = s_dbm
        sinr[chunk] = 10.0 * np.log10(10.0 ** (s_dbm / 10.0) / (interference + noise))
        distance[chunk] = d_km[rows, top]
        if cells.size > 1:
            runner = order[:, 1]
            second[chunk] = np.where(covered, cells[runner], -1)
            second_level[chunk] = level[rows, runner]
    return LayerCoverage(
        band=band,
        technology=technology,
        best=best,
        best_level_dbm=best_level,
        second=second,
        second_level_dbm=second_level,
        sinr_db=sinr,
        distance_km=distance,
    )


def all_layers(state: CellState, grid: Grid) -> dict[str, LayerCoverage]:
    """Coverage of every band that has cells in service.

    Args:
        state: Cell parameters.
        grid: The grid.

    Returns:
        Band to layer coverage.
    """
    bands = sorted({str(b) for b, down in zip(state.band, state.down, strict=True) if not down})
    return {b: layer_coverage(state, grid, b) for b in bands}
