import copy
import csv
import io
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from judging_core import calculate, export_files, rounded
from judging_store import Store

CONFIG = json.loads(Path('config.json').read_text(encoding='utf-8'))['criteria']


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='tmp')
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.setup('correct-password-123', 'local-secret', 'local-secret', CONFIG)
        self.admin = self.store.login('correct-password-123')

    def prepare(self, n=1, works=1, algorithm='criterion_trim_v1'):
        self.store.configure(self.admin, 'Тест', algorithm, None, CONFIG)
        tokens = [self.store.add_judge(self.admin) for _ in range(n)]
        pids = [self.store.add_participant(self.admin, f'Работа {i}') for i in range(works)]
        self.store.transition(self.admin, 'scoring')
        for pid in pids:
            self.store.open_participant(self.admin, pid)
        return tokens, pids

    def scores(self, value=1):
        return {k: min(value, c['max']) for k, c in CONFIG.items()}

    def test_authentication_and_revoke(self):
        with self.assertRaises(PermissionError):
            self.store.admin_view('')
        with self.assertRaises(PermissionError):
            self.store.login('wrong')
        tokens, pids = self.prepare(2)
        jid = self.store.judge_view(tokens[0])['judge']['id']
        other = self.store.judge_view(tokens[1])['judge']['id']
        self.store.save_card(tokens[1], pids[0], self.scores(), '', 0, True)
        self.assertEqual(self.store.judge_view(tokens[0])['cards'], [])
        new = self.store.rotate_judge(self.admin, jid, 'Потеряна ссылка')
        with self.assertRaises(PermissionError):
            self.store.save_card(tokens[0], pids[0], self.scores(), '', 0)
        self.assertEqual(self.store.judge_view(new)['judge']['id'], jid)
        self.assertNotEqual(jid, other)

    def test_setup_secret_and_password(self):
        other = Store(Path(self.tmp.name) / 'other')
        with self.assertRaises(PermissionError):
            other.setup('password-long-123', '', '', CONFIG)
        with self.assertRaises(ValueError):
            self.store.setup('password-long-123', 'a', 'a', CONFIG)

    def test_rate_limit_persists(self):
        for _ in range(5):
            with self.assertRaises(PermissionError):
                self.store.login('wrong')
        with self.assertRaises(PermissionError):
            Store(self.tmp.name).login('correct-password-123')

    def test_method_must_be_confirmed(self):
        self.store.add_judge(self.admin)
        self.store.add_participant(self.admin, 'Работа')
        with self.assertRaises(ValueError):
            self.store.transition(self.admin, 'scoring')

    def test_draft_edit_conflict_and_submit(self):
        tokens, pids = self.prepare()
        token, pid = tokens[0], pids[0]
        self.store.save_card(token, pid, {'1': 0}, '', 0)
        with self.assertRaises(ValueError):
            self.store.save_card(token, pid, {'1': 3}, '', 0)
        self.assertEqual(self.store.judge_view(token)['cards'][0]['scores'], {'1': 0})
        with self.assertRaises(ValueError):
            self.store.save_card(token, pid, {'1': 0}, '', 1, True)
        version = self.store.save_card(token, pid, self.scores(), 'ok', 1, True)
        self.assertEqual(version, 2)
        self.assertEqual(self.store.save_card(token, pid, self.scores(), 'ok', 1, True), 2)
        with self.assertRaises(ValueError):
            self.store.save_card(token, pid, self.scores(2), '', 2)
        self.assertNotIn('scores', self.store.admin_view(self.admin)['cards'][0])
        with self.assertRaises(PermissionError):
            self.store.snapshot(self.admin)

    def test_close_approve_reopen_history(self):
        tokens, pids = self.prepare()
        with self.assertRaises(ValueError):
            self.store.transition(self.admin, 'closed')
        self.store.save_card(tokens[0], pids[0], self.scores(), '', 0, True)
        self.store.transition(self.admin, 'closed')
        with self.assertRaises(ValueError):
            self.store.save_card(tokens[0], pids[0], self.scores(2), '', 1)
        self.store.transition(self.admin, 'approved')
        old = self.store.snapshot(self.admin)
        self.assertEqual(old, self.store.snapshot(self.admin))
        self.store.transition(self.admin, 'scoring', 'Исправление')
        jid = self.store.judge_view(tokens[0])['judge']['id']
        self.store.unlock(self.admin, jid, pids[0], 'Опечатка')
        card = self.store.judge_view(tokens[0])['cards'][0]
        self.assertEqual(card['scores'], self.scores())
        self.store.save_card(tokens[0], pids[0], self.scores(2), '', card['version'], True)
        self.store.transition(self.admin, 'closed')
        self.assertNotEqual(old['cards'], self.store.snapshot(self.admin)['cards'])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(json.loads(db.execute('SELECT payload FROM snapshots').fetchone()[0]), old)

    def test_concurrent_seven_judges(self):
        tokens, pids = self.prepare(7)
        with ThreadPoolExecutor(max_workers=7) as pool:
            list(pool.map(lambda t: self.store.save_card(t, pids[0], self.scores(), '', 0, True), tokens))
        self.store.transition(self.admin, 'closed')
        snap = self.store.snapshot(self.admin)
        self.assertEqual(len(snap['cards']), 7)
        self.assertEqual(calculate(snap)[0]['total'], Decimal('7.00'))
        backup = self.store.backup(self.admin)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cards').fetchone()[0], 7)

    def test_validation(self):
        tokens, pids = self.prepare()
        for wrong in [self.scores() | {'1': True}, self.scores() | {'1': -1}, self.scores() | {'1': 16}, self.scores() | {'8': 3}]:
            with self.assertRaises(ValueError):
                self.store.save_card(tokens[0], pids[0], wrong, '', 0, True)
        self.assertEqual(self.store.judge_view(tokens[0])['cards'], [])

    def test_migration_and_backup(self):
        source = Path(self.tmp.name) / 'db.json'
        payload = {'judges_count': 1, 'commands': {'0': {'title': 'Старая', 'members': [], 'rates': {'1': self.scores()}}}, 'secretary_code': 'DO_NOT_EXPORT', 'judge_codes': {}}
        source.write_text(json.dumps(payload), encoding='utf-8')
        original = source.read_bytes()
        self.store.migrate(self.admin, source)
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(list((Path(self.tmp.name) / 'backups').glob('legacy_*.json')))
        with self.assertRaises(ValueError):
            self.store.migrate(self.admin, source)
        view = self.store.admin_view(self.admin)
        self.assertEqual(len(view['participants']), 1)
        with self.assertRaises(ValueError):
            self.store.transition(self.admin, 'scoring')

    def test_bad_migration_is_atomic(self):
        source = Path(self.tmp.name) / 'bad.json'
        source.write_text(json.dumps({'judges_count': 1, 'commands': {'0': {'title': 'bad', 'rates': {'1': {'1': 999}}}}}))
        with self.assertRaises(ValueError):
            self.store.migrate(self.admin, source)
        self.assertEqual(self.store.admin_view(self.admin)['judges'], [])

    def test_offline_restore(self):
        from maintenance import restore
        original = self.store.backup(self.admin)
        self.store.add_participant(self.admin, 'After backup')
        previous = restore(self.tmp.name, original)
        self.assertTrue(previous.exists())
        with self.assertRaises(PermissionError):
            self.store.admin_view(self.admin)
        token = self.store.login('correct-password-123')
        self.assertEqual(self.store.admin_view(token)['participants'], [])

    def test_csv_complete_matrix_and_no_secrets(self):
        tokens, pids = self.prepare(7, 1)
        with self.store.transaction() as db:
            snap = self.store._snapshot(db)
        snap['participants'] = [dict(snap['participants'][0], id=f'p{i}', number=i+1) for i in range(100)]
        files = export_files(snap)
        self.assertEqual(len(list(csv.reader(io.StringIO(files['summary.csv'].decode('utf-8-sig')), delimiter=';'))), 101)
        self.assertEqual(len(list(csv.reader(io.StringIO(files['scores.csv'].decode('utf-8-sig')), delimiter=';'))), 701)
        self.assertTrue(files['summary.csv'].startswith(b'\xef\xbb\xbf'))
        for token in tokens + [self.admin]:
            self.assertNotIn(token.encode(), files['metadata.json'])
        self.assertNotIn(b'token_hash', files['metadata.json'])
        self.assertTrue(files['protocol.zip'].startswith(b'PK'))


