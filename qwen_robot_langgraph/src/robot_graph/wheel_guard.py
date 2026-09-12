"""Wire-level allowlist for the isolated head driver, independent of ROS/planner."""
import math
import struct


def validate_head_packet(func, data, motor_function):
    payload = bytes(data)
    if int(func) != int(motor_function) or len(payload) != 6 or payload[0] != 0 or payload[1] != 3:
        raise PermissionError("wheel_locked_packet_rejected")
    speed = struct.unpack('<f', payload[2:])[0]
    if not math.isfinite(speed) or abs(speed) > 40:
        raise PermissionError("head_speed_out_of_bounds")
    return payload


def validate_zero_wheel_stop(left, right):
    """Accept only an exact, finite zero-speed chassis initialization request."""
    if isinstance(left, bool) or isinstance(right, bool):
        raise PermissionError("wheel_locked_command_rejected")
    try:
        left_value, right_value = float(left), float(right)
    except (TypeError, ValueError):
        raise PermissionError("wheel_locked_command_rejected")
    if not math.isfinite(left_value) or not math.isfinite(right_value):
        raise PermissionError("wheel_locked_command_rejected")
    if left_value != 0.0 or right_value != 0.0:
        raise PermissionError("wheel_locked_command_rejected")
    return left_value, right_value
