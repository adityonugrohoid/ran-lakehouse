"""Who each cell serves (rule M3): subscribers, their radio conditions and
the neighbour relations that follow from coverage.

Subscribers come from the population layer (rule W4); at each grid point
they split over the LTE layers in proportion to layer bandwidth, each layer
weighted by how far its RSRP sits inside the margin of the strongest layer
and above the layer floor, and within a layer between the best and the
second server by their difference in level plus offset. Both weights are
logistic, not steps (rule M3): users' signal varies inside a grid point, so
a small change moves users gradually. GSM voice users split over the GSM
best and second server the same way. Per cell, the model keeps the
user-weighted radio statistics the counters need (rule M4).
"""

from dataclasses import dataclass

import numpy as np

from ran_lakehouse.model.cells import CellState
from ran_lakehouse.model.coverage import (
    GSM_MIN_RXLEV_DBM,
    LTE_MIN_RSRP_DBM,
    NEIGHBOUR_MARGIN_DB,
    Grid,
    LayerCoverage,
)
from ran_lakehouse.model.radio import TA_STEP_M, cqi_index, spectral_efficiency
from ran_lakehouse.world.network import AREA_CLASSES

# Mobile data subscribers per person (ASSUMPTION).
LTE_SUBSCRIBERS_PER_PERSON = 0.8
# GSM voice users per person, where GSM is present (ASSUMPTION: 2G-only
# handsets and voice fallback).
GSM_USERS_PER_PERSON = 0.1
# A layer's weight at a point falls as its best RSRP drops below this floor
# (ASSUMPTION, START) ...
LAYER_MIN_RSRP_DBM = -110.0
# ... and as it drops out of this margin of the point's strongest LTE layer,
# so a sparse capacity layer serves mostly near its own sites (ASSUMPTION,
# START, in the manner of an inter-frequency reselection threshold).
LAYER_MARGIN_DB = 8.0
# Scale of the logistic that softens both thresholds, and of the one that
# splits a layer's users between its best and second server (rule M3,
# START: the spread of users' signal inside a grid point).
LAYER_SPREAD_DB = 3.0
SERVER_SPREAD_DB = 3.0
# Users below this SINR count as cell-edge users (ASSUMPTION).
LTE_EDGE_SINR_DB = 0.0
# GSM users below this carrier-to-interference-plus-noise count as edge
# users (ASSUMPTION).
GSM_EDGE_CI_DB = 9.0
# Timing advance histogram edges in TA steps (modelled on vendor TA
# distance counters; the 3GPP CARR.TADist counts TA command values instead).
TA_BIN_EDGES_STEPS = (0, 1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 1283)


@dataclass(frozen=True)
class Serving:
    """What each cell serves, one entry per cell (global cell order).

    Attributes:
        subscribers: LTE subscribers (LTE cells) or GSM voice users (GSM
            cells) whose best server this is.
        subscribers_by_class: The same split by the area class of the point
            they live at, shape (cells, 3) in AREA_CLASSES order.
        spectral_efficiency: User-weighted mean DL spectral efficiency,
            bit/s/Hz (LTE; 0 for GSM).
        edge_share: Share of served users at the cell edge.
        mean_sinr_db: User-weighted mean SINR (or C/I for GSM).
        cqi_share: Share of served users per CQI 0-15, shape (cells, 16).
        ta_share: Share of served users per TA bin, shape (cells, bins).
        relations: (source, target, weight) of neighbour relations; weight
            is the user mass in the handover overlap.
        unserved_persons: Persons at points no LTE layer covers.
    """

    subscribers: np.ndarray
    subscribers_by_class: np.ndarray
    spectral_efficiency: np.ndarray
    edge_share: np.ndarray
    mean_sinr_db: np.ndarray
    cqi_share: np.ndarray
    ta_share: np.ndarray
    relations: tuple[np.ndarray, np.ndarray, np.ndarray]
    unserved_persons: float


def logistic(x: np.ndarray) -> np.ndarray:
    """The logistic function, without overflow for large arguments.

    Args:
        x: Arguments.

    Returns:
        1 / (1 + exp(-x)).
    """
    out: np.ndarray = 0.5 * (1.0 + np.tanh(0.5 * x))
    return out


def best_share(state: CellState, layer: LayerCoverage) -> np.ndarray:
    """Share of a point's users on its best server; the rest go to the second.

    Args:
        state: Cell parameters (the cell individual offsets).
        layer: Layer coverage.

    Returns:
        Share per point: 1 where there is no second server that covers the
        point, else a logistic in the ranked level difference (level plus
        offset), at least 0.5.
    """
    minimum = LTE_MIN_RSRP_DBM if layer.technology == "LTE" else GSM_MIN_RXLEV_DBM
    has_second = (layer.best >= 0) & (layer.second >= 0) & (layer.second_level_dbm >= minimum)
    best = np.where(layer.best >= 0, layer.best, 0)
    second = np.where(layer.second >= 0, layer.second, 0)
    gap = (layer.best_level_dbm + state.cio_db[best]) - (
        layer.second_level_dbm + state.cio_db[second]
    )
    gap = np.where(has_second, gap, 0.0)
    share: np.ndarray = np.where(has_second, logistic(gap / SERVER_SPREAD_DB), 1.0)
    return share


