from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import inspect
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from realtime_core import build_session_update, build_websocket_url


SPEAKABLE_KINDS = {
    "acknowledgement", "progress", "ready", "count", "attention", "result",
}
EVENT_PRIORITIES = {
    "count": 0, "ready": 1, "attention": 2,
    "acknowledgement": 3, "progress": 4, "result": 5,
}


class QwenSkillEventSpeaker:
    """Speak live skill events with the same Qwen voice as the conversation.

    A dedicated Realtime connection is deliberate: the primary connection is
    waiting for the long-running Function Call to return.  Using a second,
    text-only connection keeps repetition counts live without local ASR/TTS and
    without corrupting the main conversation's tool-call state.
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice: str,
        workspace: str,
        region: str,
        endpoint: str,
        connect_timeout: float,
        enqueue_pcm: Callable[[bytes], None],
        log: Callable[..., None],
        cache_dir: Path | None = None,
    ) -> None:
        self.api_key = api_key
        self.voice = voice
        self.workspace = workspace
        self.region = region
        self.endpoint = endpoint
        self.connect_timeout = connect_timeout
        self.enqueue_pcm = enqueue_pcm
        self.log = log
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        # Playback is a causal timeline.  Sequence, not event importance, is
        # therefore the first sort key: a later ready/result event must never
        # jump in front of an earlier acknowledgement or progress sentence.
        self.queue: asyncio.PriorityQueue[tuple[int, int, dict[str, Any]]] = asyncio.PriorityQueue(maxsize=64)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task[None] | None = None
        self.websocket: Any = None
        self._last_key = ""
        self._event_sequence = 0
        self._generation = 0

    def submit_from_thread(self, event: dict[str, Any]) -> None:
        kind = str(event.get("kind") or "")
        text = str(event.get("text") or "").strip()
        if kind not in SPEAKABLE_KINDS or not text or self.loop is None:
            return
        key = (
            f"{event.get('event_id') or event.get('turn_id') or ''}:"
            f"{event.get('skill_name')}:{kind}:{event.get('count', '')}:{text}"
        )
        if key == self._last_key:
            return
        self._last_key = key

        def put() -> None:
            if self.queue.full():
                self.log("skill_event_audio_dropped", reason="queue_full", kind=kind, text=text)
                return
            self._event_sequence += 1
            payload = dict(event)
            payload["_generation"] = self._generation
            self.queue.put_nowait(
                (
                    self._event_sequence,
                    EVENT_PRIORITIES.get(kind, 9),
                    payload,
                )
            )

        self.loop.call_soon_threadsafe(put)

    def cancel_pending(self) -> None:
        """Invalidate current/queued speech when the user starts talking."""

        if self.loop is None:
            return

        def cancel() -> None:
            self._generation += 1
            dropped = 0
            while True:
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    dropped += 1
                    self.queue.task_done()
            self.log("skill_event_audio_cancelled", generation=self._generation, dropped=dropped)

        self.loop.call_soon_threadsafe(cancel)

    def _event_is_current(self, event: dict[str, Any]) -> bool:
        return int(event.get("_generation", self._generation)) == self._generation

    async def start(self) -> None:
        if self.task is not None:
            return
        self.loop = asyncio.get_running_loop()
        try:
            await self._connect()
        except Exception as exc:
            self.log("skill_event_audio_preconnect_error", error=f"{type(exc).__name__}:{exc}")
            self.websocket = None
        self.task = asyncio.create_task(self._run(), name="qwen-skill-event-speaker")

    async def close(self) -> None:
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.websocket is not None:
            try:
                await self.websocket.close()
            except Exception:
                pass
            self.websocket = None

    async def wait_idle(self, timeout: float = 6.0) -> bool:
        """Wait until queued status speech has been synthesized and enqueued.

        The initial ``sleep(0)`` lets submissions scheduled from a worker
        thread enter the asyncio queue before ``join`` checks it.  A bounded
        wait keeps a temporary TTS outage from delaying the authoritative
        final tool result indefinitely.
        """

        if self.task is None:
            return True
        await asyncio.sleep(0)
        try:
            await asyncio.wait_for(self.queue.join(), timeout=max(0.1, float(timeout)))
            return True
        except asyncio.TimeoutError:
            self.log("skill_event_audio_idle_timeout", queued=self.queue.qsize())
            return False

    async def _receive_until(self, wanted: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.connect_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"skill_event_wait_{wanted}")
            event = json.loads(await asyncio.wait_for(self.websocket.recv(), remaining))
            if event.get("type") == "error":
                error = event.get("error") or event
                raise RuntimeError(str(error.get("message") or error))
            if event.get("type") == wanted:
                return event

    async def _connect(self) -> None:
        import websockets

        url = self.endpoint.strip() or build_websocket_url(self.workspace, self.region)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "x-dashscope-dataInspection": "disable",
        }
        keyword = "additional_headers" if "additional_headers" in inspect.signature(websockets.connect).parameters else "extra_headers"
        self.websocket = await websockets.connect(
            url,
            open_timeout=self.connect_timeout,
            close_timeout=3,
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
            **{keyword: headers},
        )
        await self._receive_until("session.created")
        await self.websocket.send(
            json.dumps(
                build_session_update(
                    voice=self.voice,
                    instructions=(
                        "你是机器人实时动作播报器。只逐字朗读用户提供的播报文字，"
                        "不回答、不解释、不加称呼、语气词、前后缀或标点外内容。"
                    ),
                    turn_detection="smart_turn",
                    silence_duration_ms=600,
                    threshold=0.5,
                    max_history_turns=2,
                ),
                ensure_ascii=False,
            )
        )
        await self._receive_until("session.updated")
        self.log("skill_event_audio_ready", voice=self.voice)

    async def _synthesize(self, event: dict[str, Any]) -> None:
        text = str(event.get("text") or "").strip()
        if not self._event_is_current(event):
            return
        started = time.monotonic()
        cache_path = self._cache_path(text)
        if cache_path is not None and cache_path.is_file():
            pcm = cache_path.read_bytes()
            if pcm and self._event_is_current(event):
                self.enqueue_pcm(pcm)
                latency_ms = round((time.monotonic() - started) * 1000.0, 1)
                self.log(
                    "skill_event_audio_first_chunk",
                    skill=event.get("skill_name"),
                    kind=event.get("kind"),
                    text=text,
                    latency_ms=latency_ms,
                    cached=True,
                )
                self.log(
                    "skill_event_audio_generated",
                    skill=event.get("skill_name"),
                    kind=event.get("kind"),
                    text=text,
                    transcript=text,
                    pcm_bytes=len(pcm),
                    latency_ms=latency_ms,
                    cached=True,
                )
                return
        await self.websocket.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"只朗读：{text}"}],
                    },
                },
                ensure_ascii=False,
            )
        )
        await self.websocket.send(json.dumps({"type": "response.create", "response": {"modalities": ["audio", "text"]}}))
        pcm = bytearray()
        transcript = ""
        first_chunk_at: float | None = None
        # Start playback as soon as enough PCM for roughly 80 ms is available.
        # Waiting for response.done used to buffer the complete sentence and
        # added the whole synthesis duration to every acknowledgement.
        stream_buffer = bytearray()
        stream_chunk_bytes = 24000 * 2 * 80 // 1000
        while True:
            message = json.loads(await self.websocket.recv())
            kind = str(message.get("type") or "")
            if kind == "error":
                error = message.get("error") or message
                raise RuntimeError(str(error.get("message") or error))
            if kind == "response.audio.delta":
                chunk = base64.b64decode(str(message.get("delta") or ""))
                pcm.extend(chunk)
                stream_buffer.extend(chunk)
                while len(stream_buffer) >= stream_chunk_bytes:
                    if first_chunk_at is None:
                        first_chunk_at = time.monotonic()
                        self.log(
                            "skill_event_audio_first_chunk",
                            skill=event.get("skill_name"),
                            kind=event.get("kind"),
                            text=text,
                            latency_ms=round((first_chunk_at - started) * 1000.0, 1),
                        )
                    if self._event_is_current(event):
                        self.enqueue_pcm(bytes(stream_buffer[:stream_chunk_bytes]))
                    del stream_buffer[:stream_chunk_bytes]
            elif kind == "response.audio_transcript.done":
                transcript = str(message.get("transcript") or "").strip()
            elif kind == "response.done":
                break
        if stream_buffer and self._event_is_current(event):
            if first_chunk_at is None:
                first_chunk_at = time.monotonic()
                self.log(
                    "skill_event_audio_first_chunk",
                    skill=event.get("skill_name"),
                    kind=event.get("kind"),
                    text=text,
                    latency_ms=round((first_chunk_at - started) * 1000.0, 1),
                )
            self.enqueue_pcm(bytes(stream_buffer))
        self.log(
            "skill_event_audio_generated",
            skill=event.get("skill_name"),
            kind=event.get("kind"),
            text=text,
            transcript=transcript,
            pcm_bytes=len(pcm),
            latency_ms=round((time.monotonic() - started) * 1000.0, 1),
            cached=False,
        )
        if pcm and cache_path is not None:
            self._write_cache(cache_path, bytes(pcm))

    def _cache_path(self, text: str) -> Path | None:
        if self.cache_dir is None or not text:
            return None
        key = hashlib.sha256(
            f"qwen-audio-3.0-realtime-flash\0{self.voice}\0{text}".encode("utf-8")
        ).hexdigest()
        return self.cache_dir / self.voice / f"{key}.pcm"

    def _write_cache(self, path: Path, pcm: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                path.parent.chmod(0o700)
            temporary = path.with_suffix(f".tmp.{os.getpid()}")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, pcm)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, path)
            with contextlib.suppress(OSError):
                path.chmod(0o600)
            self._prune_cache(path.parent)
        except OSError as exc:
            self.log("skill_event_audio_cache_error", error=f"{type(exc).__name__}:{exc}")

    @staticmethod
    def _prune_cache(directory: Path, max_files: int = 256) -> None:
        files = sorted(
            (path for path in directory.glob("*.pcm") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale in files[max_files:]:
            with contextlib.suppress(OSError):
                stale.unlink()

    async def _run(self) -> None:
        while True:
            _sequence, _priority, event = await self.queue.get()
            try:
                # DashScope closes this dedicated TTS connection after a long
                # idle period.  Reconnect once and retry the same event instead
                # of silently dropping the first sentence after the timeout.
                for attempt in range(2):
                    try:
                        if not self._event_is_current(event):
                            break
                        if self.websocket is None:
                            await self._connect()
                        await self._synthesize(event)
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if self.websocket is not None:
                            with contextlib.suppress(Exception):
                                await self.websocket.close()
                        self.websocket = None
                        if attempt:
                            raise
                        self.log(
                            "skill_event_audio_retry",
                            kind=event.get("kind"),
                            text=event.get("text"),
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(
                    "skill_event_audio_error",
                    kind=event.get("kind"),
                    text=event.get("text"),
                    error=f"{type(exc).__name__}:{exc}",
                )
                if self.websocket is not None:
                    try:
                        await self.websocket.close()
                    except Exception:
                        pass
                    self.websocket = None
            finally:
                self.queue.task_done()
