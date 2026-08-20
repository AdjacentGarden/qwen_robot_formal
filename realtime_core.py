from __future__ import annotations

import base64
import math
import re
from dataclasses import dataclass, field
from typing import Any


MODEL = "qwen-audio-3.0-realtime-flash"
INPUT_RATE = 16_000
OUTPUT_RATE = 24_000
SAMPLE_WIDTH = 2


def build_websocket_url(workspace_id: str = "", region: str = "cn-beijing") -> str:
    workspace = workspace_id.strip()
    if not workspace and region == "cn-beijing":
        return (
            "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
            f"?model={MODEL}"
        )
    if not workspace or not re.fullmatch(r"[A-Za-z0-9-]{3,128}", workspace):
        raise ValueError("invalid_workspace_id")
    if region not in {"cn-beijing", "ap-southeast-1"}:
        raise ValueError("unsupported_region")
    return (
        f"wss://{workspace}.{region}.maas.aliyuncs.com"
        f"/api-ws/v1/realtime?model={MODEL}"
    )


def build_session_update(
    *,
    voice: str,
    instructions: str,
    turn_detection: str,
    silence_duration_ms: int,
    threshold: float,
    max_history_turns: int,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if turn_detection == "smart_turn":
        detection: dict[str, Any] = {"type": "smart_turn"}
    elif turn_detection == "server_vad":
        detection = {
            "type": "server_vad",
            "threshold": min(1.0, max(0.0, float(threshold))),
            "silence_duration_ms": min(3000, max(200, int(silence_duration_ms))),
        }
    else:
        raise ValueError("unsupported_turn_detection")
    session: dict[str, Any] = {
        "modalities": ["text", "audio"],
        "voice": voice.strip() or "longanqian",
        "instructions": instructions.strip(),
        "input_audio_format": "pcm",
        "output_audio_format": "pcm",
        "turn_detection": detection,
        "max_history_turns": min(50, max(1, int(max_history_turns))),
    }
    if tools:
        session["tools"] = tools
    return {
        "type": "session.update",
        "session": session,
    }


def pcm_rms(pcm: bytes) -> float:
    if not pcm or len(pcm) % 2:
        return 0.0
    total = 0
    count = len(pcm) // 2
    for offset in range(0, len(pcm), 2):
        value = int.from_bytes(pcm[offset : offset + 2], "little", signed=True)
        total += value * value
    return math.sqrt(total / count)


@dataclass(frozen=True)
class Action:
    kind: str
    value: Any = None


@dataclass
class ConversationState:
    response_active: bool = False
    audio_suppressed: bool = False
    current_response_id: str | None = None
    current_item_id: str | None = None
    input_transcripts: list[str] = field(default_factory=list)
    output_transcripts: list[str] = field(default_factory=list)
    event_counts: dict[str, int] = field(default_factory=dict)

    def process(self, event: dict[str, Any]) -> list[Action]:
        event_type = str(event.get("type") or "unknown")
        self.event_counts[event_type] = self.event_counts.get(event_type, 0) + 1
        actions: list[Action] = []
        if event_type == "error":
            error = event.get("error") or event
            actions.append(
                Action(
                    "error",
                    {
                        "code": str(error.get("code") or "service_error"),
                        "message": str(error.get("message") or error),
                    },
                )
            )
        elif event_type == "response.created":
            self.response_active = True
            self.audio_suppressed = False
            self.current_response_id = str((event.get("response") or {}).get("id") or "") or None
        elif event_type == "response.output_item.added":
            self.current_item_id = str((event.get("item") or {}).get("id") or "") or None
        elif event_type == "response.done":
            self.response_active = False
            self.current_response_id = None
            self.current_item_id = None
        elif event_type == "input_audio_buffer.speech_started":
            actions.append(Action("interrupt_playback"))
            if self.response_active:
                self.audio_suppressed = True
                self.response_active = False
                self.current_response_id = None
                self.current_item_id = None
                actions.append(Action("cancel_response"))
        elif event_type == "response.audio.delta" and not self.audio_suppressed:
            try:
                pcm = base64.b64decode(str(event.get("delta") or ""), validate=True)
            except Exception:
                actions.append(Action("error", {"code": "invalid_audio_delta", "message": "音频数据格式错误"}))
            else:
                if pcm:
                    actions.append(Action("play_audio", pcm))
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = str(event.get("transcript") or "").strip()
            if transcript:
                self.input_transcripts.append(transcript)
                actions.append(Action("input_transcript", transcript))
        elif event_type == "response.audio_transcript.done":
            transcript = str(event.get("transcript") or "").strip()
            if transcript:
                self.output_transcripts.append(transcript)
                actions.append(Action("output_transcript", transcript))
        elif event_type == "response.function_call_arguments.done":
            actions.append(
                Action(
                    "function_call",
                    {
                        "call_id": str(event.get("call_id") or ""),
                        "name": str(event.get("name") or ""),
                        "arguments": str(event.get("arguments") or "{}"),
                    },
                )
            )
        return actions