class CalculationTests(unittest.TestCase):
    def snapshot(self, values, algorithm='criterion_trim_v1'):
        return {'id': 'x', 'created_at': '2026-11-03T00:00:00+00:00', 'event': {'id': 'e', 'name': 'Тест', 'status': 'closed', 'algorithm': algorithm, 'criteria': CONFIG, 'config_version': '1', 'threshold': None}, 'judges': [{'id': str(i), 'number': i+1, 'active': 1} for i in range(len(values))], 'participants': [{'id': 'p', 'number': 1, 'title': '=1+1;"тест"\nстрока', 'members': ['Иванов'], 'work_type': '', 'stage': '', 'archived': 0}], 'cards': [{'participant_id': 'p', 'judge_id': str(i), 'scores': {k: v.get(k, 0) for k in CONFIG}, 'status': 'submitted', 'comment': '@SUM(A1)', 'version': 1, 'updated_at': '', 'submitted_at': ''} for i, v in enumerate(values)]}

    def test_counts_1_through_7(self):
        for count in [1, 2, 3, 4, 5, 7]:
            snap = self.snapshot([{'1': v} for v in range(count)])
            row = calculate(snap)[0]
            self.assertEqual(row['total'], rounded(Decimal(count-1)/2))
            self.assertEqual(sum(d['decision'].startswith('снята') for d in row['detail']), 14 if count >= 5 else 0)

    def test_legacy_and_criterion_differ(self):
        values = [{'1': 15, '2': 0}, {'1': 0, '2': 20}, {'1': 10, '2': 10}, {'1': 10, '2': 10}, {'1': 15, '2': 15}]
        new = calculate(self.snapshot(values))[0]['total']
        old = calculate(self.snapshot(values, 'legacy_total_trim_v1'))[0]['total']
        self.assertNotEqual(new, old)
        self.assertEqual(new, Decimal('23.33'))

    def test_csv_roundtrip_injection_and_profiles(self):
        snap = self.snapshot([{'1': 0}, {'1': 1}, {'1': 1}])
        for universal in [False, True]:
            files = export_files(snap, universal)
            rows = list(csv.DictReader(io.StringIO(files['summary.csv'].decode('utf-8-sig')), delimiter=',' if universal else ';'))
            self.assertTrue(rows[0]['Команда_или_работа'].startswith("'=1+1"))
            self.assertIn('\n', rows[0]['Команда_или_работа'])
            self.assertEqual(rows[0]['Итог'], '0.67' if universal else '0,67')

    def test_ties_and_incomplete(self):
        snap = self.snapshot([{'1': 2}])
        snap['participants'] += [dict(snap['participants'][0], id='p2', number=2)]
        snap['cards'] += [dict(snap['cards'][0], participant_id='p2')]
        self.assertEqual([r['rank'] for r in calculate(snap)], [1, 1])
        snap['cards'].pop()
        self.assertEqual([r['rank'] for r in calculate(snap)], [None, None])


if __name__ == '__main__':
    unittest.main()
