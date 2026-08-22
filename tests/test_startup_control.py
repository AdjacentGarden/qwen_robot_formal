from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE if (HERE / "resident_service.sh").exists() else HERE.parent


def wait_for(path: Path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def test_two_starts_share_one_live_process(tmp_path: Path) -> None:
    if shutil.which("flock") is None:
        pytest.skip("flock is provided by the robot Linux image")
    service = tmp_path / "resident_service.sh"
    shutil.copy2(PROJECT_ROOT / "resident_service.sh", service)
    run = tmp_path / "run.sh"
    run.write_text(
        "#!/usr/bin/env bash\ntrap 'exit 0' TERM INT\nwhile true; do sleep 1; done\n",
        encoding="utf-8",
    )
    run.chmod(0o755)
    first = subprocess.Popen(
        ["bash", str(service), "start"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid_file = tmp_path / "runtime/resident_service/service.pid"
    try:
        wait_for(pid_file)
        first_pid = int(pid_file.read_text().strip())
        second = subprocess.run(
            ["bash", str(service), "start"],
            text=True,
            capture_output=True,
            timeout=2,
            check=True,
        )
        result = json.loads(second.stdout)
        assert result["state"] == "starting"
        assert int(result["pid"]) == first_pid
        assert int(pid_file.read_text().strip()) == first_pid
        os.kill(first_pid, 0)
    finally:
        subprocess.run(["bash", str(service), "stop"], capture_output=True, timeout=8)
        first.wait(timeout=8)


def test_stale_pid_is_never_treated_as_our_service(tmp_path: Path) -> None:
    service = tmp_path / "resident_service.sh"
    shutil.copy2(PROJECT_ROOT / "resident_service.sh", service)
    run = tmp_path / "run.sh"
    run.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    run.chmod(0o755)
    state = tmp_path / "runtime/resident_service"
    state.mkdir(parents=True)
    state.joinpath("service.pid").write_text(str(os.getpid()), encoding="utf-8")
    result = subprocess.run(
        ["bash", str(service), "status"], text=True, capture_output=True, timeout=2
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["state"] == "stopped"
