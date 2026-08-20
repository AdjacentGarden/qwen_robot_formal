from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/home/test/new_project")

from new_project.reminder_cli import main


if __name__ == "__main__":
    raise SystemExit(main(Path(__file__).resolve().parent.name))
