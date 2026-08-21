from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class UserMemoryStore:
    """Persistent user facts, separate from disposable task runtime state."""

    def __init__(self, config: dict[str, Any]):
        memory = config.get("memory", {})
        self.enabled = bool(memory.get("enabled", True))
        self.path = Path(memory.get("facts_file", "/home/test/new_project/data/user_facts.json"))
        self.max_facts = max(1, int(memory.get("max_facts", 200)))
        self._lock = threading.RLock()

    def compact_context(self, limit: int = 30) -> list[dict[str, Any]]:
        facts = self.list_facts()
        facts.sort(key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0), reverse=True)
        return [
            {
                "fact_id": item.get("fact_id"),
                "category": item.get("category"),
                "subject": item.get("subject"),
                "predicate": item.get("predicate"),
                "value": item.get("value"),
            }
            for item in facts[: max(0, int(limit))]
        ]

    def list_facts(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        with self._lock:
            data = self._read()
            return [dict(item) for item in data.get("facts", []) if isinstance(item, dict)]

    def interpret(self, text: str, model_decision: dict[str, Any] | None = None) -> dict[str, Any]:
        text = str(text or "").strip()
        model = self._normalize_model_operation(model_decision or {}, text)
        if model.get("operation") != "none":
            return model
        return self._local_operation(text)

    def apply(self, operation: dict[str, Any]) -> dict[str, Any]:
        op = str(operation.get("operation") or "none")
        if not self.enabled or op == "none":
            return {"ok": True, "handled": False, "operation": "none"}
        if op == "remember":
            fact = operation.get("fact") if isinstance(operation.get("fact"), dict) else {}
            return self._remember(fact)
        if op == "forget":
            return self._forget(str(operation.get("query") or ""), operation.get("fact_id"))
        if op == "query":
            return self._query(str(operation.get("query") or ""))
        return {"ok": False, "handled": False, "operation": op, "error": "unknown_memory_operation"}

    def _remember(self, fact: dict[str, Any]) -> dict[str, Any]:
        category = str(fact.get("category") or "personal").strip()
        subject = str(fact.get("subject") or "user").strip()
        predicate = str(fact.get("predicate") or "note").strip()
        value = str(fact.get("value") or "").strip()
        if not value:
            return {"ok": False, "handled": True, "operation": "remember", "error": "memory_value_missing", "reply": "我还不确定要记住什么。"}
        now = time.time()
        with self._lock:
            data = self._read()
            facts = [item for item in data.get("facts", []) if isinstance(item, dict)]
            existing = next(
                (
                    item
                    for item in facts
                    if str(item.get("category")) == category
                    and str(item.get("subject")) == subject
                    and str(item.get("predicate")) == predicate
                ),
                None,
            )
            previous = None
            if existing is None:
                existing = {
                    "fact_id": f"fact_{uuid.uuid4().hex[:12]}",
                    "category": category,
                    "subject": subject,
                    "predicate": predicate,
                    "created_at": now,
                }
                facts.append(existing)
            else:
                previous = existing.get("value")
            existing.update({"value": value, "updated_at": now, "source_text": fact.get("source_text")})
            data["facts"] = facts[-self.max_facts :]
            self._write(data)
        reply = self._remember_reply(category, subject, predicate, value, previous)
        return {"ok": True, "handled": True, "operation": "remember", "fact": dict(existing), "previous_value": previous, "reply": reply}

    def _forget(self, query: str, fact_id: Any = None) -> dict[str, Any]:
        query_norm = self._compact(query)
        with self._lock:
            data = self._read()
            facts = [item for item in data.get("facts", []) if isinstance(item, dict)]
            removed: list[dict[str, Any]] = []
            kept: list[dict[str, Any]] = []
            for item in facts:
                searchable = self._compact(" ".join(str(item.get(key) or "") for key in ("category", "subject", "predicate", "value")))
                matches_id = bool(fact_id and str(item.get("fact_id")) == str(fact_id))
                matches_query = bool(query_norm and (query_norm in searchable or any(token and token in searchable for token in self._query_tokens(query_norm))))
                if matches_id or matches_query:
                    removed.append(item)
                else:
                    kept.append(item)
            if removed:
                data["facts"] = kept
                self._write(data)
        if not removed:
            return {"ok": True, "handled": True, "operation": "forget", "removed": [], "reply": "我没有找到这条记忆。"}
        names = list(dict.fromkeys(str(item.get("value") or item.get("subject") or "") for item in removed))
        label = "、".join(item for item in names if item)
        return {"ok": True, "handled": True, "operation": "forget", "removed": removed, "reply": f"好，关于{label}的记忆已经删除了。" if label else "好，这条记忆已经删除了。"}

    def _query(self, query: str) -> dict[str, Any]:
        query_norm = self._compact(query)
        matches = []
        for item in self.list_facts():
            searchable = self._compact(" ".join(str(item.get(key) or "") for key in ("category", "subject", "predicate", "value")))
            if not query_norm or query_norm in searchable or any(token and token in searchable for token in self._query_tokens(query_norm)):
                matches.append(item)
        if not matches:
            return {"ok": True, "handled": True, "operation": "query", "facts": [], "reply": "这件事我还没有记住。"}
        item = matches[-1]
        if item.get("category") == "pet" and item.get("predicate") == "name":
            animal = {"dog": "狗", "cat": "猫", "pet": "宠物"}.get(str(item.get("subject")), "宠物")
            reply = f"你的{animal}叫{item.get('value')}。"
        elif item.get("category") == "preference":
            reply = f"我记得你喜欢{item.get('value')}。"
        else:
            reply = f"我记得的是：{item.get('value')}。"
        return {"ok": True, "handled": True, "operation": "query", "facts": matches, "reply": reply}

    def _normalize_model_operation(self, decision: dict[str, Any], source_text: str) -> dict[str, Any]:
        op = str(decision.get("memory_operation") or "none").strip().lower()
        if op not in {"remember", "forget", "query"}:
            return {"operation": "none"}
        fact = decision.get("memory_fact") if isinstance(decision.get("memory_fact"), dict) else {}
        if fact:
            fact = dict(fact)
            fact["source_text"] = source_text
        return {
            "operation": op,
            "fact": fact,
            "fact_id": decision.get("memory_fact_id"),
            "query": str(decision.get("memory_query") or source_text),
            "source": "model",
        }

    def _local_operation(self, text: str) -> dict[str, Any]:
        compact = self._compact(text)
        pet_match = re.search(r"(?:请(?:你)?)?(?:记住|记一下)(?:我的)?(小狗|狗|小猫|猫|宠物)(?:名字)?叫([\u4e00-\u9fffA-Za-z0-9_]{1,16})", compact)
        if pet_match:
            animal, name = pet_match.groups()
            subject = "dog" if "狗" in animal else "cat" if "猫" in animal else "pet"
            return {
                "operation": "remember",
                "source": "local",
                "fact": {"category": "pet", "subject": subject, "predicate": "name", "value": name, "source_text": text},
            }
        preference = re.search(r"(?:请(?:你)?)?(?:记住|记一下)(?:我)?喜欢(.+)", compact)
        if preference:
            return {
                "operation": "remember",
                "source": "local",
                "fact": {"category": "preference", "subject": "user", "predicate": "likes", "value": preference.group(1), "source_text": text},
            }
        if any(marker in compact for marker in ("删除", "删掉", "清除", "忘掉", "忘记")):
            query = re.sub(r"^(?:请(?:你)?)?(?:删除|删掉|清除|忘掉|忘记)", "", compact)
            query = re.sub(r"(?:的)?(?:相关)?信息$", "", query)
            query = re.sub(r"^(?:我的)?(?:宠物|小狗|狗|小猫|猫)", "", query) or compact
            return {"operation": "forget", "query": query, "source": "local"}
        pet_query = re.search(r"我的(小狗|狗|小猫|猫|宠物)(?:叫什么|名字是什么|叫啥)", compact)
        if pet_query:
            animal = pet_query.group(1)
            return {"operation": "query", "query": "dog" if "狗" in animal else "cat" if "猫" in animal else "pet", "source": "local"}
        return {"operation": "none"}

    @staticmethod
    def _remember_reply(category: str, subject: str, predicate: str, value: str, previous: Any) -> str:
        if category == "pet" and predicate == "name":
            animal = {"dog": "狗", "cat": "猫", "pet": "宠物"}.get(subject, "宠物")
            if previous and str(previous) != value:
                return f"记住了，你的{animal}现在叫{value}。"
            return f"记住了，你的{animal}叫{value}。"
        if category == "preference":
            return f"记住了，你喜欢{value}。"
        return "好，我记住了。"

    @staticmethod
    def _compact(text: str) -> str:
        return re.sub(r"[\s\u3000，。,.！!？?、；;：:]+", "", str(text or "")).strip()

    @staticmethod
    def _query_tokens(query: str) -> list[str]:
        noise = ("我的", "宠物", "小狗", "狗", "小猫", "猫", "相关", "信息", "名字", "叫")
        tokens = [query]
        reduced = query
        for item in noise:
            reduced = reduced.replace(item, "")
        if reduced and reduced != query:
            tokens.append(reduced)
        return tokens

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "facts": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"version": 1, "facts": []}
        except Exception:
            return {"version": 1, "facts": []}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(data)
        data["version"] = 1
        data["updated_at"] = time.time()
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
