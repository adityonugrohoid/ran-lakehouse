{#- The window a run builds. ran_lakehouse.lake.gold passes, in UTC:
    day_start/day_end (the UTC day of silver being built), local_start/
    local_end (the WIB days that day touches) and week_start/week_end (the
    WIB weeks), plus local_first/local_last and week_first/week_last as
    dates. -#}
{% macro ts(name) -%}TIMESTAMPTZ '{{ var(name) }}'{%- endmacro %}

{% macro dt(name) -%}DATE '{{ var(name) }}'{%- endmacro %}

{#- Merge predicates: the target rows a run may touch, so the MERGE reads
    only those partitions; optionally only one KPI (history reprocessing,
    rule D6). -#}
{% macro kpi_predicates(column, low, high, kind, per_kpi=true) -%}
  {%- set out = [] -%}
  {%- if kind == 'ts' -%}
    {%- do out.append("DBT_INTERNAL_DEST." ~ column ~ " >= " ~ ts(low)) -%}
    {%- do out.append("DBT_INTERNAL_DEST." ~ column ~ " < " ~ ts(high)) -%}
  {%- else -%}
    {%- do out.append("DBT_INTERNAL_DEST." ~ column ~ " >= " ~ dt(low)) -%}
    {%- do out.append("DBT_INTERNAL_DEST." ~ column ~ " <= " ~ dt(high)) -%}
  {%- endif -%}
  {%- if per_kpi and var('only_kpi') -%}
    {%- do out.append("DBT_INTERNAL_DEST.kpi_id = '" ~ var('only_kpi') ~ "'") -%}
  {%- endif -%}
  {{ return(out) }}
{%- endmacro %}

{#- Partition an Iceberg table when it is first created (the file target
    for tests has no partitioning). -#}
{% macro partition_by(spec) -%}
  {%- if var('partitioned') and not is_incremental() -%}
    ALTER TABLE {{ this }} SET PARTITIONED BY ({{ spec }})
  {%- else -%}
    SELECT 1
  {%- endif -%}
{%- endmacro %}
