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
