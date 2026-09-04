from ros_robot_controller.head_settle_policy import (
    HeadArrivalBrakePolicy,
    HeadCommandDeadline,
    HeadSpeedProfile,
    HeadSettlePolicy,
)


def test_head_command_deadline_is_strict_and_restartable():
    deadline = HeadCommandDeadline(5.0)
    deadline.begin(now=10.0)
    assert not deadline.expired(now=14.999)
    assert deadline.expired(now=15.0)
    deadline.begin(now=20.0)
    assert not deadline.expired(now=20.0)
    deadline.finish()
    assert not deadline.expired(now=100.0)


def test_head_command_deadline_rejects_invalid_timeout():
    for value in (0.0, -1.0, float('nan')):
        try:
            HeadCommandDeadline(value)
        except ValueError:
            pass
        else:
            raise AssertionError('invalid command timeout was accepted')


def test_head_speed_profile_stops_inside_deadband():
    profile = HeadSpeedProfile()
    assert profile.desired_rate(2.0) == 0.0
    assert profile.desired_rate(-1.0) == 0.0


def test_head_speed_profile_is_fast_far_and_slow_near():
    profile = HeadSpeedProfile()
    assert profile.desired_rate(3.0) < profile.desired_rate(10.0)
    assert profile.desired_rate(10.0) < profile.desired_rate(20.0)
    assert profile.desired_rate(-20.0) == -15.0


def test_head_speed_profile_rejects_invalid_threshold_order():
    try:
        HeadSpeedProfile(deadband_deg=6.0, medium_error_deg=6.0)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid thresholds were accepted")


def test_settles_only_after_rate_is_low() -> None:
    policy = HeadSettlePolicy(185.0)
    assert not policy.update(3.0, 9.0, now=0.0)
    assert policy.update(3.0, 2.0, now=0.1)


def test_hysteresis_prevents_motor_chatter() -> None:
    policy = HeadSettlePolicy(185.0)
    assert policy.update(2.0, 1.0, now=0.0)
    for index, error in enumerate((4.5, 5.2, 6.8, 7.2, 5.9, 4.2), 1):
        assert policy.update(error, 0.2, now=index * 0.04)


def test_only_sustained_error_releases_arrival_latch() -> None:
    policy = HeadSettlePolicy(185.0, exit_hold_sec=0.20)
    assert policy.update(1.0, 0.0, now=0.0)
    assert policy.update(8.0, 0.2, now=0.10)
    assert policy.update(8.0, 0.2, now=0.29)
    assert not policy.update(8.0, 0.2, now=0.31)


def test_duplicate_target_does_not_release_latch() -> None:
    policy = HeadSettlePolicy(185.0)
    assert policy.update(1.0, 0.0, now=0.0)
    assert not policy.set_target(185.0)
    assert policy.settled
    assert policy.set_target(200.0)
    assert not policy.settled


def test_runtime_configuration_preserves_state() -> None:
    policy = HeadSettlePolicy(185.0)
    assert policy.update(1.0, 0.0, now=0.0)
    policy.configure(3.0, 6.0, 2.0, 0.3)
    assert policy.settled
    assert policy.enter_deg == 3.0
    assert policy.exit_deg == 6.0


def test_zero_exit_hold_releases_on_first_outside_sample() -> None:
    policy = HeadSettlePolicy(185.0, exit_hold_sec=0.0)
    assert policy.update(1.0, 0.0, now=0.0)
    assert not policy.update(8.0, 0.0, now=0.1)


def test_release_timer_reset_requires_new_continuous_interval() -> None:
    policy = HeadSettlePolicy(185.0, exit_hold_sec=0.20)
    assert policy.update(1.0, 0.0, now=0.0)
    assert policy.update(8.0, 0.0, now=0.10)
    policy.reset_release_timer()
    assert policy.update(8.0, 0.0, now=1.00)
    assert policy.update(8.0, 0.0, now=1.19)
    assert not policy.update(8.0, 0.0, now=1.21)


def test_equivalent_zero_and_360_targets_do_not_release_latch() -> None:
    policy = HeadSettlePolicy(0.0)
    assert policy.update(1.0, 0.0, now=0.0)
    assert not policy.set_target(360.0)
    assert policy.settled


def test_high_rate_inside_window_does_not_enter_settled() -> None:
    policy = HeadSettlePolicy(185.0)
    assert not policy.update(2.0, 4.0, now=0.0)


