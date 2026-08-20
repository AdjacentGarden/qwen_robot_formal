from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from realtime_chat import RuntimeResourceGuard, ServiceError


class ResourceGuardTests(unittest.TestCase):
    def test_second_audio_owner_is_rejected_without_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            first = RuntimeResourceGuard(Path(directory))
            second = RuntimeResourceGuard(Path(directory))
            first.acquire()
            try:
                with self.assertRaisesRegex(ServiceError, "正被其他程序占用"):
                    second.acquire()
            finally:
                second.close()
                first.close()


if __name__ == "__main__":
    unittest.main()
