import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from migrate import (
    Column,
    bq_identifier,
    bq_type,
    dataset_for_database,
    default_destination_table,
    discover_databases,
    discover_tables,
    expand_table_specs,
    parse_tables,
    table_sizes,
    CheckpointStore,
    run_with_retry,
)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, query, params=None):
        self.params = params

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return FakeCursor(self.rows)


class MappingTests(unittest.TestCase):
    def column(self, data_type, udt_name, precision=None):
        return Column("campo", data_type, udt_name, True, precision, None)

    def test_spatial_types(self):
        self.assertEqual(bq_type(self.column("USER-DEFINED", "geometry")), "GEOGRAPHY")
        self.assertEqual(bq_type(self.column("USER-DEFINED", "geography")), "GEOGRAPHY")

    def test_timestamp_types(self):
        self.assertEqual(bq_type(self.column("timestamp without time zone", "timestamp")), "DATETIME")
        self.assertEqual(bq_type(self.column("timestamp with time zone", "timestamptz")), "TIMESTAMP")

    def test_large_numeric(self):
        self.assertEqual(bq_type(Column("campo", "numeric", "numeric", True, 50, 12)), "BIGNUMERIC")

    def test_table_alias(self):
        specs = parse_tables("public.origem:destino, gis.vias")
        self.assertEqual(specs[0].destination_table, "destino")
        self.assertEqual(specs[1].destination_table, "gis_vias")

    def test_default_destination_preserves_schema(self):
        self.assertEqual(
            default_destination_table("dm_analise", "tbl_acidentes"),
            "dm_analise_tbl_acidentes",
        )

    def test_schema_wildcard_expansion(self):
        specs = parse_tables("dm_analise.*")
        expanded = expand_table_specs(
            FakeConnection([("tbl_acidentes", False), ("vw_resumo", False)]), specs
        )
        self.assertEqual(
            [item.destination_table for item in expanded],
            ["dm_analise_tbl_acidentes", "dm_analise_vw_resumo"],
        )

    def test_schema_wildcard_can_skip_partition_children(self):
        specs = parse_tables("dm_analise.*")
        conn = FakeConnection([("medicoes", False), ("medicoes_2024", True)])
        with patch.dict(os.environ, {"PG_SKIP_PARTITION_CHILDREN": "true"}, clear=False):
            expanded = expand_table_specs(conn, specs)
        self.assertEqual([item.source_table for item in expanded], ["medicoes"])

    def test_dataset_uses_database_name(self):
        self.assertEqual(dataset_for_database("2304400_fo"), "2304400_fo")

    def test_invalid_identifier_characters_are_normalized(self):
        self.assertEqual(bq_identifier("base-operacional 01", "dataset"), "base_operacional_01")

    def test_database_discovery_ignores_system_databases(self):
        conn = FakeConnection([("postgres",), ("template1",), ("2304400_fo",), ("cidade",)])
        self.assertEqual(discover_databases(conn), ["2304400_fo", "cidade"])

    def test_table_discovery_ignores_system_schemas(self):
        conn = FakeConnection([
            ("information_schema", "tables", False),
            ("pg_catalog", "pg_class", False),
            ("public", "clientes", False),
            ("dm_analise", "tbl_acidentes", False),
        ])
        tables = discover_tables(conn)
        self.assertEqual(
            [(t.source_schema, t.source_table, t.destination_table) for t in tables],
            [("public", "clientes", "public_clientes"), ("dm_analise", "tbl_acidentes", "dm_analise_tbl_acidentes")],
        )

    def test_table_discovery_can_skip_partition_children(self):
        conn = FakeConnection([
            ("dm_analise", "medicoes", False),
            ("dm_analise", "medicoes_2024", True),
        ])
        with patch.dict(os.environ, {"PG_SKIP_PARTITION_CHILDREN": "true"}, clear=False):
            tables = discover_tables(conn)
        self.assertEqual([t.source_table for t in tables], ["medicoes"])

    def test_table_sizes_batches_single_query(self):
        t1 = parse_tables("public.a")[0]
        t2 = parse_tables("public.b")[0]
        conn = FakeConnection([("public", "a", 100), ("public", "b", 0)])
        sizes = table_sizes(conn, [t1, t2])
        self.assertEqual(sizes[t1], 100)
        self.assertEqual(sizes[t2], 0)

    def test_table_sizes_defaults_missing_table_to_zero(self):
        t1 = parse_tables("public.a")[0]
        self.assertEqual(table_sizes(FakeConnection([]), [t1])[t1], 0)

    def test_table_sizes_empty_list_skips_query(self):
        self.assertEqual(table_sizes(FakeConnection([]), []), {})

    def test_checkpoint_only_marks_completed_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "checkpoint.json")
            store = CheckpointStore(path)
            table = parse_tables("dm_analise.tbl_acidentes")[0]
            self.assertFalse(store.is_completed("2304400_fo", table))
            store.complete("2304400_fo", table, 123)
            reloaded = CheckpointStore(path)
            self.assertTrue(reloaded.is_completed("2304400_fo", table))
            self.assertEqual(reloaded.data["completed"]["2304400_fo.dm_analise.tbl_acidentes"]["rows"], 123)

    def test_retry_succeeds_after_transient_failure(self):
        attempts = []
        def operation():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("transient")
            return "ok"
        self.assertEqual(run_with_retry(operation, 2, 0, "test"), "ok")
        self.assertEqual(len(attempts), 3)


if __name__ == "__main__":
    unittest.main()
