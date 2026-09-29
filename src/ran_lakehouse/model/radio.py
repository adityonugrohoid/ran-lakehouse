"""Radio functions of the network model (rule M2): path loss, antenna, link.

Path loss:
- Okumura-Hata urban median loss, COST 231 Final Report (1999) chapter 4,
  section 4.4.1, eq 4.4.1, with the small/medium-city mobile correction
  a(hm) of eq 4.4.2; stated validity 150-1000 MHz there (Hata 1980 gives
  150-1500 MHz), hb 30-200 m, hm 1-10 m, d 1-20 km.
- COST-231 Hata, same section, eq 4.4.3, for 1500-2000 MHz, with Cm = 3 dB
  in metropolitan centres (the urban share of a point) and 0 dB elsewhere.
- Suburban and open-area corrections as restated in Report ITU-R SM.2028-2
  (06/2017) section 6.1, with f clamped to 150-2000 MHz as there.
- Above 2000 MHz (B1 2100, B40 2300): the COST-231 formula at 2000 MHz plus
  10 log10(f/2000), the Extended Hata frequency term of SM.2028-2 section 6.1
  (ASSUMPTION: COST-231 Hata is not validated above 2000 MHz).
- B28 700 and the 900 MHz bands use Hata inside its range. Distances below
  1 km are extrapolated (ASSUMPTION) with the free-space loss as a floor.

Antenna: 3GPP TR 36.814 V9.2.0 Annex A.2.1.1.1 Table A.2.1.1-2: horizontal
3 dB beamwidth 70 deg, front-to-back A_m 25 dB, vertical 3 dB beamwidth
10 deg, side-lobe level SLA_v 20 dB. Gain 14 dBi including cable loss, TR
25.814 V7.1.0 Table A.2.1.8-1.

Link: noise density -174 dBm/Hz (TR 36.942 V19.0.0 Table C.1), UE noise
figure 9 dB (TR 36.942 Table 12.2), 15 kHz subcarriers, attenuated Shannon
DL mapping of TR 36.942 V19.0.0 Annex A.1 Table A.1 (alpha 0.6, SNIR_MIN
-10 dB, 4.4 bit/s/Hz maximum).
"""

import numpy as np

UE_HEIGHT_M = 1.5  # TR 36.814 V9.2.0 Table A.2.1.1-2
ANTENNA_GAIN_DBI = 14.0  # TR 25.814 V7.1.0 Table A.2.1.8-1
H_BEAMWIDTH_DEG = 70.0  # TR 36.814 V9.2.0 Table A.2.1.1-2
FRONT_TO_BACK_DB = 25.0
V_BEAMWIDTH_DEG = 10.0
SIDE_LOBE_DB = 20.0
NOISE_DENSITY_DBM_HZ = -174.0  # TR 36.942 V19.0.0 Table C.1
UE_NOISE_FIGURE_DB = 9.0  # TR 36.942 V19.0.0 Table 12.2
SUBCARRIER_HZ = 15_000.0  # TS 36.211 V19.3.0 Table 6.2.3-1
PRB_HZ = 180_000.0  # 12 subcarriers per PRB, same table
SHANNON_ALPHA = 0.6  # TR 36.942 V19.0.0 Annex A.1 Table A.1, DL
SNIR_MIN_DB = -10.0
MAX_SPECTRAL_EFFICIENCY = 4.4
# Resource blocks per channel bandwidth, TS 36.101 V20.1.0 Table 5.6-1.
N_RB = {1.4: 6, 3.0: 15, 5.0: 25, 10.0: 50, 15.0: 75, 20.0: 100}
# CQI efficiencies, TS 36.213 V19.5.0 Table 7.2.3-1 (CQI 1-15; 0 is out of
# range).
CQI_EFFICIENCY = (
    0.1523, 0.2344, 0.3770, 0.6016, 0.8770, 1.1758, 1.4766, 1.9141,
    2.4063, 2.7305, 3.3223, 3.9023, 4.5234, 5.1152, 5.5547,
)  # fmt: skip
# One timing advance step: 16 Ts, Ts = 1/30.72 MHz (TS 36.213 V19.5.0
# clause 4.2.3, TS 36.211 V19.3.0 clause 8.1); one-way distance
# c * 16 Ts / 2 = 78.07 m (derived).
TA_STEP_M = 299_792_458.0 * 16.0 / 30.72e6 / 2.0
FREE_SPACE_MIN_KM = 0.02  # distance floor (ASSUMPTION)
# COST-231 Hata metropolitan-centre correction, COST 231 Final Report
# section 4.4.1: 3 dB, carried by the urban share of a point.
METROPOLITAN_CM_DB = 3.0


def mobile_correction(f_mhz: np.ndarray, hm_m: float) -> np.ndarray:
    """Small/medium-city mobile antenna correction a(hm), COST 231 eq 4.4.2.

    Args:
        f_mhz: Frequency, MHz.
        hm_m: Mobile antenna height, m.

    Returns:
        a(hm) in dB.
    """
    log_f = np.log10(f_mhz)
    result: np.ndarray = (1.1 * log_f - 0.7) * hm_m - (1.56 * log_f - 0.8)
    return result


