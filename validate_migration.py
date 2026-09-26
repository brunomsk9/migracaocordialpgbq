#!/usr/bin/env python3
"""Valida migrações PostgreSQL -> BigQuery e gera relatório Parquet.

O relatório contém uma linha por tabela/view, comparando contagem de registros
e nomes de colunas. Opcionalmente, o próprio Parquet pode ser anexado a uma
tabela do BigQuery para consumo pelo Looker/Looker Studio.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import psycopg2
from dotenv import load_dotenv
from google.api_core.exceptions import NotFound
from google.cloud import bigquery, storage
from psycopg2 import sql
from migration_common import (OBJECTS_SQL, bq_identifier, closing_connection,
                               reject_example_placeholder, validate_bq_id)
from migrate import TableSpec, bq_type, csv_values, env_bool, get_columns


LOG = logging.getLogger("migration-validator")
SYSTEM_DATABASES = {"postgres", "template0", "template1"}
SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast"}


@dataclass
class ValidationRow:
    execution_id: str
    validated_at: datetime
    source_host: str
    source_port: int
    source_database: str
    source_schema: str
    source_table: str
    source_object_type: str
    bq_project: str
    bq_dataset: str
    bq_table: str
    source_rows: Optional[int]
    bq_rows: Optional[int]
    row_difference: Optional[int]
    row_difference_pct: Optional[float]
    source_column_count: Optional[int]
    bq_column_count: Optional[int]
    missing_columns_json: str
    extra_columns_json: str
    row_status: str
    schema_status: str
    validation_status: str
    status_description: str
    fix_command: str
    is_valid: bool
    duration_seconds: float
    error_message: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Valida PostgreSQL contra BigQuery e gera um arquivo Parquet."
    )
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).resolve().parent / ".env"),
        help="Arquivo .env (padrão: .env ao lado do script).",
    )
    parser.add_argument(
        "--databases",
        nargs="+",
        help="Bancos de origem. Sem o argumento, valida todos os bancos não-sistema.",
    )
    parser.add_argument(
        "--schemas",
        nargs="+",
        help="Schemas de origem. Sem o argumento, valida todos os schemas não-sistema.",
    )
    parser.add_argument(
        "--tables",
        nargs="+",
        help="Tabelas específicas, no formato schema.tabela ou apenas tabela.",
    )
    parser.add_argument(
        "--mapping-file",
        help="JSON opcional com sobrescritas de nomes de datasets/tabelas.",
    )
    parser.add_argument(
        "--output",
        help="Caminho do Parquet. O padrão inclui data/hora em VALIDATION_OUTPUT_DIR.",
    )
    parser.add_argument(
        "--count-mode",
        choices=("exact", "metadata"),
        default=None,
        help="exact executa COUNT(*); metadata usa estimativas rápidas (padrão: exact).",
    )
    parser.add_argument(
        "--bq-report-table",
        help="Tabela de histórico project.dataset.table; recebe append do Parquet.",
    )
    parser.add_argument(
        "--gcs-uri",
        help="Destino opcional gs://bucket/prefixo para uma cópia do Parquet.",
    )
    parser.add_argument(
        "--fail-on-difference",
        action="store_true",
        help="Retorna código 2 quando houver divergência ou erro por tabela.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def normalize_bq_id(value: str) -> str:
    """Normaliza identificadores conforme o padrão usado pela migração."""
    return bq_identifier(value)


def load_mapping(path: Optional[str]) -> Dict[str, Dict[str, str]]:
    if not path:
        return {"databases": {}, "tables": {}}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Mapping deve ser um objeto JSON")
    result = {}
    for key in ("databases", "tables"):
        values = data.get(key, {})
        if not isinstance(values, dict):
            raise ValueError(f"Mapping {key} deve ser um objeto")
        for source, destination in values.items():
            validate_bq_id(destination)
        result[key] = values
    return result


def pg_params(database: str) -> Dict[str, object]:
    params: Dict[str, object] = {
        "host": os.getenv("PG_HOST", "/var/run/postgresql"),
        "port": int(os.getenv("PG_PORT", "5432")),
        "dbname": database,
        "sslmode": os.getenv("PG_SSLMODE", "prefer"),
        "options": "-c default_transaction_read_only=on -c statement_timeout=" + str(int(os.getenv("PG_STATEMENT_TIMEOUT_MS", "0"))),
        "user": os.getenv("PG_USER", "postgres"),
        "connect_timeout": int(os.getenv("PG_CONNECT_TIMEOUT", "15")),
        "application_name": "postgres_bigquery_validator",
    }
    password = os.getenv("PG_PASSWORD")
    if password:
        params["password"] = password
    return params


def list_databases(selected: Optional[Sequence[str]]) -> List[str]:
    if selected is not None:
        return list(dict.fromkeys(selected))
    selected = csv_values(os.getenv("PG_DATABASES"))
    excludes = SYSTEM_DATABASES | set(csv_values(os.getenv("PG_EXCLUDE_DATABASES")))
    if selected:
        return list(dict.fromkeys(name for name in selected if name not in excludes))
    admin_database = os.getenv("PG_ADMIN_DATABASE", "postgres")
    with closing_connection(psycopg2.connect, **pg_params(admin_database)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT datname
                FROM pg_database
                WHERE datallowconn AND NOT datistemplate
                ORDER BY datname
                """
            )
            return [row[0] for row in cur.fetchall() if row[0] not in excludes]


