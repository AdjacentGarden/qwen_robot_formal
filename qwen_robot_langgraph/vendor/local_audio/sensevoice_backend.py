"""Offline CPU SenseVoice via the existing sherpa-onnx runtime."""
from pathlib import Path
import threading
import time
import numpy as np

DEFAULT_MODEL=Path(__file__).resolve().parent/'runtime/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'


class SenseVoiceASR:
    def __init__(self,model_dir=DEFAULT_MODEL,threads=4):
        self.model_dir=Path(model_dir)
        self.threads=threads
        self.engine=None
        self.load_seconds=0.0
        self.lock=threading.RLock()

    def load(self):
        if self.engine is not None: return self.load_seconds
        import sherpa_onnx
        started=time.monotonic()
        self.engine=sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(self.model_dir/'model.int8.onnx'),tokens=str(self.model_dir/'tokens.txt'),
            num_threads=self.threads,provider='cpu',language='',use_itn=False,debug=False)
        self.load_seconds=time.monotonic()-started
        return self.load_seconds

    def transcribe(self,pcm,sample_rate=16000):
        if sample_rate!=16000 or not pcm or len(pcm)%2:
            raise ValueError('expected nonempty mono PCM16 at 16000 Hz')
        self.load()
        started=time.monotonic()
        samples=np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768.0
        with self.lock:
            stream=self.engine.create_stream()
            stream.accept_waveform(sample_rate,samples)
            self.engine.decode_stream(stream)
            text=stream.result.text.strip()
        return text,time.monotonic()-started

    def close(self):
        with self.lock: self.engine=None
