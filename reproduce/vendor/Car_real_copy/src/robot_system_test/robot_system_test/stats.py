"""ROS-independent stream statistics, kept separate for unit testing."""

import math


class StreamStats:
    def __init__(self):
        self.arrivals = []
        self.invalid = 0
        self.timestamp_backwards = 0
        self._last_stamp = None

    def add(self, arrival, valid=True, stamp=None):
        self.arrivals.append(float(arrival))
        if not valid:
            self.invalid += 1
        if stamp is not None and stamp > 0.0:
            if self._last_stamp is not None and stamp <= self._last_stamp:
                self.timestamp_backwards += 1
            self._last_stamp = stamp

    def result(self, duration):
        gaps = [b - a for a, b in zip(self.arrivals, self.arrivals[1:])]
        count = len(self.arrivals)
        return {
            'messages': count,
            'hz': count / duration if duration > 0 else 0.0,
            'max_gap_sec': max(gaps) if gaps else (duration if count == 0 else 0.0),
            'invalid_messages': self.invalid,
            'invalid_ratio': self.invalid / count if count else 1.0,
            'timestamp_backwards': self.timestamp_backwards,
        }


def finite(values):
    return all(math.isfinite(float(value)) for value in values)


def evaluate(result, limits):
    failures = []
    if result['messages'] < int(limits.get('min_messages', 1)):
        failures.append('message_count')
    if result['hz'] < float(limits.get('min_hz', 0.0)):
        failures.append('frequency')
    if result['max_gap_sec'] > float(limits.get('max_gap_sec', math.inf)):
        failures.append('data_gap')
    if result['invalid_ratio'] > float(limits.get('max_invalid_ratio', 0.0)):
        failures.append('invalid_data')
    if result['timestamp_backwards']:
        failures.append('timestamp_not_monotonic')
    return failures

