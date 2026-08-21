from __future__ import annotations

import argparse
import asyncio
import importlib.util
import math
import shutil
import sys
import subprocess
import time
from pathlib import Path
from typing import Any

from .resources import ResourceManager
from .speech_policy import SpeechPolicy


class AudioManager:
    def __init__(self, config: dict[str, Any], resources: ResourceManager | None = None, realtime_voice: Any | None = None):
        self.config = config
        self.audio = config["audio"]
        self.resources = resources or ResourceManager(config)
        self.realtime_voice = realtime_voice
        self.speech_policy = SpeechPolicy(config)
        self.last_speech_started_at = 0.0
        self.last_speech_ended_at = 0.0
        self.last_speech_text = ""

    def record_pcm(self, output_path: str | Path | None = None, seconds: float | None = None) -> Path:
        path = Path(output_path or self.audio["command_pcm_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        duration = max(1, int(math.ceil(float(seconds or self.audio["default_record_seconds"]))))
        cmd = [
            self.audio["record_command"],
            self.audio["quiet_arg"],
            "-D",
            self.audio["input_device"],
            "-t",
            "raw",
            "-f",
            self.audio["sample_format"],
            "-r",
            str(self.audio["record_sample_rate"]),
            "-c",
            str(self.audio["channels"]),
            "-d",
            str(duration),
            str(path),
        ]
        with self.resources.acquire(["mic"]):
            subprocess.run(cmd, check=True)
        return path

    def record_wav(self, output_path: str | Path | None = None, seconds: float | None = None) -> Path:
        path = Path(output_path or self.audio["command_wav_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        duration = max(1, int(math.ceil(float(seconds or self.audio["default_record_seconds"]))))
        cmd = [
            self.audio["record_command"],
            self.audio["quiet_arg"],
            "-D",
            self.audio["input_device"],
            "-t",
            "wav",
            "-f",
            self.audio["sample_format"],
            "-r",
            str(self.audio["record_sample_rate"]),
            "-c",
            str(self.audio["channels"]),
            "-d",
            str(duration),
            str(path),
        ]
        with self.resources.acquire(["mic"]):
            subprocess.run(cmd, check=True)
        return path

    def play_pcm(self, pcm_path: str | Path, sample_rate: int | None = None) -> None:
        path = Path(pcm_path)
        cmd = [
            self.audio["play_command"],
            self.audio["quiet_arg"],
            "-D",
            self.audio["output_device"],
            "-t",
            "raw",
            "-f",
            self.audio["sample_format"],
            "-r",
            str(sample_rate or self.audio["play_sample_rate"]),
            "-c",
            str(self.audio["channels"]),
            str(path),
        ]
        with self.resources.acquire(["speaker"]):
            subprocess.run(cmd, check=True)

    def record_then_play(self, seconds: float | None = None) -> Path:
        path = self.record_pcm(seconds=seconds)
        self.play_pcm(path, sample_rate=int(self.audio.get("echo_play_sample_rate", self.audio["record_sample_rate"])))
        return path

    def listen_once(self, seconds: float | None = None) -> dict[str, Any]:
        if self.audio.get("voice_io_backend") == "wake_skill_agent":
            return self.listen_once_wake_skill_agent(seconds=seconds)
        wav_path = self.record_wav(seconds=seconds)
        try:
            text = self.recognize_wav(wav_path)
        except Exception as exc:
            return {"audio_path": str(wav_path), "text": "", "error": str(exc)}
        return {"audio_path": str(wav_path), "text": text}

    def listen_once_wake_skill_agent(self, seconds: float | None = None) -> dict[str, Any]:
        try:
            return asyncio.run(self._listen_once_wake_skill_agent(seconds=seconds))
        except RuntimeError as exc:
            if "asyncio.run" not in str(exc):
                raise
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(self._listen_once_wake_skill_agent(seconds=seconds))
            finally:
                loop.close()

    async def _listen_once_wake_skill_agent(self, seconds: float | None = None) -> dict[str, Any]:
        modules = self._load_voice_modules()
        args = self._voice_args(seconds=seconds)
        with self.resources.acquire(["mic"]):
            pcm = await modules["record_one_turn_pcm"](args)
        if not pcm:
            return {"audio_path": str(args.asr_wav_path), "text": "", "error": "no_valid_speech_detected"}
        modules["write_pcm_wav"](Path(args.asr_wav_path), pcm, args.input_rate)
        try:
            text = await asyncio.to_thread(
                self._recognize_wav_with_module,
                Path(args.asr_wav_path),
                args.asr_backend,
                args.asr_timeout,
            )
        except Exception as exc:
            return {"audio_path": str(args.asr_wav_path), "text": "", "error": str(exc)}
        text = (text or "").strip()
        return {"audio_path": str(args.asr_wav_path), "text": text, "error": "" if text else "empty_asr_text"}

    def recognize_wav(self, wav_path: str | Path) -> str:
        module_path = Path(self.audio["asr_module"])
        if not module_path.exists():
            raise FileNotFoundError(f"ASR module not found: {module_path}")
        spec = importlib.util.spec_from_file_location("self_program_asr", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load ASR module: {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.recognize_wav(Path(wav_path), backend=self.audio.get("asr_backend", "local_zipformer"))

    def speak_text(self, text: str) -> bool:
        text = self.speech_policy.clean(text)
        if not text:
            return False
        started = time.monotonic()
        ok = False
        print(f"ROBOT_SAY:{text}", flush=True)
        try:
            if self.realtime_voice is not None and self.audio.get("voice_io_backend") == "doubao_realtime":
                ok = bool(self.realtime_voice.speak_text(text))
                if not ok and self.audio.get("tts_fallback_backend", "wake_skill_agent") == "wake_skill_agent":
                    ok = self.speak_text_wake_skill_agent(text)
                return ok
            if self.audio.get("voice_io_backend") == "wake_skill_agent":
                ok = self.speak_text_wake_skill_agent(text)
                return ok
            speak_command = self.audio.get("speak_command")
            if not speak_command:
                return False
            if isinstance(speak_command, str):
                cmd = [speak_command, text]
            else:
                cmd = [str(item).format(text=text) for item in speak_command]
            if not shutil.which(cmd[0]) and not Path(cmd[0]).exists():
                return False
            with self.resources.acquire(["speaker"]):
                completed = subprocess.run(cmd, check=False)
            ok = completed.returncode == 0
            return ok
        finally:
            self._mark_speech_finished(text, started, ok)

    def stop_speech(self) -> None:
        """Best-effort barge-in for local playback processes."""
        device = str(self.audio.get("output_device", "")).strip()
        patterns = []
        if device:
            patterns.append(f"aplay.*{device}")
        patterns.append("aplay")
        for pattern in patterns:
            try:
                subprocess.run(
                    ["pkill", "-f", pattern],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=1,
                )
            except Exception:
                pass

    def speak_wake_reply(self, text: str | None = None) -> bool:
        wake_text = text or self.audio.get("wake_reply_text", "我在")
        wake_pcm = Path(self.audio.get("wake_reply_pcm", ""))
        if wake_pcm.exists() and wake_pcm.stat().st_size > 0:
            started = time.monotonic()
            ok = False
            print(f"ROBOT_SAY:{wake_text}", flush=True)
            try:
                self.play_pcm(wake_pcm, sample_rate=int(self.audio.get("play_sample_rate", 24000)))
                ok = True
                return ok
            except Exception as exc:
                print(f"WAKE_REPLY_PCM_FAILED:{exc}", flush=True)
            finally:
                self._mark_speech_finished(wake_text, started, ok)
        return self.speak_text(wake_text)

    def _mark_speech_finished(self, text: str, started: float, ok: bool) -> None:
        self.last_speech_started_at = started
        self.last_speech_ended_at = time.monotonic()
        self.last_speech_text = text

    def wait_for_speech_settle(self, min_delay: float | None = None) -> float:
        delay = float(
            self.audio.get(
                "post_speech_listen_delay_seconds",
                1.2 if min_delay is None else min_delay,
            )
            if min_delay is None
            else min_delay
        )
        if delay <= 0 or self.last_speech_ended_at <= 0:
            return 0.0
        remaining = delay - (time.monotonic() - self.last_speech_ended_at)
        if remaining <= 0:
            return 0.0
        time.sleep(remaining)
        return remaining

    def speak_text_wake_skill_agent(self, text: str) -> bool:
        wake_text = self.audio.get("wake_reply_text", "我在")
        wake_pcm = Path(self.audio.get("wake_reply_pcm", ""))
        if text == wake_text and wake_pcm.exists() and wake_pcm.stat().st_size > 0:
            try:
                self.play_pcm(wake_pcm, sample_rate=int(self.audio.get("play_sample_rate", 24000)))
                return True
            except Exception as exc:
                print(f"TTS_FAILED:{exc}", flush=True)
                return False
        try:
            asyncio.run(self._speak_text_wake_skill_agent(text))
            return True
        except Exception as exc:
            print(f"TTS_FAILED:{exc}", flush=True)
            return False

    async def _speak_text_wake_skill_agent(self, text: str) -> None:
        modules = self._load_voice_modules()
        args = self._voice_args()
        modules["load_env_file"]()
        client = modules["DoubaoVoice"](args)
        await client.connect()
        try:
            with self.resources.acquire(["speaker"]):
                await modules["speak_text"](client, args, text)
        finally:
            await client.close()

    def _load_voice_modules(self) -> dict[str, Any]:
        self_program_dir = Path(self.config["paths"]["self_program_dir"])
        if str(self_program_dir) not in sys.path:
            sys.path.insert(0, str(self_program_dir))
        from vocal_stream_llm import DoubaoVoice, load_env_file, record_one_turn_pcm, write_pcm_wav
        from wake_vocal_stream_llm import speak_text

        return {
            "DoubaoVoice": DoubaoVoice,
            "load_env_file": load_env_file,
            "record_one_turn_pcm": record_one_turn_pcm,
            "write_pcm_wav": write_pcm_wav,
            "speak_text": speak_text,
        }

    def _voice_args(self, seconds: float | None = None) -> argparse.Namespace:
        vad = self.audio.get("vad", {})
        return argparse.Namespace(
            seconds=float(seconds or self.audio.get("default_voice_seconds") or self.audio.get("default_record_seconds", 10)),
            chunk_ms=int(self.audio.get("chunk_ms", 100)),
            silence_tail_sec=float(self.audio.get("silence_tail_sec", 0.25)),
            first_response_timeout=float(self.audio.get("first_response_timeout", 20.0)),
            listen_timeout=float(self.audio.get("listen_timeout", 8.0)),
            vad_frame_ms=int(vad.get("frame_ms", 20)),
            vad_aggressiveness=int(vad.get("aggressiveness", 2)),
            vad_min_rms=int(vad.get("min_rms", 150)),
            vad_start_speech_ms=int(vad.get("start_speech_ms", 160)),
            vad_end_silence_ms=int(vad.get("end_silence_ms", 350)),
            vad_min_speech_ms=int(vad.get("min_speech_ms", 300)),
            vad_pre_roll_ms=int(vad.get("pre_roll_ms", 300)),
            input_device=self.audio["input_device"],
            output_device=self.audio["output_device"],
            input_rate=int(self.audio["record_sample_rate"]),
            output_rate=int(self.audio["play_sample_rate"]),
            speaker=self.audio.get("speaker", "zh_female_xiaohe_jupiter_bigtts"),
            tts_system_role=self.audio.get(
                "tts_system_role",
                "你是一个亲切自然的家庭机器人语音助手。只朗读给定内容，不添加额外说明。",
            ),
            tts_speaking_style=self.audio.get(
                "tts_speaking_style",
                "使用自然口语语气，节奏轻松，停顿清楚，避免播音腔和逐字念稿感。",
            ),
            no_play=bool(self.audio.get("no_play", False)),
            asr_backend=self.audio.get("asr_backend", "local_zipformer"),
            asr_timeout=float(self.audio.get("asr_timeout", 25.0)),
            asr_wav_path=Path(self.audio.get("command_wav_path", "/home/test/new_project/runtime/audio/command.wav")),
            asr_end_smooth_ms=int(self.audio.get("asr_end_smooth_ms", 300)),
            wake_reply_timeout=float(self.audio.get("wake_reply_timeout", 3.0)),
            wake_reply_pcm=Path(self.audio.get("wake_reply_pcm", "/home/test/self_program/runtime/wake_reply.pcm")),
            refresh_wake_reply_cache=False,
            debug_timing=bool(self.audio.get("debug_timing", False)),
        )

    def _recognize_wav_with_module(self, wav_path: Path, backend: str, timeout: float) -> str:
        module_path = Path(self.audio["asr_module"])
        spec = importlib.util.spec_from_file_location("self_program_asr", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load ASR module: {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.recognize_wav(wav_path, backend=backend, timeout=timeout)
