"""Cell state (rule M1): the parameters the network model reads and a
what-if or a planted fault changes.

Values here are ASSUMPTION unless cited.
"""

from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from ran_lakehouse.model.radio import N_RB
from ran_lakehouse.world import World
from ran_lakehouse.world.network import BANDS

# Antenna height per area class, m (ASSUMPTION; kept inside Hata's 30-200 m).
ANTENNA_HEIGHT_M = {"urban": 30.0, "suburban": 32.0, "rural": 40.0}
# Downtilt per area class, degrees (ASSUMPTION: typical macro tilts, deeper
# for smaller cells; TR 36.814 V9.2.0 uses 6 deg for its 1732 m case).
ELECTRICAL_TILT_DEG = {"urban": 8.0, "suburban": 6.0, "rural": 4.0}
# Channel bandwidth per LTE band, MHz (ASSUMPTION for this synthetic
# operator's spectrum holdings).
BANDWIDTH_MHZ = {"B28": 10.0, "B8": 10.0, "B3": 20.0, "B1": 15.0, "B40": 20.0}
# LTE total transmit power: 46 dBm at 10 MHz and 49 dBm at 20 MHz, TR 36.814
# V9.2.0 Table A.2.1.1-2; other bandwidths at the same power spectral
# density (ASSUMPTION).
LTE_POWER_DBM_AT_10MHZ = 46.0
# GSM BCCH carrier power, dBm (ASSUMPTION).
GSM_POWER_DBM = 43.0
# GSM transceivers are dimensioned per cell for this grade of service at
# the busy hour, within these bounds (ASSUMPTION: the operator dimensioned
# its 2G layer with Erlang B).
GSM_GRADE_OF_SERVICE = 0.02
GSM_TRX_MIN = 1
GSM_TRX_MAX = 12
# TDD B40 downlink share of the frame (ASSUMPTION, about UL/DL
# configuration 2 with special subframes).
TDD_DL_SHARE = 0.6


@dataclass(frozen=True)
class CellState:
    """Parameters of every cell, one array entry per cell (rule M1).

    Attributes:
        cell_names: Cell names, in world order.
        site_ids: Owning site.
        x_km: Site x.
        y_km: Site y.
        azimuth_deg: Sector azimuth.
        height_m: Antenna height.
        tilt_deg: Electrical tilt.
        power_dbm: Total transmit power (LTE) or BCCH power (GSM).
        band: Band name.
        technology: "LTE" or "GSM".
        vendor: "huawei" or "nokia".
        area_class: Area class of the site.
        bandwidth_mhz: LTE channel bandwidth; 0 for GSM.
        n_rb: LTE resource blocks; 0 for GSM.
        trx: GSM transceivers (dimensioned by the model); 0 for LTE.
        cio_db: Cell individual offset for handover (0 by default).
        down: True when the cell is out of service (rule F1f).
    """

    cell_names: tuple[str, ...]
    site_ids: np.ndarray
    x_km: np.ndarray
    y_km: np.ndarray
    azimuth_deg: np.ndarray
    height_m: np.ndarray
    tilt_deg: np.ndarray
    power_dbm: np.ndarray
    band: np.ndarray
    technology: np.ndarray
    vendor: np.ndarray
    area_class: np.ndarray
    bandwidth_mhz: np.ndarray
    n_rb: np.ndarray
    trx: np.ndarray
    cio_db: np.ndarray
    down: np.ndarray

    def with_values(self, **changes: Any) -> "CellState":
        """Return a copy with some parameter arrays replaced.

        Args:
            **changes: Field name to new value (an array per cell).

        Returns:
            The changed state.
        """
        return replace(self, **changes)


def lte_power_dbm(bandwidth_mhz: float) -> float:
    """LTE total power at the TR 36.814 power spectral density.

    Args:
        bandwidth_mhz: Channel bandwidth, MHz.

    Returns:
        Total power, dBm.
    """
    return LTE_POWER_DBM_AT_10MHZ + 10.0 * float(np.log10(bandwidth_mhz / 10.0))


def build_cell_state(world: World) -> CellState:
    """Default cell parameters for a world.

    Args:
        world: The world.

    Returns:
        The cell state, in the world's cell order.
    """
    site = {s.site_id: s for s in world.sites}
    cells = world.cells
    area = np.array([site[c.site_id].area_class for c in cells])
    tech = np.array([c.technology for c in cells])
    band = np.array([c.band for c in cells])
    bw = np.array(
        [BANDWIDTH_MHZ[b] if t == "LTE" else 0.0 for b, t in zip(band, tech, strict=True)]
    )
    return CellState(
        cell_names=tuple(c.cell_name for c in cells),
        site_ids=np.array([c.site_id for c in cells]),
        x_km=np.array([site[c.site_id].x_km for c in cells]),
        y_km=np.array([site[c.site_id].y_km for c in cells]),
        azimuth_deg=np.array([c.azimuth_deg for c in cells]),
        height_m=np.array([ANTENNA_HEIGHT_M[a] for a in area]),
        tilt_deg=np.array([ELECTRICAL_TILT_DEG[a] for a in area]),
        power_dbm=np.array(
            [
                lte_power_dbm(w) if t == "LTE" else GSM_POWER_DBM
                for w, t in zip(bw, tech, strict=True)
            ]
        ),
        band=band,
        technology=tech,
        vendor=np.array([site[c.site_id].vendor for c in cells]),
        area_class=area,
        bandwidth_mhz=bw,
        n_rb=np.array([N_RB[w] if w else 0 for w in bw]),
        trx=np.where(tech == "GSM", GSM_TRX_MIN, 0),
        cio_db=np.zeros(len(cells)),
        down=np.zeros(len(cells), dtype=bool),
    )


def band_centre_mhz(band: str) -> float:
    """Downlink centre frequency of a band.

    Args:
        band: Band name.

    Returns:
        Centre of the downlink range, MHz.
    """
    low, high, _ = BANDS[band]
    return (low + high) / 2.0
