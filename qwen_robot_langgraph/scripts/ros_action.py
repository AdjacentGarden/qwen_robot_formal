#!/usr/bin/env python3
"""One bounded sensor/head call. No base publisher exists in this client."""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEAD_FAULT = ROOT/'runtime/head_motion_fault.json'

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Bool, Float32, String, UInt16
from std_srvs.srv import Empty

rclpy.init()
node = Node('langgraph_stationary_action')
observations = {}
confirmed_lidar_enabled = None
node.create_subscription(Float32, '/head/current_angle_deg', lambda m: observations.update(angle=m.data, head_at=time.monotonic()), 10)
node.create_subscription(Float32, '/head/angular_rate_dps', lambda m: observations.update(rate=m.data, rate_at=time.monotonic()), 10)
node.create_subscription(Float32, '/head/motor_command', lambda m: observations.update(motor=m.data, motor_at=time.monotonic()), 10)
node.create_subscription(Bool, '/head/in_position', lambda m: observations.update(in_position=m.data, in_position_at=time.monotonic()), 10)
node.create_subscription(String, '/head/status', lambda m: observations.update(controller_status=m.data, status_at=time.monotonic()), 10)
node.create_subscription(LaserScan, '/scan', lambda m: observations.update(scan_at=time.monotonic(), points=len(m.ranges)), qos_profile_sensor_data)


head_publisher=node.create_publisher(UInt16,'/step_motor_angle',10)
lidar_clients={enabled:node.create_client(Empty,'/start_motor' if enabled else '/stop_motor') for enabled in (True,False)}

def spin(seconds):
    end = time.monotonic()+seconds
    while time.monotonic()<end:
        rclpy.spin_once(node, timeout_sec=.03)


def gate(enabled):
    global confirmed_lidar_enabled
    now=time.monotonic()
    scan_age=now-observations.get('scan_at',0)
    # The persistent worker may receive an explicit gate action immediately
    # before/after a head action.  Reuse only a state this process previously
    # confirmed, and verify it still agrees with the scan stream.
    if confirmed_lidar_enabled is enabled:
        if enabled and scan_age < .25:
            return {'enabled':True,'confirmed':True,'already':True,**observations}
        if not enabled and scan_age > .4:
            return {'enabled':False,'confirmed':True,'already':True,**observations}
    client = lidar_clients[enabled]
    if not client.wait_for_service(timeout_sec=3):
        raise RuntimeError('lidar_service_unavailable')
    future = client.call_async(Empty.Request())
    rclpy.spin_until_future_complete(node, future, timeout_sec=3)
    if not future.done() or future.exception():
        raise RuntimeError('lidar_service_unconfirmed')
    called = time.monotonic()
    
    if enabled:
        deadline=time.monotonic()+5
        while observations.get('scan_at',0)<called and time.monotonic()<deadline:
            spin(.05)
    else:
        spin(.4)
    if enabled and observations.get('scan_at',0)<called:
        raise RuntimeError('lidar_no_fresh_scan')
    if not enabled and observations.get('scan_at',0)>called+.15:
        raise RuntimeError('lidar_still_scanning')
    confirmed_lidar_enabled=enabled
    return {'enabled':enabled,'confirmed':True,**observations}


