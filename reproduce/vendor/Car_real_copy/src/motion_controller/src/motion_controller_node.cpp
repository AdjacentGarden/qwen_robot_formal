#include <algorithm>
#include <chrono>
#include <cmath>
#include <deque>
#include <functional>
#include <limits>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "geometry_msgs/msg/twist.hpp"
#include "geometry_msgs/msg/pose2_d.hpp"
#include "nav2_msgs/action/navigate_to_pose.hpp"
#include "nav_msgs/msg/occupancy_grid.hpp"
#include "nav_msgs/msg/odometry.hpp"
#include "motion_controller/msg/nav_goal.hpp"
#include "rclcpp/rclcpp.hpp"
#include "rclcpp_action/rclcpp_action.hpp"
#include "sensor_msgs/msg/laser_scan.hpp"
#include "sensor_msgs/msg/imu.hpp"
#include "std_msgs/msg/bool.hpp"
#include "std_msgs/msg/string.hpp"
#include "std_srvs/srv/trigger.hpp"
#include "std_srvs/srv/set_bool.hpp"
#include "tf2/time.h"
#include "tf2_ros/buffer.h"
#include "tf2_ros/transform_listener.h"

namespace
{
double clamp(double value, double minimum, double maximum)
{
  return std::max(minimum, std::min(value, maximum));
}

double approach(double current, double target, double max_change)
{
  return current + clamp(target - current, -max_change, max_change);
}

double yaw_from_odometry(const nav_msgs::msg::Odometry & odometry)
{
  const auto & q = odometry.pose.pose.orientation;
  return std::atan2(
    2.0 * (q.w * q.z + q.x * q.y),
    1.0 - 2.0 * (q.y * q.y + q.z * q.z));
}

double normalize_angle(double angle)
{
  return std::atan2(std::sin(angle), std::cos(angle));
}
}  // namespace

