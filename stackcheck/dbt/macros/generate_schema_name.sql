{#- Use the configured schema as is: the default prefixes the target schema,
    naming an Iceberg namespace that does not exist. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
  {%- if custom_schema_name is none -%}{{ target.schema }}{%- else -%}{{ custom_schema_name | trim }}{%- endif -%}
{%- endmacro %}
