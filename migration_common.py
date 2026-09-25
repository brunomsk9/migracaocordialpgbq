"""Catálogo e nomes compartilhados pela migração e pela validação."""
import re
from contextlib import contextmanager

OBJECTS_SQL = """
    SELECT n.nspname, c.relname,
           CASE c.relkind WHEN 'm' THEN 'MATERIALIZED VIEW'
                WHEN 'v' THEN 'VIEW' WHEN 'f' THEN 'FOREIGN'
                WHEN 'p' THEN 'PARTITIONED TABLE' ELSE 'BASE TABLE' END
      FROM pg_catalog.pg_class c
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
       AND n.nspname <> 'information_schema'
       AND n.nspname !~ '^pg_'
"""

COLUMNS_SQL = """
    SELECT a.attname AS column_name,
           CASE WHEN t.typcategory = 'A' THEN 'ARRAY' ELSE t.typname END AS data_type,
           t.typname AS udt_name,
           CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS is_nullable,
           CASE WHEN t.typname = 'numeric' AND a.atttypmod >= 4
                THEN ((a.atttypmod - 4) >> 16) & 65535 END AS numeric_precision,
           CASE WHEN t.typname = 'numeric' AND a.atttypmod >= 4
                THEN (((a.atttypmod - 4) & 2047) # 1024) - 1024 END AS numeric_scale
      FROM pg_catalog.pg_attribute a
      JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
      JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
     WHERE n.nspname = %s AND c.relname = %s
       AND a.attnum > 0 AND NOT a.attisdropped
     ORDER BY a.attnum
"""


def bq_identifier(value, kind='identificador'):
    # Preserva exatamente a convenção histórica da migração, inclusive acentos.
    normalized = re.sub(r'[^A-Za-z0-9_]', '_', value)
    normalized = re.sub(r'_+', '_', normalized).strip('_')
    if not normalized or len(normalized) > 1024:
        raise ValueError(f'Nome de {kind} inválido após normalização: {value!r}')
    return normalized


def validate_bq_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,1024}", value):
        raise ValueError(f"Identificador BigQuery inválido: {value!r}")
    return value


def reject_collisions(pairs):
    """Recusa múltiplas origens no mesmo destino antes de escrever."""
    seen = {}
    for source, destination in pairs:
        if destination in seen and seen[destination] != source:
            raise ValueError(f'Colisão de destino {destination}: {seen[destination]} e {source}')
        seen[destination] = source


@contextmanager
def closing_connection(connect, **kwargs):
    # O context manager nativo de psycopg2 encerra a transação, mas não a conexão.
    conn = connect(**kwargs)
    try:
        with conn:
            yield conn
    finally:
        conn.close()
