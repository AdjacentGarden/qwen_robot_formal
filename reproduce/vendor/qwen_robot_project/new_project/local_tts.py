from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


class LocalMatchaTTS:
    """Persistent offline TTS used when no paid realtime speech quota exists."""

    def __init__(self, config: dict[str, Any], resources: Any | None = None):
        self.config = config
        self.resources = resources
        audio = config.get("audio", {})
        voice = config.get("voice_decision", {})
        qwen = voice.get("qwen", {}) if isinstance(voice.get("qwen"), dict) else {}
        project_root = Path(config.get("paths", {}).get("project_root", Path.cwd()))
        self.archive = Path(
            qwen.get("local_tts_archive", "/home/test/refine_0508/model/tts_matcha_icefall_zh_en.tar.gz")
        )
        self.extract_root = Path(qwen.get("local_tts_extract_root", project_root / "runtime" / "models"))
        self.model_dir = Path(qwen.get("local_tts_model_dir", self.extract_root / "matcha-icefall-zh-en"))
        self.output_device = str(audio.get("output_device", "plughw:rockchiptas6424"))
        self.play_command = str(audio.get("play_command", "aplay"))
        self.channels = int(audio.get("channels", 1))
        self.sample_format = str(audio.get("sample_format", "S16_LE"))
        self.num_threads = int(qwen.get("local_tts_threads", 4))
        self.speed = float(qwen.get("local_tts_speed", 1.03))
        self._engine: Any | None = None
        self._sample_rate = 16000
        self._ready = threading.Event()
        self._load_error = ""
        self._load_elapsed = 0.0
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None

    def start_warmup(self) -> None:
        with self._lock:
            if self._ready.is_set() or (self._thread and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self._load_once, name="qwen-local-tts-warmup", daemon=True)
            self._thread.start()

    def warmup(self, timeout: float = 45.0) -> dict[str, Any]:
        self.start_warmup()
        if not self._ready.wait(timeout=max(1.0, timeout)):
            return {"ok": False, "backend": "local_matcha", "error": "tts_warmup_timeout"}
        if self._load_error:
            return {
                "ok": False,
                "backend": "local_matcha",
                "error": self._load_error,
                "elapsed_seconds": round(self._load_elapsed, 4),
            }
        return {
            "ok": True,
            "backend": "local_matcha",
            "elapsed_seconds": round(self._load_elapsed, 4),
            "offline": True,
        }

    def speak(self, text: str) -> tuple[bool, dict[str, Any]]:
        value = str(text or "").strip()
        if not value:
            return False, {"ok": False, "error": "empty_tts_text"}
        warmup = self.warmup()
        if not warmup.get("ok"):
            return False, warmup
        started = time.monotonic()
        with self._lock:
            engine = self._engine
            if engine is None:
                return False, {"ok": False, "error": "tts_not_ready"}
            try:
                audio = engine.generate(value, sid=0, speed=self.speed)
            except TypeError:
                audio = engine.generate(value, sid=0)
            if audio is None or len(audio.samples) == 0:
                return False, {"ok": False, "error": "empty_tts_audio"}
            import numpy as np

            samples = np.clip(np.asarray(audio.samples), -1.0, 1.0)
            pcm = (samples * 32767.0).astype("<i2").tobytes()
            self._sample_rate = int(audio.sample_rate)
        synthesis_finished = time.monotonic()
        runtime_audio = Path(self.config["paths"]["runtime_dir"]) / "audio"
        runtime_audio.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="matcha_", suffix=".pcm", dir=str(runtime_audio))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(pcm)
            cmd = [
                self.play_command,
                "-q",
                "-D",
                self.output_device,
                "-t",
                "raw",
                "-f",
                self.sample_format,
                "-r",
                str(self._sample_rate),
                "-c",
                str(self.channels),
                temp_name,
            ]
            resource_context = (
                self.resources.acquire(["speaker"])
                if self.resources is not None
                else _NullContext()
            )
            with resource_context:
                completed = subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            ok = completed.returncode == 0
            error = completed.stderr.decode("utf-8", "replace")[-300:] if completed.stderr and not ok else ""
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except Exception:
                pass
        return ok, {
            "ok": ok,
            "backend": "local_matcha",
            "synthesis_seconds": round(synthesis_finished - started, 4),
            "total_seconds": round(time.monotonic() - started, 4),
            "sample_rate": self._sample_rate,
            "error": error,
        }

    def _load_once(self) -> None:
        started = time.monotonic()
        try:
            self._ensure_model_dir()
            import sherpa_onnx

            acoustic = self.model_dir / "model-steps-3.onnx"
            vocoder = self.model_dir / "vocos-16khz-univ.onnx"
            tokens = self.model_dir / "tokens.txt"
            lexicon = self.model_dir / "lexicon.txt"
            data_dir = self.model_dir / "espeak-ng-data"
            required = [acoustic, vocoder, tokens, lexicon]
            missing = [str(path) for path in required if not path.exists()]
            if missing:
                raise FileNotFoundError("missing Matcha assets: " + ", ".join(missing))
            matcha = sherpa_onnx.OfflineTtsMatchaModelConfig(
                acoustic_model=str(acoustic),
                vocoder=str(vocoder),
                tokens=str(tokens),
                lexicon=str(lexicon),
                data_dir=str(data_dir),
            )
            model = sherpa_onnx.OfflineTtsModelConfig(matcha=matcha, num_threads=self.num_threads, debug=False)
            engine = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=model))
            with self._lock:
                self._engine = engine
        except BaseException as exc:
            self._load_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._load_elapsed = time.monotonic() - started
            self._ready.set()

    def _ensure_model_dir(self) -> None:
        required = [
            self.model_dir / "model-steps-3.onnx",
            self.model_dir / "vocos-16khz-univ.onnx",
            self.model_dir / "tokens.txt",
            self.model_dir / "lexicon.txt",
        ]
        if all(path.exists() for path in required):
            return
        if not self.archive.exists():
            raise FileNotFoundError(f"TTS archive not found: {self.archive}")
        self.extract_root.mkdir(parents=True, exist_ok=True)
        root = self.extract_root.resolve()
        with tarfile.open(self.archive, "r:gz") as archive:
            for member in archive.getmembers():
                target = (self.extract_root / member.name).resolve()
                if root not in [target, *target.parents]:
                    raise RuntimeError(f"unsafe TTS archive member: {member.name}")
            archive.extractall(self.extract_root)


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return False
