from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import uuid

from .contracts import Action, PolicyError
from .audio_state import audio_status
from .workflows import atomic_plan, build_plan


def create_server(runtime, dialogue, port=18884):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, status, payload):
            body=json.dumps(payload,ensure_ascii=False,default=str).encode()
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_GET(self):
            parsed=urlparse(self.path);query=parse_qs(parsed.query);session=query.get('session',['desktop'])[0]
            if parsed.path=='/':
                body=(Path(__file__).parents[2]/'static/index.html').read_bytes()
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
            if parsed.path=='/health':
                
                from .cloud_budget import CloudBudget
                backend_config=getattr(runtime.executor.backend,'config',{})
                budget_path=backend_config.get('cloud_budget_path')
                budget=CloudBudget(budget_path).status() if budget_path else None
                model=getattr(dialogue.router,'model',None)
                self.reply(200,{'ok':True,'base_locked':True,'backend':type(runtime.executor.backend).__name__,'intent_model':getattr(model,'model',type(model).__name__ if model else 'rules_only'),'intent_endpoint':getattr(model,'url',None),'local_matching':getattr(dialogue.router,'local_matching_enabled',True),'tts':backend_config.get('speech_backend'),'asr':'sensevoice_cpu','cloud_budget':budget});return
            if parsed.path=='/audio-status':
                self.reply(200,audio_status(runtime.store));return
            if parsed.path=='/tasks':
                rows=runtime.store.all("SELECT id,status,command,created FROM tasks WHERE session=? AND id NOT LIKE 'voice:%' ORDER BY created DESC LIMIT 30",(session,))
                self.reply(200,{'tasks':rows});return
            if parsed.path=='/task':
                try:self.reply(200,runtime.status(query.get('id',[''])[0],session))
                except ValueError as exc:self.reply(404,{'error':str(exc)})
                return
            if parsed.path=='/events':
                task=query.get('task',[''])[0]
                try:runtime.status(task,session)
                except ValueError as exc:self.reply(404,{'error':str(exc)});return
                rows=runtime.store.all('SELECT * FROM events WHERE task=? ORDER BY id DESC LIMIT 200',(task,))
                self.reply(200,{'events':rows});return
            self.reply(404,{'error':'not_found'})
        def do_POST(self):
            # Local application API; reject browser-origin cross-site commands.
            origin=self.headers.get('Origin')
            if origin and urlparse(origin).netloc != self.headers.get('Host'):
                self.reply(403,{'error':'cross_origin_request_rejected'});return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if length > 65536:
                    self.connection.settimeout(3)
                    if length <= 1048576: self.rfile.read(length)
                    self.reply(413, {'error':'request_too_large'});return
                if length <= 0: raise PolicyError('invalid_body_size')
                p=json.loads(self.rfile.read(length));session=p.get('session','desktop')
                key=p.get('request_id') or uuid.uuid4().hex
                if self.path=='/turn': result=dialogue.turn(session,key,p['text'])
                elif self.path=='/voice-turn':
                    from .local_audio import record_and_transcribe
                    backend=runtime.executor.backend
                    if not hasattr(backend, 'root'): raise PolicyError('microphone_requires_hardware_backend')
                    owner='mic:'+uuid.uuid4().hex
                    # Check and claim both resources under the same lock. A queued
                    # announcement cannot start between the check and recording.
                    with runtime.store.lock:
                        audio=audio_status(runtime.store)
                        if not audio['ready']: raise PolicyError(audio['reason'])
                        if not runtime.store.claim(owner,0,['microphone','speaker']):
                            raise PolicyError('microphone_busy')
                    try:
                        recognized=record_and_transcribe(backend.root,p.get('seconds',4))
                        if recognized['text'].strip():
                            result=dialogue.turn(session,key,recognized['text'])
                        else:
                            result={'reply':'没有听清楚，请再说一次。','task_id':'','error':'no_speech_detected'}
                        result['recognized_text']=recognized['text']
                        result['audio_metrics']={k:recognized[k] for k in ('seconds','captured_seconds','backend') if k in recognized}
                        if not result.get('task_id'):
                            result['reply_audio_task']=runtime.submit(session,key+':reply',atomic_plan(Action('speech.say',{'text':result.get('reply','')[:500]})))
                    finally: runtime.store.release(owner)
                elif self.path=='/tasks':
                    plan=build_plan(p['workflow'],p.get('parameters')) if 'workflow' in p else atomic_plan(Action.parse(p['action']))
                    result={'task_id':runtime.submit(session,key,plan)}
                elif self.path=='/control': result=runtime.control(session,p['task_id'],p['command'])
                else: self.reply(404,{'error':'not_found'});return
                self.reply(200,result)
            except (ValueError,KeyError,TypeError) as exc:self.reply(400,{'error':str(exc)})
            except Exception as exc:self.reply(503,{'error':str(exc)[:200]})
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)