def execute(kind,args):
    try:
        spin(.35)
        if kind == 'sensor.gate':
            if args['enabled'] and (time.monotonic()-observations.get('head_at',0)>.4 or abs(observations.get('angle',99))>5 or abs(observations.get('rate',99))>1):
                raise RuntimeError('head_not_freshly_confirmed_level')
            result=gate(args['enabled'])
        elif kind == 'head.move':
            target={'up':211,'down':160,'level':185}[args['pose']]
            # The head adapter owns the sensor dependency even for an atomic head command.
            before_gate=time.monotonic(); gate_result=gate(False)
            pub=head_publisher
            deadline=time.monotonic()+3
            while pub.get_subscription_count()<1 and time.monotonic()<deadline:
                spin(.05)
            if pub.get_subscription_count()<1:
                raise RuntimeError('head_controller_unavailable')
            msg=UInt16();msg.data=target
            published=time.monotonic()
            for _ in range(3):
                pub.publish(msg);spin(.05)
            # The controller stops motor output no later than its own 18 s hard
            # deadline.  Allow passive motion/IMU filtering to settle afterward;
            # this extra observation time cannot extend motor actuation.
            deadline=time.monotonic()+35;stable_since=None
            while time.monotonic()<deadline:
                spin(.05)
                now=time.monotonic()
                stable=(
                    observations.get('head_at',0)>published
                    and observations.get('rate_at',0)>published
                    and observations.get('motor_at',0)>published
                    and now-observations.get('head_at',0)<.4
                    and now-observations.get('rate_at',0)<.4
                    and now-observations.get('motor_at',0)<.4
                    and abs(observations.get('angle',99)-(target-185))<=5
                    and abs(observations.get('rate',99))<1
                    and abs(observations.get('motor',99))<.1
                )
                stable_since=(stable_since or time.monotonic()) if stable else None
                if stable_since and time.monotonic()-stable_since>.5:
                    break
                # The controller's own deadline has already stopped the motor.
                # Once that latched timeout and a fresh zero command are both
                # observed, more passive waiting cannot turn this request into
                # a confirmed success.
                controller_timed_out=(
                    observations.get('status_at',0)>published
                    and str(observations.get('controller_status','')).startswith('timeout;')
                    and observations.get('motor_at',0)>published
                    and abs(observations.get('motor',99))<.1
                )
                if controller_timed_out:
                    raise RuntimeError('head_target_unconfirmed')
            else:
                raise RuntimeError('head_target_unconfirmed')
            result={'target_command':target,'target_offset_deg':target-185,**observations,'gate_ms':(published-before_gate)*1000,'head_ms':(time.monotonic()-published)*1000}
            if args['pose'] != 'level' and HEAD_FAULT.exists():
                HEAD_FAULT.unlink()
            if args['pose']=='level':
                result['recovery']=gate(True)
        else:
            raise RuntimeError('unsupported_ros_action')
        return {'ok':True,'status':'completed','executed':True,'result':result}
    except Exception as exc:
        if kind == 'head.move' and str(exc) == 'head_target_unconfirmed':
            payload = {
                'fault':'head_target_unconfirmed',
                'pose':args.get('pose'),
                'observed_at':time.time(),
                'observations':dict(observations),
            }
            temp=HEAD_FAULT.with_suffix('.tmp')
            temp.write_text(json.dumps(payload,ensure_ascii=False,indent=2))
            temp.replace(HEAD_FAULT)
        return {'ok':False,'status':'failed','executed':None,'error':str(exc),'observations':dict(observations)}


if __name__=='__main__':
    try:
        if sys.argv[1:] != ['--server']:
            print(json.dumps(execute(sys.argv[1],json.loads(sys.argv[2]))))
        else:
            import os, socket, struct, signal
            path=ROOT/'runtime/ros.sock'
            if path.exists():
                with socket.socket(socket.AF_UNIX) as probe:
                    try:probe.connect(str(path))
                    except OSError:path.unlink()
                    else:raise SystemExit('ros_worker_already_running')
            server=socket.socket(socket.AF_UNIX);server.bind(str(path));os.chmod(path,0o600)
            server.listen(8);server.settimeout(.03);stopping=False
            def stop(*_):
                global stopping
                stopping=True
            signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
            def exact(conn,n):
                data=bytearray()
                while len(data)<n:
                    chunk=conn.recv(n-len(data))
                    if not chunk:raise EOFError()
                    data.extend(chunk)
                return bytes(data)
            try:
                while not stopping:
                    spin(.03)
                    try:conn,_=server.accept()
                    except socket.timeout:continue
                    with conn:
                        conn.settimeout(2)
                        try:
                            n=struct.unpack('!I',exact(conn,4))[0]
                            if n>65536:raise ValueError('request_too_large')
                            data=json.loads(exact(conn,n))
                            # This daemon accepts no base/motor/raw-topic calls.
                            if data['kind'] not in {'head.move','sensor.gate'}:raise ValueError('forbidden_ros_operation')
                            from robot_graph.contracts import Action, StationaryPolicy
                            StationaryPolicy().validate(Action(data['kind'], data.get('args',{})))
                            result=execute(data['kind'],data.get('args',{}))
                            output=json.dumps(result).encode()
                            conn.sendall(struct.pack('!I',len(output))+output)
                        except (OSError,ValueError,EOFError):pass
            finally:server.close();path.unlink(missing_ok=True)
    finally:
        node.destroy_node();rclpy.shutdown()
