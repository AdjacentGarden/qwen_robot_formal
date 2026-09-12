"""Shared acceptance diagnostics and bounded audio-aware settling."""
import json
import time

TERMINAL = {'completed', 'failed', 'blocked', 'cancelled', 'unknown'}


def response_json(response):
    response.raise_for_status()
    return response.json()


def error_record(exc):
    row = {'error': f'{type(exc).__name__}:{exc}'}
    response = getattr(exc, 'response', None)
    if response is not None:
        row.update(http_status=response.status_code, response_body=response.text,
                   request_path=response.request.url.path)
    return row


def wait_quiet(client, session, timeout=45):
    deadline = time.monotonic() + timeout
    stable_since = None
    last = {}
    while time.monotonic() < deadline:
        tasks = response_json(client.get('/tasks', params={'session': session}))['tasks']
        audio = response_json(client.get('/audio-status'))
        pending = [x for x in tasks if x['status'] not in TERMINAL | {'active', 'paused'}]
        last = {'pending_tasks': pending, 'audio': audio}
        if not pending and audio['ready']:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= .2:
                return last
        else:
            stable_since = None
        time.sleep(.08)
    raise TimeoutError('pending_reply_or_task:' + json.dumps(last, ensure_ascii=False))
