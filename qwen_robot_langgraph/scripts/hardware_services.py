#!/usr/bin/env python3
"""Own only the three isolated sensor/head processes started by this project."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'runtime/hardware_processes.json'
ENV=ROOT/'scripts/ros_env.sh'


def alive(item):
    try:
        stat=Path(f"/proc/{item['pid']}/stat").read_text().split()
        return stat[21]==item['start_ticks'] and stat[2]!='Z'
    except (OSError,KeyError):return False


def start():
    old=json.loads(STATE.read_text()) if STATE.exists() else []
    if any(alive(x) for x in old):
        raise RuntimeError('isolated_hardware_already_running')
    # Do not open a device already owned by another project.
    for device in ['/dev/ttyS0','/dev/ttyS8']:
        result=subprocess.run(['fuser',device],capture_output=True,text=True)
        if result.stdout.strip():raise RuntimeError('device_busy:'+device)
    commands=[
      ('ros_worker',['/usr/bin/python3',str(ROOT/'scripts/ros_action.py'),'--server']),
      ('audio',['/usr/bin/python3',str(ROOT/'scripts/audio_worker.py')]),
      ('imu',['/usr/bin/python3',str(ROOT/'vendor/imu_publisher.py'),'--calibration-samples','200','--publish-euler']),
      ('lidar',['ros2','run','rplidar_ros','rplidar_node','--ros-args','-p','serial_port:=/dev/ttyS8','-p','serial_baudrate:=460800','-p','scan_mode:=Standard']),
      ('head',['/usr/bin/python3',str(ROOT/'scripts/head_only_driver.py'),'--ros-args','-p','head_max_motor_speed:=40.0','-p','head_angle_deadband_deg:=2.0','-p','head_angle_restart_deg:=4.0','-p','head_kp_rate:=2.0','-p','head_command_timeout_sec:=18.0']),
    ]
    items=[]
    for name,command in commands:
        log=(ROOT/f'runtime/{name}.log').open('ab')
        proc=subprocess.Popen(['bash',str(ENV),*command],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        log.close()
        ticks=Path(f'/proc/{proc.pid}/stat').read_text().split()[21]
        items.append({'name':name,'pid':proc.pid,'start_ticks':ticks})
        STATE.write_text(json.dumps(items,indent=2))
    print(json.dumps({'started':items,'ros_domain':83,'base_locked':True}))


def stop():
    items=json.loads(STATE.read_text()) if STATE.exists() else []
    for item in reversed(items):
        if alive(item):os.killpg(item['pid'],signal.SIGINT)
    deadline=time.monotonic()+8
    while any(alive(x) for x in items) and time.monotonic()<deadline:time.sleep(.1)
    remaining=[x for x in items if alive(x)]
    print(json.dumps({'remaining':remaining,'stopped':not remaining}))
    if remaining:raise SystemExit(1)


def restart_worker(name):
    if name not in {'audio','ros_worker'}:raise ValueError('only isolated workers may be restarted here')
    items=json.loads(STATE.read_text())
    item=next(x for x in items if x['name']==name)
    if alive(item):
        os.killpg(item['pid'],signal.SIGTERM)
        deadline=time.monotonic()+30
        while alive(item) and time.monotonic()<deadline:time.sleep(.1)
        if alive(item):raise RuntimeError('worker_did_not_stop')
    command=['/usr/bin/python3',str(ROOT/'scripts'/('audio_worker.py' if name=='audio' else 'ros_action.py'))]
    if name=='ros_worker':command+=['--server']
    with (ROOT/f'runtime/{name}.log').open('ab') as log:
        proc=subprocess.Popen(['bash',str(ENV),*command],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    item.update(pid=proc.pid,start_ticks=Path(f'/proc/{proc.pid}/stat').read_text().split()[21])
    STATE.write_text(json.dumps(items,indent=2))
    socket_path=ROOT/'runtime'/('audio.sock' if name=='audio' else 'ros.sock')
    deadline=time.monotonic()+15
    while not socket_path.is_socket() and alive(item) and time.monotonic()<deadline:time.sleep(.1)
    if not socket_path.is_socket():raise RuntimeError('worker_socket_not_ready')
    print(json.dumps({'restarted':item}))


if __name__=='__main__':
    (ROOT/'runtime').mkdir(exist_ok=True)
    if sys.argv[1]=='start':start()
    elif sys.argv[1]=='stop':stop()
    elif sys.argv[1]=='restart-worker':restart_worker(sys.argv[2])
    elif sys.argv[1]=='status':
        items=json.loads(STATE.read_text()) if STATE.exists() else []
        print(json.dumps([{**x,'alive':alive(x)} for x in items]))
    else:raise SystemExit('expected start/stop/status')
