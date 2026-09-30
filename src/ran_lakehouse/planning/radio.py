"""Coverage and line of sight over the expansion terrain (rules G5, G6).

Coverage from a candidate site to a village: the model's Hata loss for
open (rural) areas (rule M) plus the Bullington diffraction loss of the
terrain profile between them, per ITU-R P.526 (general-path method,
Bullington part; edition and clause in BULLINGTON_SOURCE). Adding the two
is an ASSUMPTION: Hata's median already holds typical terrain, and the
diffraction term adds the obstruction this particular profile has.

Microwave line of sight: every intermediate profile point, raised by the
earth bulge for the effective earth radius, must stay at least
FRESNEL_CLEARANCE of the first Fresnel zone radius below the direct path.
"""

import numpy as np

from ran_lakehouse.model.radio import UE_HEIGHT_M, antenna_gain_db, hata_path_loss_db
from ran_lakehouse.planning.terrain import Terrain

BULLINGTON_SOURCE = (
    "ITU-R P.526-16 (11/2025) Annex 1 clause 4.5.1 (Bullington model), eqs (49) to (57), "
    "with J(v) from clause 4.1 eq (31)"
)
FRESNEL_SOURCE = "ITU-R P.530-19 (09/2025) clause 2.2.1 eq (3)"
CLEARANCE_SOURCE = (
    "ITU-R P.526-16 clause 2.3 and P.530-19 clause 2.2.2: 60% of the first Fresnel zone gives "
    "free-space conditions; P.530-19 clause 2.2.2.1 plans for 1.0 F1 at median k"
)
# ITU-R P.526-16 Annex 1 clause 1: 8 500 km when no other information is
# available (k = 4/3 of 6 371 km, clause 4.4).
EFFECTIVE_EARTH_RADIUS_KM = 8500.0
PROFILE_SAMPLES = 121  # points per terrain profile, ends included (ASSUMPTION)

# Candidate sites (START unless cited): 30 m masts, three sectors at 0, 120
# and 240 degrees with the served rural layers' 4 degree downtilt and
# powers (rule M: LTE 46 dBm over 10 MHz, GSM BCCH 43 dBm).
MAST_HEIGHT_M = 30.0
SECTOR_AZIMUTHS_DEG = (0.0, 120.0, 240.0)
DOWNTILT_DEG = 4.0
LTE_POWER_DBM = 46.0
LTE_N_RB = 50
GSM_POWER_DBM = 43.0
LTE_FREQUENCY_MHZ = 942.5  # B8 downlink centre (TS 36.101 Table 5.5-1: 925-960 MHz)
GSM_FREQUENCY_MHZ = 942.5  # GSM 900 downlink, same band
# Coverage thresholds at a village centre (START, ASSUMPTION: planning
# margins, not 3GPP requirements).
LTE_RSRP_THRESHOLD_DBM = -110.0
GSM_RXLEV_THRESHOLD_DBM = -100.0
# Microwave backhaul (START).
MICROWAVE_GHZ = 8.0
FRESNEL_CLEARANCE = 0.6  # used for the options (START, CLEARANCE_SOURCE)
FULL_CLEARANCE = 1.0  # P.530-19 clause 2.2.2.1 step 1, reported beside it
# Service coverage is indoor: the outdoor level must clear the threshold by
# the median building entry loss of ITU-R P.2109-2 (08/2023) Annex 1
# clause 3 (eqs (1) to (10), Table 1) at 0.9 GHz and horizontal incidence,
# P = 50%: about 14.2 dB for traditional and 31.2 dB for thermally-efficient
# buildings (computed from the Recommendation; they match its Fig. 1).
# Village housing is modelled as the traditional class (ASSUMPTION).
P2109_MEDIAN_DB = {"traditional": 14.2, "thermally-efficient": 31.2}
INDOOR_MARGIN_DB = P2109_MEDIAN_DB["traditional"]
INDOOR_MARGIN_SOURCE = (
    "ITU-R P.2109-2 (08/2023) Annex 1 clause 3, median building entry loss at 0.9 GHz, "
    "horizontal incidence, traditional buildings (village housing, ASSUMPTION)"
)
THRESHOLD_CONTEXT = (
    "3GPP sets no planning threshold: TS 36.133 V19.5.0 clause 9.1.4 gives the RSRP reporting "
    "range (-156 to -44 dBm); TS 45.005 V19.0.0 clause 6.2 the GSM 900 reference sensitivity "
    "(-104 dBm BTS, -102 dBm small MS)"
)