def test_non_finite_measurement_fails_safe() -> None:
    policy = HeadSettlePolicy(185.0)
    assert policy.update(1.0, 0.0, now=0.0)
    assert not policy.update(float('nan'), 0.0, now=0.1)
    assert not policy.settled


def test_requires_continuous_entry_hold() -> None:
    policy = HeadSettlePolicy(185.0, enter_hold_sec=0.30)
    assert not policy.update(4.0, 3.0, now=0.00)
    assert not policy.update(4.0, 3.0, now=0.29)
    assert policy.update(4.0, 3.0, now=0.30)


def test_entry_hold_restarts_after_error_or_rate_leaves_window() -> None:
    policy = HeadSettlePolicy(185.0, enter_hold_sec=0.30)
    assert not policy.update(2.0, 1.0, now=0.00)
    assert not policy.update(4.1, 1.0, now=0.29)
    assert not policy.update(2.0, 1.0, now=1.00)
    assert not policy.update(2.0, 3.1, now=1.29)
    assert not policy.update(2.0, 1.0, now=2.00)
    assert policy.update(2.0, 1.0, now=2.30)


def test_observation_timer_reset_restarts_entry_hold() -> None:
    policy = HeadSettlePolicy(185.0, enter_hold_sec=0.30)
    assert not policy.update(2.0, 1.0, now=0.00)
    assert not policy.update(2.0, 1.0, now=0.29)
    policy.reset_observation_timers()
    assert not policy.update(2.0, 1.0, now=1.00)
    assert not policy.update(2.0, 1.0, now=1.29)
    assert policy.update(2.0, 1.0, now=1.30)


def test_new_target_restarts_but_duplicate_target_preserves_entry_hold() -> None:
    policy = HeadSettlePolicy(185.0, enter_hold_sec=0.30)
    assert not policy.update(2.0, 1.0, now=0.00)
    assert not policy.set_target(185.0)
    assert policy.update(2.0, 1.0, now=0.30)

    assert policy.set_target(200.0)
    assert not policy.update(2.0, 1.0, now=1.00)
    assert not policy.update(2.0, 1.0, now=1.29)
    assert policy.update(2.0, 1.0, now=1.30)


def test_equivalent_wrapped_target_preserves_entry_hold() -> None:
    policy = HeadSettlePolicy(0.0, enter_hold_sec=0.30)
    assert not policy.update(2.0, 1.0, now=0.00)
    assert not policy.set_target(360.0)
    assert policy.update(2.0, 1.0, now=0.30)


def test_non_finite_measurement_clears_entry_hold() -> None:
    policy = HeadSettlePolicy(185.0, enter_hold_sec=0.30)
    assert not policy.update(2.0, 1.0, now=0.00)
    assert not policy.update(float('inf'), 1.0, now=0.29)
    assert not policy.update(2.0, 1.0, now=1.00)
    assert not policy.update(2.0, 1.0, now=1.29)
    assert policy.update(2.0, 1.0, now=1.30)


def test_time_reversal_restarts_entry_hold() -> None:
    policy = HeadSettlePolicy(185.0, enter_hold_sec=0.30)
    assert not policy.update(2.0, 1.0, now=10.00)
    assert not policy.update(2.0, 1.0, now=9.00)
    assert not policy.update(2.0, 1.0, now=9.29)
    assert policy.update(2.0, 1.0, now=9.30)


def test_same_configuration_preserves_entry_hold() -> None:
    policy = HeadSettlePolicy(185.0, enter_hold_sec=0.30)
    assert not policy.update(2.0, 1.0, now=0.00)
    policy.configure(4.0, 7.0, 3.0, 0.20, 0.30)
    assert policy.update(2.0, 1.0, now=0.30)


def test_changed_entry_configuration_restarts_entry_hold() -> None:
    policy = HeadSettlePolicy(185.0, enter_hold_sec=0.30)
    assert not policy.update(2.0, 1.0, now=0.00)
    policy.configure(3.5, 7.0, 3.0, 0.20, 0.30)
    assert not policy.update(2.0, 1.0, now=0.30)
    assert policy.update(2.0, 1.0, now=0.60)


def test_non_finite_configuration_is_rejected() -> None:
    policy = HeadSettlePolicy(185.0)
    try:
        policy.configure(4.0, 7.0, 3.0, 0.20, float('nan'))
    except ValueError:
        pass
    else:
        raise AssertionError('non-finite configuration must be rejected')


