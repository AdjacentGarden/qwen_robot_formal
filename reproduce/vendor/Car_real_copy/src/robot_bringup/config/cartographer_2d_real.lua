include "map_builder.lua"
include "trajectory_builder.lua"

options = {
  map_builder = MAP_BUILDER,
  trajectory_builder = TRAJECTORY_BUILDER,
  map_frame = "map",
  tracking_frame = "imu_link",
  published_frame = "odom",
  odom_frame = "odom",
  provide_odom_frame = false,
  publish_frame_projected_to_2d = true,
  use_odometry = true,
  use_nav_sat = false,
  use_landmarks = false,
  num_laser_scans = 1,
  num_multi_echo_laser_scans = 0,
  num_subdivisions_per_laser_scan = 1,
  num_point_clouds = 0,
  lookup_transform_timeout_sec = 0.2,
  submap_publish_period_sec = 0.3,
  pose_publish_period_sec = 0.02,
  trajectory_publish_period_sec = 0.05,
  rangefinder_sampling_ratio = 1.0,
  odometry_sampling_ratio = 1.0,
  fixed_frame_pose_sampling_ratio = 1.0,
  imu_sampling_ratio = 1.0,
  landmarks_sampling_ratio = 1.0,
}

MAP_BUILDER.use_trajectory_builder_2d = true

-- Real robot supplies /imu; retain inertial heading/gravity observations while
-- reducing scan/constraint load below.
TRAJECTORY_BUILDER_2D.use_imu_data = true
TRAJECTORY_BUILDER_2D.min_range = 0.15
TRAJECTORY_BUILDER_2D.max_range = 10.0
TRAJECTORY_BUILDER_2D.missing_data_ray_length = -1
-- Merge two 10 Hz scans before creating a trajectory node. This substantially
-- reduces constraint work on the real robot while retaining every laser return.
TRAJECTORY_BUILDER_2D.num_accumulated_range_data = 2

-- false改成true,使用实时回环检测来进行前端的扫描匹配
TRAJECTORY_BUILDER_2D.use_online_correlative_scan_matching = true 
-- Avoid creating new nodes for tiny stationary yaw noise. Keep this tighter
-- than Cartographer's default so real turns are still corrected promptly.
TRAJECTORY_BUILDER_2D.motion_filter.max_angle_radians = math.rad(0.3)
-- 0.55改成0.65,Fast csm的最低分数，高于此分数才进行优化。
POSE_GRAPH.constraint_builder.min_score = 0.72
--0.6改成0.7,全局定位最小分数，低于此分数则认为目前全局定位不准确
POSE_GRAPH.constraint_builder.global_localization_min_score = 0.75
POSE_GRAPH.constraint_builder.max_constraint_distance = 2.0
-- Keep global loop-closure constraints enabled, but optimize a little less
-- often so low-end hardware has time to drain the constraint queue.
POSE_GRAPH.optimize_every_n_nodes = 35

return options
