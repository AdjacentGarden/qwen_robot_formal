from __future__ import annotations
import argparse
import asyncio
import json
import os
import signal
from pathlib import Path
import time
import uuid

from .contracts import Action
from .dialogue import ConservativeRouter, Dialogue, JsonModel
from .execution import SimulatedBackend
from .hardware import HardwareBackend
from .runtime import Runtime
from .server import create_server
from .workflows import atomic_plan, build_plan


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument('--backend',choices=['sim','hardware'],default='sim')
    parser.add_argument('--state-dir')
    parser.add_argument('--cloud-model',action='store_true')
    parser.add_argument('--hybrid-model',action='store_true',help='Local intent first; budgeted cloud fallback after invalid local proposals')
    parser.add_argument('--local-stack',action='store_true',help='Require loopback LLM and local ASR/TTS; never fall back to cloud')
    parser.add_argument('--disable-local-matching',action='store_true',help='Send every utterance to the configured intent model; keep post-model safety validation')
    parser.add_argument('--model-url',default=os.environ.get('ROBOT_MODEL_URL'))
    parser.add_argument('--model',default=os.environ.get('ROBOT_MODEL','qwen-plus'))
    sub=parser.add_subparsers(dest='command',required=True)
    serve=sub.add_parser('serve');serve.add_argument('--port',type=int,default=18884)
    demo=sub.add_parser('demo');demo.add_argument('--workflow',default='meeting_stationary');demo.add_argument('--seconds',type=float,default=2)
    action=sub.add_parser('action');action.add_argument('kind');action.add_argument('--args',default='{}')
    say=sub.add_parser('turn');say.add_argument('text')
    sub.add_parser('voice')
    args=parser.parse_args();root=Path(args.root)
    def terminate(*_): raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, terminate)
    backend=HardwareBackend(root) if args.backend=='hardware' else SimulatedBackend()
    if args.local_stack:
        from urllib.parse import urlparse
        if args.cloud_model or args.hybrid_model or not args.model_url or urlparse(args.model_url).hostname not in {'localhost','127.0.0.1','::1'}:
            parser.error('--local-stack requires a loopback --model-url and disallows --cloud-model')
        if args.command == 'voice':
            parser.error('local speech input uses the console recording button /voice-turn')
        if isinstance(backend,HardwareBackend): backend.config['speech_backend']='local'
    if args.hybrid_model:
        from urllib.parse import urlparse
        if args.cloud_model or not isinstance(backend,HardwareBackend) or urlparse(args.model_url or '').hostname not in {'localhost','127.0.0.1','::1'}:
            parser.error('--hybrid-model requires hardware and a loopback local model endpoint')
        backend.config['speech_backend']='local'
        if args.command == 'voice': parser.error('hybrid local speech input uses the console recording button /voice-turn')
    rt=Runtime(args.state_dir or root/'runtime'/args.backend,backend)
    from .cloud_budget import CloudBudget
    router=ConservativeRouter(JsonModel(args.model_url,args.model,os.environ.get('ROBOT_MODEL_KEY',''),budget=CloudBudget(root/'runtime/cloud_budget.sqlite')) if args.model_url else None, local_matching=not args.disable_local_matching)
    if args.cloud_model:
        from .realtime_model import RealtimeJsonModel
        if not isinstance(backend,HardwareBackend):raise ValueError('cloud_model_requires_robot_config')
        router=ConservativeRouter(RealtimeJsonModel(backend.config), local_matching=not args.disable_local_matching)
    if args.hybrid_model:
        from .hybrid import HybridIntentModel
        from .realtime_model import RealtimeJsonModel
        def route_event(record):
            with (root/'runtime/hybrid_routes.jsonl').open('a') as stream:
                stream.write(json.dumps({'at':time.time(),**record},ensure_ascii=False)+'\n')
        router=ConservativeRouter(HybridIntentModel(router.model,RealtimeJsonModel(backend.config),route_event), local_matching=not args.disable_local_matching)
    dialogue=Dialogue(rt,router)
    try:
        if args.command=='serve':
            rt.start();server=create_server(rt,dialogue,args.port)
            print(json.dumps({'ready':True,'url':f'http://127.0.0.1:{args.port}','base_locked':True}),flush=True)
            try:server.serve_forever()
            finally:server.server_close()
        elif args.command=='voice':
            if not isinstance(backend,HardwareBackend):raise ValueError('voice_requires_hardware_backend')
            rt.start();asyncio.run(__import__('robot_graph.voice',fromlist=['listen']).listen(dialogue,backend.config))
        else:
            if args.command=='turn':
                response=dialogue.turn('cli',uuid.uuid4().hex,args.text);print(json.dumps(response,ensure_ascii=False));task=response.get('task_id')
                if not task:return
            else:
                plan=build_plan(args.workflow,{'hold_seconds':args.seconds}) if args.command=='demo' else atomic_plan(Action(args.kind,json.loads(args.args)))
                task=rt.submit('cli',uuid.uuid4().hex,plan)
            while True:
                rt.tick_all();status=rt.status(task)
                if status['status'] in {'completed','failed','cancelled','unknown','blocked'}:
                    print(json.dumps(status,ensure_ascii=False));break
                time.sleep(.02)
    finally:rt.close(cancel_tasks=True)


if __name__=='__main__':main()