def test_motion_restart_releases_latch_inside_angle_window() -> None:
    policy = HeadSettlePolicy(
        185.0,
        motion_restart_rate_dps=1.0,
    )
    assert policy.update(1.0, 0.0, now=0.0)
    assert policy.update(1.0, 1.0, now=0.1)
    assert not policy.update(1.0, 1.01, now=0.2)


def test_default_policy_does_not_restart_from_rate_alone() -> None:
    policy = HeadSettlePolicy(185.0)
    assert policy.update(1.0, 0.0, now=0.0)
    assert policy.update(1.0, 100.0, now=0.1)


def test_arrival_brake_has_rate_hysteresis() -> None:
    brake = HeadArrivalBrakePolicy(
        engage_rate_dps=1.0,
        release_rate_dps=0.3,
    )
    assert not brake.update(2.0, 0.9, 4.0, False)
    assert brake.update(2.0, 1.0, 4.0, False)
    assert brake.update(2.0, 0.31, 4.0, False)
    assert not brake.update(2.0, 0.30, 4.0, False)


def test_arrival_brake_only_operates_inside_window_while_unsettled() -> None:
    brake = HeadArrivalBrakePolicy()
    assert not brake.update(4.1, 3.0, 4.0, False)
    assert brake.update(4.0, 3.0, 4.0, False)
    assert not brake.update(3.0, 3.0, 4.0, True)


def test_arrival_brake_enforces_minimum_in_braking_direction() -> None:
    brake = HeadArrivalBrakePolicy()
    assert brake.update(2.0, 1.2, 4.0, False)
    assert brake.apply_minimum_command(2.4, 1.2, 8.0) == 8.0
    brake.reset()
    assert brake.update(-2.0, -1.2, 4.0, False)
    assert brake.apply_minimum_command(-2.4, -1.2, 8.0) == -8.0


def test_arrival_brake_fails_safe_on_non_finite_input() -> None:
    brake = HeadArrivalBrakePolicy()
    assert brake.update(2.0, 2.0, 4.0, False)
    assert brake.apply_minimum_command(
        2.0, float('nan'), 8.0) == 0.0
    assert not brake.active


def test_zero_engage_rate_disables_arrival_brake() -> None:
    brake = HeadArrivalBrakePolicy(
        engage_rate_dps=0.0,
        release_rate_dps=0.0,
    )
    assert not brake.update(2.0, 100.0, 4.0, False)
    assert brake.apply_minimum_command(3.0, 100.0, 8.0) == 3.0


def test_same_arrival_brake_configuration_preserves_active_state() -> None:
    brake = HeadArrivalBrakePolicy()
    assert brake.update(2.0, 2.0, 4.0, False)
    brake.configure(1.0, 0.3)
    assert brake.active


def test_entry_gate_requires_full_hold_after_braking_ends() -> None:
    policy = HeadSettlePolicy(185.0, enter_hold_sec=0.50)
    assert not policy.update(
        2.0, 0.4, now=0.00, entry_allowed=False)
    assert not policy.update(
        2.0, 0.2, now=1.00, entry_allowed=True)
    assert not policy.update(
        2.0, 0.2, now=1.49, entry_allowed=True)
    assert policy.update(
        2.0, 0.2, now=1.50, entry_allowed=True)


def test_arrival_brake_releases_immediately_on_rate_sign_reversal() -> None:
    brake = HeadArrivalBrakePolicy()
    assert brake.update(2.0, 1.2, 4.0, False)
    assert not brake.update(2.0, -1.2, 4.0, False)
    assert brake.apply_minimum_command(-2.4, -1.2, 8.0) == -2.4
    assert brake.update(2.0, -1.2, 4.0, False)
    assert brake.apply_minimum_command(-2.4, -1.2, 8.0) == -8.0


def test_motion_restart_requires_sustained_rate_when_hold_is_nonzero() -> None:
    policy = HeadSettlePolicy(
        185.0,
        motion_restart_rate_dps=1.0,
        motion_restart_hold_sec=0.05,
    )
    assert policy.update(1.0, 0.0, now=0.00)
    assert policy.update(1.0, 1.1, now=0.10)
    assert policy.update(1.0, 0.2, now=0.14)
    assert policy.update(1.0, 1.1, now=1.00)
    assert policy.update(1.0, 1.1, now=1.049)
    assert not policy.update(1.0, 1.1, now=1.05)
