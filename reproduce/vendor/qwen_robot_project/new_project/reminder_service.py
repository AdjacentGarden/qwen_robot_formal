from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


class ReminderError(ValueError):
    pass


def _chinese_number(text: str) -> float:
    text = text.strip()
    if not text:
        raise ReminderError("时间数值为空")
    if text == "半":
        return 0.5
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text == "十":
        return 10.0
    if "十" in text:
        left, right = text.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return float(tens * 10 + ones)
    if all(char in digits for char in text):
        return float("".join(str(digits[char]) for char in text))
    raise ReminderError(f"无法识别时间数值: {text}")


def parse_trigger(trigger_time: Any = None, trigger_condition: Any = None, now: float | None = None) -> tuple[float, str]:
    now_ts = float(now if now is not None else time.time())
    now_dt = datetime.fromtimestamp(now_ts)
    raw = trigger_time if trigger_time not in (None, "") else trigger_condition
    if isinstance(raw, (int, float)):
        due_at = float(raw)
        if due_at <= now_ts:
            raise ReminderError("提醒时间必须晚于当前时间")
        return due_at, datetime.fromtimestamp(due_at).strftime("%m月%d日 %H:%M:%S")
    text = str(raw or "").strip()
    if not text:
        raise ReminderError("缺少提醒时间")

    relative = re.search(r"([零一二两三四五六七八九十百\d.]+|半)\s*(秒钟?|分钟?|小时|天)\s*(?:以后|之后|后)", text)
    if relative:
        value = _chinese_number(relative.group(1))
        unit = relative.group(2)
        multiplier = 1 if unit.startswith("秒") else 60 if unit.startswith("分") else 3600 if unit == "小时" else 86400
        seconds = value * multiplier
        if seconds <= 0:
            raise ReminderError("提醒间隔必须大于零")
        due_at = now_ts + seconds
        return due_at, f"{text}（{datetime.fromtimestamp(due_at).strftime('%m月%d日 %H:%M:%S')}）"

    day_offset = 0
    if "后天" in text:
        day_offset = 2
    elif "明天" in text:
        day_offset = 1
    clock = re.search(r"(?:(凌晨|早上|上午|中午|下午|傍晚|晚上)\s*)?(\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*[点时](?:(半)|(\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*分?)?", text)
    if clock:
        period = clock.group(1) or ""
        hour = int(_chinese_number(clock.group(2)))
        minute = 30 if clock.group(3) else int(_chinese_number(clock.group(4))) if clock.group(4) else 0
        if period in {"下午", "傍晚", "晚上"} and hour < 12:
            hour += 12
        elif period in {"凌晨", "早上", "上午"} and hour == 12:
            hour = 0
        elif period == "中午" and hour < 11:
            hour += 12
        if hour > 23 or minute > 59:
            raise ReminderError("提醒时刻不合法")
        due_dt = (now_dt + timedelta(days=day_offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day_offset == 0 and due_dt.timestamp() <= now_ts:
            due_dt += timedelta(days=1)
        return due_dt.timestamp(), due_dt.strftime("%m月%d日 %H:%M")

    normalized = text.replace("年", "-").replace("月", "-").replace("日", " ").replace("时", ":").replace("点", ":").replace("分", "")
    normalized = re.sub(r"\s+", " ", normalized).strip(" -:")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        with contextlib.suppress(ValueError):
            parsed = datetime.strptime(normalized, fmt)
            if fmt.startswith("%m"):
                parsed = parsed.replace(year=now_dt.year)
            elif fmt.startswith("%H"):
                parsed = parsed.replace(year=now_dt.year, month=now_dt.month, day=now_dt.day)
            if parsed.timestamp() <= now_ts and fmt.startswith(("%m", "%H")):
                parsed = parsed.replace(year=parsed.year + 1) if fmt.startswith("%m") else parsed + timedelta(days=1)
            if parsed.timestamp() <= now_ts:
                raise ReminderError("提醒时间必须晚于当前时间")
            return parsed.timestamp(), parsed.strftime("%m月%d日 %H:%M:%S")
    raise ReminderError(f"无法解析提醒时间: {text}")


class ReminderStore:
    def __init__(self, runtime_dir: str | Path):
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.runtime_dir / "reminders.json"
        self.lock_path = self.runtime_dir / "reminders.lock"

    @contextlib.contextmanager
    def _locked(self):
        import fcntl

        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ReminderError(f"提醒存储损坏: {exc}") from exc
        if not isinstance(value, list):
            raise ReminderError("提醒存储格式不是列表")
        return [item for item in value if isinstance(item, dict)]

    def _write_unlocked(self, reminders: list[dict[str, Any]]) -> None:
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with temp.open("w", encoding="utf-8") as file:
            json.dump(reminders, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, self.path)

    def schedule(self, params: dict[str, Any]) -> dict[str, Any]:
        content = str(params.get("reminder_content") or params.get("content") or "").strip()
        if not content:
            raise ReminderError("缺少提醒内容")
        trigger_condition = params.get("trigger_condition") or params.get("trigger_time_or_condition")
        due_at, display_time = parse_trigger(params.get("trigger_time"), trigger_condition)
        reminder_id = str(params.get("reminder_id") or uuid4().hex[:12]).strip()
        now = time.time()
        item = {
            "id": reminder_id,
            "content": content,
            "trigger_time": params.get("trigger_time"),
            "trigger_condition": trigger_condition,
            "due_at": due_at,
            "display_time": display_time,
            "audience": str(params.get("audience") or "current_user"),
            "created_at": now,
            "updated_at": now,
            "state": "scheduled",
            "attempts": 0,
        }
        with self._locked():
            reminders = self._read_unlocked()
            existing = next((entry for entry in reminders if str(entry.get("id")) == reminder_id), None)
            if existing:
                same = existing.get("content") == content and abs(float(existing.get("due_at") or 0) - due_at) < 1
                if same:
                    return existing
                raise ReminderError(f"提醒 ID 已存在: {reminder_id}")
            duplicate = next(
                (
                    entry for entry in reminders
                    if entry.get("state") == "scheduled"
                    and entry.get("content") == content
                    and entry.get("trigger_time") == params.get("trigger_time")
                    and entry.get("trigger_condition") == trigger_condition
                    and now - float(entry.get("created_at") or 0) < 30.0
                ),
                None,
            )
            if duplicate:
                return duplicate
            reminders.append(item)
            self._write_unlocked(reminders)
        return item

    def query(self, include_terminal: bool = False) -> list[dict[str, Any]]:
        self.quarantine_invalid()
        with self._locked():
            reminders = self._read_unlocked()
        if not include_terminal:
            reminders = [item for item in reminders if item.get("state") in {"scheduled", "firing"}]
        return sorted(reminders, key=lambda item: float(item.get("due_at") or float("inf")))

    def quarantine_invalid(self) -> int:
        now = time.time()
        count = 0
        with self._locked():
            reminders = self._read_unlocked()
            for item in reminders:
                if item.get("state") == "scheduled" and not isinstance(item.get("due_at"), (int, float)):
                    item["state"] = "invalid"
                    item["last_error"] = "legacy reminder has no executable due_at"
                    item["updated_at"] = now
                    count += 1
            if count:
                self._write_unlocked(reminders)
        return count

    def cancel(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        reminder_id = str(params.get("reminder_id") or "").strip()
        query = str(params.get("query") or params.get("content") or "").strip()
        if not reminder_id and not query:
            raise ReminderError("取消提醒必须提供 reminder_id 或 query")
        now = time.time()
        with self._locked():
            reminders = self._read_unlocked()
            candidates = [item for item in reminders if item.get("state") == "scheduled"]
            if query in {"刚才", "刚刚", "最近", "上一个"} and candidates:
                target_ids = {str(max(candidates, key=lambda item: float(item.get("created_at") or 0)).get("id"))}
            else:
                target_ids = {
                    str(item.get("id"))
                    for item in candidates
                    if (reminder_id and str(item.get("id")) == reminder_id)
                    or (query and query in str(item.get("content") or ""))
                }
            cancelled = []
            for item in reminders:
                if str(item.get("id")) in target_ids:
                    item["state"] = "cancelled"
                    item["cancelled_at"] = now
                    item["updated_at"] = now
                    cancelled.append(dict(item))
            if cancelled:
                self._write_unlocked(reminders)
        return cancelled

    def claim_due(self, lease_seconds: float = 60.0) -> dict[str, Any] | None:
        now = time.time()
        with self._locked():
            reminders = self._read_unlocked()
            changed = False
            for item in reminders:
                if item.get("state") == "firing" and now - float(item.get("claimed_at") or now) > lease_seconds:
                    item["state"] = "scheduled"
                    changed = True
            due = [
                item for item in reminders
                if item.get("state") == "scheduled"
                and isinstance(item.get("due_at"), (int, float))
                and float(item.get("next_attempt_at") or item.get("due_at")) <= now
            ]
            if not due:
                if changed:
                    self._write_unlocked(reminders)
                return None
            target = min(due, key=lambda item: float(item.get("due_at") or 0))
            target["state"] = "firing"
            target["claimed_at"] = now
            target["updated_at"] = now
            self._write_unlocked(reminders)
            return dict(target)

    def complete(self, reminder_id: str, spoken: bool, error: str = "", max_attempts: int = 3, retry_seconds: float = 5.0) -> None:
        now = time.time()
        with self._locked():
            reminders = self._read_unlocked()
            for item in reminders:
                if str(item.get("id")) != str(reminder_id):
                    continue
                attempts = int(item.get("attempts") or 0) + 1
                item["attempts"] = attempts
                item["updated_at"] = now
                item["last_error"] = error
                if spoken:
                    item["state"] = "fired"
                    item["fired_at"] = now
                elif attempts >= max_attempts:
                    item["state"] = "failed"
                    item["failed_at"] = now
                else:
                    item["state"] = "scheduled"
                    item["next_attempt_at"] = now + retry_seconds
                break
            self._write_unlocked(reminders)


class ReminderScheduler:
    def __init__(self, store: ReminderStore, callback: Callable[[dict[str, Any]], bool], poll_seconds: float = 0.2):
        self.store = store
        self.callback = callback
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.store.quarantine_invalid()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="reminder-scheduler", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                item = self.store.claim_due()
                if item is None:
                    self.stop_event.wait(self.poll_seconds)
                    continue
                spoken = bool(self.callback(item))
                self.store.complete(str(item["id"]), spoken, "" if spoken else "tts_failed")
            except Exception as exc:
                print(f"REMINDER_SCHEDULER_ERROR:{exc}", flush=True)
                self.stop_event.wait(min(1.0, self.poll_seconds * 2))


def execute_reminder_skill(skill_id: str, action: str, params: dict[str, Any], dry_run: bool, runtime_dir: str | Path) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ReminderError("参数必须是 JSON 对象")
    if dry_run:
        if skill_id == "reminder.schedule":
            content = str(params.get("reminder_content") or params.get("content") or "").strip()
            if not content:
                raise ReminderError("缺少提醒内容")
            parse_trigger(params.get("trigger_time"), params.get("trigger_condition") or params.get("trigger_time_or_condition"))
        elif skill_id == "reminder.cancel" and not (params.get("reminder_id") or params.get("query") or params.get("content")):
            raise ReminderError("取消提醒必须提供 reminder_id 或 query")
        return {"skill_id": skill_id, "action": action, "params": params, "dry_run": True, "message": "dry-run 校验通过"}
    store = ReminderStore(runtime_dir)
    if skill_id == "reminder.schedule":
        reminder = store.schedule(params)
        message = f"好的，我会在{reminder['display_time']}提醒你{reminder['content']}"
        return {"skill_id": skill_id, "action": action, "reminder": reminder, "message": message}
    if skill_id == "reminder.query":
        reminders = store.query(include_terminal=bool(params.get("include_terminal")))
        if reminders:
            details = "；".join(f"{item.get('display_time', '时间未设置')}提醒你{item.get('content', '提醒')}" for item in reminders[:5])
            suffix = f"，另外还有{len(reminders) - 5}个" if len(reminders) > 5 else ""
            message = f"你有{details}{suffix}"
        else:
            message = "目前没有待执行的提醒"
        return {"skill_id": skill_id, "action": action, "reminders": reminders, "count": len(reminders), "message": message}
    if skill_id == "reminder.cancel":
        cancelled = store.cancel(params)
        if cancelled:
            contents = "、".join(str(item.get("content") or "提醒") for item in cancelled[:3])
            message = f"已取消{len(cancelled)}个提醒：{contents}"
        else:
            message = "没有找到匹配的待执行提醒"
        return {"skill_id": skill_id, "action": action, "cancelled": len(cancelled), "cancelled_reminders": cancelled, "message": message}
    raise ReminderError(f"未知提醒 skill: {skill_id}")
