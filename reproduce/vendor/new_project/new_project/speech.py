from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class SpeechEvent:
    skill_name: str
    text: str
    source: str
    kind: str = "progress"
    raw_line: str = ""
    timestamp: float = 0.0
    payload: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {
            "skill_name": self.skill_name,
            "text": self.text,
            "source": self.source,
            "kind": self.kind,
            "raw_line": self.raw_line,
            "timestamp": self.timestamp or time.time(),
        }
        if self.payload is not None:
            data["payload"] = self.payload
        return data


class SkillSpeechRouter:
    """Convert single_function stdout into robot speech events.

    single_function remains hardware-focused and emits stdout.  The project
    runtime owns the speaker, so stdout is the speech-event transport.
    """

    _COUNT_LINE = re.compile(r"^第[零一二三四五六七八九十百千万两0-9]+个[。！？!?.]?$")
    _NOISE_PREFIXES = (
        "W ",
        "I ",
        "INFO:",
        "WARNING:",
        "Error in cpuinfo:",
        "RKNN",
        "I RKNN:",
        "W RKNN:",
        "[视频",
        "[推理",
        "[警告",
        "[SingleSubjectGate]",
        "[计数更新]",
        "[汇总统计]",
        "[pet_tracking]",
        "[person_tracking]",
    )
    _FITNESS_TECHNICAL_PHRASES = (
        "后台启动",
        "PID=",
        "最多 ",
        "无动作会自动结束",
        "无动作会提前结束",
        "录制视频将保存到",
        "带关键点标注的视频已保存到",
        "资源已安全释放",
        "计数程序结束",
    )

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.max_len = int(self.config.get("speech", {}).get("max_event_chars", 80))
        self.face_confident_score_threshold = float(
            self.config.get("speech", {}).get("face_confident_score_threshold", 0.70)
        )

    def line_to_event(self, skill_name: str, raw_line: str) -> SpeechEvent | None:
        line = self._clean(raw_line)
        if not line:
            return None
        if self._is_noise(line):
            return None

        if line.startswith("{") and line.endswith("}"):
            return self._json_line_to_event(skill_name, line)

        if skill_name in {"squat", "push_up", "pull_up"}:
            return self._fitness_line_to_event(skill_name, line)
        if skill_name in {"pet_tracking", "person_tracking"}:
            return self._tracking_line_to_event(skill_name, line)
        if skill_name.startswith("reminder_"):
            return self._reminder_line_to_event(skill_name, line)

        return self._generic_line_to_event(skill_name, line)

    def _clean(self, line: str) -> str:
        line = (line or "").strip()
        line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
        return line

    def _is_noise(self, line: str) -> bool:
        if len(line) <= 1:
            return True
        if any(line.startswith(prefix) for prefix in self._NOISE_PREFIXES):
            return True
        if re.fullmatch(r"[零一二三四五六七八九十百千万两0-9]+", line):
            return True
        if "Traceback " in line or "Exception" in line:
            return True
        return False

    def _event(self, skill_name: str, text: str, raw_line: str, kind: str = "progress", payload: dict[str, Any] | None = None) -> SpeechEvent:
        text = text[: self.max_len] if isinstance(text, str) else ""
        return SpeechEvent(
            skill_name=skill_name,
            text=text,
            source="single_function_stdout",
            kind=kind,
            raw_line=raw_line,
            timestamp=time.time(),
            payload=payload,
        )

    def _fitness_line_to_event(self, skill_name: str, line: str) -> SpeechEvent | None:
        if any(phrase in line for phrase in self._FITNESS_TECHNICAL_PHRASES):
            return None
        if self._COUNT_LINE.match(line):
            return self._event(skill_name, line.rstrip("。"), line, "count")
        if "计数已开始" in line:
            return None
        if "计数已经在后台运行中" in line:
            return self._event(skill_name, line, line, "status")
        if "计数结束" in line or "自动结束" in line:
            return self._event(skill_name, line, line, "complete")
        if "您目前做了" in line or "您一共做了" in line:
            return self._event(skill_name, line, line, "status")
        return None

    def _tracking_line_to_event(self, skill_name: str, line: str) -> SpeechEvent | None:
        phrases = (
            "这里有一只",
            "抱歉，我没有发现",
            "对不起，我没找到",
            "开始追踪",
            "开始跟随",
            "目标丢失",
            "追踪结束",
            "跟随结束",
        )
        if any(phrase in line for phrase in phrases):
            kind = "complete" if any(word in line for word in ("结束", "没有发现", "没找到")) else "progress"
            if "目标丢失" in line:
                line = "我暂时看不到目标了，已经停止跟随。"
            elif skill_name == "pet_tracking" and any(word in line for word in ("追踪结束", "跟随结束")):
                line = "宠物跟随结束了，录像已经保存好了。"
            elif skill_name == "person_tracking" and any(word in line for word in ("追踪结束", "跟随结束")):
                line = "人员跟随已经结束了。"
            return self._event(skill_name, line, line, kind)
        return None

    def _reminder_line_to_event(self, skill_name: str, line: str) -> SpeechEvent | None:
        if any(phrase in line for phrase in ("提醒已", "当前有", "没有提醒")):
            return self._event(skill_name, line, line, "complete")
        return None

    def _generic_line_to_event(self, skill_name: str, line: str) -> SpeechEvent | None:
        if skill_name in {"projector_control", "light_control", "fan_control", "feeder_control", "head_control"}:
            if any(phrase in line for phrase in ("操作完成", "任务完成", "已完成", "完成")):
                return None
        if any(phrase in line for phrase in ("已完成", "完成", "已开始", "已启动", "已停止", "已取消")):
            return self._event(skill_name, line, line, "progress")
        return None

    def _face_recognition_json_to_event(self, line: str, payload: dict[str, Any]) -> SpeechEvent | None:
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        status = str(result.get("status") or payload.get("status") or "").strip().lower()
        name = result.get("name") or payload.get("name")
        score = result.get("score")
        message = ""
        if status == "matched" and name:
            if isinstance(score, (int, float)) and float(score) < self.face_confident_score_threshold:
                message = f"看起来像{name}，不过我不太确定。"
            else:
                message = f"看起来是{name}。"
        elif status in {"no_face", "not_detected"}:
            message = "我没有看清人脸，可以面向摄像头再试一次。"
        elif status == "empty_db":
            message = "现在还没有录入过人脸。"
        elif status in {"unknown", "uncertain"}:
            message = "我看到了人脸，但没有认出是谁。"
        if message:
            return self._event("face_recognition", message, line, "complete", payload=payload)
        return None

    def _face_registration_json_to_event(self, line: str, payload: dict[str, Any]) -> SpeechEvent | None:
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        status = str(result.get("status") or payload.get("status") or "").strip().lower()
        name = result.get("name") or payload.get("name")
        message = ""
        if status == "success":
            message = f"已经记住{name}了。" if name else "人脸已经录入好了。"
        elif status in {"no_face", "not_detected"}:
            message = "我没有看清人脸，请面向摄像头再试一次。"
        if message:
            return self._event("face_registration", message, line, "complete", payload=payload)
        return None

    def _json_line_to_event(self, skill_name: str, line: str) -> SpeechEvent | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        event_name = payload.get("event")
        if event_name in {"speech", "skill_ready", "skill_event", "skill_progress"}:
            kind = str(payload.get("kind") or ("ready" if event_name == "skill_ready" else "progress"))
            message = payload.get("text") or payload.get("message") or ""
            if isinstance(message, str):
                return self._event(skill_name, message, line, kind, payload=payload)
        if skill_name == "face_recognition":
            return self._face_recognition_json_to_event(line, payload)
        if skill_name == "face_registration":
            return self._face_registration_json_to_event(line, payload)
        if skill_name in {"projector_control", "light_control", "feeder_control"}:
            message = payload.get("message")
            if payload.get("ok") is True and isinstance(message, str) and message:
                return self._event(skill_name, message, line, "complete", payload=payload)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        message = result.get("message") or payload.get("message")
        if isinstance(message, str) and message and skill_name.startswith("reminder_"):
            return self._event(skill_name, message, line, "complete")
        return None
