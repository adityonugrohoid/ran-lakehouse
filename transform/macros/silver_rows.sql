{#- Silver rows of the run's UTC day at one granularity, with the DN of the
    cell each row belongs to (a neighbour relation belongs to its source
    cell: its DN is the cell DN plus one relation component). -#}
{% macro silver_rows(granularity, measurements) -%}
select
    m.ems, m.vendor, m.period_start, m.measurement, m.bin, m.value, m.suspect,
    regexp_replace(m.object_dn, '(,(EUtranRelation|GsmRelation)=[^,]*|/(LNREL|ADJS)-[0-9]+)$', '')
        as cell_dn
from {{ source('silver', 'pm_measurements') }} m
where m.period_start >= {{ ts('day_start') }} and m.period_start < {{ ts('day_end') }}
    and m.granularity_min = {{ granularity }}
    and m.measurement in ('{{ measurements | join("', '") }}')
{%- endmacro %}

{% macro pivot(columns) -%}
  {%- for name, measurement in columns %}
    sum(r.value) filter (where r.measurement = '{{ measurement }}') as {{ name }},
  {%- endfor %}
{%- endmacro %}
