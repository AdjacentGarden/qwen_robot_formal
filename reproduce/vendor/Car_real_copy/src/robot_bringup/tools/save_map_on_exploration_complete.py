#!/usr/bin/env python3
"""Save map artifacts once frontier exploration publishes completion."""

from __future__ import annotations

import os
import math
import re
import shutil
from collections import deque
from functools import lru_cache
from pathlib import Path

import rclpy
from action_msgs.msg import GoalStatus
from cartographer_ros_msgs.srv import FinishTrajectory, ReadMetrics, WriteState
from frontier_exploration_ros2.srv import ControlExploration
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose
from nav2_msgs.srv import SaveMap
from rcl_interfaces.msg import Log
from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType
from rclpy.duration import Duration
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import Empty, String
from tf2_ros import Buffer, TransformException, TransformListener
from visualization_msgs.msg import Marker, MarkerArray


def _workspace_from_path(path: Path):
    """Return the colcon workspace containing this robot_bringup install/source."""
    resolved = path.expanduser().resolve()
    for parent in (resolved, *resolved.parents):
        if parent.name == "install":
            candidate = parent.parent
            if (candidate / "src" / "robot_bringup").is_dir():
                return candidate
    for parent in (resolved, *resolved.parents):
        if (parent / "src" / "robot_bringup").is_dir():
            return parent
    return None


@lru_cache(maxsize=1)
def workspace_root() -> Path:
    # AMENT_PREFIX_PATH follows overlay order, so the first matching package is
    # the robot_bringup selected by the install/setup.bash sourced by the user.
    for prefix_text in os.environ.get("AMENT_PREFIX_PATH", "").split(os.pathsep):
        if not prefix_text:
            continue
        prefix = Path(prefix_text)
        package_marker = (
            prefix / "share" / "ament_index" / "resource_index" /
            "packages" / "robot_bringup"
        )
        if not package_marker.is_file():
            continue
        workspace = _workspace_from_path(prefix)
        if workspace is not None:
            return workspace

    # Direct source execution and a copied install still work as long as the
    # executable remains somewhere below the workspace root.
    workspace = _workspace_from_path(Path(__file__))
    if workspace is not None:
        return workspace

    # Never fall back to the caller's current directory: doing so can silently
    # save into an unrelated home directory when the manager is launched there.
    raise RuntimeError(
        "Cannot locate the colcon workspace containing src/robot_bringup. "
        "Source that workspace's install/setup.bash before launching."
    )


def resolve_workspace_path(path: str) -> str:
    expanded = Path(os.path.expanduser(path))
    if expanded.is_absolute():
        return str(expanded)
    return str(workspace_root() / expanded)


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


