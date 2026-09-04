import importlib.util
import pathlib
import sys
import threading
import time
import unittest


HERE = pathlib.Path(__file__).resolve().parent
MODULE_PATH = HERE / "shared_runtime_server.py"
if not MODULE_PATH.exists():
    MODULE_PATH = HERE.parent / "robot_skills" / "shared_runtime_server.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("shared_runtime_server_under_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def runtime_stub():
    runtime = MODULE.SharedRuntime.__new__(MODULE.SharedRuntime)
    runtime.head_feedback_condition = threading.Condition()
    runtime.latest_head_roll = None
    runtime.latest_head_roll_rate = None
    return runtime


def feed(runtime, samples, interval=0.05):
    def worker():
        for roll, rate in samples:
            now = time.monotonic()
            with runtime.head_feedback_condition:
                runtime.latest_head_roll_rate = (rate, now)
                runtime.latest_head_roll = (roll, now)
                runtime.head_feedback_condition.notify_all()
            time.sleep(interval)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


class HeadRateGateTests(unittest.TestCase):
    def test_legacy_angle_only_caller_does_not_require_rate(self):
        runtime = runtime_stub()
        feed(runtime, [(185.0, 4.0)] * 8)
        result = runtime.wait_for_head_target(
            185.0,
            tolerance_deg=2.0,
            stable_sec=0.15,
            timeout_sec=0.8,
            require_rate_stability=False,
        )
        self.assertTrue(result["ok"])

    def test_strict_gate_rejects_continuous_high_rate(self):
        runtime = runtime_stub()
        feed(runtime, [(185.0, 0.8)] * 10)
        result = runtime.wait_for_head_target(
            185.0,
            tolerance_deg=2.0,
            maximum_rate_dps=0.3,
            maximum_instant_rate_dps=0.5,
            stable_sec=0.15,
            rebound_guard_sec=0.1,
            timeout_sec=0.45,
            require_rate_stability=True,
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["rate_safe"])

    def test_rate_spike_resets_window_before_lidar_confirmation(self):
        runtime = runtime_stub()
        samples = (
            [(185.00, 0.10)] * 4
            + [(185.02, 2.50)]
            + [(185.01, 0.10)] * 10
        )
        started = time.monotonic()
        feed(runtime, samples)
        result = runtime.wait_for_head_target(
            185.0,
            tolerance_deg=2.0,
            maximum_rate_dps=0.3,
            maximum_instant_rate_dps=1.0,
            maximum_roll_span_deg=0.2,
            stable_sec=0.15,
            rebound_guard_sec=0.1,
            timeout_sec=1.2,
            require_rate_stability=True,
        )
        elapsed = time.monotonic() - started
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(elapsed, 0.40)
        self.assertLessEqual(result["maximum_abs_roll_rate_dps"], 1.0)
        self.assertLessEqual(result["mean_abs_roll_rate_dps"], 0.3)
        self.assertGreaterEqual(result["stable_sec"], 0.135)
        self.assertGreaterEqual(result["rebound_guard_elapsed_sec"], 0.09)

    def test_rebound_guard_does_not_tighten_original_roll_span_window(self):
        runtime = runtime_stub()
        samples = [(185.0 + index * 0.01, 0.20) for index in range(14)]
        feed(runtime, samples)
        result = runtime.wait_for_head_target(
            185.0,
            tolerance_deg=2.0,
            maximum_rate_dps=0.3,
            maximum_instant_rate_dps=1.0,
            maximum_roll_span_deg=0.05,
            stable_sec=0.20,
            rebound_guard_sec=0.15,
            timeout_sec=1.2,
            require_rate_stability=True,
        )
        self.assertTrue(result["ok"])
        self.assertLessEqual(result["stable_roll_span_deg"], 0.05)
        self.assertGreaterEqual(result["rebound_guard_elapsed_sec"], 0.135)


if __name__ == "__main__":
    unittest.main()
