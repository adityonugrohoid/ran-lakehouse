# KPI catalog

Gold KPIs of the synthetic network (rules L3, E4). Every value is a ratio of sums over
the window's reported periods, never an average of ratios; a zero denominator gives
no value (NULL), never 0. Each value carries its coverage (reported / expected
15-minute periods; hourly periods for the CQI KPI) and its suspect share (suspect /
reported periods), so gaps (rule D3) and suspect data (rule D4) stay visible.
Granularities: 15 minutes and hours in UTC, days and weeks (from Monday) in WIB.
Values are per cell. Over several cells, ratio KPIs sum their counters first; PRB
utilization is weighted by each cell's N_RB (gold.cells, TS 36.101 Table 5.6-1).
Counter names: TS 32.425 (LTE) and TS 52.402 (GSM). Breach thresholds drive the
weekly worst-cell ranking (rule L5): a day is judged when its coverage is at least
0.75; a cell is persistent when it breaches on 3 of the week's 7 days. Thresholds, N and the coverage floor are START values.
Written by `python -m ran_lakehouse.lake.gold_report` from `kpi_catalog.json`.

| Id | v | Name | Unit | Source | Granularities | Vendors | Breach (START) |
|---|---|---|---|---|---|---|---|
| LTE_ERAB_ACC | 1 | E-RAB accessibility | % | 3GPP TS 32.450 V19.0.0 clause 6.1.1 | 15m, hour, day, week | huawei, nokia | below 92 |
| LTE_ERAB_RET | 1 | E-RAB retainability (R2, UE level) | releases per session hour | 3GPP TS 32.450 V19.0.0 clause 6.2.1 | 15m, hour, day, week | nokia |  |
| LTE_IP_THP_DL | 1 | E-UTRAN IP throughput, downlink | kbit/s | 3GPP TS 32.450 V19.0.0 clause 6.3.1 | 15m, hour, day, week | huawei, nokia |  |
| LTE_AVAIL | 1 | E-UTRAN cell availability | % | 3GPP TS 32.450 V19.0.0 clause 6.4.1 | 15m, hour, day, week | huawei, nokia | below 99 |
| LTE_MOB_HOSR | 1 | E-UTRAN mobility (handover success) | % | 3GPP TS 32.450 V19.0.0 clause 6.5.1, execution phase: its HO.ExeSucc / HO.ExeAtt mapped to the TS 32.425 per-relation HO.OutSuccTarget / HO.OutAttTarget | 15m, hour, day, week | huawei, nokia | below 97 |
| LTE_RRC_SSR | 1 | RRC setup success rate | % | operator-defined | 15m, hour, day, week | huawei, nokia | below 95 |
| LTE_ERAB_DROP | 1 | E-RAB drop rate | % | operator-defined | 15m, hour, day, week | huawei, nokia | above 2.5 |
| LTE_PRB_UTIL | 1 | DL PRB utilization | % | operator-defined | 15m, hour, day, week | huawei, nokia | above 80 |
| LTE_CQI_MEAN | 1 | Mean downlink wideband CQI | CQI index | operator-defined | hour, day, week | huawei, nokia |  |
| GSM_SAS | 1 | GERAN service access success rate, CS | % | 3GPP TS 32.410 V19.0.0 clause 7.4 | 15m, hour, day, week | huawei, nokia | below 96.5 |
| GSM_ABN_REL | 1 | GERAN service abnormal release rate | % | 3GPP TS 32.410 V19.0.0 clause 8.2, without the intra-cell handover terms (not modelled) | 15m, hour, day, week | huawei |  |
| GSM_HOSR | 1 | Handover success rate (cell) | % | 3GPP TS 32.410 V19.0.0 clause 9.5 | 15m, hour, day, week | huawei, nokia | below 94.5 |
| GSM_CSSR | 1 | Call setup success rate | % | operator-defined, vendor-style definition | 15m, hour, day, week | huawei, nokia | below 96 |
| GSM_TCH_BLOCK | 1 | TCH blocking | % | operator-defined, vendor-style definition | 15m, hour, day, week | huawei, nokia | above 3 |
| GSM_SDCCH_BLOCK | 1 | SDCCH blocking | % | operator-defined, vendor-style definition | 15m, hour, day, week | huawei, nokia | above 1 |
| GSM_SDCCH_DROP | 1 | SDCCH drop rate | % | operator-defined, vendor-style definition | 15m, hour, day, week | nokia |  |
| LTE_RRC_SSR | 2 | RRC setup success rate (with S1 signalling) | % | operator-defined | 15m, hour, day, week | huawei, nokia | below 95 |

## Formulas and where operators differ

### LTE_ERAB_ACC v1: E-RAB accessibility

`100 * (RRC.ConnEstabSucc.sum / RRC.ConnEstabAtt.sum) * (S1SIG.ConnEstabSucc / S1SIG.ConnEstabAtt) * (ERAB.EstabInitSuccNbr.sum / ERAB.EstabInitAttNbr.sum)`

Which RRC establishment causes count (mobile-originated only, or all including emergency and signalling), and whether the S1 term is included.

### LTE_ERAB_RET v1: E-RAB retainability (R2, UE level)

`3600 * ERAB.RelActNbr.sum / ERAB.SessionTimeUE`