def list_objects(
    database: str,
    selected_schemas: Optional[Sequence[str]],
    selected_tables: Optional[Sequence[str]],
) -> List[Tuple[str, str, str]]:
    selected_schemas = selected_schemas if selected_schemas is not None else csv_values(os.getenv("PG_SCHEMAS"))
    query = OBJECTS_SQL
    params: List[object] = []
    if selected_schemas:
        query += " AND n.nspname = ANY(%s)"
        params.append(list(selected_schemas))
    excluded = csv_values(os.getenv("PG_EXCLUDE_SCHEMAS"))
    if excluded:
        query += " AND NOT (n.nspname = ANY(%s))"
        params.append(excluded)
    query += " ORDER BY n.nspname, c.relname"

    skip_partition_children = env_bool("PG_SKIP_PARTITION_CHILDREN")
    requested = set(selected_tables or [])
    results: List[Tuple[str, str, str]] = []
    with closing_connection(psycopg2.connect, **pg_params(database)) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            objects = cur.fetchall()
            missing_schemas = set(selected_schemas or []) - set(excluded) - {row[0] for row in objects}
            if missing_schemas:
                raise ValueError("Schemas sem objetos encontrados: " + ", ".join(sorted(missing_schemas)))
            for schema_name, table_name, object_type, is_partition in objects:
                if skip_partition_children and is_partition:
                    continue
                if requested and table_name not in requested and f"{schema_name}.{table_name}" not in requested:
                    continue
                results.append((schema_name, table_name, object_type))
    matched = {name for schema, table, _ in results for name in (table, f"{schema}.{table}")}
    missing = requested - matched
    if missing:
        raise ValueError("Tabelas solicitadas não encontradas: " + ", ".join(sorted(missing)))
    return results


def source_columns(conn, schema_name: str, table_name: str) -> List[str]:
    return [column.name for column in get_columns(conn, TableSpec(schema_name, table_name, ""))]


def source_count(conn, schema_name: str, table_name: str, mode: str) -> int:
    with conn.cursor() as cur:
        if mode == "metadata":
            cur.execute(
                """
                SELECT c.reltuples::bigint, c.relkind, c.relhassubclass
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s
                """,
                (schema_name, table_name),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("Objeto não encontrado no catálogo do PostgreSQL")
            if row[0] < 0 or row[1] in ('v', 'p', 'f') or row[2]:
                raise ValueError("Estimativa indisponível/confiável para este objeto; use --count-mode exact")
            return int(row[0])
        cur.execute(
            sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                sql.Identifier(schema_name), sql.Identifier(table_name)
            )
        )
        return int(cur.fetchone()[0])