class SaveMapOnExplorationComplete(Node):
    def __init__(self):
        super().__init__("save_map_on_exploration_complete")

        self.declare_parameter("completion_topic", "exploration_complete")
        self.declare_parameter("frontier_marker_topic", "/explore/frontiers")
        self.declare_parameter("result_topic", "/mapping/save_result")
        self.declare_parameter("rosout_topic", "/rosout")
        self.declare_parameter("save_map_path", "src/car_nav2/maps/exploration_map")
        self.declare_parameter("pbstream_path", "src/robot_bringup/map/map.pbstream")
        self.declare_parameter(
            "install_map_path", "install/car_nav2/share/car_nav2/maps/exploration_map")
        self.declare_parameter(
            "install_pbstream_path", "install/robot_bringup/share/robot_bringup/map/map.pbstream")
        self.declare_parameter("finish_trajectory_service", "/finish_trajectory")
        self.declare_parameter("write_state_service", "/write_state")
        self.declare_parameter("read_metrics_service", "/read_metrics")
        self.declare_parameter("map_saver_service", "/map_saver/save_map")
        self.declare_parameter("map_topic", "/map")
        self.declare_parameter("map_image_format", "pgm")
        self.declare_parameter("map_mode", "trinary")
        self.declare_parameter("map_free_thresh", 0.25)
        self.declare_parameter("map_occupied_thresh", 0.65)
        self.declare_parameter("exploration_control_service", "/control_exploration")
        self.declare_parameter("required_exploration_rounds", 2)
        self.declare_parameter("exploration_restart_delay_s", 2.0)
        self.declare_parameter("exploration_restart_timeout_s", 30.0)
        self.declare_parameter("navigate_to_pose_action_name", "navigate_to_pose")
        self.declare_parameter("global_frame", "map")
        self.declare_parameter("robot_base_frame", "base_footprint")
        self.declare_parameter("trajectory_id", 0)
        self.declare_parameter("include_unfinished_submaps", True)
        self.declare_parameter("settle_delay_s", 2.0)
        self.declare_parameter("service_availability_timeout_s", 10.0)
        self.declare_parameter("service_timeout_s", 120.0)
        self.declare_parameter("return_to_start_before_save", True)
        self.declare_parameter("return_to_start_timeout_s", 120.0)
        self.declare_parameter("return_to_start_server_timeout_s", 10.0)
        self.declare_parameter("return_to_start_max_attempts", 3)
        self.declare_parameter("return_to_start_retry_delay_s", 2.0)
        self.declare_parameter("start_pose_lookup_timeout_s", 5.0)
        self.declare_parameter("save_on_return_failure", False)
        self.declare_parameter("save_occupancy_grid", True)
        self.declare_parameter("finish_cartographer_trajectory", True)
        self.declare_parameter("write_cartographer_state", True)
        self.declare_parameter("min_cartographer_score_mean", 0.65)
        self.declare_parameter("skip_save_if_quality_low", False)
        self.declare_parameter("require_quality_score", False)
        self.declare_parameter("max_reported_frontiers", 30)
        self.declare_parameter("debug_quality_logs", True)
        self.declare_parameter("quality_debug_log_limit", 20)

        self.completion_topic = self.get_parameter("completion_topic").value
        self.frontier_marker_topic = self.get_parameter("frontier_marker_topic").value
        self.result_topic = self.get_parameter("result_topic").value
        self.rosout_topic = self.get_parameter("rosout_topic").value
        self.get_logger().info(f"Resolved robot workspace: {workspace_root()}")
        self.save_map_path = resolve_workspace_path(self.get_parameter("save_map_path").value)
        self.pbstream_path = resolve_workspace_path(self.get_parameter("pbstream_path").value)
        self.install_map_path = resolve_workspace_path(
            self.get_parameter("install_map_path").value)
        self.install_pbstream_path = resolve_workspace_path(
            self.get_parameter("install_pbstream_path").value)
        self.finish_service_name = self.get_parameter("finish_trajectory_service").value
        self.write_state_service_name = self.get_parameter("write_state_service").value
        self.read_metrics_service_name = self.get_parameter("read_metrics_service").value
        self.map_saver_service_name = self.get_parameter("map_saver_service").value
        self.map_topic = self.get_parameter("map_topic").value
        self.map_image_format = self.get_parameter("map_image_format").value
        self.map_mode = self.get_parameter("map_mode").value
        self.map_free_thresh = float(self.get_parameter("map_free_thresh").value)
        self.map_occupied_thresh = float(
            self.get_parameter("map_occupied_thresh").value)
        self.exploration_control_service_name = self.get_parameter(
            "exploration_control_service").value
        self.required_exploration_rounds = max(
            1, int(self.get_parameter("required_exploration_rounds").value))
        self.exploration_restart_delay_s = max(
            0.0, float(self.get_parameter("exploration_restart_delay_s").value))
        self.exploration_restart_timeout_s = max(
            1.0, float(self.get_parameter("exploration_restart_timeout_s").value))
        self.navigate_to_pose_action_name = self.get_parameter(
            "navigate_to_pose_action_name").value
        self.global_frame = self.get_parameter("global_frame").value
        self.robot_base_frame = self.get_parameter("robot_base_frame").value
        self.trajectory_id = int(self.get_parameter("trajectory_id").value)
        self.include_unfinished_submaps = as_bool(
            self.get_parameter("include_unfinished_submaps").value)
        self.settle_delay_s = float(self.get_parameter("settle_delay_s").value)
        self.service_availability_timeout_s = max(
            1.0, float(self.get_parameter("service_availability_timeout_s").value))
        self.service_timeout_s = max(
            1.0, float(self.get_parameter("service_timeout_s").value))
        self.return_to_start_before_save = as_bool(
            self.get_parameter("return_to_start_before_save").value)
        self.return_to_start_timeout_s = float(
            self.get_parameter("return_to_start_timeout_s").value)
        self.return_to_start_server_timeout_s = float(
            self.get_parameter("return_to_start_server_timeout_s").value)
        self.return_to_start_max_attempts = max(
            1,
            int(self.get_parameter("return_to_start_max_attempts").value),
        )
        self.return_to_start_retry_delay_s = float(
            self.get_parameter("return_to_start_retry_delay_s").value)
        self.start_pose_lookup_timeout_s = float(
            self.get_parameter("start_pose_lookup_timeout_s").value)
        self.save_on_return_failure = as_bool(self.get_parameter("save_on_return_failure").value)
        self.save_occupancy_grid = as_bool(self.get_parameter("save_occupancy_grid").value)
        self.finish_cartographer_trajectory = as_bool(
            self.get_parameter("finish_cartographer_trajectory").value)
        self.write_cartographer_state = as_bool(self.get_parameter("write_cartographer_state").value)
        self.min_cartographer_score_mean = float(
            self.get_parameter("min_cartographer_score_mean").value)
        self.skip_save_if_quality_low = as_bool(
            self.get_parameter("skip_save_if_quality_low").value)
        self.require_quality_score = as_bool(self.get_parameter("require_quality_score").value)
        self.max_reported_frontiers = int(self.get_parameter("max_reported_frontiers").value)
        self.debug_quality_logs = as_bool(self.get_parameter("debug_quality_logs").value)
        self.quality_debug_log_limit = max(
            1, int(self.get_parameter("quality_debug_log_limit").value))

        self._started = False
        self._exploration_round = 1
        self._timer = None
        self._latest_frontiers: list[tuple[float, float]] = []
        self._latest_frontier_stamp_ns: int | None = None
        self._latest_score_mean: float | None = None
        self._latest_score_stamp_ns: int | None = None
        self._waiting_for_score_histogram_mean = False
        self._rosout_message_count = 0
        self._cartographer_log_count = 0
        self._quality_candidate_count = 0
        self._recent_cartographer_logs = deque(maxlen=self.quality_debug_log_limit)
        self._recent_quality_candidates = deque(maxlen=self.quality_debug_log_limit)
        self._start_pose: PoseStamped | None = None
        self._return_timeout_timer = None
        self._return_retry_timer = None
        self._return_attempt = 0
        self._return_in_progress = False
        self._return_goal_handle = None
        self._save_started = False
        self._quality_metrics_future = None
        self._save_service_future = None
        self._save_service_name = None
        self._save_service_timeout_timer = None
        self._exploration_control_future = None
        self._exploration_restart_timer = None
        self._exploration_restart_deadline_ns = None
        self._exploration_start_request_pending = False
        self._shutdown_timer = None
        self._failure_reasons: list[str] = []
        self._quality_failed = False
        self._quality_report = "建图质量分数: unknown"
        self._steady_clock = Clock(clock_type=ClockType.STEADY_TIME)
        self._tf_buffer = Buffer()
        self._tf_listener = TransformListener(self._tf_buffer, self)
        self._nav_client = ActionClient(
            self,
            NavigateToPose,
            self.navigate_to_pose_action_name,
        )
        self._metrics_client = self.create_client(
            ReadMetrics, self.read_metrics_service_name)
        self._finish_trajectory_client = self.create_client(
            FinishTrajectory, self.finish_service_name)
        self._write_state_client = self.create_client(
            WriteState, self.write_state_service_name)
        self._map_saver_client = self.create_client(
            SaveMap, self.map_saver_service_name)
        self._exploration_control_client = self.create_client(
            ControlExploration, self.exploration_control_service_name)
        self._result_pub = self.create_publisher(String, self.result_topic, 10)
        self.create_subscription(Empty, self.completion_topic, self._on_complete, 10)
        self.create_subscription(MarkerArray, self.frontier_marker_topic, self._on_frontiers, 10)
        self.create_subscription(Log, self.rosout_topic, self._on_rosout, 50)
        self._start_pose_timer = self.create_timer(0.5, self._record_start_pose_once)

        self.get_logger().info(
            f"Waiting for exploration completion on '{self.completion_topic}'")
        self.get_logger().info(
            f"Watching remaining frontiers on '{self.frontier_marker_topic}'; "
            f"quality metrics service is '{self.read_metrics_service_name}'")
        self.get_logger().info(f"Mapping result will be published on '{self.result_topic}'")
        self.get_logger().info(
            f"Two-pass mapping workflow: required rounds={self.required_exploration_rounds}, "
            f"control service='{self.exploration_control_service_name}'")
        self.get_logger().info(
            "Persistent save clients initialized: "
            f"finish='{self.finish_service_name}', write_state='{self.write_state_service_name}', "
            f"save_map='{self.map_saver_service_name}', timeout={self.service_timeout_s:.1f}s")
        if self.return_to_start_before_save:
            self.get_logger().info(
                f"Will return to start pose in '{self.global_frame}' before saving map; "
                f"max attempts={self.return_to_start_max_attempts}")

    def _on_complete(self, _msg: Empty) -> None:
        if self._started:
            self.get_logger().info("Completion event already handled; ignoring duplicate")
            return
        self._started = True
        self.get_logger().info(
            f"Exploration round {self._exploration_round}/"
            f"{self.required_exploration_rounds} complete; starting post-round workflow after "
            f"{self.settle_delay_s:.1f}s settle")
        self._timer = self.create_timer(max(0.0, self.settle_delay_s), self._begin_workflow)

    def _begin_workflow(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self.destroy_timer(self._timer)
            self._timer = None

        if self.return_to_start_before_save:
            self._return_to_start()
            return

        self._after_round_return()

    def _after_round_return(self) -> None:
        if self._exploration_round < self.required_exploration_rounds:
            self._restart_exploration_for_next_round()
            return
        self._save_once()

    def _restart_exploration_for_next_round(self) -> None:
        if not self._exploration_control_client.wait_for_service(timeout_sec=5.0):
            self._fail_without_saving(
                f"探索控制服务不可用: {self.exploration_control_service_name}")
            return

        self.get_logger().info(
            f"Round {self._exploration_round} returned home; stopping explorer session "
            "without saving or finishing Cartographer")
        request = ControlExploration.Request()
        request.action = ControlExploration.Request.ACTION_STOP
        request.delay_seconds = 0.0
        request.quit_after_stop = False
        self._exploration_control_future = self._exploration_control_client.call_async(request)
        self._exploration_control_future.add_done_callback(self._on_exploration_stop_response)

    def _on_exploration_stop_response(self, future) -> None:
        try:
            response = future.result()
        except Exception as exc:
            self._fail_without_saving(f"停止第一轮探索失败: {exc}")
            return
        if not response.accepted:
            self._fail_without_saving(f"停止第一轮探索被拒绝: {response.message}")
            return

        self.get_logger().info(
            f"Explorer stop accepted: {response.message}; waiting before round restart")
        self._exploration_restart_deadline_ns = (
            self.get_clock().now().nanoseconds
            + int(self.exploration_restart_timeout_s * 1e9))
        self._exploration_restart_timer = self.create_timer(
            max(0.2, self.exploration_restart_delay_s),
            self._try_start_next_exploration_round,
        )

    def _try_start_next_exploration_round(self) -> None:
        if self._exploration_start_request_pending:
            return
        if self.get_clock().now().nanoseconds >= self._exploration_restart_deadline_ns:
            self._cancel_exploration_restart_timer()
            self._fail_without_saving("等待探索节点重新进入可启动状态超时")
            return

        request = ControlExploration.Request()
        request.action = ControlExploration.Request.ACTION_START
        request.delay_seconds = 0.0
        request.quit_after_stop = False
        self._exploration_start_request_pending = True
        future = self._exploration_control_client.call_async(request)
        future.add_done_callback(self._on_exploration_start_response)

    def _on_exploration_start_response(self, future) -> None:
        self._exploration_start_request_pending = False
        try:
            response = future.result()
        except Exception as exc:
            self.get_logger().warn(f"Explorer restart request failed; retrying: {exc}")
            return
        if not response.accepted:
            self.get_logger().info(
                f"Explorer is not ready to restart yet; retrying: {response.message}")
            return

        self._cancel_exploration_restart_timer()
        self._exploration_round += 1
        self._started = False
        self._return_attempt = 0
        self._latest_frontiers = []
        self._latest_frontier_stamp_ns = None
        self.get_logger().info(
            f"Exploration round {self._exploration_round}/"
            f"{self.required_exploration_rounds} started; Cartographer remains active")

    def _cancel_exploration_restart_timer(self) -> None:
        if self._exploration_restart_timer is not None:
            self._exploration_restart_timer.cancel()
            self.destroy_timer(self._exploration_restart_timer)
            self._exploration_restart_timer = None

    def _fail_without_saving(self, reason: str) -> None:
        self._add_failure_reason(reason)
        self._quality_report = self._quality_report or "建图质量分数: unknown"
        self.get_logger().error(
            f"Mapping workflow failed without saving or finishing Cartographer: {reason}")
        self._finish_and_exit(False)

    def _save_once(self) -> None:
        if self._save_started:
            return
        self._save_started = True

        self._report_remaining_frontiers()
        if not self._metrics_client.wait_for_service(timeout_sec=2.0):
            self.get_logger().error(
                f"Cartographer metrics service '{self.read_metrics_service_name}' is unavailable; "
                "ensure cartographer_node was started with -collect_metrics")
            self._continue_save(self._report_map_quality())
            return

        self.get_logger().info("Reading Cartographer runtime quality metrics")
        self._quality_metrics_future = self._metrics_client.call_async(ReadMetrics.Request())
        self._quality_metrics_future.add_done_callback(self._on_quality_metrics)

    def _on_quality_metrics(self, future) -> None:
        try:
            response = future.result()
        except Exception as exc:
            self.get_logger().error(f"Failed to read Cartographer metrics: {exc}")
            self._continue_save(self._report_map_quality())
            return

        if response.status.code != 0:
            self.get_logger().error(
                f"Cartographer metrics unavailable: {response.status.message}")
            self._continue_save(self._report_map_quality())
            return

        quality_ok = self._evaluate_cartographer_metrics(response.metric_families)
        self._continue_save(quality_ok)

    @staticmethod
    def _histogram_approximate_mean(metric) -> tuple[float | None, int]:
        """Estimate a mean from Cartographer's non-cumulative histogram buckets."""
        weighted_sum = 0.0
        sample_count = 0
        lower_boundary = 0.0
        for bucket in metric.counts_by_bucket:
            count = int(bucket.count)
            if count <= 0:
                if math.isfinite(bucket.bucket_boundary):
                    lower_boundary = bucket.bucket_boundary
                continue

            upper_boundary = bucket.bucket_boundary
            if not math.isfinite(upper_boundary) or upper_boundary > 1.0:
                upper_boundary = 1.0
            midpoint = (lower_boundary + upper_boundary) / 2.0
            weighted_sum += midpoint * count
            sample_count += count
            if math.isfinite(bucket.bucket_boundary):
                lower_boundary = bucket.bucket_boundary

        if sample_count == 0:
            return None, 0
        return weighted_sum / sample_count, sample_count

    def _find_score_metric(self, metric_families, family_name: str, labels: dict):
        for family in metric_families:
            if family.name != family_name:
                continue
            for metric in family.metrics:
                metric_labels = {label.key: label.value for label in metric.labels}
                if all(metric_labels.get(key) == value for key, value in labels.items()):
                    return self._histogram_approximate_mean(metric)
        return None, 0

    def _evaluate_cartographer_metrics(self, metric_families) -> bool:
        """
        Evaluate Cartographer mapping quality.

        Preferred score:
          1. Local trajectory-builder scan-match score, when available.
          2. Local constraint-builder score as fallback.

        A missing real_time_correlative score is not necessarily an error:
        Cartographer may run without the online correlative scan matcher, in
        which case that histogram legitimately contains zero samples.

        Global constraints are diagnostic only. Small indoor maps may finish
        without producing global loop-closure constraints.
        """

        local_mean, local_samples = self._find_score_metric(
            metric_families,
            "mapping_2d_local_trajectory_builder_scores",
            {"scan_matcher": "real_time_correlative"},
        )

        constraint_mean, constraint_samples = self._find_score_metric(
            metric_families,
            "mapping_constraints_constraint_builder_2d_scores",
            {"search_region": "local"},
        )

        global_mean, global_samples = self._find_score_metric(
            metric_families,
            "mapping_constraints_constraint_builder_2d_scores",
            {"search_region": "global"},
        )

        details = []

        for name, mean, samples in (
            ("local_scan_match", local_mean, local_samples),
            ("local_constraint", constraint_mean, constraint_samples),
            ("global_constraint", global_mean, global_samples),
        ):
            value = "unknown" if mean is None else f"{mean:.3f}"
            details.append(f"{name}={value} (n={samples})")

        detail_text = ", ".join(details)

        # ---------------------------------------------------------
        # 1. Prefer local scan-matcher score when it actually exists.
        # ---------------------------------------------------------
        if local_mean is not None and local_samples > 0:
            selected_score = local_mean
            selected_samples = local_samples
            selected_source = "local_scan_match"

        # ---------------------------------------------------------
        # 2. Real-time correlative scan matching may be disabled.
        #    In that case use local constraint-builder quality.
        # ---------------------------------------------------------
        elif constraint_mean is not None and constraint_samples > 0:
            selected_score = constraint_mean
            selected_samples = constraint_samples
            selected_source = "local_constraint"

            self.get_logger().warn(
                "No local real_time_correlative scan-match samples were "
                "reported; using local constraint score as mapping quality "
                f"fallback: {detail_text}"
            )

        # ---------------------------------------------------------
        # 3. No usable Cartographer quality metric.
        # ---------------------------------------------------------
        else:
            self._latest_score_mean = None
            self._quality_report = (
                "建图质量分数: unknown；"
                + "，".join(details)
            )

            message = (
                "Cartographer produced no usable local quality score: "
                + detail_text
            )

            if self.require_quality_score:
                self.get_logger().error(message)
                return False

            self.get_logger().warn(
                message
                + "; require_quality_score=false, allowing map save"
            )
            return True

        # ---------------------------------------------------------
        # Evaluate selected metric.
        # ---------------------------------------------------------
        self._latest_score_mean = selected_score

        self._quality_report = (
            f"建图质量: source={selected_source}, "
            f"score={selected_score:.3f}, "
            f"required>={self.min_cartographer_score_mean:.3f}, "
            f"samples={selected_samples}；"
            + "，".join(details)
        )

        if selected_score < self.min_cartographer_score_mean:
            self.get_logger().error(self._quality_report)
            return False

        self.get_logger().info(self._quality_report)
        return True

    def _continue_save(self, quality_ok: bool) -> None:
        if not quality_ok:
            self._quality_failed = True
            self._add_failure_reason(self._quality_report)
            self.get_logger().error(
                "Map quality is below the required threshold; broadcasting rebuild request "
                "without saving or finishing Cartographer")
            self._finish_and_exit(False)
            return

        self._start_finish_trajectory()

    def _record_start_pose_once(self) -> None:
        if self._start_pose is not None:
            if self._start_pose_timer is not None:
                self._start_pose_timer.cancel()
                self.destroy_timer(self._start_pose_timer)
                self._start_pose_timer = None
            return

        self._start_pose = self._lookup_robot_pose(timeout_s=0.0)
        if self._start_pose is not None:
            p = self._start_pose.pose.position
            self.get_logger().info(
                f"Recorded map start pose: ({p.x:.2f}, {p.y:.2f})")

    def _lookup_robot_pose(self, timeout_s: float) -> PoseStamped | None:
        try:
            transform = self._tf_buffer.lookup_transform(
                self.global_frame,
                self.robot_base_frame,
                rclpy.time.Time(),
                timeout=Duration(seconds=max(0.0, timeout_s)),
            )
        except TransformException as exc:
            if timeout_s > 0.0:
                self.get_logger().error(
                    f"Could not lookup start pose {self.global_frame}->{self.robot_base_frame}: {exc}")
            return None

        pose = PoseStamped()
        pose.header.frame_id = self.global_frame
        pose.header.stamp = self.get_clock().now().to_msg()
        pose.pose.position.x = transform.transform.translation.x
        pose.pose.position.y = transform.transform.translation.y
        pose.pose.position.z = transform.transform.translation.z
        pose.pose.orientation = transform.transform.rotation
        return pose

    def _return_to_start(self) -> None:
        self._return_attempt += 1
        if self._start_pose is None:
            self.get_logger().warn(
                "Start pose was not recorded yet; trying one final TF lookup before return")
            self._start_pose = self._lookup_robot_pose(timeout_s=self.start_pose_lookup_timeout_s)

        if self._start_pose is None:
            self._handle_return_finished(False, "无法导航回起点: 起始位姿不可用")
            return

        if not self._nav_client.wait_for_server(
            timeout_sec=max(0.0, self.return_to_start_server_timeout_s)):
            self._handle_return_finished(
                False,
                f"无法导航回起点: Nav2 action server 不可用 ({self.navigate_to_pose_action_name})",
            )
            return

        goal = NavigateToPose.Goal()
        goal.pose = self._start_pose
        # A map-frame goal is spatially static.  Keep a zero timestamp so Nav2
        # always uses the latest TF during long-running replans instead of a
        # transform that may have aged out of the TF cache.
        goal.pose.header.stamp.sec = 0
        goal.pose.header.stamp.nanosec = 0
        p = goal.pose.pose.position
        self.get_logger().info(
            f"Returning to original start after exploration round {self._exploration_round}, attempt "
            f"{self._return_attempt}/{self.return_to_start_max_attempts}: "
            f"({p.x:.2f}, {p.y:.2f})")

        self._return_in_progress = True
        send_future = self._nav_client.send_goal_async(goal)
        send_future.add_done_callback(self._on_return_goal_response)
        self._return_timeout_timer = self.create_timer(
            max(1.0, self.return_to_start_timeout_s),
            self._on_return_timeout,
        )

    def _on_return_goal_response(self, future) -> None:
        if not self._return_in_progress:
            return
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().error("Return-to-start goal was rejected by Nav2")
            self._handle_return_finished(False, "无法导航回起点: Nav2 拒绝目标")
            return

        self._return_goal_handle = goal_handle
        self.get_logger().info("Return-to-start goal accepted")
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(self._on_return_result)

    def _on_return_result(self, future) -> None:
        if not self._return_in_progress:
            return
        result = future.result()
        status = result.status
        ok = status == GoalStatus.STATUS_SUCCEEDED
        if ok:
            self.get_logger().info("Returned to start pose; saving map now")
        else:
            self.get_logger().error(f"Return-to-start failed with Nav2 status={status}")
        self._handle_return_finished(ok, f"无法导航回起点: Nav2 status={status}")

    def _on_return_timeout(self) -> None:
        if not self._return_in_progress:
            return
        self.get_logger().error(
            f"Timed out returning to start after {self.return_to_start_timeout_s:.1f}s")
        if self._return_goal_handle is not None:
            self._return_goal_handle.cancel_goal_async()
        self._handle_return_finished(
            False,
            f"无法导航回起点: 超时 {self.return_to_start_timeout_s:.1f}s",
        )

    def _handle_return_finished(self, ok: bool, reason: str = "无法导航回起点") -> None:
        self._return_in_progress = False
        self._return_goal_handle = None
        if self._return_timeout_timer is not None:
            self._return_timeout_timer.cancel()
            self.destroy_timer(self._return_timeout_timer)
            self._return_timeout_timer = None

        if ok:
            self._return_attempt = 0
            self._after_round_return()
            return

        if self._return_attempt < self.return_to_start_max_attempts:
            self.get_logger().warn(
                f"{reason}; retrying return-to-start after "
                f"{self.return_to_start_retry_delay_s:.1f}s")
            self._return_retry_timer = self.create_timer(
                max(0.0, self.return_to_start_retry_delay_s),
                self._retry_return_to_start,
            )
            return

        self._add_failure_reason("无法导航回起点")
        self.get_logger().error(
            f"Return-to-start failed after {self.return_to_start_max_attempts} attempt(s); "
            "finishing mapping workflow as failed")
        if self.save_on_return_failure and self._exploration_round >= self.required_exploration_rounds:
            self._save_once()
            return
        self._fail_without_saving("无法导航回起点")

    def _retry_return_to_start(self) -> None:
        if self._return_retry_timer is not None:
            self._return_retry_timer.cancel()
            self.destroy_timer(self._return_retry_timer)
            self._return_retry_timer = None
        self._return_to_start()

    def _on_frontiers(self, msg: MarkerArray) -> None:
        frontiers: list[tuple[float, float]] = []
        latest_stamp_ns: int | None = None
        for marker in msg.markers:
            if marker.action in (Marker.DELETE, Marker.DELETEALL):
                continue
            stamp_ns = int(marker.header.stamp.sec) * 1_000_000_000 + int(marker.header.stamp.nanosec)
            latest_stamp_ns = max(latest_stamp_ns or stamp_ns, stamp_ns)
            if marker.type == Marker.POINTS:
                for point in marker.points:
                    if math.isfinite(point.x) and math.isfinite(point.y):
                        frontiers.append((float(point.x), float(point.y)))
            elif marker.type in (Marker.SPHERE, Marker.CUBE, Marker.CYLINDER):
                point = marker.pose.position
                if math.isfinite(point.x) and math.isfinite(point.y):
                    frontiers.append((float(point.x), float(point.y)))
        self._latest_frontiers = frontiers
        self._latest_frontier_stamp_ns = latest_stamp_ns

    def _on_rosout(self, msg: Log) -> None:
        self._rosout_message_count += 1

        # Do not consume this node's own diagnostic output, even when its text
        # contains the word "Cartographer".
        if msg.name == self.get_name() or msg.name.endswith(f".{self.get_name()}"):
            return

        logger_name = msg.name.lower()
        log_text = msg.msg
        if "cartographer" not in logger_name and "cartographer" not in log_text.lower():
            return
        self._cartographer_log_count += 1
        self._recent_cartographer_logs.append((msg.name, log_text))

        if re.search(r"score|histogram|constraint|scan match|mean", log_text, re.IGNORECASE):
            self._quality_candidate_count += 1
            self._recent_quality_candidates.append((msg.name, log_text))
            if self.debug_quality_logs:
                self.get_logger().info(
                    f"QUALITY_DEBUG candidate logger='{msg.name}': {log_text}")

        if "Score histogram" in log_text:
            self._waiting_for_score_histogram_mean = True
            return
        if not self._waiting_for_score_histogram_mean and "Mean:" not in log_text:
            return

        match = re.search(r"\bMean:\s*([0-9]+(?:\.[0-9]+)?)", log_text)
        if not match:
            return
        self._latest_score_mean = float(match.group(1))
        self._latest_score_stamp_ns = (
            int(msg.stamp.sec) * 1_000_000_000 + int(msg.stamp.nanosec))
        self._waiting_for_score_histogram_mean = False

    def _report_remaining_frontiers(self) -> None:
        count = len(self._latest_frontiers)
        if count == 0:
            self.get_logger().info("Remaining frontier cache is empty at completion")
            return

        self.get_logger().warn(f"Remaining frontier cache has {count} point(s) at completion")
        limit = max(0, self.max_reported_frontiers)
        for index, (x, y) in enumerate(self._latest_frontiers[:limit], start=1):
            self.get_logger().warn(f"  frontier[{index}] = ({x:.2f}, {y:.2f})")
        if count > limit:
            self.get_logger().warn(f"  ... {count - limit} more frontier point(s) not printed")

    def _report_map_quality(self) -> bool:
        if self._latest_score_mean is None:
            if self.debug_quality_logs:
                self._dump_quality_debug_logs()
            message = (
                "No Cartographer score histogram mean was observed on /rosout; "
                "cannot judge scan-match quality")
            if self.require_quality_score:
                self._quality_report = "建图质量分数: unknown，失败原因: 未收到 Cartographer score mean"
                self.get_logger().error(message)
                return False
            self._quality_report = "建图质量分数: unknown"
            self.get_logger().warn(message)
            return True

        score = self._latest_score_mean
        threshold = self.min_cartographer_score_mean
        self._quality_report = f"建图质量分数: mean={score:.3f}, required>={threshold:.3f}"
        if score < threshold:
            self.get_logger().error(
                f"Cartographer score mean is low: mean={score:.3f}, required>={threshold:.3f}")
            return False
        self.get_logger().info(
            f"Cartographer score mean OK: mean={score:.3f}, required>={threshold:.3f}")
        return True

    def _dump_quality_debug_logs(self) -> None:
        self.get_logger().warn(
            "QUALITY_DEBUG summary: "
            f"rosout_total={self._rosout_message_count}, "
            f"cartographer_logs={self._cartographer_log_count}, "
            f"quality_candidates={self._quality_candidate_count}, "
            f"waiting_for_histogram_mean={self._waiting_for_score_histogram_mean}")

        logs = self._recent_quality_candidates or self._recent_cartographer_logs
        if not logs:
            self.get_logger().warn(
                "QUALITY_DEBUG no Cartographer messages were captured from /rosout")
            return

        source = (
            "quality candidates" if self._recent_quality_candidates
            else "recent Cartographer logs")
        self.get_logger().warn(
            f"QUALITY_DEBUG dumping {len(logs)} {source} (oldest first)")
        for index, (logger_name, log_text) in enumerate(logs, start=1):
            compact_text = " ".join(log_text.split())
            self.get_logger().warn(
                f"QUALITY_DEBUG [{index}] logger='{logger_name}': {compact_text}")

    def _add_failure_reason(self, reason: str) -> None:
        if reason not in self._failure_reasons:
            self._failure_reasons.append(reason)

    def _finish_and_exit(self, success: bool) -> None:
        if success:
            message = (
                f"MAPPING_SUCCESS rounds={self.required_exploration_rounds}。"
                f"建图成功。{self._quality_report}")
            self.get_logger().info(message)
        else:
            reason_text = "；".join(self._failure_reasons) or "未知原因"
            status = "MAPPING_REBUILD_REQUIRED" if self._quality_failed else "MAPPING_FAILED"
            message = (
                f"{status}。建图失败。失败原因: {reason_text}。{self._quality_report}")
            self.get_logger().error(message)

        result_msg = String()
        result_msg.data = message
        self._result_pub.publish(result_msg)

        self._shutdown_timer = self.create_timer(0.5, self._shutdown_node)

    def _shutdown_node(self) -> None:
        if self._shutdown_timer is not None:
            self._shutdown_timer.cancel()
            self.destroy_timer(self._shutdown_timer)
            self._shutdown_timer = None
        self.get_logger().info("save_map_on_exploration_complete finished; shutting down")
        rclpy.shutdown()

    def _sync_map_artifacts_to_install(self) -> bool:
        artifacts = [
            (Path(self.pbstream_path), Path(self.install_pbstream_path)),
            (Path(f"{self.save_map_path}.yaml"), Path(f"{self.install_map_path}.yaml")),
            (Path(f"{self.save_map_path}.pgm"), Path(f"{self.install_map_path}.pgm")),
        ]
        ok = True
        for source, destination in artifacts:
            if not source.is_file() or source.stat().st_size == 0:
                self.get_logger().error(f"Cannot sync missing or empty map artifact: {source}")
                ok = False
                continue
            try:
                if source.resolve() == destination.resolve(strict=False):
                    self.get_logger().info(
                        f"Install artifact already resolves to source; no copy needed: {source}")
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(f".{destination.name}.tmp")
                shutil.copy2(source, temporary)
                os.replace(temporary, destination)
                self.get_logger().info(f"Synced map artifact: {source} -> {destination}")
            except Exception as exc:
                self.get_logger().error(
                    f"Failed to sync map artifact {source} -> {destination}: {exc}")
                ok = False
        return ok

    def _start_finish_trajectory(self) -> None:
        if not self.finish_cartographer_trajectory:
            self._start_write_state()
            return

        self.get_logger().info(
            f"Finishing Cartographer trajectory {self.trajectory_id} via "
            f"persistent client '{self.finish_service_name}'")
        request = FinishTrajectory.Request()
        request.trajectory_id = self.trajectory_id
        self._start_save_service_call(
            self._finish_trajectory_client,
            request,
            "结束 Cartographer 轨迹",
            self._on_finish_trajectory_response,
        )

    def _on_finish_trajectory_response(self, future) -> None:
        response = self._complete_save_service_call(future)
        if response is None:
            return
        if response.status.code != 0:
            self._abort_save(
                f"结束 Cartographer 轨迹失败: {response.status.message}")
            return
        self.get_logger().info("Cartographer trajectory finished")
        self._start_write_state()

    def _start_write_state(self) -> None:
        if not self.write_cartographer_state:
            self._start_save_occupancy_grid()
            return

        os.makedirs(os.path.dirname(self.pbstream_path), exist_ok=True)
        self.get_logger().info(f"Writing Cartographer state -> {self.pbstream_path}")
        request = WriteState.Request()
        request.filename = self.pbstream_path
        request.include_unfinished_submaps = self.include_unfinished_submaps
        self._start_save_service_call(
            self._write_state_client,
            request,
            "写入 Cartographer 状态",
            self._on_write_state_response,
        )

    def _on_write_state_response(self, future) -> None:
        response = self._complete_save_service_call(future)
        if response is None:
            return
        if response.status.code != 0:
            self._abort_save(
                f"写入 Cartographer 状态失败: {response.status.message}")
            return
        self.get_logger().info("Cartographer state saved")
        self._start_save_occupancy_grid()

    def _start_save_occupancy_grid(self) -> None:
        if not self.save_occupancy_grid:
            self._finish_save_workflow()
            return

        os.makedirs(os.path.dirname(self.save_map_path), exist_ok=True)
        self.get_logger().info(
            f"Saving occupancy grid map from '{self.map_topic}' -> {self.save_map_path}")
        request = SaveMap.Request()
        request.map_topic = self.map_topic
        request.map_url = self.save_map_path
        request.image_format = self.map_image_format
        request.map_mode = self.map_mode
        request.free_thresh = self.map_free_thresh
        request.occupied_thresh = self.map_occupied_thresh
        self._start_save_service_call(
            self._map_saver_client,
            request,
            "保存占据栅格地图",
            self._on_save_occupancy_grid_response,
        )

    def _on_save_occupancy_grid_response(self, future) -> None:
        response = self._complete_save_service_call(future)
        if response is None:
            return
        if not response.result:
            self._abort_save("map_saver_server 返回保存失败")
            return
        self.get_logger().info("Occupancy grid map saved")
        self._finish_save_workflow()

    def _finish_save_workflow(self) -> None:
        if not self._sync_map_artifacts_to_install():
            self._abort_save("地图文件同步到 install 目录失败")
            return
        self.get_logger().info("Map save workflow finished successfully")
        self._finish_and_exit(True)

    def _start_save_service_call(self, client, request, operation: str, callback) -> None:
        if self._save_service_future is not None:
            self._abort_save(f"保存状态机冲突，无法开始: {operation}")
            return
        if not client.wait_for_service(timeout_sec=self.service_availability_timeout_s):
            self._abort_save(
                f"{operation}失败: 服务不可用，等待 "
                f"{self.service_availability_timeout_s:.1f}s 后仍未发现")
            return

        self._save_service_name = operation
        self._save_service_future = client.call_async(request)
        self._save_service_timeout_timer = self.create_timer(
            self.service_timeout_s,
            self._on_save_service_timeout,
            clock=self._steady_clock,
        )
        self._save_service_future.add_done_callback(callback)

    def _complete_save_service_call(self, future):
        if future is not self._save_service_future:
            return None
        self._cancel_save_service_timeout()
        self._save_service_future = None
        self._save_service_name = None
        try:
            return future.result()
        except Exception as exc:
            self._abort_save(f"保存服务调用异常: {exc}")
            return None

    def _on_save_service_timeout(self) -> None:
        future = self._save_service_future
        if future is None:
            self._cancel_save_service_timeout()
            return
        if future.done():
            return

        operation = self._save_service_name or "保存服务"
        future.cancel()
        self._save_service_future = None
        self._save_service_name = None
        self._cancel_save_service_timeout()
        self._abort_save(
            f"{operation}超时，超过 {self.service_timeout_s:.1f}s 未收到响应")

    def _cancel_save_service_timeout(self) -> None:
        if self._save_service_timeout_timer is not None:
            self._save_service_timeout_timer.cancel()
            self.destroy_timer(self._save_service_timeout_timer)
            self._save_service_timeout_timer = None

    def _abort_save(self, reason: str) -> None:
        self._add_failure_reason(reason)
        self.get_logger().error(
            f"Map save workflow stopped immediately: {reason}")
        self._finish_and_exit(False)


def main(args=None):
    rclpy.init(args=args)
    node = SaveMapOnExplorationComplete()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
