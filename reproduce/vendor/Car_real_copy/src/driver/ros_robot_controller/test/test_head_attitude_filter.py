import math

import pytest

from ros_robot_controller.head_attitude_filter import GRAVITY_MS2, RobustMahonyAHRS


def gravity_for_roll(degrees: float) -> tuple[float, float, float]:
    value = math.radians(degrees)
    return 0.0, GRAVITY_MS2 * math.sin(value), GRAVITY_MS2 * math.cos(value)


def test_initialises_at_current_gravity_angle() -> None:
    attitude = RobustMahonyAHRS(Kp=10.0, Ki=0.008, roll_smoothing=1.0)
    assert attitude.reset_from_accel(*gravity_for_roll(162.0))
    roll, _, _ = attitude.get_euler()
    assert attitude.ready
    assert abs(roll - 162.0) < 0.1


def test_rejects_impossible_acceleration_bursts() -> None:
    attitude = RobustMahonyAHRS(Kp=10.0, Ki=0.008, roll_smoothing=1.0)
    assert attitude.reset_from_accel(*gravity_for_roll(162.0))
    before = attitude.get_euler()[0]
    for bad in (
        (0.0, 0.0, 0.0),
        (2.8 * GRAVITY_MS2, 0.0, 0.0),
        (0.0, -2.8 * GRAVITY_MS2, 0.0),
    ):
        attitude.update(0.0, 0.0, 0.0, *bad, 1 / 208)
    after = attitude.get_euler()[0]
    assert attitude.rejected_accel_samples == 3
    assert abs(after - before) < 0.01


def test_gyro_integration_continues_when_accel_is_invalid() -> None:
    attitude = RobustMahonyAHRS(Kp=10.0, Ki=0.008, roll_smoothing=1.0)
    assert attitude.reset_from_accel(0.0, 0.0, GRAVITY_MS2)
    for _ in range(10):
        attitude.update(1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.01)
    roll, _, _ = attitude.get_euler()
    assert attitude.rejected_accel_samples == 10
    assert roll > 5.0


def test_roll_smoothing_uses_short_path_across_wrap() -> None:
    attitude = RobustMahonyAHRS(roll_smoothing=0.5)
    attitude.roll_last = math.radians(179.0)
    attitude.q = [
        math.cos(math.radians(-179.0) / 2),
        math.sin(math.radians(-179.0) / 2),
        0.0,
        0.0,
    ]
    roll = attitude.get_euler()[0]
    assert abs(abs(roll) - 180.0) < 0.1


def test_integral_correction_is_bounded() -> None:
    attitude = RobustMahonyAHRS(Kp=0.0, Ki=10.0, roll_smoothing=1.0)
    assert attitude.reset_from_accel(0.0, 0.0, GRAVITY_MS2)
    for _ in range(1000):
        attitude.update(0.0, 0.0, 0.0, 0.0, GRAVITY_MS2, 0.0, 0.02)
    assert all(abs(value) <= 0.1 for value in attitude.eInt)


def test_50hz_jitter_does_not_under_integrate_gyro() -> None:
    attitude = RobustMahonyAHRS(Kp=0.0, Ki=0.0, roll_smoothing=1.0)
    assert attitude.reset_from_accel(0.0, 0.0, GRAVITY_MS2)

    # A 25 ms interval is plausible jitter around a 20 ms (50 Hz) period.
    # With 100 deg/s around X, the expected increment is approximately 2.5 deg.
    attitude.update(
        math.radians(100.0), 0.0, 0.0,
        0.0, 0.0, 0.0,
        0.025,
    )
    roll, _, _ = attitude.get_euler()
    assert roll == pytest.approx(2.5, abs=0.01)


def test_non_finite_input_does_not_poison_quaternion() -> None:
    attitude = RobustMahonyAHRS(roll_smoothing=1.0)
    assert attitude.reset_from_accel(0.0, 0.0, GRAVITY_MS2)
    before = tuple(attitude.q)
    attitude.update(float('nan'), 0.0, 0.0, 0.0, 0.0, GRAVITY_MS2, 0.01)
    assert tuple(attitude.q) == before
    assert attitude.rejected_accel_samples == 1


def test_zero_smoothing_means_unfiltered_roll_not_frozen() -> None:
    attitude = RobustMahonyAHRS(roll_smoothing=0.0)
    attitude.roll_last = 0.0
    value = math.radians(30.0)
    attitude.q = [math.cos(value / 2.0), math.sin(value / 2.0), 0.0, 0.0]
    assert abs(attitude.get_euler()[0] - 30.0) < 0.1


def test_rejects_non_finite_configuration() -> None:
    with pytest.raises(ValueError):
        RobustMahonyAHRS(Kp=float('nan'))