def hata_path_loss_db(
    f_mhz: float,
    d_km: np.ndarray,
    hb_m: np.ndarray,
    urban_weight: np.ndarray,
    suburban_weight: np.ndarray,
) -> np.ndarray:
    """Median path loss blended over environments, with the free-space floor.

    The urban, suburban and open-area (rural) losses are mixed with the
    given weights, the rural weight being what is left (ASSUMPTION: a
    continuous blend avoids steps in received power at area-class edges).

    Args:
        f_mhz: Carrier frequency, MHz.
        d_km: Distance, km.
        hb_m: Base station antenna height, m (broadcast with d_km).
        urban_weight: Weight of the urban loss per point, 0-1 (broadcast).
        suburban_weight: Weight of the suburban loss per point, 0-1, with
            urban_weight + suburban_weight <= 1 (broadcast).

    Returns:
        Path loss, dB.
    """
    d = np.maximum(d_km, FREE_SPACE_MIN_KM)
    log_hb = np.log10(hb_m)
    f_formula = min(f_mhz, 2000.0)
    a_hm = mobile_correction(np.asarray(f_formula), UE_HEIGHT_M)
    slope = (44.9 - 6.55 * log_hb) * np.log10(d)
    if f_mhz <= 1500.0:
        base = 69.55 + 26.16 * np.log10(f_formula)
        cm = 0.0
    else:
        base = 46.3 + 33.9 * np.log10(f_formula) + 10.0 * np.log10(f_mhz / f_formula)
        cm = METROPOLITAN_CM_DB
    medium_city = base - 13.82 * log_hb - a_hm + slope
    f_corr = min(max(150.0, f_mhz), 2000.0)
    log_fc = np.log10(f_corr)
    urban = medium_city + cm
    suburban = medium_city - 2.0 * np.log10(f_corr / 28.0) ** 2 - 5.4
    rural = medium_city - 4.78 * log_fc**2 + 18.33 * log_fc - 40.94
    rural_weight = 1.0 - urban_weight - suburban_weight
    loss = urban_weight * urban + suburban_weight * suburban + rural_weight * rural
    free_space = 32.45 + 20.0 * np.log10(f_mhz) + 20.0 * np.log10(d)
    result: np.ndarray = np.maximum(loss, free_space)
    return result


def antenna_gain_db(off_azimuth_deg: np.ndarray, off_tilt_deg: np.ndarray) -> np.ndarray:
    """3D sector antenna gain, TR 36.814 V9.2.0 Table A.2.1.1-2.

    Args:
        off_azimuth_deg: Horizontal angle from boresight, degrees.
        off_tilt_deg: Vertical angle from the tilted boresight, degrees.

    Returns:
        Gain in dBi including the boresight gain.
    """
    phi = (off_azimuth_deg + 180.0) % 360.0 - 180.0
    a_h = -np.minimum(12.0 * (phi / H_BEAMWIDTH_DEG) ** 2, FRONT_TO_BACK_DB)
    a_v = -np.minimum(12.0 * (off_tilt_deg / V_BEAMWIDTH_DEG) ** 2, SIDE_LOBE_DB)
    result: np.ndarray = ANTENNA_GAIN_DBI - np.minimum(-(a_h + a_v), FRONT_TO_BACK_DB)
    return result


def noise_per_re_dbm() -> float:
    """Thermal noise plus UE noise figure in one 15 kHz resource element.

    Returns:
        Noise power, dBm.
    """
    return NOISE_DENSITY_DBM_HZ + 10.0 * float(np.log10(SUBCARRIER_HZ)) + UE_NOISE_FIGURE_DB


def spectral_efficiency(sinr_db: np.ndarray) -> np.ndarray:
    """Attenuated Shannon DL mapping, TR 36.942 V19.0.0 Annex A.1.

    Args:
        sinr_db: SINR, dB.

    Returns:
        Throughput per Hz, bit/s/Hz.
    """
    sinr = 10.0 ** (np.asarray(sinr_db) / 10.0)
    se = SHANNON_ALPHA * np.log2(1.0 + sinr)
    result: np.ndarray = np.where(
        np.asarray(sinr_db) < SNIR_MIN_DB, 0.0, np.minimum(se, MAX_SPECTRAL_EFFICIENCY)
    )
    return result


def cqi_index(sinr_db: np.ndarray) -> np.ndarray:
    """Wideband CQI a UE reports at a given SINR.

    ASSUMPTION: the highest CQI whose TS 36.213 Table 7.2.3-1 efficiency does
    not exceed the Shannon efficiency log2(1 + SINR); 0 when none does.

    Args:
        sinr_db: SINR, dB.

    Returns:
        CQI index 0-15.
    """
    shannon = np.log2(1.0 + 10.0 ** (np.asarray(sinr_db) / 10.0))
    result: np.ndarray = np.searchsorted(np.array(CQI_EFFICIENCY), shannon, side="right")
    return result
