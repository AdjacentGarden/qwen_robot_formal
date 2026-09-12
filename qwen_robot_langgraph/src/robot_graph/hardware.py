from __future__ import annotations

import asyncio
import fcntl
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from .contracts import StationaryPolicy
from .execution import Unavailable


class HardwareBackend:
    """Explicit allowlist. No shell arguments, generic skill execution or wheel entry point."""
    supported = {'system.time','system.status','speech.say','speaker.test','camera.capture','head.move','sensor.gate',
                 'projector.start','projector.off','projector.pause','projector.resume','projector.status','light.set','feeder.feed','feeder.status'}

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.private = self.root/'config/private_robot.json'
        self.config = json.loads(self.private.read_text()) if self.private.exists() else {}
        self.policy = StationaryPolicy()

    def acquire_owner(self):
        self.owner_file = (self.root/'runtime/hardware_owner.lock').open('a')
        try:
            fcntl.flock(self.owner_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner_file.close()
            raise RuntimeError('hardware_already_owned_by_another_orchestrator')

    def release_owner(self):
        fcntl.flock(self.owner_file, fcntl.LOCK_UN)
        self.owner_file.close()

    def preflight(self, action):
        self.policy.validate(action)
        if action.kind not in self.supported:
            raise Unavailable('hardware_adapter_not_enabled:' + action.kind)
        if action.kind in {'head.move','sensor.gate'}:
            if not (self.root/'runtime/ros.sock').is_socket():
                raise Unavailable('ros_worker_not_ready')
            audit = self.root/'runtime/head_wire_audit.json'
            if not audit.exists():
                raise Unavailable('head_only_driver_not_started')
            data=json.loads(audit.read_text())
            try:
                os.kill(data['pid'],0)
            except (OSError,KeyError):
                raise Unavailable('head_only_driver_not_alive')
            if time.time()-data.get('updated_at',0)>5 or data.get('wheel_packets_sent')!=0:
                raise Unavailable('head_only_guard_not_fresh')
            fault=self.root/'runtime/head_motion_fault.json'
            if action.kind=='head.move' and action.args.get('pose')!='level' and fault.exists():
                try: reason=json.loads(fault.read_text()).get('fault','unknown')
                except (OSError,ValueError):reason='unreadable'
                raise Unavailable('head_motion_fault_latched:'+reason)
        if action.kind in {'light.set','feeder.feed','feeder.status'}:
            cfg = self.root/('config/private_light.json' if action.kind=='light.set' else 'config/private_feeder.json')
            if not cfg.exists():
                raise Unavailable('device_configuration_missing')
        if action.kind == 'speech.say' and self.config.get('speech_backend') != 'local' and not self.config.get('api_key_path'):
            raise Unavailable('tts_key_not_configured')

    def run(self, action, cancel):
        self.preflight(action)
        if action.kind == 'system.time':
            return {'ok':True,'status':'completed','executed':False,'time':datetime.now(ZoneInfo(self.config.get('timezone','Asia/Shanghai'))).isoformat()}
        if action.kind == 'system.status':
            audit=self.root/'runtime/head_wire_audit.json'
            fault=self.root/'runtime/head_motion_fault.json'
            return {'ok':True,'status':'completed','executed':False,'base_locked':True,'driver':json.loads(audit.read_text()) if audit.exists() else None,'head_motion_fault':json.loads(fault.read_text()) if fault.exists() else None,'observed_at':time.time()}
        if action.kind == 'speech.say':
            if self.config.get('speech_backend') == 'local':
                from .local_audio import speak
                return speak(self.root, action.args['text'], cancel)
            from .voice import synthesize
            return asyncio.run(synthesize(action.args['text'], self.config, cancel, self.root/'runtime'))
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1'}
        if action.kind in {'head.move','sensor.gate'}:
            from .ipc import request
            started=time.monotonic()
            result=request(self.root/'runtime/ros.sock', {'kind':action.kind,'args':action.args}, timeout=action.timeout)
            result['elapsed_ms']=(time.monotonic()-started)*1000
            if not result.get('ok') and result.get('executed') is None:
                result['status']='unknown'
            return result
        else:
            command=['/usr/bin/python3',str(self.root/'scripts/hardware_worker.py'),action.kind,json.dumps(action.args)]
        proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env,start_new_session=True)
        started=time.monotonic()
        while proc.poll() is None:
            if cancel.is_set() and action.kind=='speaker.test':
                os.killpg(proc.pid, signal.SIGTERM)
                proc.communicate(timeout=3)
                return {'ok':False,'status':'cancelled','executed':True}
            if time.monotonic()-started>action.timeout:
                os.killpg(proc.pid,signal.SIGTERM)
                try:
                    proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid,signal.SIGKILL);proc.communicate()
                return {'ok':False,'status':'unknown','executed':None,'error':'hardware_deadline_outcome_unconfirmed'}
            time.sleep(.01)
        stdout,stderr=proc.communicate()
        for line in reversed(stdout.splitlines()):
            try:
                result=json.loads(line)
            except ValueError:
                continue
            if isinstance(result,dict) and 'ok' in result:
                result['elapsed_ms']=(time.monotonic()-started)*1000
                if not result['ok'] and action.kind == 'feeder.feed':
                    result['status']='unknown'
                return result
        return {'ok':False,'status':'unknown','executed':None,'error':'driver_no_structured_result','exit_code':proc.returncode,'diagnostic':stderr[-800:]}
