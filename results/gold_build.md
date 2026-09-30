# Gold build report

Synthetic network, demo profile. 12 weeks of silver built
into gold KPIs by the dbt project (incremental merge models keyed by formula version,
rule S2), one UTC day at a time, recomputing the WIB days and weeks each day touches
(rules L3, L5, D6). Formulas: `kpi_catalog.md`. Written by
`python -m ran_lakehouse.lake.gold_report` from `gold_build.json`.

![Daily network KPIs](gold_network_trends.png)

## Formula revision (rule D6)

LTE_RRC_SSR v1 to v2, current from 2026-03-02 (WIB): history reprocessed from 2026-01-04 before the build reached UTC day 2026-03-01. Both versions are kept for the whole
history. Network daily mean over the run: v1 98.938 %, v2 98.83 %.

## Agreement with the model

Every daily KPI recomputed from the simulator's own counters (same faults) against
gold, per cell and WIB day, leaving out cell-days a planted delivery anomaly changed
(D2 conflict, D3, D4). Tolerance: relative 1e-06; availability 0.556 percentage points (10 s samples).
WIB days compared: 83 of 84: the
run's last WIB day stays partial in gold (silver builds a UTC day only after it ends).
All agree: yes.

| KPI | Cell-days compared | Agreeing | Largest difference | Missing in gold |
|---|---|---|---|---|
| GSM_ABN_REL v1 | 9,372 | 9,372 | 8.88e-16 | 0 |
| GSM_CSSR v1 | 18,744 | 18,744 | 0 | 0 |
| GSM_HOSR v1 | 18,744 | 18,744 | 1.42e-14 | 0 |
| GSM_SAS v1 | 18,744 | 18,744 | 0 | 0 |
| GSM_SDCCH_BLOCK v1 | 18,744 | 18,744 | 0 | 0 |
| GSM_SDCCH_DROP v1 | 9,372 | 9,372 | 2.22e-16 | 0 |
| GSM_TCH_BLOCK v1 | 18,744 | 18,744 | 8.88e-16 | 0 |
| LTE_AVAIL v1 | 102,852 | 102,852 | 1.42e-14 | 0 |
| LTE_CQI_MEAN v1 | 102,852 | 102,852 | 0 | 0 |
| LTE_ERAB_ACC v1 | 102,852 | 102,852 | 0 | 0 |
| LTE_ERAB_DROP v1 | 102,852 | 102,852 | 4.44e-16 | 0 |
| LTE_ERAB_RET v1 | 31,896 | 31,896 | 8.88e-16 | 0 |
| LTE_IP_THP_DL v1 | 102,852 | 102,852 | 2.72e-06 | 0 |
| LTE_MOB_HOSR v1 | 102,779 | 102,779 | 1.42e-14 | 0 |
| LTE_PRB_UTIL v1 | 102,852 | 102,852 | 0 | 0 |
| LTE_RRC_SSR v1 | 102,852 | 102,852 | 1.42e-14 | 0 |
| LTE_RRC_SSR v2 | 102,852 | 102,852 | 0 | 0 |

## Coverage and suspect data

| Daily KPI rows | Cell-days | Coverage below 1 | With suspect values |
|---|---|---|---|
| LTE | 121,968 | 18,876 | 129 |
| GSM | 22,176 | 3,432 | 0 |

## Worst cells (rule L5)

Persistent: breach on 3 of 7 days of a WIB week, days judged at coverage 0.75 or more (START).

![Worst cells](gold_worst_cells.png)

The figure: GSM_SAS v1, week of 2026-01-12, top 5 persistent cells.

| KPI | Weeks with a persistent cell | Persistent cell-weeks | Most in one week |
|---|---|---|---|
| GSM_CSSR v1 | 12 | 32 | 4 |
| GSM_HOSR v1 | 6 | 7 | 2 |
| GSM_SAS v1 | 12 | 41 | 5 |
| GSM_TCH_BLOCK v1 | 12 | 24 | 3 |
| LTE_ERAB_ACC v1 | 5 | 11 | 5 |
| LTE_ERAB_DROP v1 | 12 | 32 | 5 |
| LTE_PRB_UTIL v1 | 12 | 33 | 5 |
| LTE_RRC_SSR v1 | 7 | 16 | 5 |
| LTE_RRC_SSR v2 | 10 | 21 | 5 |

## Weekend throughput: a traffic-mix effect

Network DL IP throughput is lower at weekends (6.6 against 7.6 Mbit/s on weekdays, from the rows below). Cell by
cell it is not: 1,054 of 1,452 LTE cells have a weekend throughput at
least their weekday one, and 11 have both lower throughput and lower PRB use.
At weekends traffic moves from the urban business areas, where throughput is highest, to
residential and suburban cells, so the network mean falls (complete WIB days,
weekday against Saturday and Sunday):

| Area class | Day | DL IP throughput (Mbit/s) | Share of DL volume (%) | PRB utilization, N_RB-weighted (%) | Mean CQI |
|---|---|---|---|---|---|
| rural | weekday | 4.01 | 13.9 | 20.0 | 9.68 |
| rural | weekend | 3.67 | 16.4 | 20.4 | 9.68 |
| suburban | weekday | 7.85 | 51.4 | 26.5 | 9.71 |
| suburban | weekend | 7.42 | 61.0 | 27.2 | 9.71 |
| urban | weekday | 10.8 | 34.7 | 15.0 | 8.97 |
| urban | weekend | 9.44 | 22.6 | 11.0 | 8.6 |

## A faulted cell

![Faulted cell](gold_faulted_cell.png)

one planted F1e fault lasting 84 h, two days either side; the planted schedule is evaluation-only (rule
A3), so the cell and the date are withheld.

## Gold tables

| Table | Rows | Data files | MB |
|---|---|---|---|
| gold.cells | 1,716 | 84 | 2.5 |
| gold.lte_cell_15m | 11,592,768 | 84 | 333.1 |
| gold.lte_cell_60m | 2,898,192 | 84 | 35.3 |
| gold.gsm_cell_15m | 2,107,776 | 84 | 24.8 |
| gold.lte_kpi_15m | 96,326,960 | 140 | 701.7 |
| gold.lte_kpi_hour | 27,016,127 | 140 | 291.9 |
| gold.lte_kpi_day | 1,135,428 | 182 | 34.2 |
| gold.lte_kpi_week | 162,204 | 110 | 20.1 |
| gold.gsm_kpi_15m | 12,646,656 | 84 | 49.4 |
| gold.gsm_kpi_hour | 3,166,416 | 84 | 18.0 |
| gold.gsm_kpi_day | 133,056 | 167 | 3.6 |
| gold.gsm_kpi_week | 19,008 | 95 | 2.2 |
| gold.worst_cells_week | 217 | 115 | 0.3 |
| gold.kpi_catalog | 17 | 2 | 0.0 |
| gold.loads | 174 | 85 | 0.1 |
| total | | | 1,517.3 |

## Cost

Measured on the build machine; varies run to run. Each dbt run is its own process.

| Step | Value |
|---|---|
| UTC days built | 84 |
| dbt runs | 92 |
| dbt runs rerun after a wall-clock step | 0 |
| seconds, days | 1,498.2 |
| seconds, revision reprocessing | 113.6 |
| seconds, build total | 1,623.3 |
| seconds, model check | 110.1 |
| peak RSS, Python (MB) | 749 |
| peak RSS, largest dbt run (MB) | 1,046 |
