#!/usr/bin/env python3
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent

from face_common import run_recognition_cli


if __name__ == "__main__":
    run_recognition_cli(SKILL_DIR)
