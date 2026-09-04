#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from engine import (  # noqa: E402
    PushupPhaseTracker,
    LatestFrameStream,
    _spoken_live_count,
    _spoken_repetition_count,
    make_android_compatible_video,
    validate_live_count_completion,
)


class FitnessLatencyAndAppVideoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    def test_four_confirmed_down_frames_allow_first_strong_up_count(self) -> None:
        tracker = PushupPhaseTracker(self.config["pushup"])
        # First establish a real horizontal top pose.  Entering the workout
        # from a low/preparation pose is deliberately not a repetition.
        result = tracker.process_observation(176.0, 1.0, 12.0, 2.0)
        self.assertTrue(result["armed"])
        self.assertEqual(result["phase"], "up")
        # Establish a confirmed down posture for the first real repetition.
        for _ in range(12):
            result = tracker.process_observation(75.0, 0.35, 12.0, 2.0)
        self.assertEqual(result["phase"], "down")
        # Let the EMA recover from the bottom pose. The first two samples are
        # deliberately still below the up threshold; the first geometrically
        # valid strong-up sample must count without a second confirmation frame.
        tracker.process_observation(176.0, 1.0, 12.0, 2.0)
        tracker.process_observation(176.0, 1.0, 12.0, 2.0)
        result = tracker.process_observation(176.0, 1.0, 12.0, 2.0)
        self.assertTrue(result["fast_up_confirmation"])
        self.assertTrue(result["incremented"])
        self.assertEqual(result["count"], 1)

    def test_count_utterances_are_clear_natural_counter_phrases(self) -> None:
        self.assertEqual(_spoken_repetition_count(1), "一个")
        self.assertEqual(_spoken_repetition_count(2), "两个")
        self.assertEqual(_spoken_repetition_count(3), "三个")
        self.assertEqual(_spoken_repetition_count(4), "四个")
        self.assertEqual(_spoken_live_count(1), "第一个")
        self.assertEqual(_spoken_live_count(2), "第二个")
        self.assertEqual(_spoken_live_count(4), "第四个")

    def test_completion_utterance_does_not_prompt_or_announce_video_upload(self) -> None:
        source = (Path(__file__).with_name("engine.py")).read_text(encoding="utf-8")
        completion_line = next(
            line for line in source.splitlines()
            if "运动结束，你一共完成了" in line
        )
        self.assertNotIn("上传", completion_line)
        self.assertNotIn("同步到手机", completion_line)

    def test_completion_event_precedes_app_video_encoding_and_safety_cleanup(self) -> None:
        source = (Path(__file__).with_name("engine.py")).read_text(encoding="utf-8")
        finalization = source.index("# Finalize and close MP4 containers")
        completion = source.index('                "complete",', finalization)
        app_encode = source.index(
            "make_android_compatible_video(raw_output, app_video_output)",
            finalization,
        )
        self.assertLess(completion, app_encode)
        completion_block = source[completion:app_encode]
        self.assertIn("喝口水", completion_block)

    def test_latest_frame_stream_records_every_frame_but_skips_stale_inference_frames(self) -> None:
        class FakeCapture:
            def __init__(self) -> None:
                self.index = 0

            def read(self):
                if self.index >= 24:
                    return False, None
                self.index += 1
                time.sleep(0.001)
                return True, np.full((12, 16, 3), self.index, dtype=np.uint8)

            def get(self, _property):
                return self.index * 10.0

        class RawCollector:
            def __init__(self) -> None:
                self.frames: list[int] = []

            def write(self, frame, _timestamp):
                self.frames.append(int(frame[0, 0, 0]))

        raw = RawCollector()
        stream = LatestFrameStream(FakeCapture(), raw)  # type: ignore[arg-type]
        stream.start()
        while not stream.finished:
            time.sleep(0.005)
        packet = stream.read_latest()
        stream.stop()
        self.assertIsNotNone(packet)
        self.assertEqual(packet.sequence, 24)
        self.assertEqual(raw.frames, list(range(1, 25)))
        self.assertGreater(stream.overwritten_frames, 0)

    def test_live_frame_stream_recovers_from_temporary_read_timeouts(self) -> None:
        class FakeCapture:
            def __init__(self) -> None:
                self.outcomes = [1, None, None, 2]
                self.index = 0

            def read(self):
                time.sleep(0.01)
                if self.index < len(self.outcomes):
                    value = self.outcomes[self.index]
                    self.index += 1
                    if value is not None:
                        return True, np.full((12, 16, 3), value, dtype=np.uint8)
                return False, None

            def get(self, _property):
                return self.index * 10.0

        class RawCollector:
            def __init__(self) -> None:
                self.frames: list[int] = []

            def write(self, frame, _timestamp):
                self.frames.append(int(frame[0, 0, 0]))

        raw = RawCollector()
        stream = LatestFrameStream(
            FakeCapture(),
            raw,  # type: ignore[arg-type]
            live_source=True,
            maximum_stall_seconds=0.25,
        )
        stream.start()
        deadline = time.monotonic() + 1.0
        while len(raw.frames) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        stream.stop()
        self.assertEqual(raw.frames, [1, 2])
        self.assertGreaterEqual(stream.transient_read_timeouts, 2)
        self.assertEqual(stream.stall_recoveries, 1)
        self.assertIsNone(stream.error)

    def test_live_frame_stream_reports_sustained_stall_as_error(self) -> None:
        class FakeCapture:
            def __init__(self) -> None:
                self.sent = False

            def read(self):
                time.sleep(0.01)
                if not self.sent:
                    self.sent = True
                    return True, np.ones((12, 16, 3), dtype=np.uint8)
                return False, None

            def get(self, _property):
                return 0.0

        class RawCollector:
            def write(self, _frame, _timestamp):
                return None

        stream = LatestFrameStream(
            FakeCapture(),
            RawCollector(),  # type: ignore[arg-type]
            live_source=True,
            maximum_stall_seconds=0.1,
        )
        stream.start()
        deadline = time.monotonic() + 1.0
        while not stream.finished and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(stream.finished)
        with self.assertRaisesRegex(Exception, "live camera produced no frame"):
            stream.stop()
        self.assertEqual(stream.termination_reason, "camera_stall")
        self.assertGreaterEqual(stream.transient_read_timeouts, 1)

    def test_short_live_count_can_never_be_reported_as_completed(self) -> None:
        with self.assertRaisesRegex(
            Exception,
            "live_count_ended_before_requested_duration",
        ):
            validate_live_count_completion(
                live_source=True,
                requested_duration=30.0,
                counting_elapsed=11.116,
                termination_reason="camera_stall",
                maximum_frames=0,
                interrupted=False,
            )

    def test_requested_duration_is_the_only_normal_live_completion(self) -> None:
        validate_live_count_completion(
            live_source=True,
            requested_duration=30.0,
            counting_elapsed=30.001,
            termination_reason="duration_reached",
            maximum_frames=0,
            interrupted=False,
        )

    def test_android_copy_is_h264_yuv420p_and_faststart_readable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fitness-app-video-") as directory:
            root = Path(directory)
            source = root / "source.mp4"
            destination = root / "app.mp4"
            writer = cv2.VideoWriter(
                str(source),
                cv2.VideoWriter_fourcc(*"mp4v"),
                15.0,
                (160, 120),
            )
            self.assertTrue(writer.isOpened())
            for index in range(30):
                frame = np.full((120, 160, 3), (index * 7) % 255, dtype=np.uint8)
                writer.write(frame)
            writer.release()

            report = make_android_compatible_video(source, destination)
            self.assertEqual(report["codec"], "h264")
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name,pix_fmt",
                    "-of",
                    "json",
                    str(destination),
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            stream = json.loads(probe.stdout)["streams"][0]
            self.assertEqual(stream["codec_name"], "h264")
            self.assertEqual(stream["pix_fmt"], "yuv420p")


if __name__ == "__main__":
    unittest.main()
