import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from judging_network import local_addresses, parse_ipconfig, public_url, validate_manual_url
import tests.test_service as service


class NetworkTests(unittest.TestCase):
    def test_localized_adapters_and_invalid_addresses(self):
        output='''Адаптер беспроводной локальной сети Wi-Fi:
   IPv4-адрес. . . . . . . . . . . . : 172.17.18.194
Ethernet adapter VPN:
   IPv4 Address. . . . . . . . . . . : 10.8.0.2
Ethernet adapter disconnected:
   IPv4 Address. . . . . . . . . . . : 169.254.1.2
'''
        self.assertEqual([a['ip'] for a in parse_ipconfig(output)], ['172.17.18.194','10.8.0.2'])
        self.assertIn('Wi-Fi',parse_ipconfig(output)[0]['name'])
        self.assertEqual(public_url('172.17.18.194', 8512),'http://172.17.18.194:8512')
        self.assertEqual(validate_manual_url('http://172.17.18.194:8501/'),'http://172.17.18.194:8501')
        for url in ['http://localhost:8501','http://0.0.0.0:8501','http://127.0.0.1:8501','http://172.17.18.194:8501/?code=x']:
            with self.assertRaises(ValueError):validate_manual_url(url)


class JudgeRemovalTests(unittest.TestCase):
    setUp = service.ServiceTests.setUp
    prepare = service.ServiceTests.prepare
    scores = service.ServiceTests.scores
    # Reuse fixtures, but do not inherit all existing service tests for this run.
    def test_remove_revokes_access_and_never_reuses_identity(self):
        raw=self.store.add_judge(self.admin)
        jid=self.store.judge_view(raw)['judge']['id']
        with self.assertRaises(PermissionError):self.store.remove_judge('',jid,'test')
        with self.assertRaises(ValueError):self.store.remove_judge(self.admin,jid,'')
        self.store.remove_judge(self.admin,jid,'Создан по ошибке')
        self.assertEqual(self.store.admin_view(self.admin)['judges'],[])
        with self.assertRaises(PermissionError):self.store.judge_view(raw)
        with self.assertRaises(ValueError):self.store.rotate_judge(self.admin,jid,'test')
        newer=self.store.add_judge(self.admin)
        judge=self.store.judge_view(newer)['judge']
        self.assertNotEqual(judge['id'],jid)
        self.assertEqual(judge['number'],2)
        self.assertEqual(self.store.audit(self.admin)[1]['action'],'remove_judge')

    def test_remove_blocked_after_opening(self):
        tokens,_=self.prepare()
        jid=self.store.judge_view(tokens[0])['judge']['id']
        with self.assertRaises(ValueError):self.store.remove_judge(self.admin,jid,'test')
        self.assertTrue(self.store.judge_view(tokens[0]))
