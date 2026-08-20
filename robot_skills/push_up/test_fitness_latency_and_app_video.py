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
)


class FitnessLatencyAndAppVideoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    def test_four_confirmed_down_frames_allow_first_strong_up_count(self) -> None:
        tracker = PushupPhaseTracker(self.config["pushup"])
        # Establish horizontal readiness and a confirmed down posture.
        for _ in range(5):
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
