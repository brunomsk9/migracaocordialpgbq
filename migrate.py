#!/usr/bin/env python3
"""Migra tabelas PostgreSQL/PostGIS para BigQuery preservando tipos."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID, uuid4

import psycopg2
from dotenv import load_dotenv
from google.cloud import bigquery
from google.api_core.exceptions import Conflict, NotFound
from migration_common import (OBJECTS_SQL, COLUMNS_SQL, bq_identifier,
                              reject_collisions, closing_connection, validate_bq_id)
from psycopg2 import sql
from psycopg2.extras import RealDictCursor


LOG = logging.getLogger("pg-postgis-bq")


PG_TO_BQ = {
    "smallint": "INTEGER",
    "integer": "INTEGER",
    "bigint": "INTEGER",
    "int2": "INTEGER",
    "int4": "INTEGER",
    "int8": "INTEGER",
    "real": "FLOAT",
    "double precision": "FLOAT",
    "float4": "FLOAT",
    "float8": "FLOAT",
    "numeric": "NUMERIC",
    "decimal": "NUMERIC",
    "bool": "BOOLEAN",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "timestamp without time zone": "DATETIME",
    "timestamp with time zone": "TIMESTAMP",
    "time without time zone": "TIME",
    "timestamp": "DATETIME",
    "timestamptz": "TIMESTAMP",
    "time": "TIME",
    "bpchar": "STRING",
    "character varying": "STRING",
    "character": "STRING",
    "text": "STRING",
    "varchar": "STRING",
    "char": "STRING",
    "uuid": "STRING",
    "json": "JSON",
    "jsonb": "JSON",
    "bytea": "BYTES",
    "geometry": "GEOGRAPHY",
    "geography": "GEOGRAPHY",
}


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    udt_name: str
    nullable: bool
    numeric_precision: int | None
    numeric_scale: int | None

    @property
    def is_spatial(self) -> bool:
        return self.udt_name in {"geometry", "geography"}


@dataclass(frozen=True)
class TableSpec:
    source_schema: str
    source_table: str
    destination_table: str

    @property
    def is_schema_wildcard(self) -> bool:
        return self.source_table == "*"


SYSTEM_DATABASES = {"postgres", "template0", "template1"}
SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast"}


def csv_values(raw: str | None) -> list[str]:
    return [value.strip() for value in (raw or "").split(",") if value.strip()]


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}


def env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else default


def default_destination_table(source_schema: str, source_table: str) -> str:
    """Preserva o schema PostgreSQL no nome plano aceito pelo BigQuery."""
    return bq_identifier(f"{source_schema}_{source_table}", "tabela")


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Variável obrigatória não definida: {name}")
    return value


def parse_tables(raw: str) -> list[TableSpec]:
    """Aceita schema.tabela, schema.* ou alias explícito, separados por vírgula."""
    specs: list[TableSpec] = []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        source, sep, destination = item.partition(":")
        parts = source.split(".", 1)
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"Tabela inválida '{item}'. Use schema.tabela[:destino].")
        schema_name, table_name = parts
        if table_name == "*" and sep:
            raise ValueError(
                f"Curinga com alias não é suportado em '{item}'. Use apenas {schema_name}.*."
            )
        destination_name = destination if sep else (
            "" if table_name == "*" else default_destination_table(schema_name, table_name)
        )
        if sep and not destination_name:
            raise ValueError(f"Destino vazio em '{item}'.")
        if destination_name:
            validate_bq_id(destination_name)
        specs.append(TableSpec(schema_name, table_name, destination_name))
    if not specs:
        raise ValueError("PG_TABLES não contém nenhuma tabela.")
    return specs


def expand_table_specs(conn, specs: list[TableSpec]) -> list[TableSpec]:
    """Expande schema.* para todas as tabelas e views visíveis do schema."""
    expanded: list[TableSpec] = []
    for spec in specs:
        if not spec.is_schema_wildcard:
            expanded.append(spec)
            continue

        query = "SELECT relname FROM (" + OBJECTS_SQL + ") objects(nspname, relname, kind) WHERE nspname = %s ORDER BY relname"
        with conn.cursor() as cur:
            cur.execute(query, (spec.source_schema,))
            rows = cur.fetchall()
        if not rows:
            raise ValueError(
                f"Nenhuma tabela ou view visível encontrada no schema: {spec.source_schema}"
            )
        expanded.extend(
            TableSpec(
                spec.source_schema,
                row[0],
                default_destination_table(spec.source_schema, row[0]),
            )
            for row in rows
        )
    return expanded


def postgres_connection(database: str | None = None):
    params = dict(
        host=os.getenv("PG_HOST", "/var/run/postgresql"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=database or os.getenv("PG_DATABASE", "postgres"),
        user=os.getenv("PG_USER", "postgres"),
        sslmode=os.getenv("PG_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("PG_CONNECT_TIMEOUT", "15")),
        application_name="postgres_postgis_to_bigquery",
    )
    password = os.getenv("PG_PASSWORD", "")
    if password:
        params["password"] = password
    return psycopg2.connect(**params)


def discover_databases(conn) -> list[str]:
    includes = set(csv_values(os.getenv("PG_DATABASES")))
    excludes = SYSTEM_DATABASES | set(csv_values(os.getenv("PG_EXCLUDE_DATABASES")))
    query = """
        SELECT datname
          FROM pg_database
         WHERE datallowconn
           AND NOT datistemplate
         ORDER BY datname
    """
    with conn.cursor() as cur:
        cur.execute(query)
        names = [row[0] for row in cur.fetchall()]
    missing = includes - set(names)
    if missing:
        raise ValueError("Bancos solicitados não encontrados: " + ", ".join(sorted(missing)))
    return [name for name in names if name not in excludes and (not includes or name in includes)]


def discover_tables(conn) -> list[TableSpec]:
    includes = set(csv_values(os.getenv("PG_SCHEMAS")))
    excludes = SYSTEM_SCHEMAS | set(csv_values(os.getenv("PG_EXCLUDE_SCHEMAS")))
    query = "SELECT nspname, relname FROM (" + OBJECTS_SQL + ") objects(nspname, relname, kind) ORDER BY nspname, relname"
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
    missing_schemas = includes - excludes - {schema for schema, _ in rows}
    if missing_schemas:
        raise ValueError("Schemas sem objetos encontrados: " + ", ".join(sorted(missing_schemas)))
    return [
        TableSpec(schema, table, default_destination_table(schema, table))
        for schema, table in rows
        if schema not in excludes
        and not schema.startswith("pg_temp_")
        and not schema.startswith("pg_toast_temp_")
        and (not includes or schema in includes)
    ]


def table_size_bytes(conn, table: TableSpec) -> int:
    """Retorna o tamanho físico; views e objetos sem storage retornam zero."""
    query = """
        SELECT CASE
                 WHEN c.relkind IN ('r', 'm', 'p') THEN pg_total_relation_size(c.oid)
                 ELSE 0
               END
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname = %s
    """
    with conn.cursor() as cur:
        cur.execute(query, (table.source_schema, table.source_table))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def get_columns(conn, table: TableSpec) -> list[Column]:
    query = COLUMNS_SQL
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, (table.source_schema, table.source_table))
        rows = cur.fetchall()
    if not rows:
        raise ValueError(f"Tabela não encontrada: {table.source_schema}.{table.source_table}")
    return [
        Column(
            name=row["column_name"],
            data_type=row["data_type"],
            udt_name=row["udt_name"],
            nullable=row["is_nullable"] == "YES",
            numeric_precision=row["numeric_precision"],
            numeric_scale=row["numeric_scale"],
        )
        for row in rows
    ]


def bq_type(column: Column) -> str:
    if column.data_type == "ARRAY":
        return "STRING"
    key = column.udt_name if column.udt_name in PG_TO_BQ else column.data_type
    mapped = PG_TO_BQ.get(key)
    if not mapped:
        LOG.warning("Tipo PostgreSQL %s (%s) convertido para STRING", column.data_type, column.name)
        return "STRING"
    if mapped == "NUMERIC":
        precision, scale = column.numeric_precision, column.numeric_scale or 0
        if precision is None:
            return "STRING"  # numeric sem limite não cabe garantidamente no BQ
        integer_digits = max(precision - scale, 0)
        if integer_digits > 38 or scale > 38:
            return "STRING"
        if integer_digits > 29 or scale > 9:
            return "BIGNUMERIC"
    return mapped


def bq_schema(columns: list[Column]) -> list[bigquery.SchemaField]:
    fields = []
    for col in columns:
        # Mantemos NULLABLE para que uma linha problemática não invalide a criação.
        fields.append(bigquery.SchemaField(col.name, bq_type(col), mode="NULLABLE"))
    return fields


def select_query(table: TableSpec, columns: list[Column], default_srid: int | None):
    expressions = []
    for col in columns:
        identifier = sql.Identifier(col.name)
        if not col.is_spatial:
            expressions.append(identifier)
            continue
        geometry = sql.SQL("{}::geometry").format(identifier)
        if default_srid:
            geometry = sql.SQL(
                "CASE WHEN ST_SRID({g}) = 0 THEN ST_SetSRID({g}, {srid}) ELSE {g} END"
            ).format(g=geometry, srid=sql.Literal(default_srid))
        expression = sql.SQL(
            "CASE WHEN {c} IS NULL OR ST_IsEmpty({g}) THEN NULL "
            "ELSE ST_AsText(ST_Transform(ST_MakeValid(ST_Force2D({g})), 4326)) END AS {alias}"
        ).format(c=identifier, g=geometry, alias=identifier)
        expressions.append(expression)
    return sql.SQL("SELECT {fields} FROM {schema}.{table}").format(
        fields=sql.SQL(", ").join(expressions),
        schema=sql.Identifier(table.source_schema),
        table=sql.Identifier(table.source_table),
    )


def json_value(value: Any, target_type: str) -> Any:
    if value is None:
        return None
    if target_type == "JSON":
        # psycopg2 já decodifica JSON, inclusive strings, números e booleanos.
        return value
    if target_type == "STRING":
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, memoryview):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def read_batches(
    conn,
    table: TableSpec,
    columns: list[Column],
    batch_size: int,
    max_rows: int = 0,
) -> Iterator[list[dict]]:
    type_by_name = {col.name: bq_type(col) for col in columns}
    cursor_name = f"migrate_{table.source_schema}_{table.source_table}"[:60]
    with conn.cursor(name=cursor_name, cursor_factory=RealDictCursor) as cur:
        cur.itersize = batch_size
        cur.execute(select_query(table, columns, get_default_srid()))
        emitted = 0
        while True:
            remaining = max_rows - emitted if max_rows else batch_size
            if max_rows and remaining <= 0:
                break
            rows = cur.fetchmany(min(batch_size, remaining))
            if not rows:
                break
            emitted += len(rows)
            yield [
                {key: json_value(value, type_by_name[key]) for key, value in row.items()}
                for row in rows
            ]


def get_default_srid() -> int | None:
    value = os.getenv("PG_DEFAULT_SRID", "").strip()
    return int(value) if value else None


def ensure_dataset(client: bigquery.Client, dataset_id: str, location: str) -> None:
    dataset = bigquery.Dataset(f"{client.project}.{dataset_id}")
    dataset.location = location
    client.create_dataset(dataset, exists_ok=True)


def dataset_for_database(database: str) -> str:
    return bq_identifier(database, "dataset")


class CheckpointStore:
    """Registra somente tabelas publicadas com sucesso; escrita atômica local."""

    def __init__(self, path: str):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.data = {"completed": {}}
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Checkpoint inválido em {self.path}: {exc}") from exc

        if not isinstance(self.data, dict) or not isinstance(self.data.get("completed"), dict):
            raise ValueError(f"Estrutura de checkpoint inválida: {self.path}")
        if any(not isinstance(entry, dict) or not isinstance(entry.get("rows"), int)
               for entry in self.data["completed"].values()):
            raise ValueError(f"Registros de checkpoint inválidos: {self.path}")

    def key(self, database: str, table: TableSpec) -> str:
        return f"{database}.{table.source_schema}.{table.source_table}"

    def is_completed(self, database: str, table: TableSpec) -> bool:
        entry = self.data.get("completed", {}).get(self.key(database, table), {})
        return bool(entry) and entry.get("destination_table") == table.destination_table

    def complete(self, database: str, table: TableSpec, rows: int) -> None:
        self.data.setdefault("completed", {})[self.key(database, table)] = {
            "rows": rows,
            "destination_table": table.destination_table,
            "completed_at": datetime.now().astimezone().isoformat(),
        }
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)


def run_with_retry(operation, retries: int, delay_seconds: float, description: str):
    for attempt in range(retries + 1):
        try:
            return operation()
        except Exception:
            if attempt >= retries:
                raise
            wait = delay_seconds * (2 ** attempt)
            LOG.warning("%s falhou; nova tentativa %d/%d em %.1fs", description, attempt + 1, retries, wait)
            time_module.sleep(wait)


def migrate_table(
    conn,
    client: bigquery.Client,
    dataset_id: str,
    table: TableSpec,
    batch_size: int,
    write_mode: str,
    retries: int = 3,
    retry_delay: float = 2.0,
    batch_pause: float = 0.0,
    max_rows: int = 0,
) -> int:
    columns = get_columns(conn, table)
    schema = bq_schema(columns)
    destination = table.destination_table
    if max_rows:
        destination = f"{destination[:1008]}__sample"
    table_id = f"{client.project}.{dataset_id}.{destination}"
    run_id = uuid4().hex
    staging_name = f"{table.destination_table[:970]}__loading_{run_id}"
    staging_id = f"{client.project}.{dataset_id}.{staging_name}"
    staging = bigquery.Table(staging_id, schema=schema)
    client.create_table(staging, exists_ok=False)

    def submit_job(submit, job_id):
        try:
            job = submit(job_id)
        except Conflict:
            job = client.get_job(job_id, location=client.location)
        job.result()

    total = 0
    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        ignore_unknown_values=False,
        max_bad_records=0,
    )
    try:
        for batch_number, rows in enumerate(
            read_batches(conn, table, columns, batch_size, max_rows=max_rows), start=1
        ):
            def load_batch():
                submit_job(
                    lambda job_id: client.load_table_from_json(
                        rows, staging_id, job_config=job_config, job_id=job_id),
                    f"migration_{run_id}_batch_{batch_number}",
                )

            run_with_retry(load_batch, retries, retry_delay, f"lote {batch_number} de {table.destination_table}")
            total += len(rows)
            LOG.info("%s: lote %d carregado (%d registros acumulados)", table.destination_table, batch_number, total)
            if batch_pause > 0:
                time_module.sleep(batch_pause)

        staged = client.get_table(staging_id)
        if staged.num_rows != total:
            raise RuntimeError(f"validação falhou: origem lida={total}, preparação no BQ={staged.num_rows}")

        copy_config = bigquery.CopyJobConfig(
            write_disposition=(
                bigquery.WriteDisposition.WRITE_TRUNCATE
                if write_mode == "truncate"
                else bigquery.WriteDisposition.WRITE_APPEND
            )
        )
        def publish():
            submit_job(
                lambda job_id: client.copy_table(
                    staging_id, table_id, job_config=copy_config, job_id=job_id),
                f"migration_{run_id}_publish",
            )
        run_with_retry(publish, retries, retry_delay, f"publicação de {table.destination_table}")
        LOG.info("%s concluída e publicada: %d registros", table_id, total)
        return total
    finally:
        try:
            client.delete_table(staging_id, not_found_ok=True)
        except Exception:
            LOG.warning("Não foi possível remover preparação %s", staging_id, exc_info=True)


def main() -> int:
    env_parser = argparse.ArgumentParser(add_help=False)
    env_parser.add_argument("--env-file", default=str(Path(__file__).resolve().parent / ".env"))
    env_args, _ = env_parser.parse_known_args()
    load_dotenv(env_args.env_file)
    parser = argparse.ArgumentParser(description=__doc__, parents=[env_parser])
    parser.add_argument("--tables", help="Sobrescreve PG_TABLES")
    parser.add_argument("--databases", help="Bancos a migrar, separados por vírgula")
    parser.add_argument("--schemas", help="Schemas a migrar, separados por vírgula")
    parser.add_argument("--inventory", action="store_true", help="Lista bancos, schemas e objetos sem acessar o BigQuery")
    parser.add_argument("--dry-run", action="store_true", help="Mostra o plano, inclusive destinos, sem criar ou carregar")
    parser.add_argument("--fail-fast", action="store_true", default=env_bool("FAIL_FAST"), help="Interrompe no primeiro banco/tabela com erro")
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("BATCH_SIZE", "5000")))
    parser.add_argument("--batch-pause", type=float, default=float(os.getenv("BATCH_PAUSE_SECONDS", "0")))
    parser.add_argument("--retries", type=int, default=env_int("MAX_RETRIES", 3))
    parser.add_argument("--retry-delay", type=float, default=float(os.getenv("RETRY_DELAY_SECONDS", "2")))
    parser.add_argument("--max-table-bytes", type=int, default=env_int("MAX_TABLE_BYTES", 0), help="Ignora objetos maiores que este limite; 0 desativa")
    parser.add_argument("--max-rows", type=int, default=env_int("MAX_ROWS_PER_TABLE", 0), help="Limite de segurança/teste; 0 migra tudo")
    parser.add_argument("--checkpoint", default=os.getenv("CHECKPOINT_FILE", ".migration-checkpoint.json"))
    parser.add_argument("--resume", action="store_true", default=env_bool("RESUME", False), help="Ignora tabelas já concluídas no checkpoint")
    parser.add_argument("--table-order", choices=("smallest", "largest", "name"), default=os.getenv("TABLE_ORDER", "smallest"))
    parser.add_argument("--write-mode", choices=("truncate", "append"), default=os.getenv("WRITE_MODE", "truncate"))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")

    if args.max_rows < 0 or args.max_table_bytes < 0:
        parser.error("limites não podem ser negativos")
    if args.batch_size < 1:
        parser.error("--batch-size deve ser maior que zero")
    if min(args.batch_pause, args.retry_delay) < 0 or args.retries < 0:
        parser.error("pausas e tentativas não podem ser negativas")

    location = os.getenv("BQ_LOCATION", "US")
    if args.databases:
        os.environ["PG_DATABASES"] = args.databases
    if args.schemas:
        os.environ["PG_SCHEMAS"] = args.schemas

    automatic = env_bool("AUTO_DISCOVER", True) and not args.tables
    if automatic:
        with closing_connection(lambda: postgres_connection(os.getenv("PG_ADMIN_DATABASE", "postgres"))) as admin_conn:
            databases = discover_databases(admin_conn)
    else:
        databases = [required_env("PG_DATABASE")]
    if not databases:
        raise ValueError("Nenhum banco PostgreSQL selecionado para migração.")

    if automatic:
        reject_collisions((db, dataset_for_database(db)) for db in databases)
    client = None if args.inventory or args.dry_run else bigquery.Client(project=required_env("BQ_PROJECT"), location=location)
    failures: list[str] = []
    checkpoint_path = Path(args.checkpoint)
    if not checkpoint_path.is_absolute():
        checkpoint_path = Path(env_args.env_file).resolve().parent / checkpoint_path
    checkpoint = CheckpointStore(str(checkpoint_path))
    context = {key: os.getenv(key, "") for key in (
        "PG_HOST", "PG_PORT", "PG_USER", "BQ_PROJECT", "BQ_DATASET", "PG_DEFAULT_SRID")}
    context["write_mode"] = args.write_mode
    if client is not None and args.resume and checkpoint.data.get("context") != context:
        raise ValueError("Checkpoint antigo ou de outro contexto; execute sem --resume para iniciar uma nova rodada")
    if not args.resume:
        checkpoint.data = {"context": context, "completed": {}}
        if client is not None:
            checkpoint.save()
    for database in databases:
        dataset = dataset_for_database(database) if automatic else (os.getenv("BQ_DATASET") or dataset_for_database(database))
        validate_bq_id(dataset)
        try:
            with closing_connection(lambda: postgres_connection(database)) as conn:
                conn.set_session(readonly=True, autocommit=False)
                statement_timeout = env_int("PG_STATEMENT_TIMEOUT_MS", 0)
                if statement_timeout:
                    with conn.cursor() as timeout_cur:
                        timeout_cur.execute("SET statement_timeout = %s", (statement_timeout,))
                if automatic:
                    tables = discover_tables(conn)
                else:
                    tables = expand_table_specs(conn, parse_tables(args.tables or required_env("PG_TABLES")))
                tables = list(dict.fromkeys(tables))
                reject_collisions(((t.source_schema, t.source_table), t.destination_table) for t in tables)
                sizes = {table: table_size_bytes(conn, table) for table in tables}
                if args.table_order == "smallest":
                    tables.sort(key=lambda item: (sizes[item], item.source_schema, item.source_table))
                elif args.table_order == "largest":
                    tables.sort(key=lambda item: (-sizes[item], item.source_schema, item.source_table))
                LOG.info("Banco %s: %d tabela(s)/view(s), dataset %s", database, len(tables), dataset)
                if not tables:
                    failures.append(f"banco {database}: nenhum objeto selecionado")
                    LOG.warning("Banco %s não possui objetos selecionados", database)
                    continue
                if client is not None:
                    ensure_dataset(client, dataset, location)
                for table in tables:
                    LOG.info("%s.%s.%s → %s.%s", database, table.source_schema, table.source_table, dataset, table.destination_table)
                    if client is None:
                        continue
                    if args.resume and checkpoint.is_completed(database, table):
                        try:
                            target = client.get_table(f"{client.project}.{dataset}.{table.destination_table}")
                        except NotFound:
                            LOG.warning("Destino ausente apesar do checkpoint: %s", table.destination_table)
                        else:
                            expected = checkpoint.data["completed"][checkpoint.key(database, table)]["rows"]
                            if args.write_mode == "append" or target.num_rows == expected:
                                LOG.info("%s já concluída no checkpoint; ignorada", table.destination_table)
                                continue
                    if args.max_table_bytes and sizes[table] > args.max_table_bytes:
                        LOG.warning("%s ignorada: %d bytes excedem o limite de %d", table.destination_table, sizes[table], args.max_table_bytes)
                        failures.append(f"{database}.{table.source_schema}.{table.source_table}: ignorada por limite de tamanho")
                        continue
                    try:
                        if statement_timeout:
                            with conn.cursor() as timeout_cur:
                                timeout_cur.execute("SET LOCAL statement_timeout = %s", (statement_timeout,))
                        rows = migrate_table(
                            conn, client, dataset, table, args.batch_size, args.write_mode,
                            retries=args.retries, retry_delay=args.retry_delay,
                            batch_pause=args.batch_pause, max_rows=args.max_rows,
                        )
                        if args.max_rows:
                            LOG.warning("%s: amostra limitada a %d linhas publicada com sufixo __sample; sem checkpoint", table.destination_table, args.max_rows)
                        else:
                            checkpoint.complete(database, table, rows)
                    except Exception as exc:
                        message = f"{database}.{table.source_schema}.{table.source_table}: {exc}"
                        failures.append(message)
                        LOG.exception("Falha em %s", message)
                        if args.fail_fast:
                            raise
                    finally:
                        conn.rollback()
        except Exception as exc:
            message = f"banco {database}: {exc}"
            if message not in failures:
                failures.append(message)
            LOG.exception("Falha ao processar %s", message)
            if args.fail_fast:
                raise
    if failures:
        LOG.error("Migração terminou com %d falha(s):\n- %s", len(failures), "\n- ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
