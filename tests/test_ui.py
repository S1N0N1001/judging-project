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
            score_fields = [widget for widget in at.selectbox if widget.key and widget.key.startswith('score_')]
            self.assertEqual(len(score_fields),7)
            self.assertEqual(len(at.number_input),0)
            self.assertTrue(all(widget.value is None for widget in score_fields))
            for n in score_fields:n.select(0)
            at.run()
            next(b for b in at.button if b.label=='Отправить и заблокировать').click().run()
            self.assertFalse(at.exception)
            self.assertEqual(s.judge_view(judge)['cards'][0]['status'],'submitted')
            self.assertFalse(any(widget.key and widget.key.startswith('score_') for widget in at.selectbox))
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

    def test_partial_choice_and_saved_draft_restore_without_keyboard_commit(self):
        with tempfile.TemporaryDirectory(dir='tmp') as folder, patch.dict(os.environ, {'JUDGING_DATA_DIR': str(Path(folder).resolve())}):
            s=Store(folder);cfg=json.loads(Path('config.json').read_text(encoding='utf-8'))['criteria']
            s.setup('test-password-long','local','local',cfg);admin=s.login('test-password-long')
            s.configure(admin,'Choice test','criterion_trim_v1',None,cfg)
            token=s.add_judge(admin);pid=s.add_participant(admin,'Choice work')
            s.transition(admin,'scoring');s.open_participant(admin,pid)
            at=AppTest.from_file(str(Path('main.py').resolve()),default_timeout=20)
            at.query_params['code']=token;at.run()
            fields=[w for w in at.selectbox if w.key and w.key.startswith('score_')]
            fields[0].select(0).run()
            self.assertTrue(any('Заполнено: 1 / 7' in item.value for item in at.markdown))
            self.assertTrue(next(b for b in at.button if b.label=='Отправить и заблокировать').disabled)
            next(b for b in at.button if b.label=='Сохранить черновик').click().run()
            restored=AppTest.from_file(str(Path('main.py').resolve()),default_timeout=20)
            restored.query_params['code']=token;restored.run()
            fields=[w for w in restored.selectbox if w.key and w.key.startswith('score_')]
            self.assertEqual([w.value for w in fields],[0,None,None,None,None,None,None])
            for idx,crit in enumerate(list(cfg.values())[1:],1):
                fields=[w for w in restored.selectbox if w.key and w.key.startswith('score_')]
                fields[idx].select(crit['max']).run()
                self.assertTrue(any(f'Заполнено: {idx+1} / 7' in item.value for item in restored.markdown))
            next(b for b in restored.button if b.label=='Отправить и заблокировать').click().run()
            card=s.judge_view(token)['cards'][0]
            self.assertEqual(card['status'],'submitted')
            self.assertEqual(card['scores'],{k:0 if k=='1' else c['max'] for k,c in cfg.items()})
