{#- Rule D6 through an incremental model: each formula version is merged in
    beside the others, keyed by formula_version, so both stay queryable. -#}
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['cell_id', 'day', 'formula_version'],
) }}

{%- set formula_version = var('formula_version') | int %}

select
    cell_id,
    cast(period_start as date) as day,
    {{ formula_version }} as formula_version,
    {%- if formula_version == 1 %}
    sum(rrc_succ) / sum(rrc_att) as value
    {%- else %}
    (sum(rrc_succ) / sum(rrc_att)) * (sum(erab_succ) / sum(erab_att)) as value
    {%- endif %}
from {{ source('ran', 'counters') }}
group by cell_id, cast(period_start as date)