def j_loss_db(v: np.ndarray) -> np.ndarray:
    """Single knife-edge diffraction loss J(v), ITU-R P.526-16 clause 4.1 eq (31).

    Args:
        v: Diffraction parameter.

    Returns:
        Loss in dB, 0 for v <= -0.78 (clause 4.5.1, after eq (52)).
    """
    v = np.asarray(v, dtype=float)
    loss = 6.9 + 20.0 * np.log10(np.sqrt((v - 0.1) ** 2 + 1.0) + v - 0.1)
    result: np.ndarray = np.where(v > -0.78, loss, 0.0)
    return result


def bullington_db(
    d_km: np.ndarray, ground_m: np.ndarray, h_tx_m: np.ndarray, h_rx_m: np.ndarray, f_mhz: float
) -> np.ndarray:
    """Bullington diffraction loss of terrain profiles (BULLINGTON_SOURCE).

    Line of sight: the largest v over the profile points (eqs (49) to (52));
    beyond it, v at the Bullington point where the slopes from the two ends
    meet (eqs (53) to (56)); then the tapered correction of eq (57). The
    complete method's spherical-earth part (clause 4.5.2) is not applied:
    the earth bulge enters through the effective radius (ASSUMPTION).

    Args:
        d_km: Path length per profile, shape (paths,).
        ground_m: Ground height at evenly spaced points, ends included,
            shape (paths, samples).
        h_tx_m: Transmitter antenna height above sea level, shape (paths,).
        h_rx_m: Receiver antenna height above sea level, shape (paths,).
        f_mhz: Frequency, MHz.

    Returns:
        Loss per path, dB.
    """
    n = ground_m.shape[1]
    d = np.asarray(d_km, dtype=float)[:, None]
    di = d * np.linspace(0.0, 1.0, n)[None, :]
    di, h = di[:, 1:-1], ground_m[:, 1:-1]
    hts = np.asarray(h_tx_m, dtype=float)[:, None]
    hrs = np.asarray(h_rx_m, dtype=float)[:, None]
    wavelength = 299.792458 / f_mhz
    # Earth curvature: heights raised by the bulge for the effective radius.
    hc = h + 500.0 * di * (d - di) / EFFECTIVE_EARTH_RADIUS_KM
    s_tim = ((hc - hts) / di).max(axis=1)
    s_tr = ((hrs - hts) / d)[:, 0]
    line = (hts * (d - di) + hrs * di) / d
    v_max = ((hc - line) * np.sqrt(0.002 * d / (wavelength * di * (d - di)))).max(axis=1)
    s_rim = ((hc - hrs) / (d - di)).max(axis=1)
    dd = d[:, 0]
    d_b = np.clip(
        (hrs[:, 0] - hts[:, 0] + s_rim * dd) / np.maximum(s_tim + s_rim, 1e-9), 1e-6, dd - 1e-6
    )
    v_b = (hts[:, 0] + s_tim * d_b - (hts[:, 0] * (dd - d_b) + hrs[:, 0] * d_b) / dd) * np.sqrt(
        0.002 * dd / (wavelength * d_b * (dd - d_b))
    )
    luc = j_loss_db(np.where(s_tim < s_tr, v_max, v_b))
    result: np.ndarray = luc + (1.0 - np.exp(-luc / 6.0)) * (10.0 + 0.02 * dd)
    return result


