from __future__ import annotations

import math


GRAVITY_MS2 = 9.80665


class RobustMahonyAHRS:
    """Mahony attitude filter for SI-unit data from the exclusive IMU topic.

    Accelerometer samples outside the configured gravity window are not used
    for gravity correction, but gyro integration continues. This prevents a
    transient acceleration burst from appearing as a large head movement.
    """

    def __init__(
        self,
        Kp: float = 2.0,
        Ki: float = 0.0,
        *,
        min_accel_ms2: float = 0.75 * GRAVITY_MS2,
        max_accel_ms2: float = 1.25 * GRAVITY_MS2,
        roll_smoothing: float = 0.15,
    ) -> None:
        values = tuple(float(value) for value in (
            Kp, Ki, min_accel_ms2, max_accel_ms2, roll_smoothing))
        if not all(math.isfinite(value) for value in values):
            raise ValueError("attitude filter values must be finite")
        Kp, Ki, min_accel_ms2, max_accel_ms2, roll_smoothing = values
        if min_accel_ms2 <= 0.0 or min_accel_ms2 >= max_accel_ms2:
            raise ValueError("invalid acceleration validity window")
        self.Kp = Kp
        self.Ki = Ki
        self.min_accel_ms2 = min_accel_ms2
        self.max_accel_ms2 = max_accel_ms2
        self.roll_smoothing = max(0.0, min(1.0, roll_smoothing))
        self.q = [1.0, 0.0, 0.0, 0.0]
        self.eInt = [0.0, 0.0, 0.0]
        self.roll_last: float | None = None
        self.ready = False
        self.valid_accel_samples = 0
        self.rejected_accel_samples = 0

    def accel_is_valid(self, ax: float, ay: float, az: float) -> bool:
        if not all(math.isfinite(value) for value in (ax, ay, az)):
            return False
        norm = math.sqrt(ax * ax + ay * ay + az * az)
        return self.min_accel_ms2 <= norm <= self.max_accel_ms2

    def reset_from_accel(self, ax: float, ay: float, az: float) -> bool:
        """Reset roll/pitch from a stationary gravity sample in m/s^2."""
        norm = math.sqrt(ax * ax + ay * ay + az * az)
        if not self.min_accel_ms2 <= norm <= self.max_accel_ms2:
            return False
        roll = math.atan2(ay, az)
        pitch = math.atan2(-ax, math.sqrt(ay * ay + az * az))
        cr, sr = math.cos(roll / 2.0), math.sin(roll / 2.0)
        cp, sp = math.cos(pitch / 2.0), math.sin(pitch / 2.0)
        self.q = [cr * cp, sr * cp, cr * sp, -sr * sp]
        self.eInt = [0.0, 0.0, 0.0]
        self.roll_last = roll
        self.ready = True
        return True

    def update(
        self,
        gx: float,
        gy: float,
        gz: float,
        ax: float,
        ay: float,
        az: float,
        dt: float,
    ) -> None:
        values = tuple(float(value) for value in (gx, gy, gz, ax, ay, az))
        if not all(math.isfinite(value) for value in values):
            self.rejected_accel_samples += 1
            return
        gx, gy, gz, ax, ay, az = values
        dt = float(dt)
        if not math.isfinite(dt):
            dt = 1.0 / 208.0
        # 50 Hz nominal input has a 20 ms period. Allow normal scheduling/DDS
        # jitter without under-integrating the gyro, while still bounding a
        # delayed sample to 50 ms. The controller separately treats gaps over
        # 100 ms as discontinuities and resets its observation timers.
        dt = max(0.001, min(0.05, dt))
        accel_norm = math.sqrt(ax * ax + ay * ay + az * az)
        accel_valid = self.min_accel_ms2 <= accel_norm <= self.max_accel_ms2

        if not self.ready:
            if not accel_valid:
                self.rejected_accel_samples += 1
                return
            self.reset_from_accel(ax, ay, az)

        q0, q1, q2, q3 = self.q
        ex = ey = ez = 0.0
        if accel_valid:
            self.valid_accel_samples += 1
            ax /= accel_norm
            ay /= accel_norm
            az /= accel_norm
            vx = 2.0 * (q1 * q3 - q0 * q2)
            vy = 2.0 * (q0 * q1 + q2 * q3)
            vz = q0 * q0 - q1 * q1 - q2 * q2 + q3 * q3
            ex = ay * vz - az * vy
            ey = az * vx - ax * vz
            ez = ax * vy - ay * vx
            for index, error in enumerate((ex, ey, ez)):
                value = self.eInt[index] + error * self.Ki * dt
                self.eInt[index] = max(-0.1, min(0.1, value))
        else:
            self.rejected_accel_samples += 1

        gx += self.Kp * ex + self.eInt[0]
        gy += self.Kp * ey + self.eInt[1]
        gz += self.Kp * ez + self.eInt[2]

        half_dt = dt / 2.0
        # All four updates deliberately use the same previous quaternion.
        n0 = q0 + (-q1 * gx - q2 * gy - q3 * gz) * half_dt
        n1 = q1 + (q0 * gx + q2 * gz - q3 * gy) * half_dt
        n2 = q2 + (q0 * gy - q1 * gz + q3 * gx) * half_dt
        n3 = q3 + (q0 * gz + q1 * gy - q2 * gx) * half_dt
        norm = math.sqrt(n0 * n0 + n1 * n1 + n2 * n2 + n3 * n3)
        if not math.isfinite(norm) or norm < 1e-12:
            return
        self.q = [n0 / norm, n1 / norm, n2 / norm, n3 / norm]

    def get_euler(self) -> tuple[float, float, float]:
        q0, q1, q2, q3 = self.q
        raw_roll = math.atan2(
            2.0 * (q2 * q3 + q0 * q1),
            1.0 - 2.0 * (q1 * q1 + q2 * q2),
        )
        if self.roll_last is None or self.roll_smoothing == 0.0:
            self.roll_last = raw_roll
        else:
            delta = math.atan2(
                math.sin(raw_roll - self.roll_last),
                math.cos(raw_roll - self.roll_last),
            )
            self.roll_last += self.roll_smoothing * delta
            self.roll_last = (
                self.roll_last + math.pi
            ) % (2.0 * math.pi) - math.pi

        pitch_term = max(-1.0, min(1.0, -2.0 * q1 * q3 + 2.0 * q0 * q2))
        pitch = math.asin(pitch_term)
        yaw = math.atan2(
            2.0 * (q1 * q2 + q0 * q3),
            1.0 - 2.0 * (q2 * q2 + q3 * q3),
        )
        return (
            math.degrees(self.roll_last),
            math.degrees(pitch),
            math.degrees(yaw),
        )
