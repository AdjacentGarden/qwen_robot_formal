#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <functional>
#include <limits>
#include <memory>
#include <optional>
#include <random>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "geometry_msgs/msg/twist.hpp"
#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/laser_scan.hpp"
#include "std_msgs/msg/bool.hpp"
#include "std_msgs/msg/float64.hpp"
#include "std_msgs/msg/string.hpp"
#include "std_srvs/srv/set_bool.hpp"

namespace
{
constexpr double kPi = 3.14159265358979323846;

double normalize_angle(double angle)
{
  return std::atan2(std::sin(angle), std::cos(angle));
}

double clamp(double value, double low, double high)
{
  return std::max(low, std::min(value, high));
}

struct Point2
{
  double x;
  double y;
};

struct WallFit
{
  bool valid{false};
  double normal_angle{0.0};
  double distance{0.0};
  double span{0.0};
  double rms{0.0};
  std::size_t inliers{0};
  double inlier_ratio{0.0};
};
}  // namespace

class WallAlignmentNode : public rclcpp::Node
{
public:
  WallAlignmentNode()
  : Node("wall_alignment"), rng_(std::random_device{}())
  {
    scan_topic_ = declare_parameter<std::string>("scan_topic", "/scan");
    cmd_vel_topic_ = declare_parameter<std::string>("cmd_vel_topic", "/cmd_vel_wall_align");
    start_on_launch_ = declare_parameter<bool>("start_on_launch", false);

    lidar_yaw_offset_ = declare_parameter<double>("lidar_yaw_offset", 0.0);
    max_total_correction_ = declare_parameter<double>("max_total_correction", 0.6);
    goal_error_ = declare_parameter<double>("goal_error", 5.0 * kPi / 180.0);
    target_error_ = declare_parameter<double>("target_error", 1.0 * kPi / 180.0);

    front_fov_ = declare_parameter<double>("front_fov", 120.0 * kPi / 180.0);
    min_range_ = declare_parameter<double>("min_range", 0.25);
    max_range_ = declare_parameter<double>("max_range", 4.0);
    min_wall_distance_ = declare_parameter<double>("min_wall_distance", 0.35);
    max_wall_distance_ = declare_parameter<double>("max_wall_distance", 3.5);
    min_wall_span_ = declare_parameter<double>("min_wall_span", 0.80);
    ransac_iterations_ = declare_parameter<int>("ransac_iterations", 100);
    ransac_inlier_distance_ = declare_parameter<double>("ransac_inlier_distance", 0.035);
    min_inliers_ = declare_parameter<int>("min_inliers", 24);
    min_inlier_ratio_ = declare_parameter<double>("min_inlier_ratio", 0.30);
    max_fit_rms_ = declare_parameter<double>("max_fit_rms", 0.025);
    max_wall_normal_error_ = declare_parameter<double>("max_wall_normal_error", 0.70);

    max_scan_age_ = declare_parameter<double>("max_scan_age", 0.25);
    measurement_frames_ = declare_parameter<int>("measurement_frames", 2);
    max_measurement_spread_ = declare_parameter<double>("max_measurement_spread", 0.035);

    control_frequency_ = declare_parameter<double>("control_frequency", 20.0);
    rotation_kp_ = declare_parameter<double>("rotation_kp", 1.0);
    min_angular_speed_ = declare_parameter<double>("min_angular_speed", 0.15);
    max_angular_speed_ = declare_parameter<double>("max_angular_speed", 0.60);
    max_speed_error_threshold_ = declare_parameter<double>(
      "max_speed_error_threshold", 0.30);
    zero_publish_time_ = declare_parameter<double>("zero_publish_time", 0.10);
    task_timeout_ = declare_parameter<double>("task_timeout", 4.0);

    validate_parameters();

    cmd_pub_ = create_publisher<geometry_msgs::msg::Twist>(cmd_vel_topic_, 10);
    error_pub_ = create_publisher<std_msgs::msg::Float64>("~/yaw_error", 10);
    aligned_pub_ = create_publisher<std_msgs::msg::Bool>("~/aligned", 10);
    status_pub_ = create_publisher<std_msgs::msg::String>("~/status", 10);
    scan_sub_ = create_subscription<sensor_msgs::msg::LaserScan>(
      scan_topic_, rclcpp::SensorDataQoS().keep_last(1),
      std::bind(&WallAlignmentNode::scan_callback, this, std::placeholders::_1));
    enable_srv_ = create_service<std_srvs::srv::SetBool>(
      "~/enable", std::bind(
        &WallAlignmentNode::enable_callback, this, std::placeholders::_1,
        std::placeholders::_2));

    const auto period = std::chrono::duration<double>(1.0 / control_frequency_);
    timer_ = create_wall_timer(
      std::chrono::duration_cast<std::chrono::nanoseconds>(period),
      std::bind(&WallAlignmentNode::control_tick, this));

    state_enter_time_ = now();
    last_tick_time_ = now();
    publish_aligned(false);
    publish_status("idle");
    if (start_on_launch_) {
      start_alignment();
    }
  }

private:
  enum class State {IDLE, MEASURING, ROTATING, ZEROING};

