# Network model report

Synthetic network (rules M1-M5): coverage, load and 15-minute counters of the demo
profile, generated in memory. Stated simplifications (rule M7): no terrain in the served
region, no scheduler, fading or mobility traces; interference at a 50% reference load.
Written by `python -m ran_lakehouse.model.report` from `model.json`.

Demo: 1452 LTE and 264 GSM cells, 12 weeks, 13,837,824 cell-periods; 5329 neighbour relations; 0 persons without LTE coverage.

![Best-server RSRP](model_best_server.png)

![SINR](model_sinr.png)

![Load by hour of week](model_diurnal.png)

![IP throughput against PRB use](model_prb_throughput.png)

![GSM blocking against offered traffic](model_gsm_blocking.png)

The blocking figure shows the three commonest TCH counts, one week of cell-periods.

## Coverage per layer

| Band | Cells | Population covered | Level p10 / p50 / p90, dBm | SINR p10 / p50 / p90, dB |
|---|---|---|---|---|
| B1 | 336 | 0.959 | -112.0 / -101.2 / -88.0 | -5.5 / 0.7 / 10.9 |
| B28 | 36 | 0.756 | -114.4 / -93.5 / -76.2 | -0.6 / 4.6 / 15.7 |
| B3 | 828 | 1.0 | -94.8 / -84.9 / -73.1 | -1.2 / 4.5 / 14.9 |
| B40 | 186 | 0.876 | -116.3 / -104.9 / -91.1 | -6.2 / 0.2 / 10.3 |
| B8 | 66 | 0.702 | -113.1 / -90.2 / -72.7 | -1.2 / 4.3 / 14.6 |
| G1800 | 90 | 0.799 | -101.8 / -91.8 / -78.7 | -1.2 / 2.6 / 10.3 |
| G900 | 174 | 1.0 | -79.5 / -59.6 / -45.5 | 6.5 / 12.2 / 22.7 |

Level is RSRP for LTE and RxLev for GSM; GSM SINR is carrier to interference plus noise.

## KPIs over the 12 weeks (ratio of sums)

| KPI | Value |
|---|---|
| LTE E-RAB accessibility, TS 32.450 6.1.1 (%) | 98.383 |
| LTE RRC setup success rate, operator-defined (%) | 98.998 |
| LTE E-RAB retainability R2, TS 32.450 6.2.1 (releases per session hour) | 0.9615 |
| LTE E-RAB drop rate, operator-defined (%) | 0.543 |
| LTE DL IP throughput, TS 32.450 6.3.1 (kbit/s) | 7291.5 |
| LTE cell availability, TS 32.450 6.4.1 (%) | 100.0 |
| LTE intra-frequency handover success, HO.IntraFreqOut* (%) | 98.711 |
| LTE mean DL PRB use, operator-defined (%) | 20.21 |
| GSM service access success, TS 32.410 7.4 (%) | 97.806 |
| GSM TCH blocking, vendor-style (%) | 0.342 |
| GSM handover success per cell, TS 32.410 9.5 (%) | 96.284 |
| GSM abnormal release rate, TS 32.410 8.2 without intra-cell terms (%) | 2.511 |

## Counter invariants over every cell-period

| Invariant broken | Cell-periods |
|---|---|
| RRC.ConnEstabSucc.sum > RRC.ConnEstabAtt.sum | 0 |
| S1SIG.ConnEstabSucc > S1SIG.ConnEstabAtt | 0 |
| ERAB.EstabInitSuccNbr.sum > ERAB.EstabInitAttNbr.sum | 0 |
| HO.IntraFreqOutSucc > HO.IntraFreqOutAtt | 0 |
| succTCHSeizures > attTCHSeizures | 0 |
| succImmediateAssingProcs > attImmediateAssingProcs | 0 |
| succOutgoingInternalInterCellHDOs > attOutgoingInternalInterCellHDOs | 0 |
| RRU.PrbTotDl outside 0-100 | 0 |
| negative counter values | 0 |
| DRB.IPVolDl > 0 with DRB.IPTimeDl = 0 | 0 |

## Counter relationships, first week

| Check | Statistic | Value | Expected | Result |
|---|---|---|---|---|
| IP throughput falls as PRB use rises | Spearman | -0.5996 | < 0 | pass |
| RRC setup success lower at PRB >= 95% than below 80% | success rate below 80% minus at >= 95% | 0.0252 | > 0 | pass |
| E-RAB drop rate rises with cell-edge share | Spearman | 0.855 | > 0 | pass |
| Mean CQI rises with mean SINR | Spearman | 0.9947 | > 0 | pass |
| Median TA distance grows urban < suburban < rural | median mean TA step per class | [3.769, 11.495, 25.491] | increasing | pass |
| PRB use rises with connected users | Spearman | 0.9571 | > 0 | pass |
| TCH blocking higher at TCH occupancy >= 0.8 than below 0.5 | blocking share at >= 0.8 minus below 0.5 | 0.0655 | > 0 | pass |

## GSM transceivers

Dimensioned per cell for 2% blocking at the busy hour (Erlang B), 1 to 12 TRX:

| TRX | Cells |
|---|---|
| 1 | 97 |
| 2 | 91 |
| 3 | 17 |
| 4 | 20 |
| 5 | 17 |
| 6 | 5 |
| 7 | 9 |
| 8 | 2 |
| 10 | 4 |
| 11 | 2 |

## Timing

Measured on the build machine; varies run to run.

| Step | Seconds |
|---|---|
| demo model build (coverage and serving) | 6.9 |
| demo 12 weeks of counters | 7.6 |
| tiny build and 2 days | 0.0 |

Tiny profile: 27 cells, 2 days, 2739826 RRC setup attempts.
