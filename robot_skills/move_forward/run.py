#!/usr/bin/env python3
SKILL_NAME = 'move_forward'
def _single_function_cli_preflight(skill_name):
    import json as _json, os as _os, sys as _sys, time as _time
    raw = list(_sys.argv[1:])
    dry_run = False
    json_mode = False
    timeout = None
    kept = [_sys.argv[0]]
    i = 0
    while i < len(raw):
        arg = raw[i]
        if arg == '--dry-run':
            dry_run = True
            i += 1
            continue
        if arg == '--json':
            json_mode = True
            i += 1
            continue
        if arg == '--timeout':
            if i + 1 < len(raw):
                timeout = raw[i + 1]
                i += 2
            else:
                i += 1
            continue
        if arg.startswith('--timeout='):
            timeout = arg.split('=', 1)[1]
            i += 1
            continue
        kept.append(arg)
        i += 1
    _sys.argv[:] = kept
    if json_mode:
        _os.environ['SINGLE_FUNCTION_JSON'] = '1'
    if timeout is not None:
        _os.environ['SINGLE_FUNCTION_TIMEOUT'] = str(timeout)
    if dry_run:
        action = 'default'
        for token in kept[1:]:
            if not token.startswith('-'):
                action = token
                break
        print(_json.dumps({
            'ok': True,
            'status': 'dry_run',
            'skill': skill_name,
            'action': action,
            'result': {'argv': kept[1:], 'timeout': timeout},
            'error': None,
            'metrics': {'ts': round(_time.time(), 3)},
        }, ensure_ascii=False))
        raise SystemExit(0)

_single_function_cli_preflight(SKILL_NAME)

import argparse, json, os, time
try:
    import rclpy
    from geometry_msgs.msg import Twist
except Exception as exc:
    raise SystemExit(f'ROS2 Python modules are unavailable. Use ./run.sh so ROS2 is sourced. Detail: {exc}')
parser=argparse.ArgumentParser(description='Move forward skill.')
parser.add_argument('--speed', type=float, default=0.12)
parser.add_argument('--angular-speed', type=float, default=0.35)
parser.add_argument('--duration', type=float, default=1.0)
parser.add_argument('--topic', default='/cmd_vel')
parser.add_argument('--discovery-timeout', type=float, default=float(os.getenv('CMD_VEL_DISCOVERY_TIMEOUT_SEC', '0.8')))
parser.add_argument('--allow-no-subscriber', action='store_true')
args=parser.parse_args()
rclpy.init(args=None)
node=rclpy.create_node('skill_move_forward')
pub=node.create_publisher(Twist, args.topic, 10)
def wait_for_subscribers(timeout):
    end=time.time()+max(0.0,float(timeout))
    count=int(pub.get_subscription_count())
    while rclpy.ok() and count<=0 and time.time()<end:
        rclpy.spin_once(node, timeout_sec=0.05)
        count=int(pub.get_subscription_count())
    return count
def publish(x,z):
    msg=Twist(); msg.linear.x=float(x); msg.angular.z=float(z); pub.publish(msg)
try:
    subscribers=wait_for_subscribers(args.discovery_timeout)
    require_subscriber=os.getenv('CMD_VEL_REQUIRE_SUBSCRIBER','1').strip().lower() not in {'0','false','no','off'} and not args.allow_no_subscriber
    if require_subscriber and subscribers<=0:
        print(json.dumps({'ok': False, 'skill': 'move_forward', 'topic': args.topic, 'duration': args.duration, 'subscribers': subscribers, 'error': 'cmd_vel_subscribers_0'}, ensure_ascii=False))
        raise SystemExit(3)
    end=time.time()+max(0.05,float(args.duration))
    while time.time()<end:
        publish(abs(args.speed), 0.0); rclpy.spin_once(node, timeout_sec=0.02); time.sleep(0.08)
    for _ in range(6):
        publish(0.0,0.0); rclpy.spin_once(node, timeout_sec=0.02); time.sleep(0.05)
    subscribers=max(subscribers, int(pub.get_subscription_count()))
    print(json.dumps({'ok': True, 'skill': 'move_forward', 'topic': args.topic, 'duration': args.duration, 'subscribers': subscribers}, ensure_ascii=False))
finally:
    node.destroy_node(); rclpy.shutdown()