class MotionController : public rclcpp::Node
{
public:
  MotionController()
  : Node("motion_controller")
  {
    navigation_topic_ = declare_parameter<std::string>(
      "navigation_cmd_topic", "/cmd_vel_nav_smoothed");
    wall_topic_ = declare_parameter<std::string>(
      "wall_alignment_cmd_topic", "/cmd_vel_wall_align");
    external_topic_ = declare_parameter<std::string>(
      "external_cmd_topic", "/cmd_vel_external");
    output_topic_ = declare_parameter<std::string>("output_cmd_topic", "/cmd_vel");
    raw_scan_topic_ = declare_parameter<std::string>("raw_scan_topic", "/scan");
    gated_scan_topic_ = declare_parameter<std::string>("gated_scan_topic", "/scan_gated");
    raw_imu_topic_ = declare_parameter<std::string>("raw_imu_topic", "/imu");
    gated_imu_topic_ = declare_parameter<std::string>(
      "gated_imu_topic", "/imu_cartographer_gated");
    raw_odom_topic_ = declare_parameter<std::string>("raw_odom_topic", "/odom");
    gated_odom_topic_ = declare_parameter<std::string>(
      "gated_odom_topic", "/odom_cartographer_gated");
    rf2o_odom_topic_ = declare_parameter<std::string>("rf2o_odom_topic", "/odom_rf2o");
    sensor_gate_min_scans_ = declare_parameter<int>("sensor_gate_min_scans", 30);
    sensor_gate_stable_duration_ = declare_parameter<double>(
      "sensor_gate_stable_duration", 3.0);
    sensor_gate_disable_stable_duration_ = declare_parameter<double>(
      "sensor_gate_disable_stable_duration", 2.5);
    sensor_gate_disable_timeout_ = declare_parameter<double>(
      "sensor_gate_disable_timeout", 10.0);
    sensor_gate_disable_max_linear_velocity_ = declare_parameter<double>(
      "sensor_gate_disable_max_linear_velocity", 0.001);
    sensor_gate_disable_max_angular_velocity_ = declare_parameter<double>(
      "sensor_gate_disable_max_angular_velocity", 0.0005);
    sensor_gate_disable_max_position_step_ = declare_parameter<double>(
      "sensor_gate_disable_max_position_step", 0.0003);
    sensor_gate_disable_max_yaw_step_ = declare_parameter<double>(
      "sensor_gate_disable_max_yaw_step", 0.00035);
    sensor_gate_disable_window_duration_ = declare_parameter<double>(
      "sensor_gate_disable_window_duration", 1.0);
    sensor_gate_disable_max_window_position_change_ = declare_parameter<double>(
      "sensor_gate_disable_max_window_position_change", 0.0002);
    sensor_gate_disable_max_window_yaw_change_ = declare_parameter<double>(
      "sensor_gate_disable_max_window_yaw_change", 0.000174532925);
    sensor_gate_max_scan_gap_ = declare_parameter<double>("sensor_gate_max_scan_gap", 0.25);
    sensor_gate_max_imu_age_ = declare_parameter<double>("sensor_gate_max_imu_age", 0.25);
    sensor_gate_max_odom_age_ = declare_parameter<double>("sensor_gate_max_odom_age", 0.5);
    sensor_gate_odom_stable_duration_ = declare_parameter<double>(
      "sensor_gate_odom_stable_duration", 2.0);
    sensor_gate_max_tf_age_ = declare_parameter<double>("sensor_gate_max_tf_age", 0.5);
    sensor_gate_map_stable_duration_ = declare_parameter<double>(
      "sensor_gate_map_stable_duration", 2.0);
    sensor_gate_recovery_timeout_ = declare_parameter<double>(
      "sensor_gate_recovery_timeout", 15.0);
    sensor_gate_recovery_required_ = declare_parameter<bool>(
      "sensor_gate_recovery_required", true);
    sensor_gate_allow_external_motion_ = declare_parameter<bool>(
      "sensor_gate_allow_external_motion", false);
    nav_goal_map_margin_ = declare_parameter<double>("nav_goal_map_margin", 0.20);
    nav_goal_require_map_bounds_ = declare_parameter<bool>("nav_goal_require_map_bounds", true);
    nav_goal_topic_ = declare_parameter<std::string>("nav_goal_topic", "~/nav_goal");
    nav_goal_with_options_topic_ = declare_parameter<std::string>(
      "nav_goal_with_options_topic", "~/nav_goal_with_options");
    nav_goal_frame_ = declare_parameter<std::string>("nav_goal_frame", "map");
    nav_goal_server_timeout_ = declare_parameter<double>("nav_goal_server_timeout", 5.0);
    nav_goal_feedback_period_ = declare_parameter<double>("nav_goal_feedback_period", 1.0);
    // Kept only so old launch commands remain valid. Automatic arbitration does
    // not preselect or lock any source.
    declare_parameter<std::string>("initial_mode", "stop");
    publish_frequency_ = declare_parameter<double>("publish_frequency", 20.0);
    input_timeout_ = declare_parameter<double>("input_timeout", 0.30);
    switch_stop_duration_ = declare_parameter<double>("switch_stop_duration", 0.30);
    max_linear_velocity_ = declare_parameter<double>("max_linear_velocity", 0.60);
    max_angular_velocity_ = declare_parameter<double>("max_angular_velocity", 0.60);
    max_linear_acceleration_ = declare_parameter<double>("max_linear_acceleration", 0.80);
    max_angular_acceleration_ = declare_parameter<double>("max_angular_acceleration", 0.80);
    max_external_linear_velocity_ = declare_parameter<double>(
      "max_external_linear_velocity", 0.30);
    max_external_angular_velocity_ = declare_parameter<double>(
      "max_external_angular_velocity", 0.40);
    direction_reverse_ = declare_parameter<double>("direction_reverse", 1.0);
    command_deadband_ = declare_parameter<double>("conflict_command_deadband", 0.001);
    stop_on_conflict_ = declare_parameter<bool>("stop_on_conflict", false);
    shutdown_zero_count_ = declare_parameter<int>("shutdown_zero_count", 5);
    validate_parameters();

    const auto feedback_qos = rclcpp::QoS(1).reliable().transient_local();
    output_pub_ = create_publisher<geometry_msgs::msg::Twist>(output_topic_, 10);
    gated_scan_pub_ = create_publisher<sensor_msgs::msg::LaserScan>(
      gated_scan_topic_, rclcpp::SensorDataQoS());
    gated_imu_pub_ = create_publisher<sensor_msgs::msg::Imu>(
      gated_imu_topic_, rclcpp::SensorDataQoS());
    gated_odom_pub_ = create_publisher<nav_msgs::msg::Odometry>(
      gated_odom_topic_, rclcpp::SensorDataQoS());
    status_pub_ = create_publisher<std_msgs::msg::String>("~/status", feedback_qos);
    conflict_pub_ = create_publisher<std_msgs::msg::Bool>(
      "~/control_conflict", feedback_qos);
    lidar_enabled_pub_ = create_publisher<std_msgs::msg::Bool>(
      "~/lidar_enabled", feedback_qos);
    sensor_gate_state_pub_ = create_publisher<std_msgs::msg::String>(
      "~/sensor_gate_state", feedback_qos);
    warning_pub_ = create_publisher<std_msgs::msg::String>("~/warning", feedback_qos);
    nav_goal_status_pub_ = create_publisher<std_msgs::msg::String>(
      "~/nav_goal_status", feedback_qos);
    const auto wall_feedback_qos = rclcpp::QoS(10).reliable().transient_local();
    wall_alignment_status_pub_ = create_publisher<std_msgs::msg::String>(
      "~/wall_alignment_status", wall_feedback_qos);
    nav_action_client_ = rclcpp_action::create_client<NavigateToPose>(
      this, "/navigate_to_pose");
    tf_buffer_ = std::make_unique<tf2_ros::Buffer>(get_clock());
    tf_listener_ = std::make_shared<tf2_ros::TransformListener>(*tf_buffer_);

    navigation_sub_ = create_subscription<geometry_msgs::msg::Twist>(
      navigation_topic_, rclcpp::QoS(1).reliable(),
      [this](const geometry_msgs::msg::Twist::SharedPtr message) {
        navigation_.command = sanitize(*message);
        navigation_.received = SteadyClock::now();
        navigation_.valid = true;
      });
    wall_sub_ = create_subscription<geometry_msgs::msg::Twist>(
      wall_topic_, rclcpp::QoS(1).reliable(),
      [this](const geometry_msgs::msg::Twist::SharedPtr message) {
        wall_.command = sanitize(*message);
        wall_.received = SteadyClock::now();
        wall_.valid = true;
      });
    external_sub_ = create_subscription<geometry_msgs::msg::Twist>(
      external_topic_, rclcpp::QoS(1).reliable(),
      [this](const geometry_msgs::msg::Twist::SharedPtr message) {
        external_.command = sanitize(*message);
        external_.command.linear.x = clamp(
          external_.command.linear.x,
          -max_external_linear_velocity_, max_external_linear_velocity_);
        external_.command.angular.z = clamp(
          external_.command.angular.z,
          -max_external_angular_velocity_, max_external_angular_velocity_);
        external_.received = SteadyClock::now();
        external_.valid = true;
      });
    raw_scan_sub_ = create_subscription<sensor_msgs::msg::LaserScan>(
      raw_scan_topic_, rclcpp::SensorDataQoS(),
      [this](const sensor_msgs::msg::LaserScan::SharedPtr message) {
        if (lidar_enabled_) {
          gated_scan_pub_->publish(*message);
          const auto now = SteadyClock::now();
          if (last_gated_scan_ != SteadyClock::time_point{} &&
            std::chrono::duration<double>(now - last_gated_scan_).count() >
            sensor_gate_max_scan_gap_)
          {
            sensor_gate_scan_count_ = 0;
            sensor_gate_stable_since_ = now;
          }
          last_gated_scan_ = now;
          ++sensor_gate_scan_count_;
        }
      });
    raw_imu_sub_ = create_subscription<sensor_msgs::msg::Imu>(
      raw_imu_topic_, rclcpp::SensorDataQoS(),
      [this](const sensor_msgs::msg::Imu::SharedPtr message) {
        if (lidar_enabled_) {
          gated_imu_pub_->publish(*message);
          last_gated_imu_ = SteadyClock::now();
        }
      });
    raw_odom_sub_ = create_subscription<nav_msgs::msg::Odometry>(
      raw_odom_topic_, rclcpp::SensorDataQoS(),
      [this](const nav_msgs::msg::Odometry::SharedPtr message) {
        const auto now = SteadyClock::now();
        last_fused_odom_ = now;
        log_sensor_gate_odometry("EKF", *message, latest_ekf_odom_, sensor_gate_ekf_start_);
        update_sensor_gate_disable_stability(*message, latest_ekf_odom_, now);
        latest_ekf_odom_ = *message;
        if (lidar_enabled_ && first_fused_odom_ == SteadyClock::time_point{}) {
          first_fused_odom_ = now;
        }
        if (lidar_enabled_) {
          gated_odom_pub_->publish(*message);
        }
      });
    rf2o_odom_sub_ = create_subscription<nav_msgs::msg::Odometry>(
      rf2o_odom_topic_, rclcpp::SensorDataQoS(),
      [this](const nav_msgs::msg::Odometry::SharedPtr message) {
        const auto now = SteadyClock::now();
        last_rf2o_odom_ = now;
        log_sensor_gate_odometry("RF2O", *message, latest_rf2o_odom_, sensor_gate_rf2o_start_);
        latest_rf2o_odom_ = *message;
        if (lidar_enabled_ && first_rf2o_odom_ == SteadyClock::time_point{}) {
          first_rf2o_odom_ = now;
        }
      });
    map_sub_ = create_subscription<nav_msgs::msg::OccupancyGrid>(
      "/map", rclcpp::QoS(1).reliable().transient_local(),
      [this](const nav_msgs::msg::OccupancyGrid::SharedPtr message) {
        const auto now = SteadyClock::now();
        const bool bounds_changed = !latest_map_ ||
          latest_map_->info.width != message->info.width ||
          latest_map_->info.height != message->info.height ||
          std::abs(latest_map_->info.resolution - message->info.resolution) > 1e-9 ||
          std::abs(latest_map_->info.origin.position.x - message->info.origin.position.x) > 1e-4 ||
          std::abs(latest_map_->info.origin.position.y - message->info.origin.position.y) > 1e-4;
        latest_map_ = *message;
        last_map_ = now;
        if (bounds_changed || map_bounds_stable_since_ == SteadyClock::time_point{}) {
          map_bounds_stable_since_ = now;
        }
      });
    nav_goal_sub_ = create_subscription<geometry_msgs::msg::Pose2D>(
      nav_goal_topic_, rclcpp::QoS(1).reliable(),
      std::bind(&MotionController::nav_goal_callback, this, std::placeholders::_1));
    nav_goal_with_options_sub_ = create_subscription<motion_controller::msg::NavGoal>(
      nav_goal_with_options_topic_, rclcpp::QoS(1).reliable(),
      std::bind(
        &MotionController::nav_goal_with_options_callback, this, std::placeholders::_1));
    wall_aligned_sub_ = create_subscription<std_msgs::msg::Bool>(
      "/wall_alignment/aligned", rclcpp::QoS(10).reliable(),
      std::bind(&MotionController::wall_aligned_callback, this, std::placeholders::_1));
    wall_status_sub_ = create_subscription<std_msgs::msg::String>(
      "/wall_alignment/status", rclcpp::QoS(10).reliable(),
      std::bind(&MotionController::wall_status_callback, this, std::placeholders::_1));
    wall_enable_client_ = create_client<std_srvs::srv::SetBool>("/wall_alignment/enable");

    create_compatibility_service("~/select_navigation");
    create_compatibility_service("~/select_wall_alignment");
    create_compatibility_service("~/select_external");
    stop_service_ = create_service<std_srvs::srv::Trigger>(
      "~/stop",
      [this](const std::shared_ptr<std_srvs::srv::Trigger::Request>,
      std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
        active_source_ = Source::NONE;
        stop_latched_ = true;
        last_output_ = geometry_msgs::msg::Twist();
        publish_zero();
        publish_status("stop_latched_waiting_all_inputs_zero");
        response->success = true;
        response->message =
          "output stopped; automatic arbitration resumes after all inputs become zero/stale";
      });
    cancel_nav_goal_service_ = create_service<std_srvs::srv::Trigger>(
      "~/cancel_nav_goal",
      [this](const std::shared_ptr<std_srvs::srv::Trigger::Request>,
      std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
        if (pending_post_nav_wall_alignment_) {
          pending_post_nav_wall_alignment_ = false;
          publish_nav_goal_status("cancelled_waiting_wall_alignment");
          response->success = true;
          response->message = "pending post-navigation wall alignment cancelled";
        } else if (pending_nav_goal_) {
          pending_nav_goal_.reset();
          pending_align_to_wall_ = false;
          pending_goal_waiting_for_gate_ = false;
          publish_nav_goal_status("cancelled_pending");
          response->success = true;
          response->message = "pending navigation goal cancelled";
        } else if (nav_goal_handle_) {
          nav_action_client_->async_cancel_goal(nav_goal_handle_);
          publish_nav_goal_status("cancel_requested");
          response->success = true;
          response->message = "Nav2 goal cancellation requested";
        } else {
          response->success = false;
          response->message = "no pending or active navigation goal";
        }
      });
    align_wall_service_ = create_service<std_srvs::srv::Trigger>(
      "~/align_wall",
      [this](const std::shared_ptr<std_srvs::srv::Trigger::Request>,
      std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
        if (pending_nav_goal_ || nav_goal_in_progress_ || pending_post_nav_wall_alignment_) {
          response->success = false;
          response->message = "navigation goal is pending or active";
          return;
        }
        if (active_source_ != Source::NONE) {
          response->success = false;
          response->message =
            "motion cmd occupied by " + source_name(active_source_);
          publish_wall_alignment_status("rejected_cmd_occupied:" +
            source_name(active_source_));
          return;
        }
        response->success = request_wall_alignment(false);
        response->message = response->success ?
          "wall alignment request forwarded" :
          "wall alignment busy or /wall_alignment/enable unavailable";
      });
    cancel_wall_alignment_service_ = create_service<std_srvs::srv::Trigger>(
      "~/cancel_wall_alignment",
      [this](const std::shared_ptr<std_srvs::srv::Trigger::Request>,
      std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
        if (!wall_alignment_active_ || !wall_enable_client_->service_is_ready()) {
          response->success = false;
          response->message = "no active wall alignment or service unavailable";
          return;
        }
        auto request = std::make_shared<std_srvs::srv::SetBool::Request>();
        request->data = false;
        wall_enable_client_->async_send_request(request);
        response->success = true;
        response->message = "wall alignment cancellation forwarded";
      });
    lidar_enable_service_ = create_service<std_srvs::srv::SetBool>(
      "~/set_sensor_gate_enabled",
      [this](const std::shared_ptr<std_srvs::srv::SetBool::Request> request,
      std::shared_ptr<std_srvs::srv::SetBool::Response> response) {
        if (!request->data &&
          (pending_nav_goal_ || nav_goal_in_progress_ || pending_post_nav_wall_alignment_ ||
          wall_alignment_active_ || is_motion_command(last_output_)))
        {
          response->success = false;
          response->message = "cannot disable lidar while a motion task is active";
          return;
        }
        if (!request->data && sensor_gate_disable_pending_) {
          response->success = true;
          response->message = "sensor gate is already stopping";
          return;
        }
        if (!request->data && !lidar_enabled_) {
          response->success = true;
          response->message = "sensor gate is already disabled";
          return;
        }
        if (request->data) {
          sensor_gate_disable_pending_ = false;
          sensor_gate_disable_is_stable_ = false;
          sensor_gate_disable_stable_since_ = SteadyClock::time_point{};
          sensor_gate_disable_ekf_window_.clear();
          lidar_enabled_ = true;
          sensor_gate_ready_ = !sensor_gate_recovery_required_;
          sensor_gate_scan_count_ = 0;
          const auto now = SteadyClock::now();
          sensor_gate_recovery_started_ = now;
          sensor_gate_stable_since_ = now;
          last_gated_scan_ = SteadyClock::time_point{};
          last_gated_imu_ = SteadyClock::time_point{};
          last_rf2o_odom_ = SteadyClock::time_point{};
          last_fused_odom_ = SteadyClock::time_point{};
          first_rf2o_odom_ = SteadyClock::time_point{};
          first_fused_odom_ = SteadyClock::time_point{};
          last_map_ = SteadyClock::time_point{};
          map_bounds_stable_since_ = SteadyClock::time_point{};
          sensor_gate_timeout_reported_ = false;
          publish_sensor_gate_state(sensor_gate_ready_ ? "ready" : "recovering");
          publish_lidar_enabled();
          response->message = "sensor gate enabled; localization recovery in progress";
        } else {
          sensor_gate_disable_pending_ = true;
          sensor_gate_disable_started_ = SteadyClock::now();
          sensor_gate_rf2o_start_ = latest_rf2o_odom_;
          sensor_gate_ekf_start_ = latest_ekf_odom_;
          sensor_gate_rf2o_samples_ = 0;
          sensor_gate_ekf_samples_ = 0;
          sensor_gate_disable_stable_since_ = SteadyClock::time_point{};
          sensor_gate_disable_is_stable_ = false;
          sensor_gate_disable_ekf_window_.clear();
          publish_sensor_gate_state("stopping");
          sensor_gate_ready_ = false;
          RCLCPP_INFO(
            get_logger(),
            "[sensor_gate_monitor] shutdown observation started: stable_duration=%.3fs "
            "window=%.3fs timeout=%.3fs "
            "rf2o_baseline=%s ekf_baseline=%s",
            sensor_gate_disable_stable_duration_, sensor_gate_disable_window_duration_,
            sensor_gate_disable_timeout_,
            sensor_gate_rf2o_start_ ? "yes" : "no",
            sensor_gate_ekf_start_ ? "yes" : "no");
          response->message = "sensor gate stopping; waiting for EKF stability";
        }
        response->success = true;
      });

    const auto now = SteadyClock::now();
    switch_until_ = now;
    last_publish_time_ = now;
    const auto timer_period = std::chrono::duration<double>(1.0 / publish_frequency_);
    timer_ = create_wall_timer(
      std::chrono::duration_cast<std::chrono::nanoseconds>(timer_period),
      std::bind(&MotionController::publish_tick, this));
    publish_status("idle");
    publish_conflict(false);
    publish_warning("");
    publish_nav_goal_status("idle");
    publish_wall_alignment_status("idle");
    publish_lidar_enabled();
    // A clean process start has no stale Cartographer queue to drain. Requiring
    // a post-start /map here deadlocks the manager before it launches
    // Cartographer. Only an explicit disabled -> enabled transition recovers.
    sensor_gate_recovery_started_ = now;
    sensor_gate_stable_since_ = now;
    sensor_gate_ready_ = true;
    publish_sensor_gate_state("ready");
    publish_zero();
  }

