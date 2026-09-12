#!/usr/bin/env python3
"""Own one local Qwen server; no NPU reset, no firmware changes, no competing models."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'runtime/local_model_process.json'
MODEL=Path('/home/test/qwen3_4b_rknn3_benchmark/model')
PORT=18087


def alive(data):
    try:
        stat=Path(f"/proc/{data['pid']}/stat").read_text().split()
        return stat[21]==data['start_ticks'] and stat[2]!='Z'
    except (OSError,KeyError):return False


if __name__=='__main__':
    if sys.argv[1]=='start':
        if subprocess.run(['pgrep','-x','rkllm3-server'],stdout=subprocess.DEVNULL).returncode==0:
            raise SystemExit('An rkllm3-server already exists; refusing to start a competing model.')
        with socket.socket() as probe:probe.bind(('127.0.0.1',PORT))
        command=['/usr/bin/rkllm3-server','-m',str(MODEL/'Qwen3-4B-38400.rknn'),'--weight',str(MODEL/'Qwen3-4B.weight'),'--embed',str(MODEL/'Qwen3-4B.embed.bin'),'--vocab',str(MODEL/'Qwen3-4B.tokenizer.gguf'),'-a','Qwen3-4B','-c','4096','-n','512','--host','127.0.0.1','--port',str(PORT),'--device-id','0004:41:00.0']
        with (ROOT/'runtime/local_model.log').open('ab') as log:
            proc=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env={**os.environ,'RKLLM_LOG_LEVEL':'1'})
        data={'pid':proc.pid,'start_ticks':Path(f'/proc/{proc.pid}/stat').read_text().split()[21],'port':PORT}
        STATE.write_text(json.dumps(data));start=time.monotonic()
        # A cold RKNN load can exceed a minute after the transfer service was
        # restarted.  Keep the single owned process alive while its health
        # endpoint comes up instead of killing a healthy load at 75 seconds.
        while time.monotonic()-start<240 and proc.poll() is None:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/health',timeout=2) as response:
                    if response.status==200:
                        print(json.dumps({'ready':True,'seconds':time.monotonic()-start,**data}));break
            except Exception:pass
            time.sleep(.5)
        else:
            if proc.poll() is None:proc.terminate()
            raise SystemExit('Model startup failed; see only the new project runtime/local_model.log')
    elif sys.argv[1]=='stop':
        data=json.loads(STATE.read_text()) if STATE.exists() else {}
        if alive(data):os.killpg(data['pid'],signal.SIGTERM)
        print(json.dumps({'stop_requested':bool(data)}))
    elif sys.argv[1]=='status':
        data=json.loads(STATE.read_text()) if STATE.exists() else {}
        print(json.dumps({'alive':alive(data),**data}))
