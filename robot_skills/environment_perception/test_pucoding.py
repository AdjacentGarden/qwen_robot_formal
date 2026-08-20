import importlib.util
import json
import os
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


RUN_PATH = Path(__file__).with_name("run.py")
SPEC = importlib.util.spec_from_file_location("fixed_environment_perception_run", RUN_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        decision = {
            "summary": "画面正常",
            "suitable": {
                "projection": {"ok": True, "confidence": 0.9, "reasons": [], "blockers": []},
                "push_up": {"ok": False, "confidence": 0.8, "reasons": [], "blockers": []},
                "squat": {"ok": False, "confidence": 0.8, "reasons": [], "blockers": []},
                "pull_up": {"ok": False, "confidence": 0.8, "reasons": [], "blockers": []},
            },
            "projection_candidates": [],
            "exercise_area": {},
            "visible_people": [],
            "unsafe_reasons": [],
            "recommendations": [],
        }
        return json.dumps({
            "choices": [{"message": {"content": json.dumps(decision, ensure_ascii=False)}}],
            "usage": {"total_tokens": 12},
        }, ensure_ascii=False).encode()


class _Opener:
    def __init__(self):
        self.urls = []

    def open(self, request, timeout):
        self.urls.append(request.full_url)
        if request.full_url.startswith("http://127.0.0.1"):
            raise urllib.error.URLError("offline relay")
        return _Response()


class PuCodingEnvironmentTests(unittest.TestCase):
    def test_dead_loopback_relay_falls_back_to_direct_pucoding(self):
        opener = _Opener()
        frame = {
            "ok": True,
            "role": "front_camera",
            "device": "/dev/video22",
            "image_jpeg_base64": "AA==",
        }
        with mock.patch.dict(os.environ, {
            "PUCODING_API_KEY": "test-key",
            "PUCODING_BASE_URL": "http://127.0.0.1:18888/v1/chat/completions",
        }, clear=False), mock.patch.object(MODULE.urllib.request, "build_opener", return_value=opener):
            decision, usage = MODULE.call_pucoding_vlm(
                "general", [frame], "gpt-5.4-mini",
                "http://127.0.0.1:18888/v1/chat/completions", 1.0,
            )
        self.assertEqual(decision["summary"], "画面正常")
        self.assertEqual(usage["total_tokens"], 12)
        self.assertEqual(len(opener.urls), 2)
        self.assertEqual(opener.urls[-1], MODULE.DEFAULT_PUCODING_BASE_URL)


if __name__ == "__main__":
    unittest.main()
