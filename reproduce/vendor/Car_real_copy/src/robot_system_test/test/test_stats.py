from robot_system_test.stats import StreamStats, evaluate


def test_stable_stream_passes():
    stats = StreamStats()
    for index in range(101):
        stats.add(index * 0.01, stamp=index * 0.01 + 1.0)
    result = stats.result(1.01)
    assert evaluate(result, {
        'min_messages': 100, 'min_hz': 90, 'max_gap_sec': 0.02,
        'max_invalid_ratio': 0.0,
    }) == []


def test_bad_stream_reports_reasons():
    stats = StreamStats()
    stats.add(0.0, valid=False, stamp=2.0)
    stats.add(1.0, stamp=1.0)
    failures = evaluate(stats.result(2.0), {
        'min_messages': 3, 'min_hz': 2, 'max_gap_sec': 0.5,
        'max_invalid_ratio': 0.1,
    })
    assert set(failures) == {
        'message_count', 'frequency', 'data_gap', 'invalid_data',
        'timestamp_not_monotonic',
    }