  ~MotionController() override
  {
    for (int i = 0; i < shutdown_zero_count_; ++i) {
      publish_zero();
    }
  }

private:
  using SteadyClock = std::chrono::steady_clock;
  enum class Source {NONE, NAVIGATION, WALL_ALIGNMENT, EXTERNAL};
  using NavigateToPose = nav2_msgs::action::NavigateToPose;
  using NavGoalHandle = rclcpp_action::ClientGoalHandle<NavigateToPose>;

  struct InputState
  {
    geometry_msgs::msg::Twist command;
    SteadyClock::time_point received{};
    bool valid{false};
  };

  struct TimedPose
  {
    SteadyClock::time_point received;
    double x;
    double y;
    double yaw;
  };

  void create_compatibility_service(const std::string & name)
  {
    auto service = create_service<std_srvs::srv::Trigger>(
      name,
      [this](const std::shared_ptr<std_srvs::srv::Trigger::Request>,
      std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
        response->success = true;
        response->message =
          "automatic arbitration enabled; publish a non-zero command to request control";
        publish_status(active_source_ == Source::NONE ?
          "idle" : "active_" + source_name(active_source_));
      });
    compatibility_services_.push_back(service);
  }

  void validate_parameters() const
  {
    if (publish_frequency_ <= 0.0 || input_timeout_ <= 0.0 || switch_stop_duration_ < 0.0 ||
      max_linear_velocity_ <= 0.0 || max_angular_velocity_ <= 0.0 ||
      max_linear_acceleration_ <= 0.0 || max_angular_acceleration_ <= 0.0 ||
      max_external_linear_velocity_ <= 0.0 || max_external_angular_velocity_ <= 0.0 ||
      command_deadband_ < 0.0 || nav_goal_server_timeout_ <= 0.0 ||
      nav_goal_feedback_period_ <= 0.0 ||
      sensor_gate_min_scans_ < 1 || sensor_gate_stable_duration_ < 0.0 ||
      sensor_gate_disable_stable_duration_ <= 0.0 || sensor_gate_disable_timeout_ <= 0.0 ||
      sensor_gate_disable_max_linear_velocity_ < 0.0 ||
      sensor_gate_disable_max_angular_velocity_ < 0.0 ||
      sensor_gate_disable_max_position_step_ < 0.0 ||
      sensor_gate_disable_max_yaw_step_ < 0.0 ||
      sensor_gate_disable_window_duration_ <= 0.0 ||
      sensor_gate_disable_max_window_position_change_ < 0.0 ||
      sensor_gate_disable_max_window_yaw_change_ < 0.0 ||
      sensor_gate_max_scan_gap_ <= 0.0 || sensor_gate_max_imu_age_ <= 0.0 ||
      sensor_gate_max_odom_age_ <= 0.0 ||
      sensor_gate_odom_stable_duration_ < 0.0 || sensor_gate_max_tf_age_ <= 0.0 ||
      sensor_gate_map_stable_duration_ < 0.0 ||
      sensor_gate_recovery_timeout_ <= 0.0 || nav_goal_map_margin_ < 0.0 ||
      (direction_reverse_ != 1.0 && direction_reverse_ != -1.0) ||
      nav_goal_frame_.empty() || shutdown_zero_count_ < 1)
    {
      throw std::invalid_argument("motion controller parameters contain invalid limits");
    }
    if (navigation_topic_ == output_topic_ || wall_topic_ == output_topic_ ||
      external_topic_ == output_topic_)
    {
      throw std::invalid_argument("an input cmd_vel topic must not equal output_cmd_topic");
    }
    if (raw_scan_topic_.empty() || gated_scan_topic_.empty() ||
      raw_scan_topic_ == gated_scan_topic_)
    {
      throw std::invalid_argument("raw_scan_topic and gated_scan_topic must be non-empty and differ");
    }
    if (raw_imu_topic_.empty() || gated_imu_topic_.empty() ||
      raw_imu_topic_ == gated_imu_topic_ || raw_odom_topic_.empty() ||
      gated_odom_topic_.empty() || raw_odom_topic_ == gated_odom_topic_)
    {
      throw std::invalid_argument("sensor gate input and output topics must be non-empty and differ");
    }
    if (navigation_topic_ == wall_topic_ || navigation_topic_ == external_topic_ ||
      wall_topic_ == external_topic_)
    {
      throw std::invalid_argument("all cmd_vel input topics must differ");
    }
  }

