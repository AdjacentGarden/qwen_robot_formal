#!/usr/bin/env python3
"""Harmless stage process used by startup orchestration tests."""

import argparse
import signal
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--mode", choices=("ready", "fail"), default="ready")
    args = parser.parse_args()
    if args.mode == "fail":
        time.sleep(0.15)
        return 23
    time.sleep(0.1)
    Path(args.ready_file).write_text(args.name + "\n", encoding="utf-8")
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    while running:
        time.sleep(0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
