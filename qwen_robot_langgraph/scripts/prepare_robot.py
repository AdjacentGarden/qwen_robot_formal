#!/usr/bin/env python3
"""Create private, redirected configuration only in the NEW project."""
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]
old=Path('/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test')
(ROOT/'runtime').mkdir(exist_ok=True)
for name in ['projector','light','feeder']:
    data=json.loads((old/'robot_skills'/f'{name}_control/config.json').read_text())
    for key in ['state_path','lock_path']:
        if key in data:
            data[key]=str(ROOT/'runtime'/name/Path(data[key]).name)
    if 'auth_path' in data:
        auth=ROOT/'config/private_mijia_auth.json'
        if not auth.exists():
            shutil.copy2(data['auth_path'],auth);auth.chmod(0o600)
        data['auth_path']=str(auth)
        data['refresh_token']=False
    dest=ROOT/f'config/private_{name}.json'
    dest.write_text(json.dumps(data,ensure_ascii=False,indent=2));dest.chmod(0o600)
config={'cloud_budget_path':str(ROOT/'runtime/cloud_budget.sqlite'),'speech_backend':'local','api_key_path':str(old/'runtime/api_key'), 'realtime_url':'wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-flash','voice':'longanqian'}
dest=ROOT/'config/private_robot.json';dest.write_text(json.dumps(config,indent=2));dest.chmod(0o600)
print('Private configuration prepared; original files untouched.')
