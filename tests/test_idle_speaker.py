from __future__ import annotations

import sys
import time
import types
import unittest


class FakeStream:
    def __init__(self, *, active: bool) -> None:
        self.active = active
        self.start_calls = 0
        self.stop_calls = 0
        self.abort_calls = 0
        self.writes: list[tuple[bytes, bool]] = []

    def is_active(self) -> bool:
        return self.active

    def is_stopped(self) -> bool:
        return not self.active

    def start_stream(self) -> None:
        self.start_calls += 1
        self.active = True

    def stop_stream(self) -> None:
        self.stop_calls += 1
        self.active = False

    def abort_stream(self) -> None:
        self.abort_calls += 1
        self.active = False

    def read(self, frames: int, exception_on_overflow: bool = False) -> bytes:
        return b"\x00\x00" * frames

    def write(self, pcm: bytes, exception_on_underflow: bool = True) -> None:
        self.writes.append((bytes(pcm), exception_on_underflow))

    def close(self) -> None:
        self.active = False


class FakePyAudioInterface:
    def __init__(self) -> None:
        self.input_stream: FakeStream | None = None
        self.output_stream: FakeStream | None = None

    def open(self, **kwargs):
        stream = FakeStream(active=bool(kwargs.get("start", True)))
        if kwargs.get("input"):
            self.input_stream = stream
        if kwargs.get("output"):
            self.output_stream = stream
        return stream

    def terminate(self) -> None:
        return None


class AudioEngineIdleSpeakerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.interfaces: list[FakePyAudioInterface] = []

        def create_interface() -> FakePyAudioInterface:
            value = FakePyAudioInterface()
            self.interfaces.append(value)
            return value

        sys.modules["pyaudio"] = types.SimpleNamespace(
            paInt16=8,
            PyAudio=create_interface,
        )
        from realtime_chat import AudioEngine

        self.engine = AudioEngine(
            input_device_index=None,
            output_device_index=None,
            chunk_ms=20,
        )
        self.output = self.interfaces[-1].output_stream
        assert self.output is not None

    def tearDown(self) -> None:
        self.engine.close()

    def wait_for(self, predicate, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("condition did not become true")

    def test_output_starts_stopped_and_returns_to_stopped_after_audio(self) -> None:
        self.assertFalse(self.output.active)
        self.assertEqual(self.output.start_calls, 1)
        self.assertEqual(self.output.stop_calls, 1)
        self.engine.enqueue(b"\x01\x00" * 960)
        self.wait_for(lambda: bool(self.output.writes))
        self.assertEqual(self.output.start_calls, 2)
        self.assertTrue(all(not underflow for _, underflow in self.output.writes))
        self.wait_for(lambda: not self.output.active)
        self.assertEqual(self.output.stop_calls, 2)
        self.assertFalse(self.engine.playing.is_set())

    def test_a_later_utterance_restarts_the_same_stream(self) -> None:
        self.engine.enqueue(b"\x01\x00" * 480)
        self.wait_for(lambda: self.output.stop_calls == 2)
        first_write_count = len(self.output.writes)
        self.engine.enqueue(b"\x02\x00" * 480)
        self.wait_for(lambda: len(self.output.writes) > first_write_count)
        self.wait_for(lambda: self.output.stop_calls == 3)
        self.assertEqual(self.output.start_calls, 3)


if __name__ == "__main__":
    unittest.main()