def bq_count(client: bigquery.Client, table_ref: str, mode: str) -> int:
    if mode == "metadata":
        return int(client.get_table(table_ref).num_rows)
    if "`" in table_ref:
        raise ValueError("Referência BigQuery inválida")
    query = f"SELECT COUNT(*) AS total FROM `{table_ref}`"
    return int(next(iter(client.query(query).result())).total)


def target_names(
    database: str,
    schema_name: str,
    table_name: str,
    mapping: Dict[str, Dict[str, str]],
) -> Tuple[str, str]:
    dataset = mapping["databases"].get(database)
    if dataset is None:
        dataset = normalize_bq_id(database)
    source_key = f"{database}.{schema_name}.{table_name}"
    table = mapping["tables"].get(source_key)
    if table is None:
        table = normalize_bq_id(f"{schema_name}_{table_name}")
    return dataset, table


STATUS_DESCRIPTIONS_PT = {
    "OK": "Contagem e schema batem entre origem e destino.",
    "ROW_MISMATCH": "A quantidade de linhas no BigQuery é diferente da origem.",
    "SCHEMA_MISMATCH": "Colunas ausentes, extras ou com tipo diferente do esperado no BigQuery.",
    "ROW_AND_SCHEMA_MISMATCH": "Linhas e schema divergem entre a origem e o BigQuery.",
    "DESTINATION_NOT_FOUND": "A tabela existe na origem mas nunca foi publicada no BigQuery.",
    "ESTIMATE_MATCH": "Contagens estimadas batem, mas isso não comprova igualdade exata (modo metadata); use --count-mode exact para confirmar.",
    "ERROR": "Falha ao validar este objeto; veja error_message para o detalhe técnico.",
}


def status_description(status: str) -> str:
    """Explicação em português do validation_status, para leitura direta na tabela do BigQuery."""
    return STATUS_DESCRIPTIONS_PT.get(status, "")


def suggested_fix_command(
    database: str, schema_name: str, table_name: str, status: str
) -> str:
    """Comando pronto para corrigir esta linha, para copiar direto da tabela do BigQuery."""
    if status in ("OK", "ESTIMATE_MATCH"):
        return ""
    if not schema_name or not table_name:
        port = os.getenv("PG_PORT", "5432")
        return f'sudo -u postgres psql -p {port} -d "{database}" -c "\\dn"'
    if status == "ERROR":
        return (
            "sudo -u postgres env -u GOOGLE_APPLICATION_CREDENTIALS "
            "/opt/migracao/.venv/bin/python /opt/migracao/validate_migration.py "
            "--env-file /opt/migracao/.env.validacao "
            f"--databases {database} --schemas {schema_name} --tables {table_name} "
            "--count-mode exact"
        )
    return (
        f'sudo -u postgres env PG_DATABASE="{database}" '
        "/opt/migracao/.venv/bin/python /opt/migracao/migrate.py "
        f'--tables "{schema_name}.{table_name}"'
    )


