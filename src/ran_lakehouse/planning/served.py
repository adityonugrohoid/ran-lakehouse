"""What the served network delivers at the expansion villages today (rule G3).

The served cells' levels at each village come from the model (rule M: the
cells' powers, heights, antenna patterns and Hata for open areas) with the
same Bullington diffraction over the terrain profile that candidate sites
get (planning.radio); west of the expansion area the profile continues at
the area's edge heights (planning.terrain). Only cells within
REACH_KM of the served region's edge can matter.
"""

import numpy as np

from ran_lakehouse.model import NetworkModel
from ran_lakehouse.model.cells import band_centre_mhz
from ran_lakehouse.model.coverage import Grid, received_dbm
from ran_lakehouse.model.radio import UE_HEIGHT_M
from ran_lakehouse.planning.radio import PROFILE_SAMPLES, bullington_db
from ran_lakehouse.planning.terrain import Terrain

REACH_KM = 25.0  # served cells farther from the edge are left out (ASSUMPTION)


def served_levels(
    model: NetworkModel, terrain: Terrain, x_km: np.ndarray, y_km: np.ndarray
) -> dict[str, np.ndarray]:
    """Best served LTE RSRP and GSM RxLev at points in the expansion area.

    Args:
        model: The network as built.
        terrain: The expansion terrain.
        x_km: Point x.
        y_km: Point y.

    Returns:
        "LTE" and "GSM": best level per point, dBm (-inf where no cell of
        that technology is within reach).
    """
    state = model.state
    n = x_km.size
    zeros = np.zeros(n)
    grid = Grid(
        x_km,
        y_km,
        zeros,
        np.array(["rural"] * n),
        zeros,
        zeros,
        (zeros.astype(int), zeros.astype(int)),
    )
    best = {"LTE": np.full(n, -np.inf), "GSM": np.full(n, -np.inf)}
    t = np.linspace(0.0, 1.0, PROFILE_SAMPLES)
    edge = model.world.profile.served_width_km
    for band in np.unique(state.band):
        cells = np.flatnonzero((state.band == band) & ~state.down)
        cells = cells[state.x_km[cells] > edge - REACH_KM]
        if cells.size == 0:
            continue
        level, distance = received_dbm(state, cells, grid, np.arange(n), str(band))
        sx = np.broadcast_to(state.x_km[cells][None, :], level.shape).ravel()
        sy = np.broadcast_to(state.y_km[cells][None, :], level.shape).ravel()
        px = np.repeat(x_km, cells.size)
        py = np.repeat(y_km, cells.size)
        ground = terrain.height_at(
            sx[:, None] + t * (px - sx)[:, None], sy[:, None] + t * (py - sy)[:, None]
        )
        heights = np.broadcast_to(state.height_m[cells][None, :], level.shape).ravel()
        loss = bullington_db(
            np.maximum(distance.ravel(), 0.02),
            ground,
            ground[:, 0] + heights,
            ground[:, -1] + UE_HEIGHT_M,
            band_centre_mhz(str(band)),
        )
        technology = str(state.technology[cells[0]])
        best[technology] = np.maximum(
            best[technology], (level.ravel() - loss).reshape(level.shape).max(axis=1)
        )
    return best