  void validate_parameters()
  {
    auto positive = [this](double value, const char * name) {
        if (!(value > 0.0)) {
          throw std::invalid_argument(std::string(name) + " must be > 0");
        }
      };
    positive(max_total_correction_, "max_total_correction");
    positive(goal_error_, "goal_error");
    positive(target_error_, "target_error");
    positive(front_fov_, "front_fov");
    positive(control_frequency_, "control_frequency");
    positive(rotation_kp_, "rotation_kp");
    positive(max_speed_error_threshold_, "max_speed_error_threshold");
    positive(max_scan_age_, "max_scan_age");
    positive(task_timeout_, "task_timeout");
    if (front_fov_ >= kPi || min_range_ < 0.0 || max_range_ <= min_range_) {
      throw std::invalid_argument("invalid scan field-of-view/range parameters");
    }
    if (target_error_ > goal_error_) {
      throw std::invalid_argument("target_error must be <= goal_error");
    }
    if (min_wall_distance_ < 0.0 || max_wall_distance_ <= min_wall_distance_) {
      throw std::invalid_argument("invalid wall distance limits");
    }
    if (ransac_iterations_ < 1 || min_inliers_ < 2 || measurement_frames_ < 1) {
      throw std::invalid_argument("RANSAC/frame counts must be positive");
    }
    if (min_inlier_ratio_ <= 0.0 || min_inlier_ratio_ > 1.0)
    {
      throw std::invalid_argument("ratio parameters must be in (0, 1]");
    }
    if (min_angular_speed_ <= 0.0 || max_angular_speed_ < min_angular_speed_) {
      throw std::invalid_argument("invalid angular speed limits");
    }
    if (max_speed_error_threshold_ <= goal_error_) {
      throw std::invalid_argument("max_speed_error_threshold must be > goal_error");
    }
  }

  void enable_callback(
    const std::shared_ptr<std_srvs::srv::SetBool::Request> request,
    std::shared_ptr<std_srvs::srv::SetBool::Response> response)
  {
    if (request->data) {
      if (state_ != State::IDLE) {
        response->success = false;
        response->message = "wall alignment is already active";
        return;
      }
      start_alignment();
      response->success = true;
      response->message = "wall alignment started";
    } else {
      if (state_ == State::IDLE) {
        response->success = true;
        response->message = "wall alignment is already idle";
        return;
      }
      begin_zeroing("cancelled", false);
      response->success = true;
      response->message = "wall alignment cancelled";
    }
  }

  void start_alignment()
  {
    total_observed_correction_ = 0.0;
    previous_measured_error_.reset();
    measurements_.clear();
    last_scan_receive_time_.reset();
    task_succeeded_ = false;
    task_start_time_ = now();
    publish_aligned(false);
    enter_state(State::MEASURING, "measuring_wall");
    publish_zero();
  }

