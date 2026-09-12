from __future__ import annotations

import subprocess
import time
import wave
from pathlib import Path

import numpy as np


class MatchaTTS:
    """Persistent sherpa-onnx Matcha Chinese TTS."""

    def __init__(
        self,
        model_dir: Path = Path("/home/test/qwen_robot_project/runtime/models/matcha-icefall-zh-en"),
        threads: int = 4,
        speed: float = 1.03,
    ) -> None:
        self.model_dir = model_dir
        self.threads = threads
        self.speed = speed
        self.engine = None
        self.sample_rate = 16000
        self.load_seconds = 0.0

    def load(self) -> float:
        if self.engine is not None:
            return self.load_seconds
        started = time.perf_counter()
        import sherpa_onnx

        required = {
            "acoustic_model": self.model_dir / "model-steps-3.onnx",
            "vocoder": self.model_dir / "vocos-16khz-univ.onnx",
            "tokens": self.model_dir / "tokens.txt",
            "lexicon": self.model_dir / "lexicon.txt",
        }
        missing = [str(path) for path in required.values() if not path.exists()]
        if missing:
            raise FileNotFoundError("missing TTS assets: " + ", ".join(missing))
        matcha = sherpa_onnx.OfflineTtsMatchaModelConfig(
            acoustic_model=str(required["acoustic_model"]),
            vocoder=str(required["vocoder"]),
            tokens=str(required["tokens"]),
            lexicon=str(required["lexicon"]),
            data_dir=str(self.model_dir / "espeak-ng-data"),
        )
        model = sherpa_onnx.OfflineTtsModelConfig(
            matcha=matcha, num_threads=self.threads, debug=False
        )
        self.engine = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=model))
        self.load_seconds = time.perf_counter() - started
        return self.load_seconds

    def synthesize(self, text: str) -> tuple[bytes, int, float]:
        self.load()
        started = time.perf_counter()
        try:
            audio = self.engine.generate(text, sid=0, speed=self.speed)
        except TypeError:
            audio = self.engine.generate(text, sid=0)
        samples = np.clip(np.asarray(audio.samples), -1.0, 1.0)
        pcm = (samples * 32767.0).astype("<i2").tobytes()
        self.sample_rate = int(audio.sample_rate)
        return pcm, self.sample_rate, time.perf_counter() - started

    @staticmethod
    def save_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(sample_rate)
            stream.writeframes(pcm)

    @staticmethod
    def play_wav(path: Path) -> float:
        from audio_io import play_wav
        return play_wav(path)
