from __future__ import annotations

import hashlib
import contextlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


MEMORY_TOOL_NAMES = {"memory_save", "memory_query", "memory_delete"}


def _clean_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "").replace("\x00", " ")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", "", _clean_text(value).lower())


class MemoryStore:
    """Small local memory store for one robot user profile.

    Conversation transcripts and explicit long-term facts are intentionally
    separated. Runtime files are private and writes are atomic where state can
    be replaced or deleted.
    """

    def __init__(
        self,
        root: Path,
        *,
        enabled: bool = True,
        max_history_items: int = 1000,
        max_facts: int = 200,
        timezone_name: str = "Asia/Shanghai",
        profile_path: Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.enabled = bool(enabled)
        self.max_history_items = max(20, int(max_history_items))
        self.max_facts = max(1, int(max_facts))
        self.history_path = self.root / "conversation_history.jsonl"
        self.facts_path = self.root / "long_term_memory.json"
        self.commands_path = self.root / "command_history.jsonl"
        self.timezone_name = str(timezone_name or "Asia/Shanghai")
        self.profile_path = Path(profile_path) if profile_path is not None else None
        try:
            self.timezone = ZoneInfo(self.timezone_name)
        except Exception:
            self.timezone = ZoneInfo("Asia/Shanghai")
        self._lock = threading.RLock()

    @property
    def tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "memory_save",
                    "description": (
                        "仅当用户明确说“记住、以后记得、保存这个偏好”时，把用户要求长期记住的事实或偏好保存到本机。"
                        "不要自行保存普通闲聊、密码、密钥、身份证号、支付信息或其他敏感信息。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "用户明确要求长期记住的简洁事实。"},
                            "category": {
                                "type": "string",
                                "enum": [
                                    "identity", "preference", "habit", "relationship",
                                    "pet", "location", "other",
                                ],
                            },
                        },
                        "required": ["content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memory_query",
                    "description": (
                        "查询机器人本机保存的长期记忆、对话历史或已调用的指令账本。"
                        "用户问上一条/倒数第几条/最早一条执行指令，必须 scope=command_history 并选择"
                        " latest/recent/offset/first/ordinal；问今天、昨天、前天或某时间段的指令时使用 time_range。"
                        "不得只凭当前上下文猜测，也不得凭空声称没有记忆。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "要查找的关键词；查询全部时可省略。"},
                            "scope": {
                                "type": "string",
                                "enum": ["all", "long_term", "conversation_history", "command_history"],
                            },
                            "query_type": {
                                "type": "string",
                                "enum": [
                                    "search", "latest", "recent", "offset", "first", "ordinal", "time_range",
                                ],
                                "description": "结构化检索方式；默认 search。",
                            },
                            "offset": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 999,
                                "description": "仅 offset 使用：0 是上一条，1 是往前数两条/倒数第二条。",
                            },
                            "position": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 1000,
                                "description": "仅 ordinal 使用；从最早开始按 1 计数，例如第二条为 2。",
                            },
                            "date_period": {
                                "type": "string",
                                "enum": ["today", "yesterday", "day_before_yesterday"],
                            },
                            "start_time": {
                                "type": "string",
                                "description": "时间范围起点，ISO 8601 或 YYYY-MM-DD。",
                            },
                            "end_time": {
                                "type": "string",
                                "description": "时间范围终点，ISO 8601 或 YYYY-MM-DD；日期按该日结束处理。",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 50,
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memory_delete",
                    "description": (
                        "仅当用户明确要求“忘记、删除记忆、清空历史”时删除本机记忆。"
                        "只删除用户指定范围，不得因为话题切换或普通否定句自动删除。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "memory_id": {"type": "string", "description": "查询结果中的长期记忆编号。"},
                            "query": {"type": "string", "description": "要忘记的内容关键词。"},
                            "scope": {
                                "type": "string",
                                "enum": ["long_term", "conversation_history", "command_history", "all"],
                            },
                            "delete_all": {
                                "type": "boolean",
                                "description": "只有用户明确要求清空对应范围的全部记忆时才为 true。",
                            },
                        },
                        "required": [],
                    },
                },
            },
        ]

    def is_tool(self, name: str) -> bool:
        return self.enabled and name in MEMORY_TOOL_NAMES

    def append_conversation(self, role: str, text: str, session_id: str = "") -> None:
        if not self.enabled or role not in {"user", "assistant"}:
            return
        cleaned = _clean_text(text, 3000)
        if not cleaned:
            return
        record = {
            "ts": round(time.time(), 3),
            "role": role,
            "text": cleaned,
            "session_id": _clean_text(session_id, 120),
        }
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                self.root.chmod(0o700)
            fd = os.open(self.history_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            with contextlib.suppress(OSError):
                self.history_path.chmod(0o600)
            if self.history_path.stat().st_size > 4 * 1024 * 1024:
                self._compact_history()

    def record_command(
        self,
        *,
        user_text: str,
        session_id: str,
        turn_id: str | int,
        skill: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        received_at: float | None = None,
    ) -> None:
        """Upsert one user turn in the durable command ledger.

        One utterance may result in several model tool calls.  They are kept
        under one command record so "the previous command" means the previous
        user instruction rather than an implementation detail.
        """

        if not self.enabled or skill in MEMORY_TOOL_NAMES:
            return
        cleaned = _clean_text(user_text, 3000)
        if not cleaned:
            return
        session = _clean_text(session_id, 120)
        turn = _clean_text(turn_id, 80)
        command_id = hashlib.sha256(f"{session}:{turn}:{cleaned}".encode("utf-8")).hexdigest()[:20]
        now = round(time.time(), 3)
        call = {
            "skill": _clean_text(skill, 160),
            "arguments": self._redact_sensitive(arguments),
            "ok": bool(result.get("ok")),
            "validation_ok": bool(result.get("validation_ok")),
            "executed": bool(result.get("executed")),
            "mode": _clean_text(result.get("mode"), 80),
            "error": _clean_text(result.get("error"), 240),
            "summary": _clean_text(result.get("spoken_summary"), 1000),
            "completed_at": now,
        }
        with self._lock:
            records = self._read_commands()
            existing = next((item for item in records if item.get("id") == command_id), None)
            if existing is None:
                existing = {
                    "id": command_id,
                    "ts": round(float(received_at or time.time()), 3),
                    "session_id": session,
                    "turn_id": turn,
                    "text": cleaned,
                    "calls": [],
                }
                records.append(existing)
            calls = existing.setdefault("calls", [])
            signature = json.dumps(
                {"skill": call["skill"], "arguments": call["arguments"]},
                ensure_ascii=False,
                sort_keys=True,
            )
            replaced = False
            for index, prior in enumerate(calls):
                prior_signature = json.dumps(
                    {"skill": prior.get("skill"), "arguments": prior.get("arguments")},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if prior_signature == signature:
                    calls[index] = call
                    replaced = True
                    break
            if not replaced:
                calls.append(call)
            existing["updated_at"] = now
            self._write_commands(records)

    def recent_history(self, max_turns: int = 12, max_chars: int = 6000) -> list[dict[str, str]]:
        records = self._read_history()
        selected: list[dict[str, str]] = []
        used = 0
        for item in reversed(records):
            text = _clean_text(item.get("text"), 3000)
            role = str(item.get("role") or "")
            if role not in {"user", "assistant"} or not text:
                continue
            if used + len(text) > max_chars and selected:
                break
            selected.append({"role": role, "text": text})
            used += len(text)
            if len(selected) >= max(1, int(max_turns)) * 2:
                break
        return list(reversed(selected))

    def facts_for_prompt(self, limit: int = 20, max_chars: int = 3000) -> str:
        memories = self._all_facts()[-max(1, int(limit)) :]
        compact = [
            {"id": item.get("id"), "category": item.get("category"), "content": item.get("content")}
            for item in memories
        ]
        return json.dumps(compact, ensure_ascii=False)[:max_chars]

    def decision_context_for_prompt(self, command_limit: int = 8, max_chars: int = 5000) -> str:
        """Safe cross-session context for decisions, never live device state."""

        facts = [
            {"id": item.get("id"), "category": item.get("category"), "content": item.get("content")}
            for item in self._all_facts()[-20:]
        ]
        commands = self._read_commands()[-max(1, int(command_limit)) :]
        compact_commands = [
            {
                "time": self._format_ts(item.get("ts")),
                "user_request": item.get("text"),
                "skills": [call.get("skill") for call in item.get("calls") or []],
            }
            for item in commands
        ]
        payload = {
            "saved_user_facts": facts,
            "recent_user_requests": compact_commands,
        }
        rendered = json.dumps(payload, ensure_ascii=False)
        while len(rendered) > max(200, int(max_chars)) and compact_commands:
            compact_commands.pop(0)
            rendered = json.dumps(payload, ensure_ascii=False)
        while len(rendered) > max(200, int(max_chars)) and facts:
            facts.pop(0)
            rendered = json.dumps(payload, ensure_ascii=False)
        return rendered

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return self._result(False, name, "memory_disabled", "本地记忆功能没有启用。")
        if name == "memory_save":
            return self._save(arguments)
        if name == "memory_query":
            return self._query(arguments)
        if name == "memory_delete":
            return self._delete(arguments)
        return self._result(False, name, "unknown_memory_tool", "未知的记忆操作，没有执行。")

    def _save(self, arguments: dict[str, Any]) -> dict[str, Any]:
        content = _clean_text(arguments.get("content"), 1000)
        category = _clean_text(arguments.get("category") or "other", 40)
        if not content:
            return self._result(False, "memory_save", "missing_content", "请告诉我要记住什么。")
        sensitive_markers = ("sk-", "api key", "apikey", "密码", "身份证", "银行卡", "accesskey secret")
        lowered = content.lower()
        if any(marker in lowered for marker in sensitive_markers):
            return self._result(False, "memory_save", "sensitive_memory_rejected", "为保护隐私，这类敏感信息不会保存。")
        with self._lock:
            data = self._load_facts()
            normalized = _normalized(content)
            for item in data["memories"]:
                if _normalized(item.get("content")) == normalized:
                    item["updated_at"] = round(time.time(), 3)
                    item["category"] = category
                    self._write_facts(data)
                    return self._result(
                        True,
                        "memory_save",
                        None,
                        f"我记得这件事：{content}",
                        memory=item,
                    )
            stamp = time.time()
            memory_id = "mem_" + hashlib.sha256(f"{stamp}:{content}".encode("utf-8")).hexdigest()[:12]
            item = {
                "id": memory_id,
                "content": content,
                "category": category,
                "created_at": round(stamp, 3),
                "updated_at": round(stamp, 3),
            }
            data["memories"].append(item)
            data["memories"] = data["memories"][-self.max_facts :]
            self._write_facts(data)
        return self._result(True, "memory_save", None, f"好的，我会记住：{content}", memory=item)

    def _query(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = _clean_text(arguments.get("query"), 300)
        scope = str(arguments.get("scope") or "all")
        query_type = str(arguments.get("query_type") or "search")
        limit = min(50, max(1, int(arguments.get("limit") or 20)))
        facts: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        commands: list[dict[str, Any]] = []
        if scope in {"all", "long_term"}:
            facts = self._match_items(self._all_facts(), query, limit=limit)
        if scope in {"all", "conversation_history"}:
            history = self._match_items(self._read_history(), query, limit=limit)
        if scope in {"all", "command_history"}:
            commands = self._query_commands(arguments, query_type=query_type, query=query, limit=limit)
        if not facts and not history and not commands:
            message = "我没有找到相关记忆。" if query else "目前还没有保存可用的记忆。"
        else:
            parts = []
            if facts:
                parts.append(f"{len(facts)}条长期记忆")
            if history:
                parts.append(f"{len(history)}条对话记录")
            if commands:
                parts.append(f"{len(commands)}条执行指令")
            message = "找到了" + "、".join(parts) + "。"
        return self._result(
            True,
            "memory_query",
            None,
            message,
            facts=facts,
            history=history,
            commands=commands,
            query=query,
            scope=scope,
            query_type=query_type,
            timezone=self.timezone_name,
        )

    def _delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        scope = str(arguments.get("scope") or "long_term")
        query = _clean_text(arguments.get("query"), 300)
        memory_id = _clean_text(arguments.get("memory_id"), 120)
        delete_all = bool(arguments.get("delete_all"))
        if not delete_all and not query and not memory_id:
            return self._result(False, "memory_delete", "missing_delete_target", "请明确告诉我要忘记哪一项内容。")
        deleted_facts = 0
        deleted_history = 0
        with self._lock:
            if scope in {"long_term", "all"}:
                data = self._load_facts()
                before = len(data["memories"])
                if delete_all:
                    data["memories"] = []
                else:
                    data["memories"] = [
                        item
                        for item in data["memories"]
                        if not self._matches_delete(item, query=query, memory_id=memory_id)
                    ]
                deleted_facts = before - len(data["memories"])
                self._write_facts(data)
            if scope in {"conversation_history", "all"}:
                records = self._read_history()
                before = len(records)
                if delete_all:
                    records = []
                else:
                    records = [item for item in records if not self._matches_delete(item, query=query, memory_id="")]
                deleted_history = before - len(records)
                self._write_history(records)
            deleted_commands = 0
            if scope in {"command_history", "all"}:
                records = self._read_commands()
                before = len(records)
                if delete_all:
                    records = []
                else:
                    records = [item for item in records if not self._matches_delete(item, query=query, memory_id=memory_id)]
                deleted_commands = before - len(records)
                self._write_commands(records)
        total = deleted_facts + deleted_history + deleted_commands
        message = f"已经删除{total}条记忆。" if total else "没有找到需要删除的记忆。"
        return self._result(
            True,
            "memory_delete",
            None,
            message,
            deleted_facts=deleted_facts,
            deleted_history=deleted_history,
            deleted_commands=deleted_commands,
        )

    def _query_commands(
        self,
        arguments: dict[str, Any],
        *,
        query_type: str,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        records = self._read_commands()
        if query_type == "latest":
            selected = records[-1:]
        elif query_type == "recent":
            selected = records[-limit:]
        elif query_type == "offset":
            offset = min(999, max(0, int(arguments.get("offset") or 0)))
            selected = records[-offset - 1 : -offset] if offset else records[-1:]
        elif query_type == "first":
            selected = records[:1]
        elif query_type == "ordinal":
            position = min(1000, max(1, int(arguments.get("position") or 1)))
            selected = records[position - 1 : position]
        elif query_type == "time_range":
            start, end = self._resolve_time_range(arguments)
            selected = [item for item in records if start <= float(item.get("ts") or 0) < end]
            selected = self._match_items(selected, query, limit) if query else selected[-limit:]
        else:
            selected = self._match_items(records, query, limit=limit)
        return [self._public_command(item) for item in selected]

    def _resolve_time_range(self, arguments: dict[str, Any]) -> tuple[float, float]:
        now = datetime.now(self.timezone)
        period = str(arguments.get("date_period") or "")
        if period:
            days = {"today": 0, "yesterday": 1, "day_before_yesterday": 2}.get(period, 0)
            start_dt = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
            return start_dt.timestamp(), (start_dt + timedelta(days=1)).timestamp()
        start_dt = self._parse_datetime(arguments.get("start_time"), end_of_day=False)
        end_dt = self._parse_datetime(arguments.get("end_time"), end_of_day=True)
        if start_dt is None:
            start_dt = datetime.fromtimestamp(0, self.timezone)
        if end_dt is None:
            end_dt = now + timedelta(seconds=1)
        if end_dt <= start_dt:
            return 1.0, 0.0
        return start_dt.timestamp(), end_dt.timestamp()

    def _parse_datetime(self, value: Any, *, end_of_day: bool) -> datetime | None:
        text = _clean_text(value, 80)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.timezone)
        else:
            parsed = parsed.astimezone(self.timezone)
        if len(text) == 10 and end_of_day:
            parsed += timedelta(days=1)
        return parsed

    def _read_commands(self) -> list[dict[str, Any]]:
        if not self.commands_path.is_file():
            return []
        records: list[dict[str, Any]] = []
        with self._lock:
            for line in self.commands_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
        records.sort(key=lambda item: float(item.get("ts") or 0))
        return records[-self.max_history_items :]

    def _write_commands(self, records: list[dict[str, Any]]) -> None:
        content = "".join(
            json.dumps(item, ensure_ascii=False) + "\n"
            for item in records[-self.max_history_items :]
        )
        self._atomic_write(self.commands_path, content)

    def _public_command(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id"),
            "time": self._format_ts(item.get("ts")),
            "timestamp": item.get("ts"),
            "text": item.get("text"),
            "calls": item.get("calls") or [],
        }

    def _format_ts(self, value: Any) -> str:
        try:
            return datetime.fromtimestamp(float(value), self.timezone).isoformat(timespec="seconds")
        except Exception:
            return ""

    @classmethod
    def _redact_sensitive(cls, value: Any, key: str = "") -> Any:
        lowered = str(key).lower()
        if any(marker in lowered for marker in ("password", "passwd", "secret", "token", "api_key", "apikey", "access_key")):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(k): cls._redact_sensitive(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._redact_sensitive(item, key) for item in value[:100]]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return _clean_text(value, 500)

    def _read_history(self) -> list[dict[str, Any]]:
        if not self.history_path.is_file():
            return []
        records: list[dict[str, Any]] = []
        with self._lock:
            for line in self.history_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
        return records[-self.max_history_items :]

    def _compact_history(self) -> None:
        self._write_history(self._read_history()[-self.max_history_items :])

    def _write_history(self, records: list[dict[str, Any]]) -> None:
        content = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records[-self.max_history_items :])
        self._atomic_write(self.history_path, content)

    def _load_facts(self) -> dict[str, Any]:
        if not self.facts_path.is_file():
            return {"version": 1, "memories": []}
        try:
            value = json.loads(self.facts_path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "memories": []}
        memories = value.get("memories") if isinstance(value, dict) else []
        return {"version": 1, "memories": memories if isinstance(memories, list) else []}

    def _load_profile_facts(self) -> list[dict[str, Any]]:
        """Read stable deployment facts without mixing them into user memory.

        These facts describe the resident environment (for example the pet
        name and deployment location).  They are intentionally kept in a
        reviewable config file, while facts explicitly requested by the user
        continue to live in long_term_memory.json.
        """

        if self.profile_path is None or not self.profile_path.is_file():
            return []
        try:
            value = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        raw_facts = value.get("facts") if isinstance(value, dict) else []
        if not isinstance(raw_facts, list):
            return []
        facts: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_facts[:100], 1):
            if not isinstance(raw, dict):
                continue
            content = _clean_text(raw.get("content"), 1000)
            if not content:
                continue
            key = re.sub(r"[^a-z0-9_-]+", "_", _clean_text(raw.get("key"), 80).lower()).strip("_")
            if not key:
                key = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
            facts.append(
                {
                    "id": f"profile_{key}",
                    "content": content,
                    "category": _clean_text(raw.get("category") or "other", 40),
                    "source": "profile_config",
                    "profile_order": index,
                }
            )
        return facts

    def _all_facts(self) -> list[dict[str, Any]]:
        profile = self._load_profile_facts()
        dynamic = self._load_facts()["memories"]
        seen = {_normalized(item.get("content")) for item in profile}
        return [
            item for item in dynamic
            if _normalized(item.get("content")) not in seen
        ] + profile

    def _write_facts(self, data: dict[str, Any]) -> None:
        payload = {"version": 1, "memories": list(data.get("memories") or [])[-self.max_facts :]}
        self._atomic_write(self.facts_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def _atomic_write(self, path: Path, content: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.root.chmod(0o700)
        temporary = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        with contextlib.suppress(OSError):
            path.chmod(0o600)

    @staticmethod
    def _matches_delete(item: dict[str, Any], *, query: str, memory_id: str) -> bool:
        if memory_id and str(item.get("id") or "") == memory_id:
            return True
        needle = _normalized(query)
        return bool(needle and needle in _normalized(item.get("content") or item.get("text")))

    @staticmethod
    def _match_items(items: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
        needle = _normalized(query)
        if not needle:
            return list(items[-limit:])
        exact: list[dict[str, Any]] = []
        fuzzy: list[dict[str, Any]] = []
        category_matches: list[dict[str, Any]] = []
        query_chars = set(needle)
        # Profile facts are written as natural sentences, while users often
        # ask for a semantic field whose exact words do not occur in that
        # sentence (for example “常驻在哪个城市和区”).  Use narrow category
        # hints after exact/fuzzy matching instead of returning every profile
        # fact or relying on a brittle full-string overlap threshold.
        category_hint = ""
        if re.search(r"常驻|所在地|位置|地址|城市|区县|街道|省份|国家|gps|经纬度", needle):
            category_hint = "location"
        elif re.search(r"宠物|狗|猫|豆豆", needle):
            category_hint = "pet"
        for item in reversed(items):
            searchable = item.get("content") or item.get("text") or ""
            # Command history must also be searchable by the authoritative
            # result and structured call.  Natural requests often omit the
            # scene label (“我要开会了”), while the call/summary records that
            # it was a meeting projection.  This remains read-only and keeps
            # long-term facts/conversation records unchanged.
            if item.get("calls"):
                searchable = f"{searchable} {json.dumps(item.get('calls'), ensure_ascii=False)}"
            haystack = _normalized(searchable)
            if needle in haystack:
                exact.append(item)
            elif query_chars and len(query_chars & set(haystack)) / len(query_chars) >= 0.6:
                fuzzy.append(item)
            elif category_hint and str(item.get("category") or "").lower() == category_hint:
                category_matches.append(item)
        return (exact + fuzzy + category_matches)[:limit]

    @staticmethod
    def _result(ok: bool, skill: str, error: str | None, message: str, **extra: Any) -> dict[str, Any]:
        return {
            "ok": ok,
            "validation_ok": ok,
            "executed": ok,
            "device_state_changed": False,
            "skill": skill,
            "mode": "local_memory",
            "error": error,
            "message": message,
            "spoken_summary": message,
            "resources": ["local_memory"],
            **extra,
        }
