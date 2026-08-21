from __future__ import annotations

import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any

from .models import WakeupEvent

class WakeupListener:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.process: subprocess.Popen[str] | None = None
        self.events: queue.Queue[WakeupEvent] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        wakeup = self.config["wakeup"]
        setup = " && ".join(f"source {path}" for path in self.config["paths"].get("ros_setup_files", []))
        script = Path(wakeup["listener_script"])
        command = f"{setup} && python3 {script}" if setup else f"python3 {script}"
        env = dict(os.environ)
        env["WAKEUP_TOPIC"] = wakeup["topic"]
        self.process = subprocess.Popen(
            ["bash", "-lc", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def wait(self, timeout: float | None = None) -> WakeupEvent | None:
        self.start()
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> list[WakeupEvent]:
        drained: list[WakeupEvent] = []
        while True:
            try:
                drained.append(self.events.get_nowait())
            except queue.Empty:
                return drained

    def _reader(self) -> None:
        assert self.process is not None
        event_prefix = self.config["wakeup"].get("event_prefix", "WAKEUP:")
        assert self.process.stdout is not None
        for line in self.process.stdout:
            text = line.strip()
            if text.startswith(event_prefix):
                raw_value = text.split(event_prefix, 1)[1].strip()
                self.events.put(WakeupEvent(source="ros_wakeup", raw_value=raw_value))