  geometry_msgs::msg::Twist sanitize(const geometry_msgs::msg::Twist & input) const
  {
    geometry_msgs::msg::Twist output;
    if (std::isfinite(input.linear.x)) {
      output.linear.x = clamp(input.linear.x, -max_linear_velocity_, max_linear_velocity_);
    }
    if (std::isfinite(input.angular.z)) {
      output.angular.z = clamp(input.angular.z, -max_angular_velocity_, max_angular_velocity_);
    }
    return output;
  }

  bool is_motion_command(const geometry_msgs::msg::Twist & command) const
  {
    return std::abs(command.linear.x) > command_deadband_ ||
           std::abs(command.angular.z) > command_deadband_;
  }

  bool requesting(Source source, const SteadyClock::time_point & now) const
  {
    const auto & input = input_for(source);
    // Keep wall-alignment ownership for its complete task, including initial
    // measurement and terminal zero publication.
    const bool wall_task_holds_control =
      source == Source::WALL_ALIGNMENT && wall_alignment_active_;
    return input.valid &&
           std::chrono::duration<double>(now - input.received).count() <= input_timeout_ &&
           (wall_task_holds_control || is_motion_command(input.command));
  }

  const InputState & input_for(Source source) const
  {
    switch (source) {
      case Source::NAVIGATION:
        return navigation_;
      case Source::WALL_ALIGNMENT:
        return wall_;
      case Source::EXTERNAL:
        return external_;
      case Source::NONE:
      default:
        return navigation_;  // Never used for NONE.
    }
  }

  std::vector<Source> current_requests(const SteadyClock::time_point & now) const
  {
    std::vector<Source> requests;
    for (const Source source : {Source::NAVIGATION, Source::WALL_ALIGNMENT, Source::EXTERNAL}) {
      if (requesting(source, now)) {
        requests.push_back(source);
      }
    }
    return requests;
  }

  void publish_tick()
  {
    const auto now = SteadyClock::now();
    if (sensor_gate_disable_pending_) {
      const double elapsed = std::chrono::duration<double>(
        now - sensor_gate_disable_started_).count();
      if (sensor_gate_disable_is_stable_) {
        finish_sensor_gate_disable(false);
      } else if (elapsed >= sensor_gate_disable_timeout_) {
        finish_sensor_gate_disable(true);
      }
    }
    update_sensor_gate_recovery(now);
    process_pending_nav_goal(now);
    process_pending_post_nav_wall_alignment(now);
    publish_nav_goal_heartbeat(now);
    const double dt = clamp(
      std::chrono::duration<double>(now - last_publish_time_).count(), 0.0, 0.20);
    last_publish_time_ = now;
    const std::vector<Source> requests = current_requests(now);

    const bool blocked_autonomous_request = !sensor_gate_ready_ &&
      (requesting(Source::NAVIGATION, now) ||
      (!sensor_gate_allow_external_motion_ && requesting(Source::EXTERNAL, now)));
    const bool blocked_active_source = !sensor_gate_ready_ &&
      (active_source_ == Source::NAVIGATION ||
      (!sensor_gate_allow_external_motion_ && active_source_ == Source::EXTERNAL));
    if (blocked_autonomous_request || blocked_active_source) {
      active_source_ = Source::NONE;
      hard_stop("sensor_gate_not_ready");
      update_conflict(false, "");
      return;
    }

    if (stop_latched_) {
      if (requests.empty()) {
        stop_latched_ = false;
        switch_until_ = now + std::chrono::duration_cast<SteadyClock::duration>(
          std::chrono::duration<double>(switch_stop_duration_));
        hard_stop("idle");
        update_conflict(false, "");
        return;
      }
      hard_stop("stop_latched_waiting_all_inputs_zero");
      update_conflict(false, "");
      return;
    }

    if (active_source_ != Source::NONE && !requesting(active_source_, now)) {
      const std::string released = source_name(active_source_);
      active_source_ = Source::NONE;
      switch_until_ = now + std::chrono::duration_cast<SteadyClock::duration>(
        std::chrono::duration<double>(switch_stop_duration_));
      hard_stop("released_" + released);
      update_conflict(false, "");
      return;
    }

    if (active_source_ == Source::NONE) {
      if (now < switch_until_) {
        hard_stop("switching_zero_hold");
        update_conflict(false, "");
        return;
      }
      if (requests.empty()) {
        hard_stop("idle");
        update_conflict(false, "");
        return;
      }
      if (requests.size() > 1) {
        std::string reason = "simultaneous requests while idle; rejected:";
        for (const Source source : requests) {
          reason += " " + source_name(source);
        }
        hard_stop("request_conflict_no_owner");
        update_conflict(true, reason);
        return;
      }
      active_source_ = requests.front();
      publish_status("accepted_" + source_name(active_source_));
    }

    std::vector<Source> rejected;
    for (const Source source : requests) {
      if (source != active_source_) {
        rejected.push_back(source);
      }
    }
    if (!rejected.empty()) {
      std::string reason = "cmd occupied by " + source_name(active_source_) + "; rejected:";
      for (const Source source : rejected) {
        reason += " " + source_name(source);
      }
      update_conflict(true, reason);
      if (stop_on_conflict_) {
        hard_stop("conflict_stopped_active_" + source_name(active_source_));
        return;
      }
    } else {
      update_conflict(false, "");
    }

    const auto & target = input_for(active_source_).command;
    geometry_msgs::msg::Twist output;
    if (active_source_ == Source::WALL_ALIGNMENT) {
      // The wall controller already generates a bounded dynamic angular speed.
      // Forward it directly so a second acceleration controller does not alter
      // its observed closed-loop response.
      output = target;
    } else {
      output.linear.x = approach(
        last_output_.linear.x, target.linear.x, max_linear_acceleration_ * dt);
      output.angular.z = approach(
        last_output_.angular.z, target.angular.z, max_angular_acceleration_ * dt);
    }
    output = sanitize(output);
    output_pub_->publish(output);
    last_output_ = output;
    publish_status("active_" + source_name(active_source_));
  }

  void nav_goal_callback(const geometry_msgs::msg::Pose2D::SharedPtr message)
  {
    queue_nav_goal(message->x, message->y, message->theta, false);
  }

  void nav_goal_with_options_callback(
    const motion_controller::msg::NavGoal::SharedPtr message)
  {
    queue_nav_goal(message->x, message->y, message->yaw, message->align_to_wall);
  }

