from __future__ import annotations

import argparse
import importlib.util
import threading
import time
from pathlib import Path
from typing import Any


class PersistentZipformerASR:
    """Keep the existing RKNN Zipformer loaded across command turns.

    The legacy ASR entry point starts a new Python process and reloads three
    RKNN models for every utterance.  On this robot that cold path costs about
    eleven seconds.  This adapter loads the same proven model once and resets
    only its streaming caches between utterances.
    """

    def __init__(self, config: dict[str, Any], resources: Any | None = None):
        self.config = config
        self.resources = resources
        voice = config.get("voice_decision", {})
        qwen = voice.get("qwen", {}) if isinstance(voice.get("qwen"), dict) else {}
        self.module_path = Path(
            qwen.get("zipformer_module", "/home/test/refine_0508/llm/zipformer.py")
        )
        self.model_dir = Path(qwen.get("zipformer_model_dir", "/home/test/refine_0508/model"))
        self.target = str(qwen.get("zipformer_target", "rk3588"))
        self.device_id = qwen.get("zipformer_device_id")
        self.load_timeout = float(qwen.get("asr_warmup_timeout_seconds", 45.0))
        self._module: Any | None = None
        self._model: Any | None = None
        self._vocab: dict[str, str] = {}
        self._load_started_at = 0.0
        self._load_elapsed = 0.0
        self._load_error = ""
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._load_thread: threading.Thread | None = None

    def start_warmup(self) -> None:
        with self._lock:
            if self._ready.is_set() or (self._load_thread and self._load_thread.is_alive()):
                return
            self._load_thread = threading.Thread(
                target=self._load_once,
                name="qwen-zipformer-warmup",
                daemon=True,
            )
            self._load_thread.start()

    def warmup(self, timeout: float | None = None) -> dict[str, Any]:
        self.start_warmup()
        waited = self._ready.wait(timeout=max(1.0, float(timeout or self.load_timeout)))
        if not waited:
            return {
                "ok": False,
                "backend": "persistent_zipformer_rknn",
                "error": "asr_warmup_timeout",
            }
        if self._load_error:
            return {
                "ok": False,
                "backend": "persistent_zipformer_rknn",
                "error": self._load_error,
                "elapsed_seconds": round(self._load_elapsed, 4),
            }
        return {
            "ok": True,
            "backend": "persistent_zipformer_rknn",
            "elapsed_seconds": round(self._load_elapsed, 4),
            "model_reused_between_turns": True,
        }

    def transcribe_pcm(self, pcm: bytes, sample_rate: int = 16000) -> tuple[str, dict[str, Any]]:
        if not pcm:
            return "", {"ok": False, "error": "empty_pcm"}
        warmup = self.warmup()
        if not warmup.get("ok"):
            raise RuntimeError(str(warmup.get("error") or "zipformer_not_ready"))
        started = time.monotonic()
        with self._lock:
            module = self._module
            model = self._model
            if module is None or model is None:
                raise RuntimeError("zipformer_not_ready")
            import numpy as np
            import torch

            waveform = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            audio = torch.from_numpy(waveform)
            # The encoder cache carries acoustic state and must be fresh for
            # each user turn even though the heavyweight RKNN contexts persist.
            model.init_encoder_input()
            resource_context = (
                self.resources.acquire(["npu"])
                if self.resources is not None
                else _NullContext()
            )
            with resource_context:
                hyp, timestamps = module.run_model(model, audio, sample_rate=sample_rate)
            text, real_timestamps = module.post_process(hyp, self._vocab, timestamps)
        elapsed = time.monotonic() - started
        return str(text or "").strip(), {
            "ok": True,
            "backend": "persistent_zipformer_rknn",
            "elapsed_seconds": round(elapsed, 4),
            "audio_seconds": round(len(pcm) / max(1, sample_rate * 2), 4),
            "timestamp_count": len(real_timestamps),
            "model_reused": True,
        }

    def close(self) -> None:
        with self._lock:
            model = self._model
            self._model = None
            if model is not None:
                try:
                    model.release_model()
                except Exception:
                    pass

    def _load_once(self) -> None:
        self._load_started_at = time.monotonic()
        try:
            if not self.module_path.exists():
                raise FileNotFoundError(f"zipformer module not found: {self.module_path}")
            required = {
                "encoder_model_path": self.model_dir / "encoder-epoch-99-avg-1.rknn",
                "decoder_model_path": self.model_dir / "decoder-epoch-99-avg-1.rknn",
                "joiner_model_path": self.model_dir / "joiner-epoch-99-avg-1.rknn",
                "vocab_path": self.model_dir / "vocab.txt",
            }
            missing = [str(path) for path in required.values() if not path.exists()]
            if missing:
                raise FileNotFoundError("missing Zipformer assets: " + ", ".join(missing))
            spec = importlib.util.spec_from_file_location("qwen_robot_zipformer", self.module_path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load Zipformer module: {self.module_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            args = argparse.Namespace(
                encoder_model_path=str(required["encoder_model_path"]),
                decoder_model_path=str(required["decoder_model_path"]),
                joiner_model_path=str(required["joiner_model_path"]),
                target=self.target,
                device_id=self.device_id,
            )
            resource_context = (
                self.resources.acquire(["npu"])
                if self.resources is not None
                else _NullContext()
            )
            with resource_context:
                model = module.set_model(args)
                model.init_encoder_input()
            vocab = module.read_vocab(str(required["vocab_path"]))
            with self._lock:
                self._module = module
                self._model = model
                self._vocab = vocab
        except BaseException as exc:
            self._load_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._load_elapsed = time.monotonic() - self._load_started_at
            self._ready.set()


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return False
