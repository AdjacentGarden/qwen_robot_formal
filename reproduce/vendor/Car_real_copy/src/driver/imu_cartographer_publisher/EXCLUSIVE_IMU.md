# Exclusive ASM330LHH ownership

The robot uses `/dev/i2c-4`, address `0x6a`. Only
`imu_cartographer_publisher` may access that hardware. The head controller and
all other ROS nodes subscribe to `/imu/raw` or `/imu`.

The ASM330LHH remains configured at a 208 Hz hardware ODR, while ROS publishes
`/imu`, `/imu/raw`, `/imu/raw_counts`, and `/imu/euler_deg` at 50 Hz by default
(`sample_period=0.02`). These time-sensitive topics use ROS 2 SensorDataQoS:
`BEST_EFFORT`, `VOLATILE`, and `KEEP_LAST`. Subscribers must therefore request
`BEST_EFFORT` (or use `qos_profile_sensor_data`). Gyroscope calibration retains
its independent 5 ms read period, so lowering ROS publication frequency does
not lengthen or reduce the 1000-sample calibration.

Install the boot-time ownership service once:

```bash
cd /home/test/Car_real_copy_imu
sudo src/driver/imu_cartographer_publisher/scripts/install_imu_exclusive_service.sh
```

The service runtime-masks `iio-sensor-proxy` and unbinds `4-006a` from the
kernel `st_lsm6dsx_i2c` driver. The publisher intentionally uses `I2C_SLAVE`,
not `I2C_SLAVE_FORCE`, and refuses to start if the device is still bound.
The service deliberately does not rebind the kernel driver while the system is
shutting down. Rebinding in `ExecStop` can race with a publisher that has not
exited yet and produce a misleading ownership failure. A normal boot binds the
driver before this service acquires exclusive ownership again.

Inspect ownership:

```bash
sudo /usr/local/lib/robot-imu/imu_exclusive_control.sh status
```

To return the IMU to the desktop IIO stack, first stop the ROS publisher and
then run:

```bash
sudo systemctl disable --now robot-imu-exclusive.service
sudo /usr/local/lib/robot-imu/imu_exclusive_control.sh release
```