  void scan_callback(const sensor_msgs::msg::LaserScan::SharedPtr scan)
  {
    if (state_ != State::MEASURING && state_ != State::ROTATING) {
      return;
    }
    last_scan_receive_time_ = now();
    if (!scan_is_fresh(*scan)) {
      return;
    }
    const WallFit fit = fit_wall(*scan);
    if (!fit.valid) {
      return;
    }

    const double corrected_error = normalize_angle(fit.normal_angle + lidar_yaw_offset_);
    if (std::abs(corrected_error) > max_wall_normal_error_) {
      return;
    }
    latest_error_ = corrected_error;
    publish_error(corrected_error);

    measurements_.push_back(corrected_error);
    if (static_cast<int>(measurements_.size()) > measurement_frames_) {
      measurements_.erase(measurements_.begin());
    }
  }

  bool scan_is_fresh(const sensor_msgs::msg::LaserScan & scan) const
  {
    if (scan.header.stamp.sec == 0 && scan.header.stamp.nanosec == 0) {
      return false;
    }
    const rclcpp::Time stamp(scan.header.stamp, get_clock()->get_clock_type());
    const double age = (now() - stamp).seconds();
    return age >= -0.05 && age <= max_scan_age_;
  }

  WallFit fit_wall(const sensor_msgs::msg::LaserScan & scan)
  {
    std::vector<Point2> points;
    points.reserve(scan.ranges.size());
    const double half_fov = front_fov_ * 0.5;
    double angle = scan.angle_min;
    for (const float range : scan.ranges) {
      if (std::abs(angle) <= half_fov && std::isfinite(range) &&
        range >= min_range_ && range <= max_range_ &&
        range >= scan.range_min && range <= scan.range_max)
      {
        points.push_back({range * std::cos(angle), range * std::sin(angle)});
      }
      angle += scan.angle_increment;
    }
    if (points.size() < static_cast<std::size_t>(min_inliers_)) {
      return {};
    }

    std::uniform_int_distribution<std::size_t> pick(0, points.size() - 1);
    std::vector<std::size_t> best_indices;
    double best_span = 0.0;
    for (int iteration = 0; iteration < ransac_iterations_; ++iteration) {
      const std::size_t i = pick(rng_);
      std::size_t j = pick(rng_);
      if (i == j) {
        continue;
      }
      const double dx = points[j].x - points[i].x;
      const double dy = points[j].y - points[i].y;
      const double length = std::hypot(dx, dy);
      if (length < 0.10) {
        continue;
      }
      const double nx = -dy / length;
      const double ny = dx / length;
      const double d = nx * points[i].x + ny * points[i].y;
      std::vector<std::size_t> indices;
      indices.reserve(points.size());
      double min_projection = std::numeric_limits<double>::infinity();
      double max_projection = -std::numeric_limits<double>::infinity();
      const double tx = dx / length;
      const double ty = dy / length;
      for (std::size_t k = 0; k < points.size(); ++k) {
        if (std::abs(nx * points[k].x + ny * points[k].y - d) <= ransac_inlier_distance_) {
          indices.push_back(k);
          const double projection = tx * points[k].x + ty * points[k].y;
          min_projection = std::min(min_projection, projection);
          max_projection = std::max(max_projection, projection);
        }
      }
      const double span = indices.empty() ? 0.0 : max_projection - min_projection;
      if (indices.size() > best_indices.size() ||
        (indices.size() == best_indices.size() && span > best_span))
      {
        best_indices = std::move(indices);
        best_span = span;
      }
    }

    const double ratio = static_cast<double>(best_indices.size()) / points.size();
    if (best_indices.size() < static_cast<std::size_t>(min_inliers_) ||
      ratio < min_inlier_ratio_ || best_span < min_wall_span_)
    {
      return {};
    }

    // Total-least-squares refinement (PCA) on RANSAC inliers.
    double cx = 0.0;
    double cy = 0.0;
    for (const auto index : best_indices) {
      cx += points[index].x;
      cy += points[index].y;
    }
    cx /= best_indices.size();
    cy /= best_indices.size();
    double sxx = 0.0;
    double syy = 0.0;
    double sxy = 0.0;
    for (const auto index : best_indices) {
      const double x = points[index].x - cx;
      const double y = points[index].y - cy;
      sxx += x * x;
      syy += y * y;
      sxy += x * y;
    }
    const double line_angle = 0.5 * std::atan2(2.0 * sxy, sxx - syy);
    double nx = -std::sin(line_angle);
    double ny = std::cos(line_angle);
    double distance = nx * cx + ny * cy;
    if (distance < 0.0) {
      nx = -nx;
      ny = -ny;
      distance = -distance;
    }
    double squared_error = 0.0;
    double min_projection = std::numeric_limits<double>::infinity();
    double max_projection = -std::numeric_limits<double>::infinity();
    const double tx = std::cos(line_angle);
    const double ty = std::sin(line_angle);
    for (const auto index : best_indices) {
      const double residual = nx * points[index].x + ny * points[index].y - distance;
      squared_error += residual * residual;
      const double projection = tx * points[index].x + ty * points[index].y;
      min_projection = std::min(min_projection, projection);
      max_projection = std::max(max_projection, projection);
    }
    const double rms = std::sqrt(squared_error / best_indices.size());
    const double span = max_projection - min_projection;
    const double normal_angle = std::atan2(ny, nx);
    if (distance < min_wall_distance_ || distance > max_wall_distance_ ||
      span < min_wall_span_ || rms > max_fit_rms_)
    {
      return {};
    }
    return {true, normal_angle, distance, span, rms, best_indices.size(), ratio};
  }

