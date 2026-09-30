# Planning geography report

Synthetic expansion area of the demo profile (rules G1 to G7): terrain,
villages, candidate sites, coverage and backhaul, all generated from the world's seed.
Written by `python -m ran_lakehouse.planning.report` from `planning.json`.

![Terrain](planning_terrain.png)

## Service today (rule G1)

The expansion area has little or no usable indoor service today; some villages see
weak outdoor coverage from the served edge. The served cells' levels at each village
use the model's Hata plus the same Bullington diffraction over the terrain as the
candidates. Service coverage is indoor: the outdoor level must clear the threshold by
the indoor margin (10 dB, START).

| Villages | Count |
|---|---|
| all | 150 (176,284 persons, 143 schools) |
| outdoor LTE (RSRP at threshold) | 23 |
| outdoor GSM (RxLev at threshold) | 60 |
| indoor LTE (covered_today_lte) | 8 |
| indoor GSM (covered_today_gsm) | 42 |
| persons without indoor LTE | 169,505 |
| persons without indoor GSM | 125,740 |

## Candidates and coverage (rules G4, G5)

60 candidates on local high points near villages; elevation 335.4 to 576.9 m; build cost 1,809,046,186 to 2,112,228,296 IDR (ASSUMPTION).

![Coverage](planning_coverage.png)

| Coverage from candidates | Value |
|---|---|
| villages with indoor LTE from some candidate | 143 |
| villages with indoor GSM from some candidate | 146 |
| villages per candidate, indoor LTE (min / median / max) | 3 / 9 / 17 |
| area share with indoor LTE from all candidates | 90.9% |
| area share with outdoor LTE from all candidates | 96.3% |

## Backhaul and power (rule G6)

![Line of sight](planning_los.png)

The figure: the longest clear hop, C26 to SITE0277, 22.2 km.
The gold table uses the 0.6 F1 clearance; the 1.0 F1 count is beside it.

| Option (spreads are min / median / max) | Value |
|---|---|
| fiber spur km | 0.08 / 9.97 / 22.59 |
| microwave clear at 0.6 F1 (used) | 52 |
| microwave clear at 1.0 F1 (P.530-19 2.2.2.1) | 50 |
| microwave hop km | 2.95 / 12.15 / 22.2 |
| satellite | 60 |
| hubs | 11 |
| power: grid | 16 |
| power: solar | 44 |

## Sources

- diffraction: ITU-R P.526-16 (11/2025) Annex 1 clause 4.5.1 (Bullington model), eqs (49) to (57), with J(v) from clause 4.1 eq (31)
- fresnel: ITU-R P.530-19 (09/2025) clause 2.2.1 eq (3)
- clearance: ITU-R P.526-16 clause 2.3 and P.530-19 clause 2.2.2: 60% of the first Fresnel zone gives free-space conditions; P.530-19 clause 2.2.2.1 plans for 1.0 F1 at median k
- thresholds: 3GPP sets no planning threshold: TS 36.133 V19.5.0 clause 9.1.4 gives the RSRP reporting range (-156 to -44 dBm); TS 45.005 V19.0.0 clause 6.2 the GSM 900 reference sensitivity (-104 dBm BTS, -102 dBm small MS)
- indoor margin context, ITU-R P.2109-2 median at 0.9 GHz (dB): traditional 14.2; thermally-efficient 31.2

## Parameters

| Parameter | Value |
|---|---|
| relief_m (START) | 200 / 600 |
| terrain spectral exponent (START) | 3.2 |
| terrain grid km (ASSUMPTION) | 0.1 |
| persons per school (START) | 1,500 |
| school from persons (START) | 500 |
| candidate sites (START) | 60 |
| candidate spacing km (START) | 2 |
| candidate near a village within km (START) | 3 |
| mast height m (START) | 30 |
| LTE B8 power dBm, N_RB (rule M) | 46 / 50 |
| GSM 900 BCCH power dBm (rule M) | 43 |
| LTE RSRP threshold dBm (START) | -110 |
| GSM RxLev threshold dBm (START) | -100 |
| indoor margin dB (START, ASSUMPTION) | 10 |
| microwave GHz (START) | 8 |
| Fresnel clearance used (START) | 0.6 |
| hub zone km, hub antenna m, max hop km (START) | 4 / 40 / 30 |
| grid power within km (START) | 5 |
| satellite Mbps (START) | 8 |
| costs IDR (ASSUMPTION) | build base 1,800,000,000; access road per km 120,000,000; fiber per km 150,000,000; microwave link 350,000,000; satellite terminal 150,000,000; satellite monthly 25,000,000; grid line per km 250,000,000; solar and battery 650,000,000 |

## Gold tables (rule G7)

| Table | Rows |
|---|---|
| gold.villages | 150 |
| gold.candidate_sites | 60 |
| gold.coverage | 18,000 |
| gold.backhaul_power_options | 300 |

## Cost

| Step | Value |
|---|---|
| seconds, network model and plan | 28.4 |
| seconds, total | 40.0 |
| peak RSS (MB) | 745 |
