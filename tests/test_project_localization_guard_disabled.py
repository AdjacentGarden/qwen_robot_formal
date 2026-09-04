from resident_runtime_server import ResidentSkills


class _RuntimeThatRejectsProjectValidation:
    def car_feedback_snapshot(self):
        return {
            "sensor_gate_enabled": False,
            "sensor_gate_state": "disabled",
        }

    def localization_integrity_snapshot(self):
        raise AssertionError("project localization integrity must stay unused")

    def wait_for_level_scan_preflight(self, **_kwargs):
        raise AssertionError("project scan preflight must stay unused")

    def validate_localization_recovery(self, **_kwargs):
        raise AssertionError("project map validation must stay unused")


def _owner(monkeypatch):
    monkeypatch.delenv("QWEN_PROJECT_LOCALIZATION_GUARD", raising=False)
    owner = object.__new__(ResidentSkills)
    owner.runtime = _RuntimeThatRejectsProjectValidation()
    calls = []

    def set_gate(enabled, timeout=2.0):
        calls.append((enabled, timeout))
        return {"ok": True, "called": True, "state": "ready", "enabled": True}

    owner._set_lidar_live = set_gate
    return owner, calls


def test_default_recovery_delegates_once_to_car_real_copy(monkeypatch):
    owner, calls = _owner(monkeypatch)

    result = owner._recover_lidar_after_level(timeout=18.0)

    assert result["ok"] is True
    assert result["message"] == "car_real_copy_sensor_gate_authoritative"
    assert calls == [(True, 18.0)]


def test_default_supervisor_does_not_retry_gate_only_recovery(monkeypatch):
    owner, _calls = _owner(monkeypatch)

    assert owner._level_gate_recovery_needed() is False


def test_diagnostic_switch_can_restore_old_project_guard(monkeypatch):
    owner, _calls = _owner(monkeypatch)
    monkeypatch.setenv("QWEN_PROJECT_LOCALIZATION_GUARD", "1")

    assert owner._level_gate_recovery_needed() is True
