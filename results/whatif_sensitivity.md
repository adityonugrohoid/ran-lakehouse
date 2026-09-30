# What-if sensitivity

Synthetic network, clean demo network (no planted fault present); written by `python -m ran_lakehouse.faults.sensitivity` from `whatif_sensitivity.json` (rule M6).

100 of the 1452 LTE cells (seed 20260930) each got tilt +1, tilt -1, power +1, power -1, one change at a time; days 84 to 90 were replayed before and after with common random numbers. A neighbour is every LTE cell the change touches (rule M6). Changes are absolute values of after minus before; relative changes are against the before value.

## Changed cell (400 cases)

| KPI | relative change %: median, p90, p99, max | absolute change: median, p90, p99, max | share moved > 10% |
|---|---|---|---|
| DL IP throughput (kbit/s) | 4.08, 15.69, 49.51, 85.06 | 858.44, 2476.64, 5276.37, 8255.9 | 18.5% |
| Mean DL PRB use (%) | 6.42, 15.43, 33.01, 78.27 | 0.89, 3.64, 9.31, 13.41 | 27.5% |
| E-RAB drop rate (%) | 11.37, 72.54, 430.77, 2442.05 | 0.06, 0.39, 1.72, 4.04 | 54.0% |
| E-RAB accessibility (%) | 0.11, 0.51, 1.22, 2.04 | 0.11, 0.5, 1.2, 1.97 | 0.0% |
| subscribers | 11.82, 21.03, 42.04, 70.83 | | |

## Touched neighbours (1713 cases)

| KPI | relative change %: median, p90, p99, max | absolute change: median, p90, p99, max | share moved > 10% |
|---|---|---|---|
| DL IP throughput (kbit/s) | 1.34, 5.03, 12.61, 39.42 | 252.8, 648.57, 1706.9, 4071.93 | 2.0% |
| Mean DL PRB use (%) | 1.12, 3.5, 8.45, 20.1 | 0.17, 0.93, 2.02, 6.51 | 0.6% |
| E-RAB drop rate (%) | 1.54, 20.04, 372.23, 1762.24 | 0.01, 0.13, 1.19, 4.07 | 15.3% |
| E-RAB accessibility (%) | 0.01, 0.18, 0.55, 1.24 | 0.01, 0.17, 0.54, 1.23 | 0.0% |
| subscribers | 1.67, 4.55, 8.87, 21.06 | | |

Neighbour throughput moves by a median 1.34% and a p90 of 5.03%; 2.0% of neighbour cases move by more than 10%. One-step changes do not routinely move neighbours by more than 10% (p90 below 10%), but the tail is long.

By how the neighbour relates to the changed cell (DL IP throughput):

| Relation | cases | relative change %: median, p90, p99, max | share moved > 10% |
|---|---|---|---|
| same band | 1194 | 1.43, 4.87, 12.21, 39.42 | 2.1% |
| other band, same site | 211 | 0.76, 3.02, 15.39, 18.6 | 3.3% |
| other band, other site | 308 | 1.51, 5.68, 9.27, 12.63 | 0.6% |

By change (DL IP throughput of neighbours):

| Change | cases | relative change %: median, p90, p99, max | share moved > 10% |
|---|---|---|---|
| tilt +1 | 391 | 1.64, 5.41, 14.43, 18.69 | 2.0% |
| tilt -1 | 361 | 1.61, 5.29, 14.63, 39.32 | 3.0% |
| power +1 | 507 | 1.12, 4.39, 12.11, 39.42 | 1.8% |
| power -1 | 454 | 1.22, 4.9, 10.32, 19.35 | 1.3% |

## Why a neighbour loses throughput: the API smoke case

`ENB0041_B3_1 tilt +1` (the API smoke test's change) makes `ENB0031_B3_2` the best server at 5 grid points (160 persons, median SINR -1.4 dB before and -2.8 dB after); under rule M3 their users move in part, by the logistic split. The neighbour's subscribers go from 908 to 926, its mean spectral efficiency from 1.491 to 1.491 bit/s/Hz, its edge share from 0.172 to 0.152, and its PRB use 24.6% to 25% on average, 46% to 47% at p95 and 71% to 72% at the busiest period; its DL IP throughput goes from 18503 to 18292 kbit/s. With hard thresholds the same change took it from 963 to 1091 subscribers and from 16629 to 12342 kbit/s (PRB p95 50.4% to 61%). Per-user throughput is capacity times (1 - load) (rule M4, processor sharing) and the KPI is volume over active time, so it weighs the busy periods, where an added edge user costs the most.

## Largest co-sited layer shift

`ENB0238_B3_2 power -1` moves 38 points to another best server inside its own layer; the co-sited `ENB0238_B28_2` goes from 1524 to 1590 subscribers (throughput 5047 to 4798 kbit/s). The layer share moves 68 subscribers onto it, at 272 points where its share went from 0.454 to 0.493 (median). At each grid point, users split over the LTE layers in proportion to bandwidth, each layer weighted by logistics (scale 3 dB) in how far its RSRP sits inside the 8 dB margin of the strongest layer and above the -110 dBm floor; within a layer, users split between the best and second server by a logistic (scale 3 dB) in their level difference (rule M3, ASSUMPTION).

## Before and after the soft assignment

Before: the same report on the model with hard thresholds (one best server per point, a layer's share switched on or off at the margin and the floor), from `whatif_sensitivity_hard_thresholds.json`. After: this report (rule M3 soft assignment).

| Figure | before: median, p90, p99, max | after: median, p90, p99, max |
|---|---|---|
| neighbour throughput, relative % | 2.2, 8.11, 28.48, 79.41 | 1.34, 5.03, 12.61, 39.42 |
| co-sited layer subscribers, relative % | 2.94, 9.73, 93.4, 94.87 | 1.6, 3.77, 5.05, 6.31 |
| changed cell subscribers, relative % | 10.26, 26.2, 100, 142.67 | 11.82, 21.03, 42.04, 70.83 |

Neighbour cases moved by more than 10% in throughput: 7.6% before, 2.0% after.

## Finding

The tail shrank: neighbour throughput p99 28.48% to 12.61% (max 79.41% to 39.42%), co-sited layer subscribers p99 93.4% to 5.05% (max 94.87% to 6.31%). The medians move from 2.2% to 1.34% (neighbour throughput) and from 10.26% to 11.82% (the changed cell's subscribers).

## Cost

Report run 890.3 s, peak resident set 642 MB.
