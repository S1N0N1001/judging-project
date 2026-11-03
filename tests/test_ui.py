import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from judging_store import Store

class InterfaceTests(unittest.TestCase):
    def test_first_setup_and_admin_login(self):
        with tempfile.TemporaryDirectory(dir='tmp') as folder, patch.dict(os.environ, {'JUDGING_DATA_DIR': str(Path(folder).resolve()), 'JUDGING_SETUP_TOKEN': 'ui-local-secret'}):
            at=AppTest.from_file(str(Path('main.py').resolve()),default_timeout=20)
            at.run()
            self.assertEqual(len(at.text_input),0)
            at.query_params['setup']='ui-local-secret'; at.run()
            at.text_input[0].set_value('test-password-long')
            at.text_input[1].set_value('test-password-long')
            at.button[0].click().run()
            at.text_input[0].set_value('test-password-long')
            at.button[0].click().run()
            self.assertFalse(at.exception)
            self.assertEqual(len(at.tabs),6)

    def test_judge_zero_scores_lock_and_export(self):
        with tempfile.TemporaryDirectory(dir='tmp') as folder, patch.dict(os.environ, {'JUDGING_DATA_DIR': str(Path(folder).resolve())}):
            s=Store(folder);cfg=json.loads(Path('config.json').read_text(encoding='utf-8'))['criteria']
            s.setup('test-password-long','local','local',cfg);admin=s.login('test-password-long')
            s.configure(admin,'UI test','criterion_trim_v1',None,cfg)
            judge=s.add_judge(admin);pid=s.add_participant(admin,'UI work')
            s.transition(admin,'scoring');s.open_participant(admin,pid)
            at=AppTest.from_file(str(Path('main.py').resolve()),default_timeout=20);at.query_params['code']=judge;at.run()
            self.assertFalse(at.exception)
            self.assertEqual(len(at.number_input),7)
            for n in at.number_input:n.set_value(0)
            at.run()
            next(b for b in at.button if b.label=='Отправить и заблокировать').click().run()
            self.assertFalse(at.exception)
            self.assertEqual(s.judge_view(judge)['cards'][0]['status'],'submitted')
            self.assertEqual(len(at.number_input),0)
            s.transition(admin,'closed')
            a=AppTest.from_file(str(Path('main.py').resolve()),default_timeout=20);a.session_state['admin_token']=admin;a.run()
            self.assertFalse(a.exception)
            self.assertEqual(len(a.get('download_button')),4)

    def test_manual_lan_qr_and_remove_judge(self):
        with tempfile.TemporaryDirectory(dir='tmp') as folder, patch.dict(os.environ, {'JUDGING_DATA_DIR': str(Path(folder).resolve())}):
            s=Store(folder);cfg=json.loads(Path('config.json').read_text(encoding='utf-8'))['criteria']
            s.setup('test-password-long','local','local',cfg);admin=s.login('test-password-long')
            raw=s.add_judge(admin);jid=s.judge_view(raw)['judge']['id']
            at=AppTest.from_file(str(Path('main.py').resolve()),default_timeout=20)
            at.session_state['admin_token']=admin
            at.session_state['new_judge_link']=raw
            at.run()
            at.selectbox(key='lan_adapter').select('manual').run()
            next(t for t in at.text_input if t.label.startswith('Адрес сервера для телефонов')).set_value('http://172.17.18.194:8501').run()
            self.assertTrue(any(c.value.startswith('http://172.17.18.194:8501/?code=') for c in at.code))
            at.text_input(key='rotate_reason_'+jid).set_value('Создан по ошибке')
            at.checkbox(key='remove_confirm_'+jid).check().run()
            at.button(key='remove_'+jid).click().run()
            self.assertFalse(at.exception)
            self.assertEqual(s.admin_view(admin)['judges'],[])
            with self.assertRaises(PermissionError):s.judge_view(raw)
