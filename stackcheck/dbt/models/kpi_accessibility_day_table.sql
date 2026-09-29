{#- Rule D6 the plain dbt way: a table model rebuilt with the new formula. -#}
{{ config(materialized='table') }}

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
