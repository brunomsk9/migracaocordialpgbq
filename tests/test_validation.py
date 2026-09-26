"""Regressões de inventário, publicação e relatório; não acessa serviços reais."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
from google.api_core.exceptions import Conflict, NotFound

import migrate as m
import validate_migration as v
from migration_common import closing_connection, reject_collisions


class RegressionTests(unittest.TestCase):
    def test_names_match_migration_including_accents(self):
        for name in ['São-Paulo', 'a__b', '23070001-99-SV']:
            self.assertEqual(m.bq_identifier(name, 'dataset'), v.normalize_bq_id(name))
        with self.assertRaises(ValueError):
            v.normalize_bq_id('x' * 1025)

    def test_collisions_detected(self):
        with self.assertRaisesRegex(ValueError, 'Colisão'):
            reject_collisions([('a-b', 'a_b'), ('a_b', 'a_b')])

    def test_json_scalars(self):
        for value in ['hello', 42, True, False, None, {'a': [1]}]:
            self.assertEqual(m.json_value(value, 'JSON'), value)
        self.assertEqual(m.json_value(42, 'STRING'), '42')

    def test_numeric_ranges(self):
        for precision, scale, expected in [
            (38, 9, 'NUMERIC'), (38, 0, 'BIGNUMERIC'),
            (30, 12, 'BIGNUMERIC'), (80, 2, 'STRING'),
            (None, None, 'STRING'), (10, -30, 'STRING'),
        ]:
            self.assertEqual(m.bq_type(m.Column('n', 'numeric', 'numeric', True, precision, scale)), expected)

    def test_catalog_type_aliases(self):
        for name, expected in [('timestamp', 'DATETIME'), ('timestamptz', 'TIMESTAMP'), ('time', 'TIME')]:
            self.assertEqual(m.bq_type(m.Column('c', name, name, True, None, None)), expected)

    def test_connections_closed_after_failure(self):
        connection = MagicMock()
        with self.assertRaises(RuntimeError):
            with closing_connection(lambda: connection):
                raise RuntimeError('failed')
        connection.close.assert_called_once()

    def test_checkpoint_alias_change_is_not_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = m.CheckpointStore(str(Path(tmp) / 'state.json'))
            store.complete('db', m.TableSpec('public', 't', 'old'), 1)
            self.assertFalse(store.is_completed('db', m.TableSpec('public', 't', 'new')))

    def validate(self, client, mode='exact'):
        return v.validate_object('run', datetime.now(timezone.utc), 'db', 'public', 't',
                                 'BASE TABLE', client, 'project', {'databases': {}, 'tables': {}}, mode)

    @patch.object(v.psycopg2, 'connect')
    def test_missing_destination_does_not_count_source(self, connect):
        client = MagicMock()
        client.get_table.side_effect = NotFound('missing')
        row = self.validate(client)
        self.assertEqual(row.validation_status, 'DESTINATION_NOT_FOUND')
        self.assertIsNone(row.source_rows)
        self.assertIn('migrate.py', row.fix_command)
        self.assertIn('--tables "public.t"', row.fix_command)
        self.assertTrue(row.status_description)
        connect.assert_not_called()

    def test_suggested_fix_command_by_status(self):
        self.assertEqual(v.suggested_fix_command('db', 'public', 't', 'OK'), '')
        self.assertEqual(v.suggested_fix_command('db', 'public', 't', 'ESTIMATE_MATCH'), '')
        self.assertIn('psql', v.suggested_fix_command('db', '', '', 'ERROR'))
        self.assertIn('validate_migration.py', v.suggested_fix_command('db', 'public', 't', 'ERROR'))
        migrate_cmd = v.suggested_fix_command('db', 'public', 't', 'SCHEMA_MISMATCH')
        self.assertIn('migrate.py', migrate_cmd)
        self.assertIn('--tables "public.t"', migrate_cmd)

    def test_status_description_covers_every_known_status(self):
        for status in ('OK', 'ROW_MISMATCH', 'SCHEMA_MISMATCH', 'ROW_AND_SCHEMA_MISMATCH',
                       'DESTINATION_NOT_FOUND', 'ESTIMATE_MATCH', 'ERROR'):
            self.assertTrue(v.status_description(status), status)

    @patch.object(v.psycopg2, 'connect')
    @patch.object(v, 'source_count', return_value=10)
    @patch.object(v, 'get_columns', return_value=[m.Column('id', 'int4', 'int4', True, None, None)])
    @patch.object(v, 'bq_count', return_value=10)
    def test_schema_type_and_metadata_validation(self, count, columns, pg_count, connect):
        client = MagicMock()
        client.get_table.return_value.schema = [m.bigquery.SchemaField('id', 'STRING')]
        self.assertEqual(self.validate(client).validation_status, 'SCHEMA_MISMATCH')
        client.get_table.return_value.schema = [m.bigquery.SchemaField('id', 'INTEGER')]
        self.assertEqual(self.validate(client).validation_status, 'OK')
        row = self.validate(client, 'metadata')
        self.assertEqual(row.validation_status, 'ESTIMATE_MATCH')
        self.assertFalse(row.is_valid)

    def test_inventory_error_suggests_schema_diagnostic(self):
        row = v.inventory_error('run', datetime.now(timezone.utc), 'db', 'project', 'inaccessible')
        self.assertIn('psql', row.fix_command)
        self.assertIn('"db"', row.fix_command)
        self.assertTrue(row.status_description)

    def test_report_all_null_counts_has_stable_types(self):
        import pyarrow.parquet as pq
        row = v.inventory_error('run', datetime.now(timezone.utc), 'db', 'project', 'inaccessible')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.parquet'
            frame = v.write_parquet([row], path)
            self.assertFalse(frame.iloc[0].is_valid)
            self.assertEqual(str(pq.read_schema(path).field('row_difference_pct').type), 'double')
            self.assertEqual(str(pq.read_schema(path).field('source_rows').type), 'int64')
            self.assertEqual(len(pd.read_parquet(path)), 1)

    @patch.object(m, 'read_batches', return_value=iter([[{'id': 1}]]))
    @patch.object(m, 'get_columns', return_value=[m.Column('id', 'int4', 'int4', True, None, None)])
    def test_retry_uses_same_job_id_and_recovers_conflict(self, columns, batches):
        client = MagicMock(project='project', location='US')
        client.get_table.return_value.num_rows = 1
        first_job = MagicMock()
        first_job.result.side_effect = TimeoutError('lost response')
        client.load_table_from_json.side_effect = [first_job, Conflict('already submitted')]
        self.assertEqual(m.migrate_table(MagicMock(), client, 'dataset', m.TableSpec('public', 't', 'public_t'),
                                        10, 'append', retries=1, retry_delay=0), 1)
        calls = client.load_table_from_json.call_args_list
        self.assertEqual(calls[0].kwargs['job_id'], calls[1].kwargs['job_id'])
        client.get_job.assert_called_once()
        client.copy_table.assert_called_once()

    @patch.object(m, 'read_batches', side_effect=RuntimeError('source failed'))
    @patch.object(m, 'get_columns', return_value=[m.Column('id', 'int4', 'int4', True, None, None)])
    def test_load_failure_never_publishes(self, columns, batches):
        client = MagicMock(project='project')
        client.delete_table.side_effect = RuntimeError('cleanup failed')
        with self.assertRaisesRegex(RuntimeError, 'source failed'):
            m.migrate_table(MagicMock(), client, 'dataset', m.TableSpec('public', 't', 'public_t'), 10, 'truncate')
        client.copy_table.assert_not_called()

    def test_report_preserves_counts_above_float_precision(self):
        row = v.inventory_error('run', datetime.now(timezone.utc), 'db', 'project', '')
        row.source_rows = 2**53 + 1
        empty = v.inventory_error('run', row.validated_at, 'missing', 'project', '')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.parquet'
            v.write_parquet([row, empty], path)
            self.assertEqual(pd.read_parquet(path).iloc[0].source_rows, 2**53 + 1)

    @patch.object(m, 'read_batches', return_value=iter([[{'id': 1}]]))
    @patch.object(m, 'get_columns', return_value=[m.Column('id', 'int4', 'int4', True, None, None)])
    def test_sample_does_not_replace_official_destination(self, columns, batches):
        client = MagicMock(project='project')
        client.get_table.return_value.num_rows = 1
        m.migrate_table(MagicMock(), client, 'dataset', m.TableSpec('public', 't', 'public_t'),
                        10, 'truncate', max_rows=1)
        self.assertEqual(client.copy_table.call_args.args[1], 'project.dataset.public_t__sample')

    @patch.object(m, 'read_batches', return_value=iter([[{'id': 1}]]))
    @patch.object(m, 'get_columns', return_value=[m.Column('id', 'int4', 'int4', True, None, None)])
    def test_append_publication_retry_has_stable_job_id(self, columns, batches):
        client = MagicMock(project='project', location='US')
        client.get_table.return_value.num_rows = 1
        uncertain = MagicMock()
        uncertain.result.side_effect = TimeoutError('lost response after copy')
        client.copy_table.side_effect = [uncertain, Conflict('already published')]
        m.migrate_table(MagicMock(), client, 'dataset', m.TableSpec('public', 't', 'public_t'),
                        10, 'append', retries=1, retry_delay=0)
        calls = client.copy_table.call_args_list
        self.assertEqual(calls[0].kwargs['job_id'], calls[1].kwargs['job_id'])
        client.get_job.assert_called_once()

    def test_unknown_metadata_is_not_zero(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (-1, 'r', False)
        with self.assertRaises(ValueError):
            v.source_count(connection, 'public', 't', 'metadata')

    @patch.object(v.psycopg2, 'connect')
    def test_list_objects_can_skip_partition_children(self, connect):
        connect.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            ('dm_analise', 'medicoes', 'BASE TABLE', False),
            ('dm_analise', 'medicoes_2024', 'BASE TABLE', True),
        ]
        with patch.dict(os.environ, {'PG_SKIP_PARTITION_CHILDREN': 'true'}, clear=False):
            objects = v.list_objects('db', None, None)
        self.assertEqual([table for _, table, _ in objects], ['medicoes'])

    def test_mapping_accepts_explicit_underscores(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'mapping.json'
            path.write_text(json.dumps({'tables': {'db.public.t': '_explicit__name'}}))
            self.assertEqual(v.load_mapping(str(path))['tables']['db.public.t'], '_explicit__name')

    @patch.object(v, 'append_parquet_to_bigquery')
    @patch.object(v, 'write_parquet')
    @patch.object(v, 'validate_object')
    @patch.object(v, 'list_objects', side_effect=[RuntimeError('offline'), [('public', 't', 'BASE TABLE')]])
    @patch.object(v, 'list_databases', return_value=['offline', 'online'])
    @patch.object(v.bigquery, 'Client')
    @patch.dict(os.environ, {'BQ_PROJECT': 'project'}, clear=True)
    def test_database_failure_keeps_other_database_report(self, client, databases, objects, validate, write, upload):
        validate.return_value = v.inventory_error('run', datetime.now(timezone.utc), 'online', 'project', 'test')
        write.return_value = pd.DataFrame({'is_valid': [False, False]})
        with patch('sys.argv', ['validate_migration.py', '--env-file', '/tmp/nonexistent-review-env', '--fail-on-difference']):
            self.assertEqual(v.main(), 2)
        rows = write.call_args.args[0]
        self.assertEqual([row.source_database for row in rows], ['offline', 'online'])
        validate.assert_called_once()

    @patch.object(m, 'migrate_table')
    @patch.object(m, 'table_sizes')
    @patch.object(m, 'discover_tables')
    @patch.object(m, 'discover_databases', return_value=['offline', 'online'])
    @patch.object(m, 'postgres_connection')
    @patch.object(m.bigquery, 'Client')
    @patch.dict(os.environ, {'BQ_PROJECT': 'project', 'AUTO_DISCOVER': 'true'}, clear=True)
    def test_migration_database_failure_keeps_other_database_running(
        self, bq_client_cls, postgres_connection, discover_databases, discover_tables, table_sizes, migrate_table
    ):
        table = m.TableSpec('public', 't', 'public_t')
        discover_tables.side_effect = [RuntimeError('conexão perdida'), [table]]
        table_sizes.return_value = {table: 0}
        migrate_table.return_value = 5
        postgres_connection.return_value = MagicMock()
        bq_client_cls.return_value = MagicMock(project='project')

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = str(Path(tmp) / 'checkpoint.json')
            with patch('sys.argv', ['migrate.py', '--env-file', '/tmp/nonexistent-review-env',
                                     '--checkpoint', checkpoint]):
                self.assertEqual(m.main(), 1)

        self.assertEqual(discover_tables.call_count, 2)
        migrate_table.assert_called_once()

    @patch.object(m.bigquery, 'Client')
    @patch.dict(os.environ, {'BQ_PROJECT': 'seu-projeto-gcp', 'AUTO_DISCOVER': 'false',
                              'PG_DATABASE': 'db', 'PG_TABLES': 'public.t'}, clear=True)
    def test_migration_rejects_placeholder_project_before_any_bq_call(self, bq_client_cls):
        with patch('sys.argv', ['migrate.py', '--env-file', '/tmp/nonexistent-review-env']):
            with self.assertRaisesRegex(ValueError, 'BQ_PROJECT'):
                m.main()
        bq_client_cls.assert_not_called()

    @patch.object(v.bigquery, 'Client')
    @patch.dict(os.environ, {'BQ_PROJECT': 'seu-projeto-gcp'}, clear=True)
    def test_validation_rejects_placeholder_project_before_any_bq_call(self, bq_client_cls):
        with patch('sys.argv', ['validate_migration.py', '--env-file', '/tmp/nonexistent-review-env']):
            with self.assertRaisesRegex(ValueError, 'BQ_PROJECT'):
                v.main()
        bq_client_cls.assert_not_called()

    @patch.object(v.bigquery, 'Client')
    @patch.dict(os.environ, {'BQ_PROJECT': 'sv-443512'}, clear=True)
    def test_validation_rejects_placeholder_report_table_before_any_bq_call(self, bq_client_cls):
        argv = ['validate_migration.py', '--env-file', '/tmp/nonexistent-review-env',
                '--bq-report-table', 'seu-projeto-gcp.monitoramento.validacao_migracao']
        with patch('sys.argv', argv):
            with self.assertRaisesRegex(ValueError, 'VALIDATION_BQ_TABLE'):
                v.main()
        bq_client_cls.assert_not_called()


if __name__ == '__main__':
    unittest.main()