def lte_layer_weights(layers: list[LayerCoverage], bandwidth: dict[str, float]) -> np.ndarray:
    """Share of each point's LTE users on each layer.

    Args:
        layers: LTE layer coverages.
        bandwidth: Band to bandwidth, MHz.

    Returns:
        Weights, shape (layers, points); columns sum to 1 where covered.
    """
    level = np.array([layer.best_level_dbm for layer in layers])
    covered = np.array([layer.best >= 0 for layer in layers])
    width = np.array([bandwidth[layer.band] for layer in layers])[:, None]
    strongest = np.where(covered, level, -np.inf).max(axis=0)
    finite = np.where(covered, level, LTE_MIN_RSRP_DBM)
    inside = logistic((finite - (strongest - LAYER_MARGIN_DB)) / LAYER_SPREAD_DB)
    above = logistic((finite - LAYER_MIN_RSRP_DBM) / LAYER_SPREAD_DB)
    raw = np.where(covered, width * inside * above, 0.0)
    total = raw.sum(axis=0)
    weights: np.ndarray = np.divide(raw, total, out=np.zeros_like(raw), where=total > 0)
    return weights


def build_serving(
    state: CellState, grid: Grid, coverage: dict[str, LayerCoverage], persons: np.ndarray
) -> Serving:
    """Assign users to cells and summarize each cell's radio conditions.

    Args:
        state: Cell parameters.
        grid: The grid.
        coverage: Band to layer coverage.
        persons: Persons per grid point (the grid's, or a changed layer).

    Returns:
        The serving summary.
    """
    n_cells = len(state.cell_names)
    subs = np.zeros(n_cells)
    by_class = np.zeros((n_cells, len(AREA_CLASSES)))
    class_column = np.array([AREA_CLASSES.index(str(c)) for c in grid.area_class])
    se_mass = np.zeros(n_cells)
    edge_mass = np.zeros(n_cells)
    sinr_mass = np.zeros(n_cells)
    n_ta = len(TA_BIN_EDGES_STEPS) - 1
    cqi = np.zeros((n_cells, 16))
    ta = np.zeros((n_cells, n_ta))
    sources, targets, weights = [], [], []

    lte = [c for c in coverage.values() if c.technology == "LTE"]
    bandwidth = {
        str(b): float(w) for b, w in zip(state.band, state.bandwidth_mhz, strict=True) if w
    }
    layer_share = lte_layer_weights(lte, bandwidth)
    unserved = float(persons[layer_share.sum(axis=0) == 0].sum())

    plans: list[tuple[LayerCoverage, np.ndarray]] = [
        (layer, persons * LTE_SUBSCRIBERS_PER_PERSON * share)
        for layer, share in zip(lte, layer_share, strict=True)
    ]
    plans += [
        (layer, persons * GSM_USERS_PER_PERSON * (layer.best >= 0))
        for layer in coverage.values()
        if layer.technology == "GSM"
    ]
    # GSM users split over GSM layers where two GSM bands overlap.
    gsm_layers = [layer for layer in coverage.values() if layer.technology == "GSM"]
    if len(gsm_layers) > 1:
        present = np.array([layer.best >= 0 for layer in gsm_layers]).sum(axis=0)
        plans = [
            (layer, users / np.maximum(present, 1) if layer.technology == "GSM" else users)
            for layer, users in plans
        ]

    for layer, users in plans:
        served = (layer.best >= 0) & (users > 0)
        share = best_share(state, layer)
        to_second = served & (share < 1.0)
        for mask, cells_at, sinr_at, distance_at, part in (
            (served, layer.best, layer.sinr_db, layer.distance_km, share),
            # A user on the second server has it stronger at its own
            # position, so it keeps the point's radio quality (ASSUMPTION);
            # only the serving cell and its distance change.
            (to_second, layer.second, layer.sinr_db, layer.second_distance_km, 1.0 - share),
        ):
            cell = cells_at[mask]
            u = (users * part)[mask]
            sinr = sinr_at[mask]
            subs += np.bincount(cell, u, n_cells)
            np.add.at(by_class, (cell, class_column[mask]), u)
            sinr_mass += np.bincount(cell, u * sinr, n_cells)
            if layer.technology == "LTE":
                se_mass += np.bincount(cell, u * spectral_efficiency(sinr), n_cells)
                edge_mass += np.bincount(cell, u * (sinr < LTE_EDGE_SINR_DB), n_cells)
                np.add.at(cqi, (cell, cqi_index(sinr)), u)
            else:
                edge_mass += np.bincount(cell, u * (sinr < GSM_EDGE_CI_DB), n_cells)
            steps = distance_at[mask] * 1000.0 / TA_STEP_M
            ta_bin = np.clip(
                np.searchsorted(TA_BIN_EDGES_STEPS, steps, side="right") - 1, 0, n_ta - 1
            )
            np.add.at(ta, (cell, ta_bin), u)
        overlap = served & (layer.second >= 0)
        overlap &= layer.second_level_dbm >= layer.best_level_dbm - NEIGHBOUR_MARGIN_DB
        sources.append(layer.best[overlap])
        targets.append(layer.second[overlap])
        weights.append(users[overlap])

    safe = np.maximum(subs, 1e-9)
    src = np.concatenate(sources)
    tgt = np.concatenate(targets)
    wgt = np.concatenate(weights)
    pair = src * n_cells + tgt
    keys, inverse = np.unique(pair, return_inverse=True)
    mass = np.bincount(inverse, wgt)
    return Serving(
        subscribers=subs,
        subscribers_by_class=by_class,
        spectral_efficiency=se_mass / safe,
        edge_share=edge_mass / safe,
        mean_sinr_db=sinr_mass / safe,
        cqi_share=cqi / safe[:, None],
        ta_share=ta / safe[:, None],
        relations=(keys // n_cells, keys % n_cells, mass),
        unserved_persons=unserved,
    )
