#!/usr/bin/env python3
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'runtime/app_process.json'

def alive(data):
    try:
        stat=Path(f"/proc/{data['pid']}/stat").read_text().split()
        return stat[21]==data['start_ticks'] and stat[2]!='Z'
    except (OSError,KeyError):return False

def service_command(mode='hybrid'):
    command=[str(ROOT/'.venv/bin/python'),'-m','robot_graph.cli','--root',str(ROOT),'--backend','hardware','--state-dir',str(ROOT/'runtime/service')]
    if mode == 'hybrid': command += ['--hybrid-model', '--model-url', 'http://127.0.0.1:18087/v1', '--model', 'Qwen3-4B']
    elif mode == 'local': command += ['--local-stack', '--model-url', 'http://127.0.0.1:18087/v1', '--model', 'Qwen3-4B']
    elif mode == 'cloud': command += ['--cloud-model']
    else: raise ValueError('expected hybrid, local or cloud model mode')
    return command + ['--disable-local-matching', 'serve']


if __name__=='__main__':
    (ROOT/'runtime').mkdir(exist_ok=True)
    data=json.loads(STATE.read_text()) if STATE.exists() else {}
    action=sys.argv[1]
    if action=='start':
        if alive(data):raise SystemExit('new architecture service already running')
        mode=sys.argv[2] if len(sys.argv)>2 else 'hybrid'
        command=service_command(mode)
        if mode == 'local':
            with urllib.request.urlopen('http://127.0.0.1:18087/health',timeout=3) as response:
                if response.status != 200: raise SystemExit('local_model_not_ready')
        with (ROOT/'runtime/app.log').open('ab') as log:
            proc=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1'})
        data={'pid':proc.pid,'start_ticks':Path(f'/proc/{proc.pid}/stat').read_text().split()[21], 'model_mode':mode};STATE.write_text(json.dumps(data))
        deadline=time.monotonic()+15
        while alive(data) and time.monotonic()<deadline:
            try:
                with urllib.request.urlopen('http://127.0.0.1:18884/health',timeout=1) as response:
                    if response.status==200:print(response.read().decode());break
            except Exception:pass
            time.sleep(.1)
        else:raise SystemExit('service_start_failed: check runtime/app.log')
    elif action=='stop':
        if alive(data):os.killpg(data['pid'],signal.SIGTERM)
        deadline=time.monotonic()+55
        while alive(data) and time.monotonic()<deadline:time.sleep(.1)
        print(json.dumps({'stopped':not alive(data)}))
        if alive(data):raise SystemExit(1)
    elif action=='status':print(json.dumps({'alive':alive(data),**data}))
    else:raise SystemExit('expected start/stop/status')
