# Network model report

Synthetic network (rules M1-M5): coverage, load and 15-minute counters of the demo
profile, generated in memory. Stated simplifications (rule M7): no terrain in the served
region, no scheduler, fading or mobility traces; interference at a 50% reference load.
Written by `python -m ran_lakehouse.model.report` from `model.json`.

Demo: 1452 LTE and 264 GSM cells, 12 weeks, 13,837,824 cell-periods; 5159 neighbour relations; 0 persons without LTE coverage.

![Best-server RSRP](model_best_server.png)

![SINR](model_sinr.png)

![Load by hour of week](model_diurnal.png)

![IP throughput against PRB use](model_prb_throughput.png)

![GSM blocking against offered traffic](model_gsm_blocking.png)

The blocking figure shows the three commonest TCH counts, one week of cell-periods.

## Coverage per layer

| Band | Cells | Population covered | Level p10 / p50 / p90, dBm | SINR p10 / p50 / p90, dB |
|---|---|---|---|---|
| B1 | 336 | 0.938 | -114.5 / -103.6 / -91.8 | -5.5 / 0.6 / 10.7 |
| B28 | 36 | 0.592 | -115.1 / -94.8 / -76.5 | -0.5 / 4.7 / 16.0 |
| B3 | 828 | 1.0 | -99.7 / -87.8 / -75.0 | -1.2 / 4.5 / 14.9 |
| B40 | 186 | 0.772 | -117.4 / -107.2 / -94.0 | -6.0 / 0.5 / 10.5 |
| B8 | 66 | 0.532 | -113.0 / -91.6 / -72.6 | -0.8 / 4.5 / 14.9 |
| G1800 | 90 | 0.706 | -102.1 / -93.5 / -82.2 | -1.0 / 2.8 / 10.3 |
| G900 | 174 | 1.0 | -83.5 / -62.0 / -46.4 | 6.5 / 12.2 / 22.7 |

Level is RSRP for LTE and RxLev for GSM; GSM SINR is carrier to interference plus noise.

## KPIs over the 12 weeks (ratio of sums)

| KPI | Value |
|---|---|
| LTE E-RAB accessibility, TS 32.450 6.1.1 (%) | 97.894 |
| LTE RRC setup success rate, operator-defined (%) | 98.653 |
| LTE E-RAB retainability R2, TS 32.450 6.2.1 (releases per session hour) | 1.1106 |
| LTE E-RAB drop rate, operator-defined (%) | 0.63 |
| LTE DL IP throughput, TS 32.450 6.3.1 (kbit/s) | 5888.2 |
| LTE cell availability, TS 32.450 6.4.1 (%) | 100.0 |
| LTE intra-frequency handover success, HO.IntraFreqOut* (%) | 98.674 |
| LTE mean DL PRB use, operator-defined (%) | 19.34 |
| GSM service access success, TS 32.410 7.4 (%) | 97.883 |
| GSM TCH blocking, vendor-style (%) | 0.593 |
| GSM handover success per cell, TS 32.410 9.5 (%) | 96.354 |
| GSM abnormal release rate, TS 32.410 8.2 without intra-cell terms (%) | 2.433 |

## Peak congestion by area class

At each class's busiest 15-minute slot of the week (by mean PRB use), over 12 weeks:

| Class | Busiest slot | Mean PRB use, % | Cells above 80% PRB | Highest share, any slot |
|---|---|---|---|---|
| urban | Tue 11:30 | 28.8 | 0.031 | 0.04 |
| suburban | Fri 20:30 | 50.1 | 0.2 | 0.2 |
| rural | Fri 20:30 | 37.1 | 0.124 | 0.126 |

Per-cell load follows cell size. LTE subscribers per cell: urban 407, suburban 1144, rural 596. The dense city grid is lightly loaded per cell, and the towns, sited on the suburban lattice, are the network's hotspots; they are where capacity faults (rule F1d) belong. Users at urban points follow a business-hours activity curve, so the urban peak falls in the weekday daytime; suburban and rural points follow the residential curve with its evening peak (both ASSUMPTION).

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
| IP throughput falls as PRB use rises | Spearman | -0.5634 | < 0 | pass |
| RRC setup success lower at PRB >= 95% than below 80% | success rate below 80% minus at >= 95% | 0.0373 | > 0 | pass |
| E-RAB drop rate rises with cell-edge share | Spearman | 0.8413 | > 0 | pass |
| Mean CQI rises with mean SINR | Spearman | 0.9951 | > 0 | pass |
| Median TA distance grows urban < suburban < rural | median mean TA step per class | [3.764, 11.32, 25.32] | increasing | pass |
| PRB use rises with connected users | Spearman | 0.9649 | > 0 | pass |
| TCH blocking higher at TCH occupancy >= 0.8 than below 0.5 | blocking share at >= 0.8 minus below 0.5 | 0.0794 | > 0 | pass |

## GSM transceivers

Dimensioned per cell for 2% blocking at the busy hour (Erlang B), 1 to 12 TRX:

| TRX | Cells |
|---|---|
| 1 | 94 |
| 2 | 88 |
| 3 | 26 |
| 4 | 16 |
| 5 | 17 |
| 6 | 7 |
| 7 | 9 |
| 8 | 2 |
| 9 | 2 |
| 10 | 2 |
| 11 | 1 |

## Timing

Measured on the build machine; varies run to run.

| Step | Seconds |
|---|---|
| demo model build (coverage and serving) | 7.0 |
| demo 12 weeks of counters | 7.7 |
| tiny build and 2 days | 0.0 |

Tiny profile: 27 cells, 2 days, 2740066 RRC setup attempts.
