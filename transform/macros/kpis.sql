{#- KPI formulas (rule L3), one place for every granularity. Each KPI is a
    ratio of sums over the window's reported periods, never an average of
    ratios. A KPI is emitted only where its vendor's dictionary carries the
    counters it needs; a zero denominator gives NULL, never 0. The catalog
    (ran_lakehouse.lake.kpi_catalog) states each formula and its source. -#}

{#- Sums of the cell counters over a window, grouped by the caller. -#}
{% macro lte_sums() -%}
    any_value(vendor) as vendor,
    sum(rrc_att) as rrc_att, sum(rrc_succ) as rrc_succ,
    sum(s1_att) as s1_att, sum(s1_succ) as s1_succ,
    sum(erab_att) as erab_att, sum(erab_succ) as erab_succ,
    sum(erab_rel) as erab_rel, sum(session_s) as session_s,
    sum(ip_vol_kbit) as ip_vol_kbit, sum(ip_time_ms) as ip_time_ms,
    sum(prb_pct) as prb_sum, count(prb_pct) as prb_n,
    sum(unavail_s) as unavail_s, count(unavail_s) as unavail_n,
    sum(ho_att) as ho_att, sum(ho_succ) as ho_succ,
    count(*) filter (where reported) as periods_reported,
    count(*) filter (where reported and suspect) as periods_suspect
{%- endmacro %}

{% macro gsm_sums() -%}
    any_value(vendor) as vendor,
    sum(tch_req) as tch_req, sum(tch_blocked) as tch_blocked, sum(tch_succ) as tch_succ,
    sum(ia_att) as ia_att, sum(ia_succ) as ia_succ,
    sum(sdcch_blocked) as sdcch_blocked, sum(sdcch_lost) as sdcch_lost,
    sum(tch_lost) as tch_lost, sum(ho_att) as ho_att, sum(ho_succ) as ho_succ,
    sum(ho_unsucc) as ho_unsucc, sum(ho_in) as ho_in,
    count(*) filter (where reported) as periods_reported,
    count(*) filter (where reported and suspect) as periods_suspect
{%- endmacro %}

{% macro cqi_sums() -%}
    any_value(vendor) as vendor,
    sum(cqi_weighted) as cqi_weighted, sum(cqi_samples) as cqi_samples,
    count(*) filter (where reported) as periods_reported,
    count(*) filter (where reported and suspect) as periods_suspect
{%- endmacro %}

{#- Whether this run computes a KPI version: the versions var lists the
    versions per KPI (default [1]); only_kpi restricts a run to one KPI. -#}
{% macro wanted(kpi_id, version) -%}
  {%- set versions = var('versions').get(kpi_id, [1]) -%}
  {%- set only = var('only_kpi') -%}
  {{ return(version in versions and (not only or only == kpi_id)) }}
{%- endmacro %}

{% macro kpi_row(kpi_id, version, value, numerator, denominator, needs, period, expected) -%}
select
    cell_name, {{ period }}, vendor,
    '{{ kpi_id }}' as kpi_id,
    {{ version }} as formula_version,
    cast({{ value }} as double) as value,
    cast({{ numerator }} as double) as numerator,
    cast({{ denominator }} as double) as denominator,
    {{ expected }} as periods_expected,
    periods_reported,
    periods_reported / {{ expected }} as coverage,
    periods_suspect / nullif(periods_reported, 0) as suspect_share
from agg
where {{ needs }}
{%- endmacro %}

{% macro lte_kpi_rows(period, expected) -%}
  {%- set rows = [
    ('LTE_ERAB_ACC', 1,
     '100 * (rrc_succ / nullif(rrc_att, 0)) * (s1_succ / nullif(s1_att, 0)) * (erab_succ / nullif(erab_att, 0))',
     'null', 'null', 'rrc_att is not null and s1_att is not null and erab_att is not null'),
    ('LTE_ERAB_RET', 1, '3600 * erab_rel / nullif(session_s, 0)', 'erab_rel', 'session_s / 3600',
     'erab_rel is not null and session_s is not null'),
    ('LTE_IP_THP_DL', 1, '1000 * ip_vol_kbit / nullif(ip_time_ms, 0)', 'ip_vol_kbit', 'ip_time_ms / 1000',
     'ip_vol_kbit is not null and ip_time_ms is not null'),
    ('LTE_AVAIL', 1, '100 * (900 * unavail_n - unavail_s) / nullif(900 * unavail_n, 0)',
     '900 * unavail_n - unavail_s', '900 * unavail_n', 'unavail_n > 0'),
    ('LTE_MOB_HOSR', 1, '100 * ho_succ / nullif(ho_att, 0)', 'ho_succ', 'ho_att',
     'ho_att is not null'),
    ('LTE_RRC_SSR', 1, '100 * rrc_succ / nullif(rrc_att, 0)', 'rrc_succ', 'rrc_att',
     'rrc_att is not null'),
    ('LTE_RRC_SSR', 2, '100 * (rrc_succ / nullif(rrc_att, 0)) * (s1_succ / nullif(s1_att, 0))',
     'null', 'null', 'rrc_att is not null and s1_att is not null'),
    ('LTE_ERAB_DROP', 1, '100 * erab_rel / nullif(erab_succ, 0)', 'erab_rel', 'erab_succ',
     'erab_rel is not null and erab_succ is not null'),
    ('LTE_PRB_UTIL', 1, 'prb_sum / nullif(prb_n, 0)', 'prb_sum', 'prb_n', 'prb_n > 0'),
  ] -%}
  {{ emit(rows, period, expected) }}
{%- endmacro %}

{% macro gsm_kpi_rows(period, expected) -%}
  {%- set rows = [
    ('GSM_SAS', 1,
     '100 * (tch_succ / nullif(tch_req - tch_blocked, 0)) * (ia_succ / nullif(ia_att, 0))',
     'null', 'null', 'tch_req is not null and tch_blocked is not null and ia_att is not null'),
    ('GSM_ABN_REL', 1, '100 * (tch_lost + ho_unsucc) / nullif(tch_succ + ho_in, 0)',
     'tch_lost + ho_unsucc', 'tch_succ + ho_in',
     'tch_lost is not null and ho_unsucc is not null and ho_in is not null'),
    ('GSM_HOSR', 1, '100 * ho_succ / nullif(ho_att, 0)', 'ho_succ', 'ho_att', 'ho_att is not null'),
    ('GSM_CSSR', 1,
     '100 * (1 - sdcch_blocked / nullif(ia_att, 0)) * (tch_succ / nullif(tch_req, 0))',
     'null', 'null', 'sdcch_blocked is not null and ia_att is not null and tch_req is not null'),
    ('GSM_TCH_BLOCK', 1, '100 * tch_blocked / nullif(tch_req, 0)', 'tch_blocked', 'tch_req',
     'tch_blocked is not null and tch_req is not null'),
    ('GSM_SDCCH_BLOCK', 1, '100 * sdcch_blocked / nullif(ia_att, 0)', 'sdcch_blocked', 'ia_att',
     'sdcch_blocked is not null and ia_att is not null'),
    ('GSM_SDCCH_DROP', 1, '100 * sdcch_lost / nullif(ia_succ, 0)', 'sdcch_lost', 'ia_succ',
     'sdcch_lost is not null and ia_succ is not null'),
  ] -%}
  {{ emit(rows, period, expected) }}
{%- endmacro %}

{% macro cqi_kpi_rows(period, expected) -%}
  {%- set rows = [
    ('LTE_CQI_MEAN', 1, 'cqi_weighted / nullif(cqi_samples, 0)', 'cqi_weighted', 'cqi_samples',
     'cqi_samples is not null'),
  ] -%}
  {{ emit(rows, period, expected) }}
{%- endmacro %}

{% macro emit(rows, period, expected) -%}
  {%- set kept = [] -%}
  {%- for r in rows -%}
    {%- if wanted(r[0], r[1]) -%}{%- do kept.append(r) -%}{%- endif -%}
  {%- endfor -%}
  {%- if kept | length == 0 -%}
select * from (
    {{ kpi_row('NONE', 0, 'null', 'null', 'null', 'false', period, expected) }}
)
  {%- else -%}
  {%- for r in kept %}
{{ kpi_row(r[0], r[1], r[2], r[3], r[4], r[5], period, expected) }}
{{ 'union all' if not loop.last }}
  {%- endfor -%}
  {%- endif -%}
{%- endmacro %}
