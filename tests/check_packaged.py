"""Exercise the packaged server over its real HTTP and WebSocket protocol."""
import asyncio
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets
from google.protobuf.json_format import MessageToDict
from streamlit.proto.BackMsg_pb2 import BackMsg
from streamlit.proto.ForwardMsg_pb2 import ForwardMsg

from judging_store import Store


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def fetch(url, token=None):
    request=urllib.request.Request(url,headers={'X-Judging-Control':token} if token else {})
    with urllib.request.urlopen(request,timeout=3) as f:return f.read().decode()


async def page(server_port, query):
    messages=[]
    async with websockets.connect(f'ws://127.0.0.1:{server_port}/_stcore/stream', origin=f'http://127.0.0.1:{server_port}', subprotocols=['streamlit'], max_size=32*1024*1024) as ws:
        msg=BackMsg()
        msg.rerun_script.query_string=query
        msg.rerun_script.is_auto_rerun=False
        await ws.send(msg.SerializeToString())
        for _ in range(150):
            raw=await asyncio.wait_for(ws.recv(),timeout=20)
            forward=ForwardMsg();forward.ParseFromString(raw)
            data=MessageToDict(forward,preserving_proto_field_name=True)
            if forward.HasField('delta') and forward.delta.HasField('new_element') and forward.delta.new_element.HasField('exception'):
                raise AssertionError(str(data))
            messages.append(data)
            if forward.WhichOneof('type')=='script_finished':break
        else:raise AssertionError('No completed script')
    return json.dumps(messages,ensure_ascii=False)


def main():
    exe=Path(os.environ.get('JUDGING_TEST_EXE', 'dist/Judging/Judging.exe')).resolve()
    with tempfile.TemporaryDirectory(dir='tmp') as folder:
        store=Store(folder)
        cfg=json.loads(Path('config.json').read_text(encoding='utf-8'))['criteria']
        store.setup('package-test-password','x','x',cfg)
        admin=store.login('package-test-password')
        store.configure(admin,'Packaged test','criterion_trim_v1',None,cfg)
        token=store.add_judge(admin)
        pid=store.add_participant(admin,'Packaged work')
        store.transition(admin,'scoring');store.open_participant(admin,pid)
        server_port, control_port=port(),port()
        secret=secrets.token_urlsafe(32)
        env=dict(os.environ,JUDGING_DATA_DIR=str(Path(folder).resolve()),JUDGING_CONTROL_SECRET=secret,JUDGING_INSTANCE_ID='package-test',JUDGING_SETUP_TOKEN='test-local',JUDGING_PORT=str(server_port))
        log=open(Path(folder)/'external.log','w',encoding='utf-8')
        child=subprocess.Popen([str(exe),'--serve','--address','127.0.0.1','--port',str(server_port),'--control-port',str(control_port)],env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            start=time.monotonic()
            while time.monotonic()-start<30:
                if child.poll() is not None:raise AssertionError('Packaged process exited')
                try:
                    if fetch(f'http://127.0.0.1:{server_port}/_stcore/health')=='ok':break
                except Exception:time.sleep(.2)
            else:raise AssertionError('No health response')
            from launcher import acquire_lock
            lock=acquire_lock(Path(folder))
            assert lock is not None
            state={'id':'package-test','secret':secret,'url':f'http://localhost:{server_port}','control':f'http://127.0.0.1:{control_port}'}
            (Path(folder)/'instance.json').write_text(json.dumps(state),encoding='utf-8')
            try:
                repeated=subprocess.run([str(exe),'--smoke-test'],env=env,timeout=15)
                assert repeated.returncode==0, (Path(folder)/'logs/launcher.log').read_text(encoding='utf-8')
                assert 'EXISTING_INSTANCE_OK' in (Path(folder)/'logs/launcher.log').read_text(encoding='utf-8')
            finally:
                lock.close()
            print('PACKAGED_SINGLE_INSTANCE_OK')
            login=asyncio.run(page(server_port,''))
            assert 'Вход секретаря' in login
            judge=asyncio.run(page(server_port,'code='+token))
            assert 'Packaged work' in judge
            assert 'Отправить и заблокировать' in judge
            assert 'Итоги и CSV' not in judge
            print('PACKAGED_HTTP_AND_WEBSOCKET_LOGIN_JUDGE_OK')
        finally:
            try:fetch(f'http://127.0.0.1:{control_port}/stop',secret)
            except Exception:pass
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.terminate();child.wait(timeout=10)
            log.close()
        print('PACKAGED_SERVER_STOPPED')


if __name__=='__main__':main()
