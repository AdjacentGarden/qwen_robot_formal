# Head motion fault handling

The isolated head driver writes only stepper motor ID 3 packets. Chassis calls
are accepted only when both requested speeds are finite, exact zero values, and
are then suppressed before the serial port. `runtime/head_wire_audit.json`
records nonzero head packets, maximum requested head speed, and the number of
suppressed chassis stop calls.

When a non-level head command cannot be confirmed, `ros_action.py` writes
`runtime/head_motion_fault.json`. Hardware preflight then rejects every new
`head.move` command except `pose=level`. Since the whole plan is preflighted
before dispatch, meeting and exercise workflows are rejected before they stop
the lidar or start any other device.

The latch must not be cleared merely because services restarted or a level
command succeeded: an already-level head does not prove that the actuator can
move. After power, driver, and cable checks, validate one non-level movement
through the restricted `runtime/ros.sock` maintenance interface. A confirmed
non-level movement clears the latch automatically. Return the head to level
and confirm a fresh lidar scan afterward.

## 2026-09-12 observation

Four bounded `pose=up` attempts stopped at the controller's 18 second motor
deadline and were observed for about 36 seconds total per request. The latest
attempt wrote 244 nonzero motor ID 3 packets at up to speed 30 while the angle
remained near -1.9 degrees. The head was then confirmed level, the motor command
was zero, and fresh lidar scans resumed. This places the unresolved fault after
the serial write boundary, in the controller output, actuator power/driver, or
wiring path. No chassis motion packet was sent.

The ROS adapter now returns as soon as it observes both the controller's
latched timeout status and a fresh zero motor command. A first failure therefore
does not spend the former extra passive observation window; subsequent requests
remain blocked by preflight until maintenance validation succeeds.

## 2026-09-14 speed check

Historical service records contain 21 confirmed `pose=up` arrivals. Their
head-control time ranges from 5.82 to 12.72 seconds, with a 7.08 second median.
This confirms that the normal motion profile has room for later tuning.

The current actuator state cannot be used to tune that profile. A baseline
attempt sent 271 nonzero commands with a maximum magnitude of 30, while a
temporary threshold test sent 268 nonzero commands at the wire guard's maximum
magnitude of 40. In both tests the measured angle stayed near -1.93 degrees and
the controller timed out after 18 seconds. The temporary desired-rate setting
was restored from 20 to 15 degrees per second afterward. The head was confirmed
level, the motor output was zero, fresh lidar scans resumed, and the wire audit
reported zero chassis packets.

Do not raise the persistent speed profile until the actuator power, driver,
wiring, and physical attachment of the head IMU have been checked. Once motion
is restored, repeat a measured 15 versus 20 degrees-per-second comparison and
keep the faster profile only if both up and level arrivals remain stable.

A full robot reboot on 2026-09-14 did not restore motion. After the new head
process calibrated at level, one `pose=up` request sent 261 nonzero commands at
a maximum magnitude of 30. The measured angle remained near -1.88 degrees and
the controller timed out after 18 seconds. Level confirmation and fresh lidar
recovery both succeeded afterward, and the chassis packet count remained zero.