Per QCI (R1) or per UE (R2); many operators report a drop rate per established E-RAB instead (LTE_ERAB_DROP). The Huawei-style dictionary carries no session-time counter, so it is computed for Nokia-style cells only.

### LTE_IP_THP_DL v1: E-UTRAN IP throughput, downlink

`1000 * DRB.IPVolDl.sum [kbit] / DRB.IPTimeDl.sum [ms]`

Whether the last TTI emptying the buffer is excluded (TS 32.450 excludes it); many operators also report cell throughput over the whole period.

### LTE_AVAIL v1: E-UTRAN cell availability

`100 * (900 * reported periods - RRU.CellUnavailableTime.sum) / (900 * reported periods)`

Whether planned maintenance and energy-saving shutdowns count as unavailable. Nokia-style availability is sampled every 10 s (ASSUMPTION), so its unavailable time has 10 s resolution.

### LTE_MOB_HOSR v1: E-UTRAN mobility (handover success)

`100 * sum over neighbour relations of HO.OutSuccTarget.sum / sum of HO.OutAttTarget.sum`

Whether the preparation phase is included (TS 32.450 includes it; the model has no preparation counters) and whether inter-RAT handovers count.

### LTE_RRC_SSR v1: RRC setup success rate

`100 * RRC.ConnEstabSucc.sum / RRC.ConnEstabAtt.sum`

Establishment causes counted; whether re-attempts within a few seconds count once; whether the S1 signalling connection must also succeed (v2).

### LTE_ERAB_DROP v1: E-RAB drop rate

`100 * ERAB.RelActNbr.sum / ERAB.EstabInitSuccNbr.sum`

Which release causes count as drops (ERAB.RelActNbr counts abnormal releases with data in the buffer) and the denominator (established E-RABs, incoming handovers included or not).

### LTE_PRB_UTIL v1: DL PRB utilization

`sum of RRU.PrbTotDl [%] / reported periods`

Mean over all periods or over the busy hour only; DL only or DL and UL. Gold's values are per cell; over several cells (an area or the network) PRB utilization is weighted by each cell's downlink resource blocks: sum(RRU.PrbTotDl * N_RB) / sum(N_RB), N_RB from the cell's bandwidth (TS 36.101 Table 5.6-1, gold.cells).

### LTE_CQI_MEAN v1: Mean downlink wideband CQI

`sum over bins i of i * CARR.WBCQIDist.Bin[i] / sum of CARR.WBCQIDist.Bin[i] (CQI index 0 to 15, TS 36.213 Table 7.2.3-1)`

Mean index or share of samples at CQI 7 and above. Reported hourly (rules P1, P4), so it has no 15-minute value.

### GSM_SAS v1: GERAN service access success rate, CS

`100 * (succTCHSeizures / attTCHSeizures) * (succImmediateAssingProcs / attImmediateAssingProcs)`

TS 32.410 excludes SDCCH set-up repetitions; vendor counters often do not. attTCHSeizures here is the vendor TCH request count minus the requests that met all TCHs busy.

### GSM_ABN_REL v1: GERAN service abnormal release rate

`100 * (nbrOfLostRadioLinksTCH + unsuccHDOsWithReconnection + unsuccHDOsWithLossOfConnection) / (succTCHSeizures + succIncomingInternalInterCellHDOs)`

Operators usually call a TCH drop rate their own formula; the Nokia-style dictionary carries no unsuccessful-handover counter, so it is computed for Huawei-style cells only.

### GSM_HOSR v1: Handover success rate (cell)

`100 * succOutgoingInternalInterCellHDOs / attOutgoingInternalInterCellHDOs`

Internal only or including external (inter-BSC) handovers.

### GSM_CSSR v1: Call setup success rate

`100 * (1 - attSDCCHSeizuresMeetingSDCCHBlockedState / attImmediateAssingProcs) * succTCHSeizures / (attTCHSeizures + attTCHSeizuresMeetingTCHBlockedState)`

The most varied GSM KPI: vendors and operators multiply different SDCCH, TCH assignment and drop terms.

### GSM_TCH_BLOCK v1: TCH blocking

`100 * attTCHSeizuresMeetingTCHBlockedState / (attTCHSeizures + attTCHSeizuresMeetingTCHBlockedState)`

Denominator with or without the blocked requests; handover requests counted or not.

### GSM_SDCCH_BLOCK v1: SDCCH blocking

`100 * attSDCCHSeizuresMeetingSDCCHBlockedState / attImmediateAssingProcs`

Denominator: immediate assignment attempts or SDCCH seizure attempts.

### GSM_SDCCH_DROP v1: SDCCH drop rate

`100 * nbrOfLostRadioLinksSDCCH / succImmediateAssingProcs`

Which SDCCH releases count as drops. The Huawei-style dictionary carries no SDCCH lost-radio-link counter, so it is computed for Nokia-style cells only.

### LTE_RRC_SSR v2: RRC setup success rate (with S1 signalling)

`100 * (RRC.ConnEstabSucc.sum / RRC.ConnEstabAtt.sum) * (S1SIG.ConnEstabSucc / S1SIG.ConnEstabAtt)`

Revised by the operator: a set-up now counts only when the S1 signalling connection also succeeds. History is reprocessed; v1 and v2 are both kept. Current from 2026-03-02 (WIB).

