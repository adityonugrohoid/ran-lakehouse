# What-if sensitivity

Synthetic network, clean demo network (no planted fault present); written by `python -m ran_lakehouse.faults.sensitivity` from `whatif_sensitivity.json` (rule M6).

100 of the 1452 LTE cells (seed 20260930) each got tilt +1, tilt -1, power +1, power -1, one change at a time; days 84 to 90 were replayed before and after with common random numbers. A neighbour is every LTE cell the change touches (rule M6). Changes are absolute values of after minus before; relative changes are against the before value.

## Changed cell (400 cases)

| KPI | relative change %: median, p90, max | absolute change: median, p90, max | share moved > 10% |
|---|---|---|---|
| DL IP throughput (kbit/s) | 5.29, 18.86, 599.76 | 972.71, 3474.17, 46684.3 | 29.1% |
| Mean DL PRB use (%) | 5.42, 18.85, 95.01 | 0.76, 4.43, 27.36 | 29.2% |
| E-RAB drop rate (%) | 11.29, 106.63, 2367.74 | 0.06, 0.57, 3.75 | 53.3% |
| E-RAB accessibility (%) | 0.1, 0.87, 3 | 0.1, 0.86, 2.9 | 0.0% |
| subscribers | 10.26, 26.2, 142.67 | | |

## Touched neighbours (1541 cases)

| KPI | relative change %: median, p90, max | absolute change: median, p90, max | share moved > 10% |
|---|---|---|---|
| DL IP throughput (kbit/s) | 2.2, 8.11, 79.41 | 425.68, 1440.56, 7811.72 | 7.6% |
| Mean DL PRB use (%) | 1.35, 6.68, 74.16 | 0.26, 1.27, 21.34 | 3.9% |
| E-RAB drop rate (%) | 1.08, 39.59, 2118.95 | 0.01, 0.23, 4.1 | 21.2% |
| E-RAB accessibility (%) | 0.01, 0.31, 1.57 | 0.01, 0.3, 1.51 | 0.0% |
| subscribers | 1.33, 9.04, 94.87 | | |

Neighbour throughput moves by a median 2.2% and a p90 of 8.11%; 7.6% of neighbour cases move by more than 10%. One-step changes do not routinely move neighbours by more than 10% (p90 below 10%), but the tail is long.

By how the neighbour relates to the changed cell (DL IP throughput):

| Relation | cases | relative change %: median, p90, max | share moved > 10% |
|---|---|---|---|
| same band | 1270 | 2.24, 7.98, 79.41 | 7.6% |
| other band, same site | 76 | 2.08, 8.33, 73.02 | 9.2% |
| other band, other site | 195 | 2.09, 8.14, 19.72 | 7.2% |

By change (DL IP throughput of neighbours):

| Change | cases | relative change %: median, p90, max | share moved > 10% |
|---|---|---|---|
| tilt +1 | 348 | 2.37, 8.6, 73.02 | 8.0% |
| tilt -1 | 342 | 2.38, 8.12, 79.41 | 8.2% |
| power +1 | 448 | 2.1, 7.65, 72.86 | 7.6% |
| power -1 | 403 | 2.12, 7.88, 71.92 | 6.7% |

## Why a neighbour can lose a quarter of its throughput

`ENB0041_B3_1 tilt +1` (the API smoke test's change) hands 5 grid points (160 persons) to `ENB0031_B3_2`. They are edge points: median SINR -1.4 dB before and -2.8 dB after. The neighbour's subscribers go from 963 to 1091, its mean spectral efficiency from 1.426 to 1.319 bit/s/Hz and its edge share from 0.195 to 0.257. Capacity scales with spectral efficiency and demand with users, so the load rises on both counts: PRB use 26.8% to 31.7% on average, 50.4% to 61% at p95 and 78% to 95% at the busiest period. Per-user throughput is capacity times (1 - load) (rule M4, processor sharing), and the throughput KPI is volume over active time, so it is weighted to the busy periods where that term falls fastest: 16629 to 12342 kbit/s. The neighbour is not lightly loaded at its busy hour; it absorbs poor edge users at a load where each extra user costs the most.

## Cross-layer jumps

`ENB0238_B3_2 power -1` moves only 38 points to another best server inside its own layer, yet the co-sited `ENB0238_B28_2` goes from 1208 to 2330 subscribers (throughput 9318 to 3745 kbit/s). 1122 of the subscribers it gains come from 258 points where the co-sited layer's share went from 0.333 to 1 (median). At each grid point, users split over the LTE layers in proportion to bandwidth, and a layer takes a share only while its RSRP is at least -110 dBm and within 8 dB of the strongest layer (ASSUMPTION, rule M3). A point whose changed layer crosses that margin hands its whole share to the other layers at once: a step, with no spread of users' signal inside the 125 m point and no hysteresis.

## Finding

Typical one-step effects are modest and plausible, and most neighbours move by a few percent. The long tail comes from hard assignments at the point level: best server and layer share both switch whole 125 m points at a threshold, so a change that tips a few dense points moves their users together, and the throughput KPI amplifies it on a neighbour that is busy at its peak. A smoother assignment (users of a point spread over servers and layers by the within-point signal spread) would shorten the tail (not tested here); it changes every generated counter, so it is a model change for its own decision, not made here.

## Cost

Report run 759.9 s, peak resident set 669 MB.
