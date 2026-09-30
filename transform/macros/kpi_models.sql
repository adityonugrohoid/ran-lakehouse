{#- The body of a KPI model: sum the cell counters over each window of the
    granularity, then apply the formulas. Windows: 15 minutes and the hour
    in UTC; the day and the week (from Monday) in WIB, the operator's local
    time (rule W5). Expected periods count 15-minute periods (60-minute ones
    for the CQI KPI) so coverage shows gaps (rule D3). -#}
{% macro window_expr(granularity) -%}
  {%- if granularity == '15m' -%}period_start
  {%- elif granularity == 'hour' -%}date_trunc('hour', period_start)
  {%- elif granularity == 'day' -%}cast(period_start + interval 7 hour as date)
  {%- else -%}cast(date_trunc('week', period_start + interval 7 hour) as date)
  {%- endif -%}
{%- endmacro %}

{% macro window_column(granularity) -%}
  {%- if granularity in ('15m', 'hour') -%}period_start
  {%- elif granularity == 'day' -%}day
  {%- else -%}week_start
  {%- endif -%}
{%- endmacro %}

{#- Source rows the window needs, in UTC: the run's UTC day for 15 minutes
    and hours, its WIB days or weeks otherwise. -#}
{% macro window_filter(granularity) -%}
  {%- if granularity in ('15m', 'hour') -%}
    period_start >= {{ ts('day_start') }} and period_start < {{ ts('day_end') }}
  {%- elif granularity == 'day' -%}
    period_start >= {{ ts('local_start') }} and period_start < {{ ts('local_end') }}
  {%- else -%}
    period_start >= {{ ts('week_start') }} and period_start < {{ ts('week_end') }}
  {%- endif -%}
{%- endmacro %}

{% macro kpi_config(granularity) -%}
  {%- set column = window_column(granularity) -%}
  {%- if granularity in ('15m', 'hour') -%}
    {%- set predicates = kpi_predicates(column, 'day_start', 'day_end', 'ts') -%}
    {%- set hook = "{{ partition_by('day(period_start)') }}" -%}
  {%- elif granularity == 'day' -%}
    {#- Day and week tables are small and left unpartitioned: DuckDB 1.5
        cannot read them after a month() or year() spec is added. -#}
    {%- set predicates = kpi_predicates(column, 'local_first', 'local_last', 'date') -%}
    {%- set hook = "select 1" -%}
  {%- else -%}
    {%- set predicates = kpi_predicates(column, 'week_first', 'week_last', 'date') -%}
    {%- set hook = "select 1" -%}
  {%- endif -%}
  {{ config(
      unique_key=['cell_name', column, 'kpi_id', 'formula_version'],
      incremental_predicates=predicates,
      post_hook={'sql': hook, 'transaction': false},
  ) }}
{%- endmacro %}

{% macro periods_expected(granularity, minutes) -%}
  {%- set per = {'15m': 15, 'hour': 60, 'day': 1440, 'week': 10080}[granularity] -%}
  {{ (per // minutes) }}
{%- endmacro %}

{% macro kpi_model(technology, granularity) -%}
{{ kpi_config(granularity) }}
{%- set column = window_column(granularity) %}
with agg as (
    select cell_name, {{ window_expr(granularity) }} as {{ column }},
        {{ lte_sums() if technology == 'lte' else gsm_sums() }}
    from {{ ref(technology ~ '_cell_15m') }}
    where {{ window_filter(granularity) }}
    group by all
)
{{ lte_kpi_rows(column, periods_expected(granularity, 15)) if technology == 'lte'
   else gsm_kpi_rows(column, periods_expected(granularity, 15)) }}
{%- if technology == 'lte' and granularity != '15m' and wanted('LTE_CQI_MEAN', 1) %}
union all
select * from (
    with agg as (
        select cell_name, {{ window_expr(granularity) }} as {{ column }}, {{ cqi_sums() }}
        from {{ ref('lte_cell_60m') }}
        where {{ window_filter(granularity) }}
        group by all
    )
    {{ cqi_kpi_rows(column, periods_expected(granularity, 60)) }}
)
{%- endif %}
{%- endmacro %}