  void queue_nav_goal(double x, double y, double yaw, bool align_to_wall)
  {
    if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(yaw))
    {
      publish_nav_goal_status("rejected_invalid_non_finite_goal");
      publish_warning("navigation goal rejected: x, y, or yaw is not finite");
      return;
    }
    if (yaw < -180.0 || yaw > 180.0)
    {
      publish_nav_goal_status("rejected_yaw_out_of_range");
      publish_warning("navigation goal rejected: yaw must be within [-180, 180] degrees");
      return;
    }
    if (!lidar_enabled_)
    {
      publish_nav_goal_status("rejected_sensor_gate_disabled");
      publish_warning("navigation goal rejected: sensor gate is disabled");
      return;
    }
    if ((pending_nav_goal_ && !pending_goal_waiting_for_gate_) || nav_goal_in_progress_ ||
      pending_post_nav_wall_alignment_ ||
      wall_alignment_active_)
    {
      publish_nav_goal_status("rejected_navigation_goal_busy");
      publish_warning(
        "navigation goal rejected: navigation or wall alignment task is already active");
      return;
    }
    geometry_msgs::msg::Pose2D goal;
    // These goal topics are the AI-facing navigation gateway. All velocity
    // inputs, including /cmd_vel_external, bypass this conversion.
    goal.x = direction_reverse_ * x;
    goal.y = direction_reverse_ * y;
    const double yaw_radians = yaw * M_PI / 180.0;
    // External goal yaw is expressed in degrees. direction_reverse=-1
    // represents a 180-degree rotation between the
    // AI-facing coordinates and the real map. Position changes sign on both
    // axes, while heading must rotate by pi (not mirror as -yaw).
    goal.theta = direction_reverse_ < 0.0 ? yaw_radians + M_PI : yaw_radians;
    pending_nav_goal_ = goal;
    pending_align_to_wall_ = align_to_wall;
    pending_nav_goal_->theta = std::atan2(
      std::sin(pending_nav_goal_->theta), std::cos(pending_nav_goal_->theta));
    pending_goal_waiting_for_gate_ = !sensor_gate_ready_;
    pending_nav_goal_deadline_ = SteadyClock::now() +
      std::chrono::duration_cast<SteadyClock::duration>(
      std::chrono::duration<double>(nav_goal_server_timeout_));
    publish_warning("");
    if (pending_goal_waiting_for_gate_) {
      publish_nav_goal_status("cached_waiting_sensor_gate_ready");
      publish_warning("navigation goal cached: localization is recovering");
    } else {
      publish_nav_goal_status(
        align_to_wall ? "queued_waiting_nav2;align_to_wall=true" :
        "queued_waiting_nav2;align_to_wall=false");
    }
  }

  void process_pending_nav_goal(const SteadyClock::time_point & now)
  {
    if (!pending_nav_goal_) {
      return;
    }
    if (!sensor_gate_ready_) {
      return;
    }
    if (pending_goal_waiting_for_gate_) {
      pending_goal_waiting_for_gate_ = false;
      pending_nav_goal_deadline_ = now +
        std::chrono::duration_cast<SteadyClock::duration>(
        std::chrono::duration<double>(nav_goal_server_timeout_));
      publish_nav_goal_status("recovered;queued_waiting_nav2");
      publish_warning("");
    }
    if (!nav_action_client_->action_server_is_ready()) {
      if (now >= pending_nav_goal_deadline_) {
        pending_nav_goal_.reset();
        publish_nav_goal_status("rejected_nav2_unavailable");
        publish_warning("navigation goal rejected: Nav2 action server unavailable");
      }
      return;
    }
    if (nav_goal_require_map_bounds_ && !goal_inside_current_map(*pending_nav_goal_)) {
      pending_nav_goal_.reset();
      pending_align_to_wall_ = false;
      publish_nav_goal_status("rejected_goal_outside_current_map");
      publish_warning("navigation goal rejected: goal is outside current map bounds");
      return;
    }

    NavigateToPose::Goal goal;
    goal.pose.header.stamp = get_clock()->now();
    goal.pose.header.frame_id = nav_goal_frame_;
    goal.pose.pose.position.x = pending_nav_goal_->x;
    goal.pose.pose.position.y = pending_nav_goal_->y;
    goal.pose.pose.orientation.z = std::sin(pending_nav_goal_->theta * 0.5);
    goal.pose.pose.orientation.w = std::cos(pending_nav_goal_->theta * 0.5);
    current_goal_align_to_wall_ = pending_align_to_wall_;
    pending_nav_goal_.reset();
    pending_align_to_wall_ = false;
    nav_goal_in_progress_ = true;
    nav_goal_accepted_ = false;
    latest_distance_remaining_.reset();
    nav_goal_started_time_ = now;
    last_nav_heartbeat_time_ = SteadyClock::time_point{};
    publish_nav_goal_status("sending_to_nav2");

    rclcpp_action::Client<NavigateToPose>::SendGoalOptions options;
    options.goal_response_callback =
      [this](const NavGoalHandle::SharedPtr & handle) {
        if (!handle) {
          nav_goal_in_progress_ = false;
          current_goal_align_to_wall_ = false;
          publish_nav_goal_status("rejected_by_nav2");
          publish_warning("navigation goal rejected by Nav2");
          return;
        }
        nav_goal_handle_ = handle;
        nav_goal_accepted_ = true;
        publish_nav_goal_status("accepted_by_nav2");
      };
    options.feedback_callback =
      [this](NavGoalHandle::SharedPtr,
      const std::shared_ptr<const NavigateToPose::Feedback> feedback) {
        // Keep this action callback constant-time. The controller timer publishes
        // the externally visible heartbeat at exactly the configured rate.
        latest_distance_remaining_ = feedback->distance_remaining;
      };
    options.result_callback =
      [this](const NavGoalHandle::WrappedResult & result) {
        nav_goal_in_progress_ = false;
        nav_goal_accepted_ = false;
        nav_goal_handle_.reset();
        switch (result.code) {
          case rclcpp_action::ResultCode::SUCCEEDED:
            if (current_goal_align_to_wall_) {
              pending_post_nav_wall_alignment_ = true;
              publish_nav_goal_status("navigation_succeeded;waiting_navigation_release");
            } else {
              publish_nav_goal_status("succeeded");
            }
            current_goal_align_to_wall_ = false;
            break;
          case rclcpp_action::ResultCode::ABORTED:
            publish_nav_goal_status("aborted");
            publish_warning("navigation goal aborted by Nav2");
            break;
          case rclcpp_action::ResultCode::CANCELED:
            publish_nav_goal_status("cancelled");
            break;
          default:
            publish_nav_goal_status("unknown_result");
            publish_warning("navigation goal finished with unknown result");
            break;
        }
        current_goal_align_to_wall_ = false;
      };
    nav_action_client_->async_send_goal(goal, options);
  }

  void process_pending_post_nav_wall_alignment(const SteadyClock::time_point & now)
  {
    if (!pending_post_nav_wall_alignment_ || active_source_ != Source::NONE ||
      now < switch_until_ || requesting(Source::NAVIGATION, now))
    {
      return;
    }
    pending_post_nav_wall_alignment_ = false;
    publish_nav_goal_status("navigation_succeeded;starting_wall_alignment");
    if (!request_wall_alignment(true)) {
      publish_nav_goal_status("failed_to_start_wall_alignment");
    }
  }

  bool request_wall_alignment(bool for_navigation_goal)
  {
    if (wall_alignment_active_ || !wall_enable_client_->service_is_ready()) {
      publish_wall_alignment_status(
        wall_alignment_active_ ? "rejected_busy" : "rejected_service_unavailable");
      return false;
    }
    wall_alignment_active_ = true;
    wall_alignment_for_nav_goal_ = for_navigation_goal;
    publish_wall_alignment_status(
      for_navigation_goal ? "requested_after_navigation" : "requested_directly");
    auto request = std::make_shared<std_srvs::srv::SetBool::Request>();
    request->data = true;
    wall_enable_client_->async_send_request(
      request,
      [this](rclcpp::Client<std_srvs::srv::SetBool>::SharedFuture future) {
        try {
          const auto response = future.get();
          if (!response->success) {
            wall_alignment_active_ = false;
            publish_wall_alignment_status("rejected_by_wall_node:" + response->message);
            if (wall_alignment_for_nav_goal_) {
              publish_nav_goal_status("wall_alignment_rejected:" + response->message);
            }
            wall_alignment_for_nav_goal_ = false;
          } else {
            publish_wall_alignment_status("accepted_by_wall_node");
          }
        } catch (const std::exception & exception) {
          wall_alignment_active_ = false;
          publish_wall_alignment_status(
            "wall_alignment_service_error:" + std::string(exception.what()));
          if (wall_alignment_for_nav_goal_) {
            publish_nav_goal_status("wall_alignment_service_error");
          }
          wall_alignment_for_nav_goal_ = false;
        }
      });
    return true;
  }

  void wall_aligned_callback(const std_msgs::msg::Bool::SharedPtr message)
  {
    if (!wall_alignment_active_ || !message->data) {
      return;
    }
    wall_alignment_active_ = false;
    publish_wall_alignment_status("succeeded");
    if (wall_alignment_for_nav_goal_) {
      publish_nav_goal_status("succeeded;wall_aligned=true");
    }
    wall_alignment_for_nav_goal_ = false;
  }

  void wall_status_callback(const std_msgs::msg::String::SharedPtr message)
  {
    if (!wall_alignment_active_) {
      return;
    }
    const std::string status = message->data;
    publish_wall_alignment_status("running:" + status);
    if (status.rfind("failed_", 0) == 0 || status == "cancelled") {
      wall_alignment_active_ = false;
      if (wall_alignment_for_nav_goal_) {
        publish_nav_goal_status("wall_alignment_" + status);
      }
      wall_alignment_for_nav_goal_ = false;
    }
  }

  void publish_nav_goal_heartbeat(const SteadyClock::time_point & now)
  {
    if (!nav_goal_in_progress_) {
      return;
    }
    if (last_nav_heartbeat_time_.time_since_epoch().count() != 0 &&
      std::chrono::duration<double>(now - last_nav_heartbeat_time_).count() <
      nav_goal_feedback_period_)
    {
      return;
    }
    last_nav_heartbeat_time_ = now;
    const double elapsed = std::chrono::duration<double>(
      now - nav_goal_started_time_).count();
    std::string status = nav_goal_accepted_ ? "navigating" : "waiting_nav2_accept";
    status += ";elapsed_s=" + std::to_string(elapsed);
    if (latest_distance_remaining_) {
      status += ";distance_remaining_m=" + std::to_string(*latest_distance_remaining_);
    } else {
      status += ";distance_remaining_m=unknown";
    }
    publish_nav_goal_status(status, true);
  }

  void update_conflict(bool conflict, const std::string & reason)
  {
    if (conflict) {
      if (!conflict_active_ || reason != last_warning_) {
        RCLCPP_WARN(get_logger(), "%s", reason.c_str());
        publish_warning(reason);
      }
    } else if (conflict_active_) {
      RCLCPP_INFO(get_logger(), "Motion command conflict cleared");
      publish_warning("");
    }
    if (conflict != conflict_active_) {
      conflict_active_ = conflict;
      publish_conflict(conflict);
    }
  }

  void hard_stop(const std::string & reason)
  {
    last_output_ = geometry_msgs::msg::Twist();
    publish_zero();
    publish_status(reason);
  }

  void publish_zero()
  {
    if (output_pub_) {
      output_pub_->publish(geometry_msgs::msg::Twist());
    }
  }

  void publish_status(const std::string & status)
  {
    if (status == last_status_) {
      return;
    }
    last_status_ = status;
    std_msgs::msg::String message;
    message.data = status;
    status_pub_->publish(message);
    RCLCPP_INFO(get_logger(), "Motion controller: %s", status.c_str());
  }

  void publish_conflict(bool conflict)
  {
    std_msgs::msg::Bool message;
    message.data = conflict;
    conflict_pub_->publish(message);
  }

  void publish_warning(const std::string & warning)
  {
    last_warning_ = warning;
    std_msgs::msg::String message;
    message.data = warning;
    warning_pub_->publish(message);
  }

  void publish_nav_goal_status(const std::string & status, bool force = false)
  {
    if (!force && status == last_nav_goal_status_) {
      return;
    }
    last_nav_goal_status_ = status;
    std_msgs::msg::String message;
    message.data = status;
    nav_goal_status_pub_->publish(message);
    RCLCPP_INFO(get_logger(), "Navigation gateway: %s", status.c_str());
  }

  void publish_wall_alignment_status(const std::string & status)
  {
    if (status == last_wall_alignment_status_) {
      return;
    }
    last_wall_alignment_status_ = status;
    std_msgs::msg::String message;
    message.data = status;
    wall_alignment_status_pub_->publish(message);
    RCLCPP_INFO(get_logger(), "Wall alignment gateway: %s", status.c_str());
  }

  void publish_lidar_enabled()
  {
    std_msgs::msg::Bool message;
    message.data = lidar_enabled_;
    lidar_enabled_pub_->publish(message);
  }

  void log_sensor_gate_odometry(
    const char * source, const nav_msgs::msg::Odometry & current,
    const std::optional<nav_msgs::msg::Odometry> & previous,
    const std::optional<nav_msgs::msg::Odometry> & baseline)
  {
    if (!sensor_gate_disable_pending_) {
      return;
    }
    const auto & position = current.pose.pose.position;
    const double yaw = yaw_from_odometry(current);
    const double elapsed = std::chrono::duration<double>(
      SteadyClock::now() - sensor_gate_disable_started_).count();
    double step_x = 0.0;
    double step_y = 0.0;
    double step_yaw = 0.0;
    if (previous) {
      step_x = position.x - previous->pose.pose.position.x;
      step_y = position.y - previous->pose.pose.position.y;
      step_yaw = normalize_angle(yaw - yaw_from_odometry(*previous));
    }
    double total_x = 0.0;
    double total_y = 0.0;
    double total_yaw = 0.0;
    if (baseline) {
      total_x = position.x - baseline->pose.pose.position.x;
      total_y = position.y - baseline->pose.pose.position.y;
      total_yaw = normalize_angle(yaw - yaw_from_odometry(*baseline));
    }
    int & samples = source[0] == 'R' ? sensor_gate_rf2o_samples_ : sensor_gate_ekf_samples_;
    ++samples;
    const double stamp = static_cast<double>(current.header.stamp.sec) +
      static_cast<double>(current.header.stamp.nanosec) * 1e-9;
    RCLCPP_INFO(
      get_logger(),
      "[sensor_gate_monitor][%s] n=%d t=%.3f stamp=%.6f "
      "pose=(%.6f,%.6f,%.6f) step=(%.6f,%.6f,%.6f) "
      "total=(%.6f,%.6f,%.6f) twist=(vx=%.6f,vy=%.6f,wz=%.6f)",
      source, samples, elapsed, stamp, position.x, position.y, yaw,
      step_x, step_y, step_yaw, total_x, total_y, total_yaw,
      current.twist.twist.linear.x, current.twist.twist.linear.y,
      current.twist.twist.angular.z);
  }

  void log_sensor_gate_shutdown_summary()
  {
    const auto log_summary = [this](
      const char * source, const std::optional<nav_msgs::msg::Odometry> & current,
      const std::optional<nav_msgs::msg::Odometry> & baseline, int samples) {
        if (!current || !baseline) {
          RCLCPP_INFO(
            get_logger(), "[sensor_gate_monitor][%s][summary] samples=%d no complete baseline",
            source, samples);
          return;
        }
        const double dx = current->pose.pose.position.x - baseline->pose.pose.position.x;
        const double dy = current->pose.pose.position.y - baseline->pose.pose.position.y;
        const double dyaw = normalize_angle(
          yaw_from_odometry(*current) - yaw_from_odometry(*baseline));
        RCLCPP_INFO(
          get_logger(),
          "[sensor_gate_monitor][%s][summary] samples=%d delta=(x=%.6f,y=%.6f,"
          "distance=%.6f,yaw=%.6f)",
          source, samples, dx, dy, std::hypot(dx, dy), dyaw);
      };
    log_summary("RF2O", latest_rf2o_odom_, sensor_gate_rf2o_start_, sensor_gate_rf2o_samples_);
    log_summary("EKF", latest_ekf_odom_, sensor_gate_ekf_start_, sensor_gate_ekf_samples_);
  }

  void update_sensor_gate_disable_stability(
    const nav_msgs::msg::Odometry & current,
    const std::optional<nav_msgs::msg::Odometry> & previous,
    const SteadyClock::time_point & now)
  {
    if (!sensor_gate_disable_pending_) {
      return;
    }
    sensor_gate_disable_ekf_window_.push_back({
      now, current.pose.pose.position.x, current.pose.pose.position.y,
      yaw_from_odometry(current)});
    while (sensor_gate_disable_ekf_window_.size() > 1 &&
      std::chrono::duration<double>(
        now - sensor_gate_disable_ekf_window_[1].received).count() >=
      sensor_gate_disable_window_duration_)
    {
      sensor_gate_disable_ekf_window_.pop_front();
    }
    if (!previous) {
      return;
    }
    const double dx = current.pose.pose.position.x - previous->pose.pose.position.x;
    const double dy = current.pose.pose.position.y - previous->pose.pose.position.y;
    const double dyaw = normalize_angle(
      yaw_from_odometry(current) - yaw_from_odometry(*previous));
    const auto & window_start = sensor_gate_disable_ekf_window_.front();
    const double window_age = std::chrono::duration<double>(
      now - window_start.received).count();
    const double window_dx = current.pose.pose.position.x - window_start.x;
    const double window_dy = current.pose.pose.position.y - window_start.y;
    const double window_distance = std::hypot(window_dx, window_dy);
    const double window_yaw = normalize_angle(yaw_from_odometry(current) - window_start.yaw);
    const bool window_ready = window_age >= sensor_gate_disable_window_duration_;
    const auto & twist = current.twist.twist;
    const bool stable =
      window_ready &&
      std::abs(twist.linear.x) <= sensor_gate_disable_max_linear_velocity_ &&
      std::abs(twist.linear.y) <= sensor_gate_disable_max_linear_velocity_ &&
      std::abs(twist.angular.z) <= sensor_gate_disable_max_angular_velocity_ &&
      std::hypot(dx, dy) <= sensor_gate_disable_max_position_step_ &&
      std::abs(dyaw) <= sensor_gate_disable_max_yaw_step_ &&
      window_distance <= sensor_gate_disable_max_window_position_change_ &&
      std::abs(window_yaw) <= sensor_gate_disable_max_window_yaw_change_;
    if (!stable) {
      if (sensor_gate_disable_stable_since_ != SteadyClock::time_point{}) {
        RCLCPP_INFO(
          get_logger(),
          "[sensor_gate_settle] EKF stability reset: step_distance=%.6f step_yaw=%.6f "
          "window_age=%.3f window_distance=%.6f window_yaw=%.6f "
          "twist=(vx=%.6f,vy=%.6f,wz=%.6f)",
          std::hypot(dx, dy), dyaw, window_age, window_distance, window_yaw,
          twist.linear.x, twist.linear.y, twist.angular.z);
      }
      sensor_gate_disable_stable_since_ = SteadyClock::time_point{};
      return;
    }
    if (sensor_gate_disable_stable_since_ == SteadyClock::time_point{}) {
      sensor_gate_disable_stable_since_ = now;
      RCLCPP_INFO(
        get_logger(),
        "[sensor_gate_settle] EKF stable candidate started: window_distance=%.6f "
        "window_yaw=%.6f",
        window_distance, window_yaw);
      return;
    }
    const double stable_for = std::chrono::duration<double>(
      now - sensor_gate_disable_stable_since_).count();
    if (stable_for >= sensor_gate_disable_stable_duration_) {
      sensor_gate_disable_is_stable_ = true;
      RCLCPP_INFO(
        get_logger(), "[sensor_gate_settle] EKF stable for %.3fs; disabling sensor gate",
        stable_for);
    }
  }

  void finish_sensor_gate_disable(bool timed_out)
  {
    log_sensor_gate_shutdown_summary();
    sensor_gate_disable_pending_ = false;
    sensor_gate_disable_is_stable_ = false;
    sensor_gate_disable_ekf_window_.clear();
    lidar_enabled_ = false;
    sensor_gate_scan_count_ = 0;
    if (timed_out) {
      const std::string warning =
        "sensor gate disable timeout: EKF did not remain stable for " +
        std::to_string(sensor_gate_disable_stable_duration_) +
        "s within " + std::to_string(sensor_gate_disable_timeout_) +
        "s; forcing disabled";
      publish_warning(warning);
      RCLCPP_WARN(get_logger(), "%s", warning.c_str());
    }
    publish_lidar_enabled();
    publish_sensor_gate_state("disabled");
  }

  void publish_sensor_gate_state(const std::string & state)
  {
    if (state == last_sensor_gate_state_) {
      return;
    }
    last_sensor_gate_state_ = state;
    std_msgs::msg::String message;
    message.data = state;
    sensor_gate_state_pub_->publish(message);
    RCLCPP_INFO(get_logger(), "Sensor gate: %s", state.c_str());
  }

  double age_seconds(
    const SteadyClock::time_point & now, const SteadyClock::time_point & stamp) const
  {
    if (stamp == SteadyClock::time_point{}) {
      return std::numeric_limits<double>::infinity();
    }
    return std::chrono::duration<double>(now - stamp).count();
  }

  void update_sensor_gate_recovery(const SteadyClock::time_point & now)
  {
    if (sensor_gate_disable_pending_ || !lidar_enabled_ || sensor_gate_ready_) {
      return;
    }
    double tf_age = std::numeric_limits<double>::infinity();
    bool tf_ready = false;
    try {
      const auto transform = tf_buffer_->lookupTransform(
        "map", "base_footprint", tf2::TimePointZero);
      tf_age = std::max(0.0, (get_clock()->now() - transform.header.stamp).seconds());
      tf_ready = tf_age <= sensor_gate_max_tf_age_;
    } catch (const std::exception &) {
      tf_ready = false;
    }
    const bool odom_stable =
      first_rf2o_odom_ != SteadyClock::time_point{} &&
      first_fused_odom_ != SteadyClock::time_point{} &&
      std::chrono::duration<double>(now - first_rf2o_odom_).count() >=
      sensor_gate_odom_stable_duration_ &&
      std::chrono::duration<double>(now - first_fused_odom_).count() >=
      sensor_gate_odom_stable_duration_;
    const bool map_stable = map_bounds_stable_since_ != SteadyClock::time_point{} &&
      std::chrono::duration<double>(now - map_bounds_stable_since_).count() >=
      sensor_gate_map_stable_duration_;
    const bool ready = sensor_gate_scan_count_ >= sensor_gate_min_scans_ &&
      age_seconds(now, last_gated_scan_) <= sensor_gate_max_scan_gap_ &&
      age_seconds(now, last_gated_imu_) <= sensor_gate_max_imu_age_ &&
      age_seconds(now, last_rf2o_odom_) <= sensor_gate_max_odom_age_ &&
      age_seconds(now, last_fused_odom_) <= sensor_gate_max_odom_age_ &&
      odom_stable && tf_ready && map_stable &&
      last_map_ > sensor_gate_recovery_started_ &&
      std::chrono::duration<double>(now - sensor_gate_stable_since_).count() >=
      sensor_gate_stable_duration_;
    double map_min_x = std::numeric_limits<double>::quiet_NaN();
    double map_min_y = std::numeric_limits<double>::quiet_NaN();
    double map_max_x = std::numeric_limits<double>::quiet_NaN();
    double map_max_y = std::numeric_limits<double>::quiet_NaN();
    if (latest_map_) {
      map_min_x = latest_map_->info.origin.position.x;
      map_min_y = latest_map_->info.origin.position.y;
      map_max_x = map_min_x + latest_map_->info.width * latest_map_->info.resolution;
      map_max_y = map_min_y + latest_map_->info.height * latest_map_->info.resolution;
    }
    RCLCPP_INFO_THROTTLE(
      get_logger(), *get_clock(), 1000,
      "Sensor gate recovering: scans=%d/%d scan_age=%.3f imu_age=%.3f rf2o_age=%.3f "
      "odom_age=%.3f odom_stable=%s tf_age=%.3f map_age=%.3f "
      "map_stable=%s bounds=[%.3f,%.3f]-[%.3f,%.3f] pending_goal=%s",
      sensor_gate_scan_count_, sensor_gate_min_scans_, age_seconds(now, last_gated_scan_),
      age_seconds(now, last_gated_imu_),
      age_seconds(now, last_rf2o_odom_), age_seconds(now, last_fused_odom_),
      odom_stable ? "true" : "false", tf_age, age_seconds(now, last_map_),
      map_stable ? "true" : "false", map_min_x, map_min_y, map_max_x, map_max_y,
      pending_nav_goal_ ? "true" : "false");
    if (ready) {
      sensor_gate_ready_ = true;
      publish_sensor_gate_state("ready");
      publish_warning("");
      RCLCPP_INFO(
        get_logger(),
        "Sensor gate recovery ready: scans=%d scan_age=%.3f imu_age=%.3f rf2o_age=%.3f "
        "odom_age=%.3f tf_age=%.3f map_bounds=[%.3f,%.3f]-[%.3f,%.3f]",
        sensor_gate_scan_count_, age_seconds(now, last_gated_scan_),
        age_seconds(now, last_gated_imu_),
        age_seconds(now, last_rf2o_odom_), age_seconds(now, last_fused_odom_), tf_age,
        latest_map_->info.origin.position.x, latest_map_->info.origin.position.y,
        latest_map_->info.origin.position.x + latest_map_->info.width * latest_map_->info.resolution,
        latest_map_->info.origin.position.y + latest_map_->info.height * latest_map_->info.resolution);
      return;
    }
    if (!sensor_gate_timeout_reported_ &&
      std::chrono::duration<double>(now - sensor_gate_recovery_started_).count() >=
      sensor_gate_recovery_timeout_)
    {
      sensor_gate_timeout_reported_ = true;
      publish_sensor_gate_state("recovery_timeout");
      publish_warning("sensor gate recovery timeout: navigation remains blocked");
      if (pending_nav_goal_) {
        const bool outside = latest_map_ && !goal_inside_current_map(*pending_nav_goal_);
        pending_nav_goal_.reset();
        pending_align_to_wall_ = false;
        pending_goal_waiting_for_gate_ = false;
        publish_nav_goal_status(outside ?
          "rejected_goal_outside_current_map" :
          "rejected_sensor_gate_recovery_timeout");
      }
    }
  }

  bool goal_inside_current_map(const geometry_msgs::msg::Pose2D & goal) const
  {
    if (!latest_map_) {
      return false;
    }
    const auto & info = latest_map_->info;
    const double min_x = info.origin.position.x + nav_goal_map_margin_;
    const double min_y = info.origin.position.y + nav_goal_map_margin_;
    const double max_x = info.origin.position.x +
      static_cast<double>(info.width) * info.resolution - nav_goal_map_margin_;
    const double max_y = info.origin.position.y +
      static_cast<double>(info.height) * info.resolution - nav_goal_map_margin_;
    return goal.x >= min_x && goal.x <= max_x && goal.y >= min_y && goal.y <= max_y;
  }

  static std::string source_name(Source source)
  {
    switch (source) {
      case Source::NAVIGATION:
        return "navigation";
      case Source::WALL_ALIGNMENT:
        return "wall_alignment";
      case Source::EXTERNAL:
        return "external";
      case Source::NONE:
      default:
        return "none";
    }
  }

  std::string navigation_topic_;
  std::string wall_topic_;
  std::string external_topic_;
  std::string output_topic_;
  std::string raw_scan_topic_;
  std::string gated_scan_topic_;
  std::string raw_imu_topic_;
  std::string gated_imu_topic_;
  std::string raw_odom_topic_;
  std::string gated_odom_topic_;
  std::string rf2o_odom_topic_;
  std::string nav_goal_topic_;
  std::string nav_goal_with_options_topic_;
  std::string nav_goal_frame_;
  double nav_goal_server_timeout_;
  double nav_goal_feedback_period_;
  int sensor_gate_min_scans_;
  double sensor_gate_stable_duration_;
  double sensor_gate_disable_stable_duration_;
  double sensor_gate_disable_timeout_;
  double sensor_gate_disable_max_linear_velocity_;
  double sensor_gate_disable_max_angular_velocity_;
  double sensor_gate_disable_max_position_step_;
  double sensor_gate_disable_max_yaw_step_;
  double sensor_gate_disable_window_duration_;
  double sensor_gate_disable_max_window_position_change_;
  double sensor_gate_disable_max_window_yaw_change_;
  double sensor_gate_max_scan_gap_;
  double sensor_gate_max_imu_age_;
  double sensor_gate_max_odom_age_;
  double sensor_gate_odom_stable_duration_;
  double sensor_gate_max_tf_age_;
  double sensor_gate_map_stable_duration_;
  double sensor_gate_recovery_timeout_;
  bool sensor_gate_recovery_required_;
  bool sensor_gate_allow_external_motion_;
  double nav_goal_map_margin_;
  bool nav_goal_require_map_bounds_;
  double publish_frequency_;
  double input_timeout_;
  double switch_stop_duration_;
  double max_linear_velocity_;
  double max_angular_velocity_;
  double max_linear_acceleration_;
  double max_angular_acceleration_;
  double max_external_linear_velocity_;
  double max_external_angular_velocity_;
  double direction_reverse_;
  double command_deadband_;
  bool stop_on_conflict_;
  int shutdown_zero_count_;

  InputState navigation_;
  InputState wall_;
  InputState external_;
  Source active_source_{Source::NONE};
  bool stop_latched_{false};
  bool conflict_active_{false};
  bool lidar_enabled_{true};
  bool sensor_gate_ready_{false};
  bool sensor_gate_disable_pending_{false};
  bool sensor_gate_disable_is_stable_{false};
  bool sensor_gate_timeout_reported_{false};
  int sensor_gate_scan_count_{0};
  SteadyClock::time_point sensor_gate_recovery_started_{};
  SteadyClock::time_point sensor_gate_stable_since_{};
  SteadyClock::time_point sensor_gate_disable_started_{};
  SteadyClock::time_point sensor_gate_disable_stable_since_{};
  std::optional<nav_msgs::msg::Odometry> latest_rf2o_odom_;
  std::optional<nav_msgs::msg::Odometry> latest_ekf_odom_;
  std::optional<nav_msgs::msg::Odometry> sensor_gate_rf2o_start_;
  std::optional<nav_msgs::msg::Odometry> sensor_gate_ekf_start_;
  int sensor_gate_rf2o_samples_{0};
  int sensor_gate_ekf_samples_{0};
  std::deque<TimedPose> sensor_gate_disable_ekf_window_;
  SteadyClock::time_point last_gated_scan_{};
  SteadyClock::time_point last_gated_imu_{};
  SteadyClock::time_point last_rf2o_odom_{};
  SteadyClock::time_point last_fused_odom_{};
  SteadyClock::time_point first_rf2o_odom_{};
  SteadyClock::time_point first_fused_odom_{};
  SteadyClock::time_point last_map_{};
  SteadyClock::time_point map_bounds_stable_since_{};
  std::optional<nav_msgs::msg::OccupancyGrid> latest_map_;
  SteadyClock::time_point switch_until_{};
  SteadyClock::time_point last_publish_time_{};
  geometry_msgs::msg::Twist last_output_;
  std::string last_status_;
  std::string last_warning_;
  std::string last_nav_goal_status_;
  std::string last_wall_alignment_status_;
  std::string last_sensor_gate_state_;
  std::optional<geometry_msgs::msg::Pose2D> pending_nav_goal_;
  bool pending_align_to_wall_{false};
  bool pending_goal_waiting_for_gate_{false};
  bool current_goal_align_to_wall_{false};
  bool pending_post_nav_wall_alignment_{false};
  SteadyClock::time_point pending_nav_goal_deadline_{};
  bool nav_goal_in_progress_{false};
  bool nav_goal_accepted_{false};
  NavGoalHandle::SharedPtr nav_goal_handle_;
  bool wall_alignment_active_{false};
  bool wall_alignment_for_nav_goal_{false};
  std::optional<double> latest_distance_remaining_;
  SteadyClock::time_point nav_goal_started_time_{};
  SteadyClock::time_point last_nav_heartbeat_time_{};

  rclcpp::Publisher<geometry_msgs::msg::Twist>::SharedPtr output_pub_;
  rclcpp::Publisher<sensor_msgs::msg::LaserScan>::SharedPtr gated_scan_pub_;
  rclcpp::Publisher<sensor_msgs::msg::Imu>::SharedPtr gated_imu_pub_;
  rclcpp::Publisher<nav_msgs::msg::Odometry>::SharedPtr gated_odom_pub_;
  rclcpp::Publisher<std_msgs::msg::Bool>::SharedPtr lidar_enabled_pub_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr status_pub_;
  rclcpp::Publisher<std_msgs::msg::Bool>::SharedPtr conflict_pub_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr warning_pub_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr nav_goal_status_pub_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr wall_alignment_status_pub_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr sensor_gate_state_pub_;
  rclcpp::Subscription<geometry_msgs::msg::Twist>::SharedPtr navigation_sub_;
  rclcpp::Subscription<geometry_msgs::msg::Twist>::SharedPtr wall_sub_;
  rclcpp::Subscription<geometry_msgs::msg::Twist>::SharedPtr external_sub_;
  rclcpp::Subscription<sensor_msgs::msg::LaserScan>::SharedPtr raw_scan_sub_;
  rclcpp::Subscription<sensor_msgs::msg::Imu>::SharedPtr raw_imu_sub_;
  rclcpp::Subscription<nav_msgs::msg::Odometry>::SharedPtr raw_odom_sub_;
  rclcpp::Subscription<nav_msgs::msg::Odometry>::SharedPtr rf2o_odom_sub_;
  rclcpp::Subscription<nav_msgs::msg::OccupancyGrid>::SharedPtr map_sub_;
  rclcpp::Subscription<geometry_msgs::msg::Pose2D>::SharedPtr nav_goal_sub_;
  rclcpp::Subscription<motion_controller::msg::NavGoal>::SharedPtr
    nav_goal_with_options_sub_;
  rclcpp::Subscription<std_msgs::msg::Bool>::SharedPtr wall_aligned_sub_;
  rclcpp::Subscription<std_msgs::msg::String>::SharedPtr wall_status_sub_;
  rclcpp_action::Client<NavigateToPose>::SharedPtr nav_action_client_;
  std::unique_ptr<tf2_ros::Buffer> tf_buffer_;
  std::shared_ptr<tf2_ros::TransformListener> tf_listener_;
  rclcpp::Client<std_srvs::srv::SetBool>::SharedPtr wall_enable_client_;
  std::vector<rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr> compatibility_services_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr stop_service_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr cancel_nav_goal_service_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr align_wall_service_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr cancel_wall_alignment_service_;
  rclcpp::Service<std_srvs::srv::SetBool>::SharedPtr lidar_enable_service_;
  rclcpp::TimerBase::SharedPtr timer_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<MotionController>());
  rclcpp::shutdown();
  return 0;
}
