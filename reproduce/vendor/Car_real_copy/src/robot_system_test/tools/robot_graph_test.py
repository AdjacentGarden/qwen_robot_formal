#!/usr/bin/env python3
"""Validate a running ROS 2 graph and detect stale/backlogged topic data."""

import argparse
from collections import defaultdict
import math
import statistics
import sys
import time

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rosidl_runtime_py.utilities import get_message
import yaml


class GraphTest(Node):
    def __init__(self, profile, defaults):
        super().__init__('robot_graph_test')
        self.profile = profile
        self.defaults = defaults
        self.samples = defaultdict(list)
        self.subscriptions_ = []

    def attach_monitors(self):
        for topic, rule in self.profile.get('topics', {}).items():
            if not any(key in rule for key in
                       ('min_hz', 'max_delay_ms', 'max_backlog_growth_ms')):
                continue
            try:
                msg_type = get_message(rule['type'])
                sub = self.create_subscription(
                    msg_type, topic,
                    lambda msg, name=topic: self._receive(name, msg),
                    qos_profile_sensor_data)
                self.subscriptions_.append(sub)
            except Exception as exc:
                self.get_logger().error(f'cannot monitor {topic}: {exc}')

    def _receive(self, topic, msg):
        receive_ns = self.get_clock().now().nanoseconds
        stamp = getattr(getattr(msg, 'header', None), 'stamp', None)
        source_ns = None
        if stamp is not None and (stamp.sec or stamp.nanosec):
            source_ns = stamp.sec * 1_000_000_000 + stamp.nanosec
        self.samples[topic].append((time.monotonic(), receive_ns, source_ns))

    @staticmethod
    def _bound(value, limits):
        return (limits.get('min', 0) <= value and
                ('max' not in limits or value <= limits['max']))

    @staticmethod
    def _bound_text(limits):
        result = f">={limits.get('min', 0)}"
        if 'max' in limits:
            result += f", <={limits['max']}"
        return result

    def evaluate(self, duration):
        results = []
        nodes = set(self.get_node_names_and_namespaces())
        node_names = {('/' if ns == '/' else ns + '/') + name for name, ns in nodes}
        for node_rule in self.profile.get('nodes', []):
            if isinstance(node_rule, str):
                candidates, required = [node_rule], True
            else:
                candidates = node_rule.get('any_of', [])
                required = node_rule.get('required', True)
            found = sorted(node_names.intersection(candidates))
            label = ' or '.join(candidates)
            passed = bool(found) or not required
            detail = f'running={found[0]}' if found else (
                'not detected (optional)' if not required else 'missing')
            results.append((f'node {label}', passed, detail))

        delay_metrics = {}
        for topic in self.profile.get('topics', {}):
            raw = [(receive - source) / 1e6 for _, receive, source in
                   self.samples[topic] if source is not None]
            threshold = self.defaults.get('clock_offset_threshold_ms', 60000.0)
            normalized = bool(
                raw and self.defaults.get('normalize_clock_offset', True)
                and abs(statistics.median(raw)) > threshold)
            offset = min(raw) if normalized else 0.0
            delays = [value - offset for value in raw]
            growth = None
            if len(delays) >= 2:
                # Window medians reject callback scheduling jitter at either endpoint.
                window = max(1, min(len(delays) // 4, math.ceil(len(delays) * 0.1)))
                growth = (statistics.median(delays[-window:]) -
                          statistics.median(delays[:window]))
            delay_metrics[topic] = (raw, delays, normalized, offset, growth)

        normalized_growth = [metric[4] for topic, metric in delay_metrics.items()
                             if metric[2] and metric[4] is not None and
                             len(self.samples[topic]) >= 10]
        common_clock_growth = (statistics.median(normalized_growth)
                               if len(normalized_growth) >= 2 else 0.0)

        for topic, rule in self.profile.get('topics', {}).items():
            publisher_info = self.get_publishers_info_by_topic(topic)
            subscriber_info = self.get_subscriptions_info_by_topic(topic)
            publishers = len(publisher_info)
            subscribers = len(subscriber_info)
            # Do not count this diagnostic node's own monitoring subscription.
            if any(sub.topic_name == topic for sub in self.subscriptions_):
                subscribers = max(0, subscribers - 1)
            for label, value in (('publishers', publishers),
                                 ('subscribers', subscribers)):
                if label not in rule:
                    continue
                limits = rule[label]
                results.append((f'{topic} {label}', self._bound(value, limits),
                                f'actual={value}, expected {self._bound_text(limits)}'))

            for label, endpoints in (('publishers', publisher_info),
                                     ('subscribers', subscriber_info)):
                rule_key = f'allowed_{label[:-1]}_nodes'
                if rule_key not in rule:
                    continue
                allowed = set(rule[rule_key])
                actual = {('/' if info.node_namespace == '/' else
                           info.node_namespace.rstrip('/') + '/') + info.node_name
                          for info in endpoints
                          if info.node_name != self.get_name()}
                unexpected = sorted(actual - allowed)
                results.append((f'{topic} allowed {label}', not unexpected,
                                f'nodes={sorted(actual)}, '
                                f'unexpected={unexpected or "none"}'))

            samples = self.samples[topic]
            if 'min_hz' in rule:
                hz = 0.0
                if len(samples) >= 2:
                    span = samples[-1][0] - samples[0][0]
                    hz = (len(samples) - 1) / span if span > 0.0 else math.inf
                tolerance = rule.get(
                    'rate_tolerance_percent',
                    self.defaults.get('rate_tolerance_percent', 0.0))
                effective_min = rule['min_hz'] * (1.0 - tolerance / 100.0)
                results.append((f'{topic} rate', hz >= effective_min,
                                f'{hz:.2f} Hz, nominal minimum={rule["min_hz"]:.2f} Hz, '
                                f'tolerance={tolerance:.1f}%, '
                                f'samples={len(samples)}, window={duration:.1f}s'))

            raw_delays, delays, normalized, offset, growth = delay_metrics[topic]
            clock_note = ''
            if normalized:
                # Some real drivers stamp from CLOCK_MONOTONIC while ROS uses epoch
                # time. Absolute transport delay is unknowable across those domains;
                # preserve delay variation/backlog after removing the fixed offset.
                clock_note = f', clock offset normalized ({offset:.1f} ms)'
            if 'max_delay_ms' in rule:
                if delays:
                    p95 = sorted(delays)[max(0, math.ceil(len(delays) * .95) - 1)]
                    passed = p95 <= rule['max_delay_ms'] and min(delays) >= -100.0
                    detail = (f'p95={p95:.1f} ms, mean={statistics.mean(delays):.1f} ms, '
                              f'maximum={rule["max_delay_ms"]:.1f} ms{clock_note}')
                else:
                    passed, detail = False, 'no non-zero header timestamps received'
                results.append((f'{topic} delay', passed, detail))

            if 'max_backlog_growth_ms' in rule:
                if growth is not None:
                    adjusted_growth = (growth - common_clock_growth
                                       if normalized else growth)
                    # Negative growth means the queue caught up, not accumulation.
                    adjusted_growth = max(0.0, adjusted_growth)
                    passed = adjusted_growth <= rule['max_backlog_growth_ms'] + 1e-6
                    detail = f'adjusted growth={adjusted_growth:.1f} ms'
                    if normalized:
                        detail += (f', raw growth={growth:.1f} ms, common clock '
                                   f'drift={common_clock_growth:.1f} ms')
                    detail += f', maximum={rule["max_backlog_growth_ms"]:.1f} ms'
                else:
                    passed, detail = False, 'insufficient timestamped samples'
                results.append((f'{topic} backlog', passed, detail))
        return results


def parse_args():
    parser = argparse.ArgumentParser(
        description='Test nodes, publisher/subscriber counts, rate, delay and backlog.')
    default_config = (get_package_share_directory('robot_system_test') +
                      '/config/robot_test.yaml')
    parser.add_argument('--config', default=default_config)
    parser.add_argument('--profile', choices=('hardware', 'integration'),
                        default='hardware')
    parser.add_argument('--settle', type=float, default=5.0)
    parser.add_argument('--duration', type=float, default=15.0)
    return parser.parse_args()


def main():
    args = parse_args()
    with open(args.config, encoding='utf-8') as stream:
        document = yaml.safe_load(stream)
    profile = document.get('profiles', {}).get(args.profile)
    if not profile:
        print(f'profile not found: {args.profile}', file=sys.stderr)
        return 2
    rclpy.init()
    node = GraphTest(profile, document.get('defaults', {}))
    node.attach_monitors()
    try:
        deadline = time.monotonic() + args.settle + args.duration
        sample_start = time.monotonic() + args.settle
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
            if time.monotonic() < sample_start:
                node.samples.clear()
        results = node.evaluate(args.duration)
        for name, passed, detail in results:
            print(f'[{"PASS" if passed else "FAIL"}] {name} — {detail}')
        failures = [name for name, passed, _ in results if not passed]
        print(f'\nRESULT: {len(results) - len(failures)}/{len(results)} passed')
        if failures:
            print('FAILED: ' + ', '.join(failures))
        return 1 if failures else 0
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