def validate_object(
    execution_id: str,
    validated_at: datetime,
    database: str,
    schema_name: str,
    table_name: str,
    object_type: str,
    bq_client: bigquery.Client,
    bq_project: str,
    mapping: Dict[str, Dict[str, str]],
    count_mode: str,
) -> ValidationRow:
    started = time.monotonic()
    dataset, bq_table = target_names(database, schema_name, table_name, mapping)
    table_ref = f"{bq_project}.{dataset}.{bq_table}"
    base = {
        "execution_id": execution_id,
        "validated_at": validated_at,
        "source_host": os.getenv("PG_HOST", "/var/run/postgresql"),
        "source_port": int(os.getenv("PG_PORT", "5432")),
        "source_database": database,
        "source_schema": schema_name,
        "source_table": table_name,
        "source_object_type": object_type,
        "bq_project": bq_project,
        "bq_dataset": dataset,
        "bq_table": bq_table,
    }
    try:
        # Verifique a existência antes de contar uma origem potencialmente enorme.
        try:
            bq_meta = bq_client.get_table(table_ref)
        except NotFound:
            return ValidationRow(
                **base,
                source_rows=None,
                bq_rows=None,
                row_difference=None,
                row_difference_pct=None,
                source_column_count=None,
                bq_column_count=None,
                missing_columns_json="[]",
                extra_columns_json="[]",
                row_status="DESTINATION_NOT_FOUND",
                schema_status="DESTINATION_NOT_FOUND",
                validation_status="DESTINATION_NOT_FOUND",
                status_description=status_description("DESTINATION_NOT_FOUND"),
                fix_command=suggested_fix_command(database, schema_name, table_name, "DESTINATION_NOT_FOUND"),
                is_valid=False,
                duration_seconds=round(time.monotonic() - started, 3),
                error_message=f"Tabela de destino não encontrada: {table_ref}",
            )

        with closing_connection(psycopg2.connect, **pg_params(database)) as pg_conn:
            pg_conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
            columns = get_columns(pg_conn, TableSpec(schema_name, table_name, ""))
            pg_columns = [column.name for column in columns]
            pg_rows = source_count(pg_conn, schema_name, table_name, count_mode)

        bq_columns = [field.name for field in bq_meta.schema]
        bq_rows = bq_count(bq_client, table_ref, count_mode)
        difference = bq_rows - pg_rows
        difference_pct = (difference / pg_rows * 100.0) if pg_rows else (0.0 if bq_rows == 0 else None)
        pg_set = set(pg_columns)
        bq_set = set(bq_columns)
        missing = sorted(pg_set - bq_set)
        extra = sorted(bq_set - pg_set)
        row_status = "OK" if difference == 0 else "ROW_MISMATCH"
        aliases = {"INT64": "INTEGER", "FLOAT64": "FLOAT", "BOOL": "BOOLEAN"}
        actual_types = {field.name: aliases.get(field.field_type, field.field_type) for field in bq_meta.schema}
        repeated = {field.name for field in bq_meta.schema if field.mode == "REPEATED"}
        type_errors = [
            f"{col.name}: esperado {bq_type(col)}, encontrado {actual_types[col.name]}"
            for col in columns if col.name in actual_types
            and (bq_type(col) != actual_types[col.name] or col.name in repeated)
        ]
        schema_status = "OK" if not missing and not extra and not type_errors else "SCHEMA_MISMATCH"
        if row_status == "OK" and schema_status == "OK":
            status = "OK"
        elif row_status != "OK" and schema_status != "OK":
            status = "ROW_AND_SCHEMA_MISMATCH"
        else:
            status = row_status if row_status != "OK" else schema_status

        if count_mode == "metadata" and status == "OK":
            status = "ESTIMATE_MATCH"
        return ValidationRow(
            **base,
            source_rows=pg_rows,
            bq_rows=bq_rows,
            row_difference=difference,
            row_difference_pct=round(difference_pct, 6) if difference_pct is not None else None,
            source_column_count=len(pg_columns),
            bq_column_count=len(bq_columns),
            missing_columns_json=json.dumps(missing, ensure_ascii=False),
            extra_columns_json=json.dumps(extra, ensure_ascii=False),
            row_status=row_status,
            schema_status=schema_status,
            validation_status=status,
            status_description=status_description(status),
            fix_command=suggested_fix_command(database, schema_name, table_name, status),
            is_valid=status == "OK",
            duration_seconds=round(time.monotonic() - started, 3),
            error_message="; ".join(type_errors)[:4000],
        )
    except Exception as exc:  # mantém o relatório mesmo quando uma tabela falha
        LOG.exception("Falha ao validar %s.%s.%s", database, schema_name, table_name)
        return ValidationRow(
            **base,
            source_rows=None,
            bq_rows=None,
            row_difference=None,
            row_difference_pct=None,
            source_column_count=None,
            bq_column_count=None,
            missing_columns_json="[]",
            extra_columns_json="[]",
            row_status="ERROR",
            schema_status="ERROR",
            validation_status="ERROR",
            status_description=status_description("ERROR"),
            fix_command=suggested_fix_command(database, schema_name, table_name, "ERROR"),
            is_valid=False,
            duration_seconds=round(time.monotonic() - started, 3),
            error_message=str(exc)[:4000],
        )