def site_levels(
    terrain: Terrain,
    site_x: float,
    site_y: float,
    x_km: np.ndarray,
    y_km: np.ndarray,
) -> dict[str, np.ndarray]:
    """Received levels from one candidate site at points on the ground.

    Args:
        terrain: The terrain.
        site_x: Site x.
        site_y: Site y.
        x_km: Point x.
        y_km: Point y.

    Returns:
        distance_km, hata_db, diffraction_db, gain_dbi, lte_rsrp_dbm and
        gsm_rxlev_dbm per point.
    """
    x, y = np.asarray(x_km, dtype=float), np.asarray(y_km, dtype=float)
    d = np.maximum(np.hypot(x - site_x, y - site_y), 0.02)
    t = np.linspace(0.0, 1.0, PROFILE_SAMPLES)[None, :]
    ground = terrain.height_at(
        site_x + t * (x[:, None] - site_x), site_y + t * (y[:, None] - site_y)
    )
    h_tx = ground[:, 0] + MAST_HEIGHT_M
    h_rx = ground[:, -1] + UE_HEIGHT_M
    diffraction = bullington_db(d, ground, h_tx, h_rx, LTE_FREQUENCY_MHZ)
    zeros = np.zeros_like(d)
    hata = hata_path_loss_db(LTE_FREQUENCY_MHZ, d, np.full_like(d, MAST_HEIGHT_M), zeros, zeros)
    bearing = np.degrees(np.arctan2(x - site_x, y - site_y))
    elevation = np.degrees(np.arctan2(h_tx - h_rx, d * 1000.0))
    gain = np.max(
        [antenna_gain_db(bearing - az, elevation - DOWNTILT_DEG) for az in SECTOR_AZIMUTHS_DEG],
        axis=0,
    )
    loss = hata + diffraction
    rs_power = LTE_POWER_DBM - 10.0 * np.log10(12.0 * LTE_N_RB)
    return {
        "distance_km": d,
        "hata_db": hata,
        "diffraction_db": diffraction,
        "gain_dbi": gain,
        "lte_rsrp_dbm": rs_power + gain - loss,
        "gsm_rxlev_dbm": GSM_POWER_DBM + gain - loss,
    }


def fresnel_radius_m(d1_km: np.ndarray, d2_km: np.ndarray, f_ghz: float) -> np.ndarray:
    """First Fresnel zone radius (FRESNEL_SOURCE).

    Args:
        d1_km: Distance from one end.
        d2_km: Distance from the other end.
        f_ghz: Frequency, GHz.

    Returns:
        Radius, m.
    """
    d1, d2 = np.asarray(d1_km, dtype=float), np.asarray(d2_km, dtype=float)
    result: np.ndarray = 17.3 * np.sqrt(d1 * d2 / (f_ghz * (d1 + d2)))
    return result


def line_of_sight(
    terrain: Terrain,
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    f_ghz: float,
    clearance: float,
) -> tuple[bool, dict[str, np.ndarray]]:
    """Whether a microwave path clears a fraction of the first Fresnel zone.

    Args:
        terrain: The terrain.
        a: (x_km, y_km, antenna height above ground in m) of one end.
        b: The other end.
        f_ghz: Frequency, GHz.
        clearance: Fraction of the first Fresnel zone radius the path must
            keep above the ground and earth bulge (FRESNEL_CLEARANCE, or
            FULL_CLEARANCE for the P.530-19 planning step).

    Returns:
        (clear, profile) with distance, ground with earth bulge, direct
        path height and the first Fresnel radius per point.
    """
    distance, ground = terrain.profile(a[0], a[1], b[0], b[1], PROFILE_SAMPLES)
    total = float(distance[-1])
    h_a, h_b = ground[0] + a[2], ground[-1] + b[2]
    bulge = 500.0 * distance * (total - distance) / EFFECTIVE_EARTH_RADIUS_KM
    path = h_a + (h_b - h_a) * distance / total
    radius = fresnel_radius_m(distance, total - distance, f_ghz)
    inner = slice(1, -1)
    clear = bool(np.all(path[inner] - (ground[inner] + bulge[inner]) >= clearance * radius[inner]))
    return clear, {
        "distance_km": distance,
        "ground_with_bulge_m": ground + bulge,
        "path_m": path,
        "fresnel_radius_m": radius,
    }
