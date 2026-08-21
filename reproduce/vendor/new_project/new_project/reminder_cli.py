from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .reminder_service import ReminderError, execute_reminder_skill


SKILL_IDS = {
    "reminder_schedule": "reminder.schedule",
    "reminder_query": "reminder.query",
    "reminder_cancel": "reminder.cancel",
}


def _parse_args(argv: list[str] | None = None) -> tuple[str, dict[str, Any], bool]:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("action", nargs="?")
    parser.add_argument("--action", dest="action_flag")
    parser.add_argument("--json-params", default="{}")
    parser.add_argument("--dry-run", action="store_true")
    known, unknown = parser.parse_known_args(argv)
    try:
        params = json.loads(known.json_params) if known.json_params else {}
    except json.JSONDecodeError as exc:
        raise ReminderError(f"json_params 格式错误: {exc}") from exc
    if not isinstance(params, dict):
        raise ReminderError("json_params 必须是 JSON 对象")
    index = 0
    while index < len(unknown):
        token = unknown[index]
        if not token.startswith("--"):
            raise ReminderError(f"无法识别的参数: {token}")
        key = token[2:].replace("-", "_")
        if index + 1 >= len(unknown) or unknown[index + 1].startswith("--"):
            params[key] = True
            index += 1
        else:
            params[key] = unknown[index + 1]
            index += 2
    return str(known.action_flag or known.action or "run"), params, bool(known.dry_run)


def main(skill_dir_name: str | None = None) -> int:
    directory = skill_dir_name or Path(__file__).resolve().parent.name
    skill_id = SKILL_IDS.get(directory, directory.replace("_", "."))
    action = "run"
    try:
        action, params, dry_run = _parse_args()
        runtime_dir = os.environ.get("SINGLE_FUNCTION_RUNTIME_DIR", "/home/test/single_function/runtime/agent_0625")
        result = execute_reminder_skill(skill_id, action, params, dry_run, runtime_dir)
        payload = {"ok": True, "status": "done", "skill": skill_id, "action": action, "result": result, "error": None}
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        payload = {"ok": False, "status": "error", "skill": skill_id, "action": action, "result": None, "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return 2