  void control_tick()
  {
    const rclcpp::Time current = now();
    const double dt = clamp((current - last_tick_time_).seconds(), 0.0, 0.2);
    last_tick_time_ = current;

    if (state_ != State::IDLE && state_ != State::ZEROING &&
      (current - task_start_time_).seconds() > task_timeout_)
    {
      if (previous_measured_error_ && std::abs(latest_error_) <= goal_error_) {
        begin_zeroing("aligned_within_tolerance_at_timeout", true);
      } else {
        begin_zeroing("failed_task_timeout", false);
      }
      return;
    }

    switch (state_) {
      case State::IDLE:
        return;
      case State::MEASURING:
        publish_zero();
        handle_measurement_batch();
        break;
      case State::ROTATING:
        handle_measurement_batch();
        if (state_ != State::ROTATING) {
          break;
        }
        handle_rotation(dt);
        break;
      case State::ZEROING:
        publish_zero();
        if ((current - state_enter_time_).seconds() >= zero_publish_time_) {
          state_ = State::IDLE;
          publish_status(task_succeeded_ ? "succeeded" : terminal_status_);
        }
        break;
    }
  }

  void handle_measurement_batch()
  {
    if (static_cast<int>(measurements_.size()) < measurement_frames_) {
      return;
    }
    std::vector<double> sorted = measurements_;
    std::sort(sorted.begin(), sorted.end());
    const double median = sorted[sorted.size() / 2];
    double spread = 0.0;
    for (const double value : sorted) {
      spread = std::max(spread, std::abs(normalize_angle(value - median)));
    }
    measurements_.clear();
    if (spread > max_measurement_spread_) {
      publish_status("unstable_wall_measurement");
      return;
    }
    latest_error_ = median;
    publish_error(median);
    if (previous_measured_error_) {
      total_observed_correction_ += std::abs(
        normalize_angle(median - *previous_measured_error_));
    }
    previous_measured_error_ = median;
    publish_status(
      "measured_wall;yaw_error_rad=" + std::to_string(median) +
      ";yaw_error_deg=" + std::to_string(median * 180.0 / kPi) +
      ";observed_correction_rad=" + std::to_string(total_observed_correction_));
    if (total_observed_correction_ > max_total_correction_) {
      begin_zeroing(
        "failed_correction_limit;yaw_error_rad=" + std::to_string(median) +
        ";yaw_error_deg=" + std::to_string(median * 180.0 / kPi) +
        ";observed_correction_rad=" + std::to_string(total_observed_correction_),
        false);
      return;
    }
    if (std::abs(median) <= target_error_) {
      begin_zeroing("aligned_target_error", true);
      return;
    }
    const bool starting_continuous_correction = state_ == State::MEASURING;
    rotation_direction_ = median >= 0.0 ? 1.0 : -1.0;
    if (starting_continuous_correction) {
      enter_state(State::ROTATING, "continuous_correction");
    }
  }

