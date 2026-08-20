from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from realtime_chat import load_api_key


class RuntimeSafetyTests(unittest.TestCase):
    def test_key_file_requires_mode_600(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api_key"
            path.write_text("sk-example", encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(PermissionError, "permissions_too_open"):
                load_api_key(path)
            path.chmod(0o600)
            self.assertEqual(load_api_key(path), "sk-example")


if __name__ == "__main__":
    unittest.main()