def inventory_error(execution_id, validated_at, database, project, message):
    """Uma falha no inventário também precisa aparecer no relatório."""
    return ValidationRow(
        execution_id=execution_id, validated_at=validated_at,
        source_host=os.getenv("PG_HOST", "/var/run/postgresql"),
        source_port=int(os.getenv("PG_PORT", "5432")),
        source_database=database, source_schema="", source_table="",
        source_object_type="DATABASE", bq_project=project, bq_dataset="", bq_table="",
        source_rows=None, bq_rows=None, row_difference=None, row_difference_pct=None,
        source_column_count=None, bq_column_count=None,
        missing_columns_json="[]", extra_columns_json="[]",
        row_status="ERROR", schema_status="ERROR", validation_status="ERROR",
        status_description=status_description("ERROR"),
        fix_command=suggested_fix_command(database, "", "", "ERROR"),
        is_valid=False, duration_seconds=0.0, error_message=message[:4000],
    )


def default_output_path() -> Path:
    output_dir = Path(os.getenv("VALIDATION_OUTPUT_DIR", "/opt/migracao/validation"))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return output_dir / f"validation_{timestamp}.parquet"


def write_parquet(rows: Iterable[ValidationRow], output_path: Path) -> pd.DataFrame:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    records = [asdict(row) for row in rows]
    frame = pd.DataFrame(records, dtype=object)
    if frame.empty:
        raise RuntimeError("Nenhum objeto foi encontrado para validação")
    nullable_ints = [
        "source_rows",
        "bq_rows",
        "row_difference",
        "source_column_count",
        "bq_column_count",
    ]
    for column in nullable_ints:
        frame[column] = frame[column].astype("Int64")
    # A construção direta evita perda de inteiros > 2**53 por inferência float.
    frame["row_difference_pct"] = frame["row_difference_pct"].astype("float64")
    frame["is_valid"] = frame["is_valid"].astype(bool)
    frame["duration_seconds"] = frame["duration_seconds"].astype("float64")
    frame["validated_at"] = pd.to_datetime(frame["validated_at"], utc=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    frame.to_parquet(temporary, engine="pyarrow", index=False, compression="snappy")
    temporary.replace(output_path)
    return frame


def upload_to_gcs(local_path: Path, gcs_uri: str) -> str:
    if not gcs_uri.startswith("gs://"):
        raise ValueError("--gcs-uri deve começar com gs://")
    bucket_and_prefix = gcs_uri[5:]
    bucket_name, _, prefix = bucket_and_prefix.partition("/")
    blob_name = "/".join(part for part in (prefix.rstrip("/"), local_path.name) if part)
    storage_client = storage.Client()
    storage_client.bucket(bucket_name).blob(blob_name).upload_from_filename(str(local_path))
    return f"gs://{bucket_name}/{blob_name}"


def ensure_dataset(client: bigquery.Client, table_ref: str) -> None:
    project, dataset, _ = table_ref.split(".", 2)
    dataset_ref = f"{project}.{dataset}"
    try:
        client.get_dataset(dataset_ref)
    except NotFound:
        dataset_obj = bigquery.Dataset(dataset_ref)
        dataset_obj.location = os.getenv("BQ_LOCATION", "US")
        client.create_dataset(dataset_obj)


def append_parquet_to_bigquery(
    client: bigquery.Client, local_path: Path, table_ref: str
) -> int:
    if len(table_ref.split(".")) != 3:
        raise ValueError("--bq-report-table deve estar no formato project.dataset.table")
    ensure_dataset(client, table_ref)
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        # Permite que novos campos de ValidationRow sejam adicionados à tabela de
        # histórico sem quebrar o append de execuções anteriores.
        schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
    )
    with local_path.open("rb") as handle:
        job = client.load_table_from_file(handle, table_ref, job_config=job_config)
    job.result()
    return int(job.output_rows or 0)


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)
    env_path = Path(args.env_file)
    if env_path.exists():
        load_dotenv(env_path, override=False)
    else:
        LOG.warning("Arquivo .env não encontrado: %s", env_path)

    # Os defaults dependem do .env e por isso são resolvidos após load_dotenv.
    bq_project = os.getenv("BQ_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not bq_project:
        raise RuntimeError("Defina BQ_PROJECT no .env ou GOOGLE_CLOUD_PROJECT no ambiente")
    reject_example_placeholder(bq_project, "BQ_PROJECT/GOOGLE_CLOUD_PROJECT")
    report_table = args.bq_report_table or os.getenv("VALIDATION_BQ_TABLE")
    if report_table:
        reject_example_placeholder(report_table, "--bq-report-table/VALIDATION_BQ_TABLE")
    gcs_uri = args.gcs_uri or os.getenv("VALIDATION_GCS_URI")
    count_mode = args.count_mode or os.getenv("VALIDATION_COUNT_MODE", "exact")
    if count_mode not in {"exact", "metadata"}:
        raise ValueError("VALIDATION_COUNT_MODE deve ser exact ou metadata")
    mapping = load_mapping(args.mapping_file or os.getenv("VALIDATION_MAPPING_FILE"))
    output_path = Path(args.output) if args.output else default_output_path()

    execution_id = str(uuid.uuid4())
    validated_at = datetime.now(timezone.utc)
    bq_client = bigquery.Client(project=bq_project, location=os.getenv("BQ_LOCATION", "US"))
    databases = list_databases(args.databases)
    LOG.info("Execução %s: %d banco(s)", execution_id, len(databases))

    rows: List[ValidationRow] = []
    plan = []
    destinations = {}
    for database in databases:
        try:
            objects = list_objects(database, args.schemas, args.tables)
            if not objects:
                raise ValueError("Nenhum objeto encontrado para os filtros selecionados")
            LOG.info("%s: %d objeto(s)", database, len(objects))
            for schema_name, table_name, object_type in objects:
                destination = target_names(database, schema_name, table_name, mapping)
                item = (database, schema_name, table_name, object_type)
                plan.append((item, destination))
                destinations.setdefault(destination, []).append(item)
        except Exception as exc:
            LOG.exception("Falha no inventário de %s", database)
            rows.append(inventory_error(execution_id, validated_at, database, bq_project, str(exc)))

    for (database, schema_name, table_name, object_type), destination in plan:
        if len(destinations[destination]) > 1:
            row = inventory_error(execution_id, validated_at, database, bq_project,
                                  f"Colisão de destino: {destination}")
            row.source_schema, row.source_table = schema_name, table_name
            row.source_object_type = object_type
            row.status_description = (
                "Duas tabelas de origem gerariam o mesmo nome de destino; a carga foi "
                "bloqueada antes de uma sobrescrever a outra. Corrija manualmente com um "
                "alias explícito (PG_TABLES=origem:alias) — não há comando automático seguro."
            )
            row.fix_command = ""
            row.bq_dataset, row.bq_table = destination
        else:
            row = validate_object(
                execution_id, validated_at, database, schema_name, table_name,
                object_type, bq_client, bq_project, mapping, count_mode,
            )
        rows.append(row)
        LOG.info("%s.%s.%s: %s (origem=%s, destino=%s)", database,
                 schema_name, table_name, row.validation_status, row.source_rows, row.bq_rows)

    frame = write_parquet(rows, output_path)
    LOG.info("Parquet gerado: %s (%d linha(s))", output_path, len(frame))

    if gcs_uri:
        uploaded_uri = upload_to_gcs(output_path, gcs_uri)
        LOG.info("Parquet enviado para %s", uploaded_uri)
    if report_table:
        loaded_rows = append_parquet_to_bigquery(bq_client, output_path, report_table)
        LOG.info("%d linha(s) anexadas a %s", loaded_rows, report_table)

    invalid_count = int((~frame["is_valid"]).sum())
    LOG.info("Resumo: %d válida(s), %d divergente(s)/erro(s)", len(frame) - invalid_count, invalid_count)
    if args.fail_on_difference and invalid_count:
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        LOG.error("Validação interrompida")
        raise SystemExit(130)
    except Exception as exc:
        LOG.exception("Falha fatal: %s", exc)
        raise SystemExit(1)
