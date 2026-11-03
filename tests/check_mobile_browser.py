"""Real touch-input regression check. Requires Node.js, Playwright and Edge/Chromium."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from judging_store import Store


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]


def main():
    node=os.environ.get('NODE_BINARY') or shutil.which('node')
    if not node:raise RuntimeError('Set NODE_BINARY or install Node.js')
    Path('tmp').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir='tmp') as folder:
        store=Store(folder);cfg=json.loads(Path('config.json').read_text(encoding='utf-8'))['criteria']
        store.setup('mobile-test-password','local','local',cfg);admin=store.login('mobile-test-password')
        store.configure(admin,'Mobile input test','criterion_trim_v1',None,cfg)
        token=store.add_judge(admin);pids=[store.add_participant(admin,f'Mobile work {i}') for i in range(1,3)]
        store.transition(admin,'scoring')
        for pid in pids:store.open_participant(admin,pid)
        n,c=port(),port();secret='mobile-test-control'
        env=dict(os.environ,JUDGING_DATA_DIR=str(Path(folder).resolve()),JUDGING_CONTROL_SECRET=secret,JUDGING_INSTANCE_ID='mobile-test',JUDGING_SETUP_TOKEN='local',JUDGING_PORT=str(n))
        exe=os.environ.get('JUDGING_TEST_EXE')
        command=[str(Path(exe).resolve())] if exe else [sys.executable,str(Path('launcher.py').resolve())]
        log=open(Path(folder)/'server-test.log','w',encoding='utf-8')
        proc=subprocess.Popen(command+['--serve','--address','127.0.0.1','--port',str(n),'--control-port',str(c)],env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            for _ in range(120):
                if proc.poll() is not None:raise RuntimeError('Test server exited')
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{n}/_stcore/health',timeout=2) as r:
                        if r.read()==b'ok':break
                except OSError:time.sleep(.2)
            else:raise RuntimeError('Server readiness timed out')
            browser_env=dict(env,JUDGING_TEST_URL=f'http://127.0.0.1:{n}/?code={token}')
            subprocess.run([node,str(Path('tests/mobile_input.cjs').resolve()),*sys.argv[1:]],env=browser_env,check=True,timeout=120)
            if '--before' not in sys.argv:
                cards=store.judge_view(token)['cards']
                assert len(cards)==2 and all(card['status']=='submitted' for card in cards)
                by_participant={card['participant_id']:card for card in cards}
                assert all(v==0 for v in by_participant[pids[1]]['scores'].values())
                assert by_participant[pids[0]]['scores']=={k:int(k) for k in cfg}
                print('MOBILE_DATABASE_VALUES_AND_SUBMISSION_OK')
        finally:
            try:
                req=urllib.request.Request(f'http://127.0.0.1:{c}/stop',headers={'X-Judging-Control':secret})
                urllib.request.urlopen(req,timeout=3).close()
                proc.wait(timeout=15)
            except Exception:
                proc.terminate();proc.wait(timeout=10)
            log.close()


if __name__=='__main__':main()
