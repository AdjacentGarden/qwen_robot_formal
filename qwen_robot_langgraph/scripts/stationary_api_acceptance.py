#!/usr/bin/env python3
"""Repeatable before/after API acceptance. No navigation or Mijia execution."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import httpx
from robot_graph.local_audio import request
from stationary_api_support import error_record, wait_quiet

TERMINAL = {'completed', 'failed', 'blocked', 'cancelled', 'unknown'}
CASES = [
    ('text', '请告诉我现在的时间', 'system.time'),
    ('text', '请检查一下设备状态', 'system.status'),
    ('text', '暂时不要开灯', 'chat'),
    ('text', '如果我要开会，你会怎么做', 'chat'),
    ('text', '抬头然后用前摄拍照', 'chat'),
    ('text', '把头调到一百九十度', 'chat'),
    ('text', '麻烦抬一下头', 'head.move:up'),
    ('text', '麻烦让头部回正', 'head.move:level'),
    ('text', '请用前摄拍照', 'camera.capture:front'),
    ('text', '请用后摄拍照', 'camera.capture:back'),
    ('voice', '请告诉我现在的时间', 'system.time'),
    ('voice', '请检查一下设备状态', 'system.status'),
    ('voice', '用前面的摄像头拍一张照片', 'camera.capture:front'),
    ('voice', '用后面的摄像头拍一张照片', 'camera.capture:back'),
    ('voice', '暂时不要开灯', 'chat'),
    ('voice', '你能开灯吗', 'chat'),
    ('text', '启动会议投影', 'blocked_navigation'),
    ('text', '原地启动会议投影', 'meeting_stationary'),
    ('text', '请暂停当前任务', 'pause'),
    ('text', '请继续当前任务', 'resume'),
    ('text', '请结束当前任务', 'cancel'),
]


def run(label, resume=False):
    out = ROOT / 'runtime' / label
    out.mkdir(exist_ok=resume)
    client = httpx.Client(base_url='http://127.0.0.1:18884', timeout=45)
    session = 'stationary_' + uuid.uuid4().hex
    before = client.get('/health').json()
    rows = [json.loads(x) for x in (out / 'results.jsonl').read_text().splitlines()] if resume and (out / 'results.jsonl').exists() else []
    finished_indices = {x['index'] for x in rows}

    def call(path, data):
        response = client.post(path, json={'session': session, **data})
        response.raise_for_status()
        return response.json()

    def wait_task(task, expected, timeout=100):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = client.get('/task', params={'session': session, 'id': task}).json()
            if state['status'] in TERMINAL or state['status'] == expected:
                return state
            time.sleep(.08)
        raise TimeoutError('task_not_confirmed')

    def quiet():
        return wait_quiet(client, session)

    try:
        for index, (mode, text, expected) in enumerate(CASES):
            if index in finished_indices:
                continue
            row = {'index': index, 'mode': mode, 'text': text, 'expected': expected}
            start = time.monotonic()
            try:
                row['quiet'] = quiet()
                if mode == 'voice':
                    speech = request(ROOT, {'op': 'tts', 'text': text})
                    clip = out / 'fixture.pcm'
                    clip.write_bytes(base64.b64decode(speech['pcm']))
                    with ThreadPoolExecutor() as pool:
                        future = pool.submit(call, '/voice-turn', {'request_id': str(index), 'seconds': 4})
                        time.sleep(.5)
                        if future.done():
                            future.result()  # Preserve rejection body before playing a fixture.
                            raise RuntimeError('recording_finished_before_fixture')
                        subprocess.run(['paplay', '--raw', '--format=s16le', '--channels=1', '--rate=' + str(speech['rate']), str(clip)], check=True, timeout=15)
                        result = future.result()
                else:
                    result = call('/turn', {'request_id': str(index), 'text': text})
                row['response'] = result
                intent = result.get('intent', {})
                task = result.get('task_id')
                target = {'meeting_stationary': 'active', 'pause': 'paused', 'resume': 'active', 'cancel': 'cancelled'}.get(expected, 'completed')
                state = wait_task(task, target) if task else None
                row['state'] = state
                if expected == 'chat':
                    passed = not task and intent.get('type') == 'chat' and not result.get('error')
                elif expected == 'blocked_navigation':
                    passed = not task and 'base_motion_forbidden' in result.get('error', '')
                elif expected in {'pause', 'resume', 'cancel'}:
                    passed = intent.get('command') == expected and state and state['status'] == target
                elif expected == 'meeting_stationary':
                    passed = intent.get('name') == expected and state and state['status'] == target
                else:
                    kind, _, arg = expected.partition(':')
                    passed = intent.get('kind') == kind and state and state['status'] == 'completed'
                    if arg:
                        passed = passed and intent.get('args', {}).get('pose' if kind == 'head.move' else 'camera') == arg
                row['passed'] = bool(passed)
                if state and state['status'] == 'unknown':
                    raise RuntimeError('unknown_hardware_result')
            except Exception as exc:
                row.update(passed=False, **error_record(exc))
            row['seconds'] = time.monotonic() - start
            rows.append(row)
            with (out / 'results.jsonl').open('a') as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            print(json.dumps({k: row.get(k) for k in ['index', 'mode', 'text', 'passed', 'seconds', 'error']}, ensure_ascii=False), flush=True)
            if 'unknown_hardware_result' in row.get('error', ''):
                break
    finally:
        tasks = client.get('/tasks', params={'session': session}).json()['tasks']
        for task in tasks:
            if task['status'] not in TERMINAL:
                call('/control', {'task_id': task['id'], 'command': 'cancel'})
                wait_task(task['id'], 'cancelled')
        # Explicit test cleanup even if a preceding spoken return-level command failed.
        cleanup = call('/tasks', {'request_id': 'head_level_cleanup', 'action': {'kind': 'head.move', 'args': {'pose': 'level'}}})
        cleanup_state = wait_task(cleanup['task_id'], 'completed')
        report = {'planned': len(CASES), 'total': len(rows), 'passed': sum(x['passed'] for x in rows), 'before': before, 'after': client.get('/health').json(), 'cleanup_status': cleanup_state['status'], 'wire_audit': json.loads((ROOT / 'runtime/head_wire_audit.json').read_text())}
        (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)
        client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    run(args.output, args.resume)
