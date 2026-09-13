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