  void handle_rotation(double dt)
  {
    if (dt <= 0.0) {
      publish_zero();
      return;
    }
    const double absolute_error = std::abs(latest_error_);
    double speed = max_angular_speed_;
    if (absolute_error < max_speed_error_threshold_) {
      const double error_scale = std::max(1.0, absolute_error / goal_error_);
      speed = clamp(
        min_angular_speed_ * rotation_kp_ * error_scale,
        min_angular_speed_, max_angular_speed_);
    }
    publish_angular(rotation_direction_ * speed);
  }

  void begin_zeroing(const std::string & status, bool succeeded)
  {
    task_succeeded_ = succeeded;
    terminal_status_ = status;
    publish_aligned(succeeded);
    enter_state(State::ZEROING, status);
    publish_zero();
  }

  void enter_state(State state, const std::string & status)
  {
    state_ = state;
    state_enter_time_ = now();
    publish_status(status);
  }

  void publish_zero()
  {
    cmd_pub_->publish(geometry_msgs::msg::Twist());
  }

  void publish_angular(double angular_z)
  {
    geometry_msgs::msg::Twist command;
    command.angular.z = angular_z;
    cmd_pub_->publish(command);
  }

  void publish_error(double error)
  {
    std_msgs::msg::Float64 message;
    message.data = error;
    error_pub_->publish(message);
  }

  void publish_aligned(bool aligned)
  {
    std_msgs::msg::Bool message;
    message.data = aligned;
    aligned_pub_->publish(message);
  }

  void publish_status(const std::string & status)
  {
    std_msgs::msg::String message;
    message.data = status;
    status_pub_->publish(message);
    RCLCPP_INFO(get_logger(), "Wall alignment: %s", status.c_str());
  }

  std::string scan_topic_;
  std::string cmd_vel_topic_;
  bool start_on_launch_;
  double lidar_yaw_offset_;
  double max_total_correction_;
  double goal_error_;
  double target_error_;
  double front_fov_;
  double min_range_;
  double max_range_;
  double min_wall_distance_;
  double max_wall_distance_;
  double min_wall_span_;
  int ransac_iterations_;
  double ransac_inlier_distance_;
  int min_inliers_;
  double min_inlier_ratio_;
  double max_fit_rms_;
  double max_wall_normal_error_;
  double max_scan_age_;
  int measurement_frames_;
  double max_measurement_spread_;
  double control_frequency_;
  double rotation_kp_;
  double min_angular_speed_;
  double max_angular_speed_;
  double max_speed_error_threshold_;
  double zero_publish_time_;
  double task_timeout_;

  State state_{State::IDLE};
  rclcpp::Time state_enter_time_{0, 0, RCL_ROS_TIME};
  rclcpp::Time last_tick_time_{0, 0, RCL_ROS_TIME};
  rclcpp::Time task_start_time_{0, 0, RCL_ROS_TIME};
  std::optional<rclcpp::Time> last_scan_receive_time_;
  std::vector<double> measurements_;
  double latest_error_{0.0};
  double rotation_direction_{0.0};
  double total_observed_correction_{0.0};
  std::optional<double> previous_measured_error_;
  bool task_succeeded_{false};
  std::string terminal_status_{"idle"};
  std::mt19937 rng_;

  rclcpp::Publisher<geometry_msgs::msg::Twist>::SharedPtr cmd_pub_;
  rclcpp::Publisher<std_msgs::msg::Float64>::SharedPtr error_pub_;
  rclcpp::Publisher<std_msgs::msg::Bool>::SharedPtr aligned_pub_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr status_pub_;
  rclcpp::Subscription<sensor_msgs::msg::LaserScan>::SharedPtr scan_sub_;
  rclcpp::Service<std_srvs::srv::SetBool>::SharedPtr enable_srv_;
  rclcpp::TimerBase::SharedPtr timer_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<WallAlignmentNode>());
  rclcpp::shutdown();
  return 0;
}
