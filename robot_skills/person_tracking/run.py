#!/usr/bin/env python3
SKILL_NAME = 'person_tracking'

# ===== Unified single-function CLI preflight =====
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
            if token in {'find', 'track', 'stop'}:
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

import os
import sys
import json
import types
from pathlib import Path
SKILL_DIR = Path(__file__).resolve().parent
SINGLE_FUNCTION_ROOT = SKILL_DIR
ASSETS_DIR = SKILL_DIR / 'assets'
RUNTIME_DIR = SKILL_DIR / 'runtime'
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault('SINGLE_FUNCTION_ROOT', str(SINGLE_FUNCTION_ROOT))
os.environ.setdefault('SINGLE_FUNCTION_RUNTIME_DIR', str(RUNTIME_DIR))
os.environ.setdefault('FITNESS_SAMPLES_DIR', str(ASSETS_DIR / 'fitness_poses_csvs_out'))
os.environ.setdefault('FITNESS_FONT_PATH', str(ASSETS_DIR / 'movement_count_2' / 'Roboto-Regular.ttf'))
DEFAULT_CAMERA = os.getenv('FACE_CAMERA_ID', os.getenv('VIDEO_SOURCE', '/dev/video22'))
os.environ.setdefault('FACE_CAMERA_ID', DEFAULT_CAMERA)
os.environ.setdefault('FACE_CAMERA_WIDTH', '640')
os.environ.setdefault('FACE_CAMERA_HEIGHT', '640')
os.environ.setdefault('PET_PRELOAD_PET_MODEL', '0')
os.environ.setdefault('PET_PRELOAD_FITNESS_MODELS', '0')


def _ensure_package(package_name):
    if not package_name:
        return None
    pkg = sys.modules.get(package_name)
    if pkg is None:
        pkg = types.ModuleType(package_name)
        pkg.__path__ = []
        sys.modules[package_name] = pkg
    parent_name, _, attr = package_name.rpartition('.')
    if parent_name:
        parent = _ensure_package(parent_name)
        setattr(parent, attr, pkg)
    return pkg

def _publish_namespace(module_name, before_names, *, top_level_aliases=()):
    module = types.ModuleType(module_name)
    module.__file__ = globals().get('__file__')
    module.__package__ = module_name.rpartition('.')[0]
    for key, value in list(globals().items()):
        if key.startswith('__') or key in before_names:
            continue
        if key in {'before_names', 'module_name', 'top_level_aliases'}:
            continue
        setattr(module, key, value)
    package_name, _, attr = module_name.rpartition('.')
    if package_name:
        package = _ensure_package(package_name)
        setattr(package, attr, module)
    sys.modules[module_name] = module
    globals()[attr] = module
    for alias in top_level_aliases:
        sys.modules[alias] = module
        globals()[alias.rpartition('.')[2]] = module
    return module


# ===== Inlined module: skills.runtime_config =====
_before_inline = set(globals())
_run_file = __file__
__file__ = str(SKILL_DIR / 'embedded_sources' / 'single_function_runtime_config.py')

import os
import multiprocessing
from pathlib import Path
PROJECT_ROOT = Path(os.getenv('SINGLE_FUNCTION_ROOT', Path(__file__).resolve().parents[1]))
ASSETS_DIR = PROJECT_ROOT / 'assets'
MODEL_DIR = ASSETS_DIR / 'model'
MOVEMENT_DIR = ASSETS_DIR / 'movement_count_2'
FITNESS_SAMPLES_DIR = Path(os.getenv('FITNESS_SAMPLES_DIR', str(ASSETS_DIR / 'fitness_poses_csvs_out')))
MEDIAPIPE_MODELS_DIR = ASSETS_DIR / 'mediapipe_models'
RUNTIME_DIR = Path(os.getenv('SINGLE_FUNCTION_RUNTIME_DIR', str(PROJECT_ROOT / 'runtime')))
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
FACE_CAMERA_ID = os.getenv('FACE_CAMERA_ID', '/dev/video22')
FACE_DB_PATH = os.getenv('FACE_DB_PATH', str(RUNTIME_DIR / 'faces.db'))
FACE_DATA_DIR = Path(os.getenv('FACE_DATA_DIR', str(PROJECT_ROOT.parent / 'face_data')))
FACE_DATA_DIR.mkdir(parents=True, exist_ok=True)
FACE_IDENTITY_EVENT_PATH = os.getenv('FACE_IDENTITY_EVENT_PATH', str(FACE_DATA_DIR / 'identity_event.json'))
PERSON_TRACKING_IDENTITY_BINDING_PATH = os.getenv(
    'PERSON_TRACKING_IDENTITY_BINDING_PATH',
    str(RUNTIME_DIR / 'identity_binding.json'),
)
Path(PERSON_TRACKING_IDENTITY_BINDING_PATH).parent.mkdir(parents=True, exist_ok=True)
FACE_CAMERA_SHOW_WINDOW = os.getenv('FACE_CAMERA_SHOW_WINDOW', '0')
FACE_CAMERA_USE_SUBPROCESS = os.getenv('FACE_CAMERA_USE_SUBPROCESS', '0')
PET_DETECTOR_MODEL = os.getenv('PET_DETECTOR_MODEL', str(MODEL_DIR / 'yolov8s_rknn3.rknn'))
PET_DETECTOR_WEIGHT = os.getenv('PET_DETECTOR_WEIGHT', str(MODEL_DIR / 'yolov8s_rknn3.weight'))
# V6 reserves the second RK1828 card for person/fitness detection.  Pet
# tracking stays on 0004:41:00.0, so the two independent workloads do not
# contend for one accelerator.
PERSON_TRACKING_RKNN_DEVICE = os.getenv('PERSON_TRACKING_RKNN_DEVICE', '0002:21:00.0')
PET_TRACKING_RESULT_PATH = os.getenv('PET_TRACKING_RESULT_PATH', str(RUNTIME_DIR / 'pet_tracking_result.txt'))
PET_TRACKING_OUTPUT_VIDEO = os.getenv('PET_TRACKING_OUTPUT_VIDEO', str(RUNTIME_DIR / 'pet_tracking_record.mp4'))
PUSHUP_VIDEO_OUTPUT = os.getenv('PUSHUP_VIDEO_OUTPUT', str(RUNTIME_DIR / 'pushup_record.mp4'))
MP_START_METHOD = os.getenv('PET_MP_START_METHOD', 'fork' if os.name == 'posix' else 'spawn')
def get_mp_context():
    try:
        return multiprocessing.get_context(MP_START_METHOD)
    except ValueError:
        return multiprocessing.get_context()
CONTROLLER_CLI_PATH = ''
STRICT_MODEL_PRELOAD = False
PRELOAD_PET_MODEL = False
PRELOAD_FITNESS_MODELS = False
os.environ.setdefault('FACE_CAMERA_ID', FACE_CAMERA_ID)
os.environ.setdefault('FACE_IDENTITY_EVENT_PATH', FACE_IDENTITY_EVENT_PATH)
os.environ.setdefault('PERSON_TRACKING_IDENTITY_BINDING_PATH', PERSON_TRACKING_IDENTITY_BINDING_PATH)
os.environ.setdefault('FACE_CAMERA_SHOW_WINDOW', FACE_CAMERA_SHOW_WINDOW)
os.environ.setdefault('FACE_CAMERA_USE_SUBPROCESS', FACE_CAMERA_USE_SUBPROCESS)
os.environ.setdefault('PET_DETECTOR_MODEL', PET_DETECTOR_MODEL)
os.environ.setdefault('PET_DETECTOR_WEIGHT', PET_DETECTOR_WEIGHT)
os.environ.setdefault('PET_TRACKING_RESULT_PATH', PET_TRACKING_RESULT_PATH)
os.environ.setdefault('PET_TRACKING_OUTPUT_VIDEO', PET_TRACKING_OUTPUT_VIDEO)
os.environ.setdefault('PERSON_TRACKING_CAMERA_FLIP', 'none')
os.environ.setdefault('PERSON_TRACKING_CAMERA_WARMUP_FRAMES', '10')
os.environ.setdefault('PERSON_TRACKING_DEBUG_LOG', str(RUNTIME_DIR / 'person_tracking_debug.log'))
os.environ.setdefault('PERSON_TRACKING_DISABLE_MOTOR', '0')
__file__ = _run_file
_publish_namespace('skills.runtime_config', _before_inline, top_level_aliases=())
del _before_inline

# ===== Inlined module: skills.runtime_models =====
_before_inline = set(globals())
_run_file = __file__
__file__ = str(SKILL_DIR / 'embedded_sources' / 'single_function_runtime_models.py')
import atexit
import threading
from dataclasses import dataclass
from typing import Callable, Dict, Optional


@dataclass
class ModelEntry:
    name: str
    instance: object
    persistent: bool = True
    description: str = ""


_lock = threading.RLock()
_models: Dict[str, ModelEntry] = {}


def register_model(name: str, instance, *, persistent: bool = True, description: str = ""):
    if instance is None:
        raise ValueError(f"cannot register empty model: {name}")
    with _lock:
        _models[name] = ModelEntry(
            name=name,
            instance=instance,
            persistent=persistent,
            description=description,
        )
    return instance


def get_model(name: str, default=None):
    with _lock:
        entry = _models.get(name)
        return entry.instance if entry is not None else default


def has_model(name: str) -> bool:
    with _lock:
        return name in _models and _models[name].instance is not None


def get_or_create(
    name: str,
    factory: Callable[[], object],
    *,
    persistent: bool = True,
    description: str = "",
):
    with _lock:
        entry = _models.get(name)
        if entry is not None and entry.instance is not None:
            return entry.instance

    instance = factory()
    return register_model(name, instance, persistent=persistent, description=description)


def acquire_or_create(
    name: str,
    factory: Callable[[], object],
    *,
    description: str = "",
):
    model = get_model(name)
    if model is not None:
        return model, False
    return factory(), True


def summary():
    with _lock:
        return {
            name: {
                "type": type(entry.instance).__name__,
                "persistent": entry.persistent,
                "description": entry.description,
            }
            for name, entry in _models.items()
        }


def release_all():
    with _lock:
        entries = list(_models.values())
        _models.clear()

    for entry in entries:
        if not entry.persistent:
            continue
        release = getattr(entry.instance, "release", None)
        close = getattr(entry.instance, "close", None)
        try:
            if callable(release):
                release()
            elif callable(close):
                close()
        except Exception as exc:
            print(f"[RuntimeModels] 释放模型失败: {entry.name}: {exc}")


atexit.register(release_all)
__file__ = _run_file
_publish_namespace('skills.runtime_models', _before_inline, top_level_aliases=())
del _before_inline

# ===== Inlined module: skills.speaker =====
_before_inline = set(globals())
_run_file = __file__
__file__ = str(SKILL_DIR / 'embedded_sources' / 'single_function_speaker.py')

# Speech is intentionally disabled in single_function skills.
# Keep the original API shape so camera/model workflows can run without TTS.
_mp_q = None

def init_mp_queue(q=None):
    global _mp_q
    _mp_q = q
    return True

def speak(text):
    return True

def stop():
    return True

def release():
    return True

__file__ = _run_file
_publish_namespace('skills.speaker', _before_inline, top_level_aliases=('speaker',))
del _before_inline

# ===== Inlined module: function.control =====
_before_inline = set(globals())
_run_file = __file__
__file__ = str(SKILL_DIR / 'embedded_sources' / 'function/control.py')
#!/usr/bin/env python3
# encoding: utf-8
# stm32 python sdk (STM32 Python 软件开发工具包)
import enum        # 枚举类型库
import time        # 时间库，用于延时
import copy        # 拷贝库（代码中未直接使用，可能用于扩展）
import queue       # 队列库，用于线程间安全的数据传递
import struct      # 结构体与字节串转换库，用于解析/打包二进制数据
import serial      # 串口通信库 (需要安装 pyserial)
import threading   # 多线程库，用于后台不断接收串口数据

SERIAL_RECONNECT_INTERVAL_SEC = 1.0
SERIAL_LOG_INTERVAL_SEC = 5.0
MAX_PACKET_DATA_LEN = 128

class PacketControllerState(enum.IntEnum):
    # 串口通信协议的解析状态机枚举
    # 协议格式: 0xAA(帧头1) 0x55(帧头2) Length(长度) Function ID(功能码) Data(数据) Checksum(CRC8校验码)
    PACKET_CONTROLLER_STATE_STARTBYTE1 = 0  # 等待接收帧头第一字节 (0xAA)
    PACKET_CONTROLLER_STATE_STARTBYTE2 = 1  # 等待接收帧头第二字节 (0x55)
    PACKET_CONTROLLER_STATE_LENGTH = 2      # 等待接收数据长度
    PACKET_CONTROLLER_STATE_FUNCTION = 3    # 等待接收功能码 (Function ID)
    PACKET_CONTROLLER_STATE_ID = 4          # (保留状态，代码实际未使用)
    PACKET_CONTROLLER_STATE_DATA = 5        # 正在接收数据体
    PACKET_CONTROLLER_STATE_CHECKSUM = 6    # 等待接收并校验校验码

class PacketFunction(enum.IntEnum):
    # 可通过串口实现的控制功能 (Function ID 列表)
    PACKET_FUNC_SYS = 0        # 系统指令 (如获取电池电压)
    PACKET_FUNC_LED = 1        # LED控制
    PACKET_FUNC_BUZZER = 2     # 蜂鸣器控制
    PACKET_FUNC_MOTOR = 3      # 电机控制
    PACKET_FUNC_SPEAKER = 4    # 语音控制
    PACKET_FUNC_WKUP = 5       # 语音唤醒信号
    PACKET_FUNC_KEY = 6        # 获取按键状态
    PACKET_FUNC_HOUSEHOLD = 7  # 家电控制
    PACKET_FUNC_GP2Y = 8       # 获取距离传感器数据
    PACKET_FUNC_LEARN = 9      # 家电学习模式
    PACKET_FUNC_SBUS = 10      # 获取航模遥控器(SBUS接收机)数据
    PACKET_FUNC_NONE = 11      # 无效/空功能

class PacketReportKeyEvents(enum.IntEnum):
    # 按键的不同触发状态枚举 (位掩码)
    KEY_EVENT_PRESSED = 0x01            # 按下
    KEY_EVENT_LONGPRESS = 0x02          # 长按
    KEY_EVENT_LONGPRESS_REPEAT = 0x04   # 长按且保持(重复触发)
    KEY_EVENT_RELEASE_FROM_LP = 0x08    # 从长按状态中释放
    KEY_EVENT_RELEASE_FROM_SP = 0x10    # 从短按状态中释放
    KEY_EVENT_CLICK = 0x20              # 单击
    KEY_EVENT_DOUBLE_CLICK= 0x40        # 双击
    KEY_EVENT_TRIPLE_CLICK = 0x80       # 三连击

#代号	对应的 C 语言类型	含义	占用字节数	取值范围大概是
#B	unsigned char	无符号单字节整数	1 个字节	0 ~ 255
#b	signed char	有符号单字节整数	1 个字节	-128 ~ 127
#H	unsigned short	无符号短整数	2 个字节	0 ~ 65535
#h	short	有符号短整数	2 个字节	-32768 ~ 32767
#f	float	单精度浮点数（带小数）	4 个字节

# 替换后的CRC8函数（与C语言Appl_Crc8Maxim完全一致）
def checksum_crc8(data):
    """
    实现与C语言Appl_Crc8Maxim一致的CRC8校验（Maxim/Dallas标准）
    :param data: 待校验的字节数据（bytes/列表）
    :return: 8位CRC校验值（0~255）
    """
    crc = 0  # 对应C代码的ZERO_INIT
    for byte in data:
        # 确保byte是8位无符号整数（兼容列表/bytes输入）
        byte = byte & 0xFF
        # 第一步：XOR当前字节
        crc ^= byte
        
        # 第二步：逐位处理8位
        for _ in range(8):
            if crc & 0x01:  # 最低位为1
                crc = (crc >> 1) ^ 0x8C  # 右移+异或0x8C（对应C代码的反射多项式）
            else:
                crc = crc >> 1  # 仅右移
            # 确保CRC始终是8位（防止Python int变长）
            crc = crc & 0xFF
    return crc & 0xFF

class SBusStatus:
    # 航模遥控器(SBUS)状态数据结构类
    def __init__(self):
        self.channels = [0] * 16;  # 16个比例通道数据
        self.channel_17 = False    # 数字通道17 (开关量)
        self.channel_18 = False    # 数字通道18 (开关量)
        self.signal_loss = True    # 信号丢失标志
        self.fail_safe = False     # 故障保护标志(失控保护)

class Board:
    # 手柄按键的位掩码字典映射，用于解析手柄按键数据包
    buttons_map = {
            'GAMEPAD_BUTTON_MASK_L2':        0x0001,
            'GAMEPAD_BUTTON_MASK_R2':        0x0002,
            'GAMEPAD_BUTTON_MASK_SELECT':    0x0004,
            'GAMEPAD_BUTTON_MASK_START':     0x0008,
            'GAMEPAD_BUTTON_MASK_L3':        0x0020,  # 左摇杆按下
            'GAMEPAD_BUTTON_MASK_R3':        0x0040,  # 右摇杆按下
            'GAMEPAD_BUTTON_MASK_CROSS':     0x0100,  # X 键
            'GAMEPAD_BUTTON_MASK_CIRCLE':    0x0200,  # O 键
            'GAMEPAD_BUTTON_MASK_SQUARE':    0x0800,  # 方块键
            'GAMEPAD_BUTTON_MASK_TRIANGLE':  0x1000,  # 三角键
            'GAMEPAD_BUTTON_MASK_L1':        0x4000,
            'GAMEPAD_BUTTON_MASK_R1':        0x8000
    }

    def __init__(self, device="/dev/ttyS0", baudrate=115200, timeout=5):
        # 初始化开发板控制对象
        self.device = device
        self.baudrate = baudrate
        self.timeout = timeout
        self.enable_recv = False # 接收线程使能标志
        self.frame = []          # 用于暂存接收到的单个数据包
        self.recv_count = 0      # 记录当前接收到的数据长度
        self.state = PacketControllerState.PACKET_CONTROLLER_STATE_STARTBYTE1 # 初始化状态机
        self.port = None
        self._recv_thread = None
        self._serial_lock = threading.RLock()
        self._last_serial_log = {}
        self._checksum_fail_count = 0
        self._invalid_frame_count = 0
        self._rx_exception_count = 0
        self._tx_exception_count = 0
        
        # 定义线程锁，防止在读取舵机返回数据时被其他线程干扰
        self.servo_read_lock = threading.Lock()
        self.pwm_servo_read_lock = threading.Lock()
        
        # 为每种传感器/数据定义一个容量为1的队列，确保读取时获取的是最新的一帧数据
        self.sys_queue = queue.Queue(maxsize=1)
        self.bus_servo_queue = queue.Queue(maxsize=1)
        self.pwm_servo_queue = queue.Queue(maxsize=1)
        self.key_queue = queue.Queue(maxsize=1)
        self.imu_queue = queue.Queue(maxsize=1)
        self.gamepad_queue = queue.Queue(maxsize=1)
        self.sbus_queue = queue.Queue(maxsize=1)
        self.wkup_queue = queue.Queue(maxsize=1)
        self.gp2y_queue = queue.Queue(maxsize=1)

        # 注册各功能码对应的数据处理回调函数
        self.parsers = {
            PacketFunction.PACKET_FUNC_SYS: self.packet_report_sys,
            PacketFunction.PACKET_FUNC_KEY: self.packet_report_key,
            PacketFunction.PACKET_FUNC_SBUS: self.packet_report_sbus,
            PacketFunction.PACKET_FUNC_WKUP: self.packet_report_wkup,
            PacketFunction.PACKET_FUNC_GP2Y: self.packet_report_gp2y
        }

        # 打开串口，默认为 /dev/ttyS0, 波特率 115200。失败时进入降级模式，
        # 接收线程会持续重连，避免串口短时异常拖垮语音/视觉主流程。
        self._open_port(initial=True)

    def _reset_parser_state(self):
        self.state = PacketControllerState.PACKET_CONTROLLER_STATE_STARTBYTE1
        self.frame = []
        self.recv_count = 0

    def _log_serial_event(self, key, message, interval=SERIAL_LOG_INTERVAL_SEC):
        now = time.time()
        last_time, suppressed = self._last_serial_log.get(key, (0.0, 0))
        if now - last_time >= interval:
            suffix = f"（期间抑制 {suppressed} 次）" if suppressed else ""
            print(f"{message}{suffix}")
            self._last_serial_log[key] = (now, 0)
        else:
            self._last_serial_log[key] = (last_time, suppressed + 1)

    def _close_port(self):
        with self._serial_lock:
            try:
                if self.port is not None and self.port.is_open:
                    self.port.close()
            except Exception:
                pass
            finally:
                self.port = None
                self._reset_parser_state()

    def _open_port(self, initial=False):
        with self._serial_lock:
            try:
                if self.port is not None and self.port.is_open:
                    return True
            except Exception:
                self.port = None

            try:
                self.port = serial.Serial(self.device, self.baudrate, timeout=self.timeout)
                self._reset_parser_state()
                if not initial:
                    self._log_serial_event("reopen", f"[串口恢复] 已重连 {self.device}", interval=0.0)
                return True
            except (serial.SerialException, OSError) as e:
                self.port = None
                level = "初始化失败" if initial else "重连失败"
                self._log_serial_event("open_failed", f"[串口告警] {level}: {self.device}, {e}")
                return False

    def _ensure_port_open(self):
        try:
            if self.port is not None and self.port.is_open:
                return True
        except Exception:
            self.port = None
        return self._open_port(initial=False)


    # ---- 以下是各种数据的接收回调函数 ----
    # put_nowait 表示非阻塞存入队列，如果队列满（抛出 queue.Full 异常）则忽略，直接丢弃旧数据
    def packet_report_sys(self, data):
        try:
            self.sys_queue.put_nowait(data)
        except queue.Full:
            pass

    def packet_report_key(self, data):
        try:
            self.key_queue.put_nowait(data)
        except queue.Full:
            pass

    def packet_report_imu(self, data):
        try:
            self.imu_queue.put_nowait(data)
        except queue.Full:
            pass

    def packet_report_gamepad(self, data):
        try:
            self.gamepad_queue.put_nowait(data)
        except queue.Full:
            pass

    def packet_report_serial_servo(self, data):
        try:
            self.bus_servo_queue.put_nowait(data)
        except queue.Full:
            pass

    def packet_report_pwm_servo(self, data):
        try:
            self.pwm_servo_queue.put_nowait(data)
        except queue.Full:
            pass

    def packet_report_sbus(self, data):
        try:
            self.sbus_queue.put_nowait(data)
        except queue.Full:
            pass

    def packet_report_wkup(self, data):
        try:
            self.wkup_queue.put_nowait(data)
        except queue.Full:
            pass

    def packet_report_gp2y(self, data):
        try:
            self.gp2y_queue.put_nowait(data)
        except queue.Full:
            pass

    # ---- 以下是用户获取数据的接口方法 ----
    def get_battery(self):
        # 获取电池电压
        if self.enable_recv:
            try:
                data = self.sys_queue.get(block=False) # 从队列中尝试获取数据
                if data[0] == 0x04: # 0x04 为电压上报的子命令码
                    # <H 代表小端模式下的 unsigned short (2字节)
                    return struct.unpack('<H', data[1:])[0] 
                else:
                    return None
            except queue.Empty:
                return None
        else:
            print('enable reception first!') # 提示需要先开启接收线程
            return None

    def get_button(self):
        # 获取板载按键状态
        if self.enable_recv:
            try:
                data = self.key_queue.get(block=False)
                key_id = data[0] # 按键ID
                key_event = PacketReportKeyEvents(data[1]) # 按键事件
                if key_event == PacketReportKeyEvents.KEY_EVENT_CLICK:
                    return key_id, 0  # 单击返回 0
                elif key_event == PacketReportKeyEvents.KEY_EVENT_PRESSED:
                    return key_id, 1  # 按下返回 1
            except queue.Empty:
                return None
        else:
            print('enable reception first!')
            return None

    def get_imu(self):
        # 获取IMU数据 (6轴: 加速度x,y,z 角速度x,y,z)
        if self.enable_recv:
            try:
                # <6f 代表解包成 6个小端模式的浮点数(float, 每个4字节)
                return struct.unpack('<6f', self.imu_queue.get(block=False))
            except queue.Empty:
                return None
        else:
            print('enable reception first!')
            return None

    def get_gamepad(self):
        # 获取无线手柄解析数据
        if self.enable_recv:
            try:
                # 解包手柄原始数据: H(2字节无符号整数:按键), B(1字节:十字键方向), 4b(4个1字节有符号整数:摇杆)
                gamepad_data = struct.unpack("<HB4b", self.gamepad_queue.get(block=False))
                
                # 初始化摇杆和十字键轴数据阵列: 'lx', 'ly', 'rx', 'ry', 'r2', 'l2', 'hat_x', 'hat_y'
                axes = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
                
                # 初始化按键阵列对应关系 (16个元素对应手柄的所有独立按键)
                buttons = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0] 
                
                # 遍历掩码字典，如果对应位为1，说明该键被按下
                for b in self.buttons_map:
                    if self.buttons_map[b] & gamepad_data[0]:
                        if b == 'GAMEPAD_BUTTON_MASK_R2':
                            axes[4] = 1.0     # R2线性扳机简化为按键映射
                        elif b == 'GAMEPAD_BUTTON_MASK_L2':
                            axes[5] = 1.0     # L2线性扳机简化为按键映射
                        # 记录按键状态为1
                        elif b == 'GAMEPAD_BUTTON_MASK_CROSS': buttons[0] = 1
                        elif b == 'GAMEPAD_BUTTON_MASK_CIRCLE': buttons[1] = 1
                        elif b == 'GAMEPAD_BUTTON_MASK_SQUARE': buttons[3] = 1
                        elif b == 'GAMEPAD_BUTTON_MASK_TRIANGLE': buttons[4] = 1
                        elif b == 'GAMEPAD_BUTTON_MASK_L1': buttons[6] = 1
                        elif b == 'GAMEPAD_BUTTON_MASK_R1': buttons[7] = 1
                        elif b == 'GAMEPAD_BUTTON_MASK_SELECT': buttons[10] = 1
                        elif b == 'GAMEPAD_BUTTON_MASK_START': buttons[11] = 1
               
                # 处理摇杆数据，将其归一化到 -1.0 到 1.0 的浮点数
                if gamepad_data[2] > 0: axes[0] = -gamepad_data[2] / 127
                elif gamepad_data[2] < 0: axes[0] = -gamepad_data[2] / 128

                if gamepad_data[3] > 0: axes[1] = gamepad_data[3] / 127
                elif gamepad_data[3] < 0: axes[1] = gamepad_data[3] / 128

                if gamepad_data[4] > 0: axes[2] = -gamepad_data[4] / 127
                elif gamepad_data[4] < 0: axes[2] = -gamepad_data[4] / 128

                if gamepad_data[5] > 0: axes[3] = gamepad_data[5] / 127
                elif gamepad_data[5] < 0: axes[3] = gamepad_data[5] / 128
            
                # 处理十字键(Hat)方向，依据特定的数值映射出XY方向的值
                if gamepad_data[1] == 9: axes[6] = 1.0
                elif gamepad_data[1] == 13: axes[6] = -1.0
                if gamepad_data[1] == 11: axes[7] = -1.0
                elif gamepad_data[1] == 15: axes[7] = 1.0
                
                return axes, buttons # 返回摇杆轴(浮点)和按键(0/1)列表
            except queue.Empty:
                return None
        else:
            print('enable reception first!')
            return None

    def get_sbus(self):
        # 获取SBUS航模遥控器数据
        if self.enable_recv:
            try:
                sbus_data = self.sbus_queue.get(block=False)
                status = SBusStatus()
                # 解包16个h(short整形通道数据) + 4个B(状态标志位)
                *status.channels, ch17, ch18, sig_loss, fail_safe = struct.unpack("<16hBBBB", sbus_data)
                
                # 转换状态标志为布尔值
                status.channel_17 = ch17 != 0
                status.channel_18 = ch18 != 0
                status.signal_loss = sig_loss != 0
                status.fail_safe = fail_safe != 0
                
                data = []
                if status.signal_loss:
                    # 如果丢失信号，所有通道回中(0.5)，将油门等关键通道设为0
                    data = 16 * [0.5]
                    data[4] = 0
                    data[5] = 0
                    data[6] = 0
                    data[7] = 0
                else:
                    # 将SBUS原始数据(通常在192-1792之间)归一化为 0.0 到 1.0
                    for i in status.channels:
                        data.append((i - 192)/(1792 - 192))
                return data
            except queue.Empty:
                return None
        else:
            print('enable reception first!')
            return None

    def get_wkup(self):
        # 获取语音唤醒信号
        if self.enable_recv:
            try:
                data = self.wkup_queue.get(block=False)
                # 解析参数：1为高电平，0为低电平
                level = data[0]
                return level
            except queue.Empty:
                return None
        else:
            print('enable reception first!')
            return None
        
    def get_gp2y(self):
        # 获取距离传感器数据
        distance = []
        if self.enable_recv:
            try:
                data = self.gp2y_queue.get(block=False)
                distance.append(data[0])
                distance.append(data[1])
                distance.append(data[2])
                distance.append(data[3])
                return distance
            except queue.Empty:
                return None
        else:
            print('enable reception first!')
            return None


    # ---- 以下是通信发送核心逻辑 ----
    def buf_write(self, func, data):
        # 通用数据包封装发送函数
        buf = bytearray([0xAA, 0x55, int(func)]) # 帧头1，帧头2，功能码
        buf.append(len(data))         # 写入数据长度
        buf.extend(data)              # 写入实际数据
        buf.append(checksum_crc8(bytes(buf[2:]))) # 计算并写入功能码+长度+数据的 CRC8 校验值
        with self._serial_lock:
            if not self._ensure_port_open():
                self._tx_exception_count += 1
                self._log_serial_event("tx_no_port", f"[串口告警] 发送失败，端口未就绪: {self.device}")
                return False
            try:
                self.port.write(bytes(buf))          # 通过串口发送出去
                return True
            except (serial.SerialException, OSError) as e:
                self._tx_exception_count += 1
                self._log_serial_event("tx_exception", f"[串口告警] 发送异常: {e}，将重连 {self.device}")
                self._close_port()
                return False

    # ---- 以下是开发板各硬件的控制命令 ----
    def set_led(self, on_time, off_time, repeat=1, led_id=1):
        # 控制板载LED闪烁 (亮的时间秒，灭的时间秒，重复次数，LED编号)
        on_time = int(on_time*1000) # 转换为毫秒
        off_time = int(off_time*1000)
        self.buf_write(PacketFunction.PACKET_FUNC_LED, struct.pack("<BHHH", led_id, on_time, off_time, repeat))

    def set_buzzer(self, freq, on_time, off_time, repeat=1):
        # 控制蜂鸣器发声 (频率Hz，响时间秒，停时间秒，重复次数)
        on_time = int(on_time*1000)
        off_time = int(off_time*1000)
        self.buf_write(PacketFunction.PACKET_FUNC_BUZZER, struct.pack("<HHHH", freq, on_time, off_time, repeat))

    def set_motor_speed(self, speeds):
        # 设置电机速度, speeds 是一个二维列表如: [[电机ID1, 速度1], [电机ID2, 速度2]]
        data = [0x01, len(speeds)] # 0x01为控制子命令，后面跟要控制的电机数量
        for i in speeds:
            # 电机ID通常习惯1开始，底层接收从0开始，所以 i[0]-1。速度为浮点数
            data.extend(struct.pack("<Bf", int(i[0]), float(i[1])))
        self.buf_write(PacketFunction.PACKET_FUNC_MOTOR, data)

    def set_single_motor_speed(self, motor_id, speed):
        data =[0x00]
        data.extend(struct.pack("<Bf", motor_id, speed))
        self.buf_write(PacketFunction.PACKET_FUNC_MOTOR, data)

    def set_household(self, state):
        self.buf_write(PacketFunction.PACKET_FUNC_HOUSEHOLD, struct.pack("<B", state))

    def set_learn(self, state):
        self.buf_write(PacketFunction.PACKET_FUNC_LEARN, struct.pack("<B", state))

    def set_speaker(self, state):
        self.buf_write(PacketFunction.PACKET_FUNC_SPEAKER, struct.pack("<B", state))


    # ---- PWM 舵机相关操作 ----
    def pwm_servo_set_position(self, duration, positions):
        # 控制PWM舵机转动到指定位置 (运行时间秒，位置二维列表 [[ID, 脉宽], ...])
        duration = int(duration * 1000) # 转毫秒
        data = [0x01, duration & 0xFF, 0xFF & (duration >> 8), len(positions)] # 命令码，时间低位，时间高位，舵机数量
        for i in positions:
            data.extend(struct.pack("<BH", i[0], i[1])) # 打包舵机ID和脉宽值(通常500-2500)
        self.buf_write(PacketFunction.PACKET_FUNC_PWM_SERVO, data)
    
    def pwm_servo_set_offset(self, servo_id, offset):
        # 设置PWM舵机偏差值(修正零位)
        data = struct.pack("<BBb", 0x07, servo_id, int(offset))
        self.buf_write(PacketFunction.PACKET_FUNC_PWM_SERVO, data)

    def pwm_servo_read_and_unpack(self, servo_id, cmd, unpack):
        # 通用的PWM舵机读取逻辑 (附带线程锁以保证收发对应)
        with self.servo_read_lock: # 加锁
            self.buf_write(PacketFunction.PACKET_FUNC_PWM_SERVO, [cmd, servo_id]) # 发送读取请求
            data = self.pwm_servo_queue.get(block=True) # 阻塞等待返回数据
            servo_id, cmd, info = struct.unpack(unpack, data) # 按指定的解包格式解析
            return info

    def pwm_servo_read_offset(self, servo_id):
        # 读取PWM舵机偏差
        return self.pwm_servo_read_and_unpack(servo_id, 0x09, "<BBb")

    def pwm_servo_read_position(self, servo_id):
        # 读取PWM舵机当前位置脉宽
        return self.pwm_servo_read_and_unpack(servo_id, 0x05, "<BBH")

    # ---- 串行总线舵机(Bus Servo)相关操作 ----
    def bus_servo_enable_torque(self, servo_id, enable):
        # 使能/卸载 总线舵机扭矩 (上电/掉电)
        if enable:
            data = struct.pack("<BB", 0x0B, servo_id)
        else:
            data = struct.pack("<BB", 0x0C, servo_id)
        self.buf_write(PacketFunction.PACKET_FUNC_BUS_SERVO, data)
        time.sleep(0.02) # 等待执行完毕

    def bus_servo_set_id(self, servo_id_now, servo_id_new):
        # 修改总线舵机ID (原ID, 新ID)
        data = struct.pack("<BBB", 0x10, servo_id_now, servo_id_new)
        self.buf_write(PacketFunction.PACKET_FUNC_BUS_SERVO, data)
        time.sleep(0.02)

    def bus_servo_set_offset(self, servo_id, offset):
        # 设置总线舵机中位偏差
        data = struct.pack("<BBb", 0x20, servo_id, int(offset))
        self.buf_write(PacketFunction.PACKET_FUNC_BUS_SERVO, data)
        time.sleep(0.02)

    def bus_servo_save_offset(self, servo_id):
        # 保存偏差到总线舵机的内部Flash中 (掉电不丢失)
        data = struct.pack("<BB", 0x24, servo_id)
        self.buf_write(PacketFunction.PACKET_FUNC_BUS_SERVO, data)
        time.sleep(0.02)

    def bus_servo_set_angle_limit(self, servo_id, limit):
        # 设置总线舵机旋转角度限制 (limit为包含最小最大角度的列表)
        data = struct.pack("<BBHH", 0x30, servo_id, int(limit[0]), int(limit[1]))
        self.buf_write(PacketFunction.PACKET_FUNC_BUS_SERVO, data)
        time.sleep(0.02)

    def bus_servo_set_vin_limit(self, servo_id, limit):
        # 设置总线舵机输入电压报警限制
        data = struct.pack("<BBHH", 0x34, servo_id, int(limit[0]), int(limit[1]))
        self.buf_write(PacketFunction.PACKET_FUNC_BUS_SERVO, data)
        time.sleep(0.02)

    def bus_servo_set_temp_limit(self, servo_id, limit):
        # 设置总线舵机内部温度报警限制
        data = struct.pack("<BBb", 0x38, servo_id, int(limit))
        self.buf_write(PacketFunction.PACKET_FUNC_BUS_SERVO, data)
        time.sleep(0.02)

    def bus_servo_stop(self, servo_id):
        # 急停指定的总线舵机，servo_id 为舵机ID列表
        data = [0x03, len(servo_id)] 
        data.extend(struct.pack("<"+'B'*len(servo_id), *servo_id))
        self.buf_write(PacketFunction.PACKET_FUNC_BUS_SERVO, data)

    def bus_servo_set_position(self, duration, positions):
        # 控制多个总线舵机运动到指定位置 (运动时间秒，[[ID1, 位置1], [ID2, 位置2]])
        duration = int(duration * 1000)
        data = [0x01, duration & 0xFF, 0xFF & (duration >> 8), len(positions)]
        for i in positions:
            data.extend(struct.pack("<BH", i[0], i[1]))
        self.buf_write(PacketFunction.PACKET_FUNC_BUS_SERVO, data)

    def bus_servo_read_and_unpack(self, servo_id, cmd, unpack):
        # 通用的总线舵机参数读取与解包逻辑
        with self.servo_read_lock:
            self.buf_write(PacketFunction.PACKET_FUNC_BUS_SERVO, [cmd, servo_id])
            data = self.bus_servo_queue.get(block=True)
            # 总线舵机的返回数据多了一个 success 标志位
            servo_id, cmd, success, *info = struct.unpack(unpack, data)
            if success == 0: # 0 表示通信读取成功
                return info

    # 以下均为总线舵机各种参数的读取封装
    def bus_servo_read_id(self, servo_id=254):
        # 默认向 254(广播ID)发送查询命令，获取当前连接舵机的真实ID
        return self.bus_servo_read_and_unpack(servo_id, 0x12, "<BBbB")

    def bus_servo_read_offset(self, servo_id):
        return self.bus_servo_read_and_unpack(servo_id, 0x22, "<BBbb")
    
    def bus_servo_read_position(self, servo_id):
        return self.bus_servo_read_and_unpack(servo_id, 0x05, "<BBbh")

    def bus_servo_read_vin(self, servo_id):
        return self.bus_servo_read_and_unpack(servo_id, 0x07, "<BBbH")
    
    def bus_servo_read_temp(self, servo_id):
        return self.bus_servo_read_and_unpack(servo_id, 0x09, "<BBbB")

    def bus_servo_read_temp_limit(self, servo_id):
        return self.bus_servo_read_and_unpack(servo_id, 0x3A, "<BBbB")

    def bus_servo_read_angle_limit(self, servo_id):
        return self.bus_servo_read_and_unpack(servo_id, 0x32, "<BBb2H")

    def bus_servo_read_vin_limit(self, servo_id):
        return self.bus_servo_read_and_unpack(servo_id, 0x36, "<BBb2H")

    def bus_servo_read_torque_state(self, servo_id):
        return self.bus_servo_read_and_unpack(servo_id, 0x0D, "<BBbb")
    


    # ---- 串口数据后台接收与解析任务 ----
    def enable_reception(self):
        # 开启接收功能，启动一个守护线程后台运行 recv_task
        self.enable_recv = True
        if self._recv_thread is None or not self._recv_thread.is_alive():
            self._recv_thread = threading.Thread(target=self.recv_task, daemon=True)
            self._recv_thread.start()

    def _handle_recv_byte(self, dat):
        # 状态机：寻找第一个帧头 0xAA
        if self.state == PacketControllerState.PACKET_CONTROLLER_STATE_STARTBYTE1:
            if dat == 0xAA:
                self.state = PacketControllerState.PACKET_CONTROLLER_STATE_STARTBYTE2
            return

        # 状态机：寻找第二个帧头 0x55
        if self.state == PacketControllerState.PACKET_CONTROLLER_STATE_STARTBYTE2:
            if dat == 0x55:
                self.state = PacketControllerState.PACKET_CONTROLLER_STATE_FUNCTION
            else:
                self._invalid_frame_count += 1
                self._reset_parser_state()
            return

        # 状态机：记录功能码 Function ID
        if self.state == PacketControllerState.PACKET_CONTROLLER_STATE_FUNCTION:
            if dat < int(PacketFunction.PACKET_FUNC_NONE):
                self.frame = [dat, 0]
                self.state = PacketControllerState.PACKET_CONTROLLER_STATE_LENGTH
            else:
                self._invalid_frame_count += 1
                self._log_serial_event("invalid_func", f"[串口告警] 非法功能码: {dat}")
                self._reset_parser_state()
            return

        # 状态机：记录数据长度
        if self.state == PacketControllerState.PACKET_CONTROLLER_STATE_LENGTH:
            if dat > MAX_PACKET_DATA_LEN:
                self._invalid_frame_count += 1
                self._log_serial_event("invalid_length", f"[串口告警] 非法数据长度: {dat}")
                self._reset_parser_state()
                return
            self.frame[1] = dat
            self.recv_count = 0
            self.state = (
                PacketControllerState.PACKET_CONTROLLER_STATE_CHECKSUM
                if dat == 0
                else PacketControllerState.PACKET_CONTROLLER_STATE_DATA
            )
            return

        # 状态机：接收数据体
        if self.state == PacketControllerState.PACKET_CONTROLLER_STATE_DATA:
            self.frame.append(dat)
            self.recv_count += 1
            if self.recv_count >= self.frame[1]:
                self.state = PacketControllerState.PACKET_CONTROLLER_STATE_CHECKSUM
            return

        # 状态机：接收校验码并验证
        if self.state == PacketControllerState.PACKET_CONTROLLER_STATE_CHECKSUM:
            crc8 = checksum_crc8(bytes(self.frame))
            if crc8 == dat:
                try:
                    func = PacketFunction(self.frame[0])
                    data = bytes(self.frame[2:])
                    parser = self.parsers.get(func)
                    if parser is not None:
                        parser(data)
                except Exception as e:
                    self._log_serial_event("parser_exception", f"[串口告警] 数据解析异常: {e}")
            else:
                self._checksum_fail_count += 1
                self._log_serial_event(
                    "checksum_failed",
                    f"[串口校验] CRC失败，累计={self._checksum_fail_count}",
                )
            self._reset_parser_state()

    def recv_task(self):
        # 后台死循环：不断从串口读取数据，并通过状态机解析数据包
        while self.enable_recv:
            if not self._ensure_port_open():
                time.sleep(SERIAL_RECONNECT_INTERVAL_SEC)
                continue

            try:
                recv_data = self.port.read(1) # 读取数据（利用了 serial 初始化时的 timeout 配置）
            except (serial.SerialException, OSError) as e:
                self._rx_exception_count += 1
                self._log_serial_event("rx_exception", f"[串口告警] 接收异常: {e}，将重连 {self.device}")
                self._close_port()
                time.sleep(SERIAL_RECONNECT_INTERVAL_SEC)
                continue
            if recv_data:
                for dat in recv_data:
                    self._handle_recv_byte(dat)
        # 退出循环后关闭串口
        self._close_port()
        print("END...")

# ---- 以下是功能测试函数(供演示/调试用) ----
def bus_servo_test(board):
    # 总线舵机测试例程
    board.bus_servo_set_position(1, [[1, 500], [2, 500]]) # 1号和2号舵机用1秒走到位置500
    time.sleep(1)
    board.bus_servo_set_position(2, [[1, 0], [2, 0]])     # 用2秒走到位置0
    time.sleep(1)
    board.bus_servo_stop([1, 2])                          # 停止运动
    time.sleep(1)
    
    servo_id = 1
    board.bus_servo_set_id(254, servo_id) # 将连在板子上的任意ID的舵机修改为ID=1
    servo_id = board.bus_servo_read_id()  # 读取测试
    if servo_id is not None:
        servo_id = servo_id[0]
        
        # 测试各类参数设置与读取
        offset_set = -10
        board.bus_servo_set_offset(servo_id, offset_set)
        board.bus_servo_save_offset(servo_id)
        
        vin_l, vin_h = 4500, 14500
        board.bus_servo_set_vin_limit(servo_id, [vin_l, vin_h])

        temp_limit = 85
        board.bus_servo_set_temp_limit(servo_id, temp_limit)

        angle_l, angle_h = 0, 1000
        board.bus_servo_set_angle_limit(servo_id, [angle_l, angle_h])
        
        board.bus_servo_enable_torque(servo_id, 1) # 使能扭矩

        # 打印读取的各种状态
        print('id:', board.bus_servo_read_id(servo_id))
        print('offset:', board.bus_servo_read_offset(servo_id), offset_set)
        print('vin:', board.bus_servo_read_vin(servo_id))
        print('temp:', board.bus_servo_read_temp(servo_id))
        print('position:', board.bus_servo_read_position(servo_id))
        print('angle_limit:', board.bus_servo_read_angle_limit(servo_id), [angle_l, angle_h])
        print('vin_limit:', board.bus_servo_read_vin_limit(servo_id), [vin_l, vin_h])
        print('temp_limit:', board.bus_servo_read_temp_limit(servo_id), temp_limit)
        print('torque_state:', board.bus_servo_read_torque_state(servo_id))

def pwm_servo_test(board):
    # PWM舵机测试例程
    servo_id = 1
    board.pwm_servo_set_position(0.5, [[servo_id, 1500]]) # 0.5秒走到1500(中位)
    board.pwm_servo_set_offset(servo_id, 0)
    print('offset:', board.pwm_servo_read_offset(servo_id))
    print('position:', board.pwm_servo_read_position(servo_id))


def set_motor(speed_right, speed_left, max_speed):
    # 电机控制测试例程，正值为正转，负值为反
    speed_right_real = max_speed * speed_right
    speed_left_real = max_speed * speed_left * (-1)
    board = Board()              # 实例化底层控制对象
    board.set_motor_speed([[1, speed_right_real], [2, speed_left_real]])

def set_rotation(speed):
    board = Board()
    board.set_single_motor_speed(3,speed)


def set_household(state):
    # 0—投食机触发一次投食功能
    # 1—灯power on
    # 2—灯 power off
    # 3—风扇 power control
    # 4—风扇 enable control
    # 5—风扇 rotate control
    print("state-----------------------------")
    board = Board()
    board.set_household(state)

# def set_household_learn(state):
#     # 0—投食机触发一次投食功能
#     # 1—灯power on
#     # 2—灯 power off
#     # 3—风扇 power control
#     # 4—风扇 enable control
#     # 5—风扇 rotate control
#     board = Board()
#     board.set_learn(state)

def set_speaker(state):
    # 0-关闭
    # 1—开启
    board = Board()
    board.set_speaker(state)



# 主程序执行入口
if False and __name__ == "__main__":
    board = Board()              # 实例化底层控制对象
    board.enable_reception()     # 必须先开启串口接收守护线程
    print("START...")

    while True:
        try:

            # 唤醒功能测试
            # wkup = board.get_wkup() # 获取语音唤醒状态
            # if wkup is not None:
            #     print("唤醒状态:", wkup)
            # time.sleep(0.01) # 短暂休眠，防止CPU占用过高

            # gp2y = board.get_gp2y() # 获取GP2Y距离传感器数据
            # if gp2y is not None:
            #     print("GP2Y距离:", gp2y)
            # time.sleep(0.01) # 短暂休眠，防止CPU占用过高

            # 电机控制测试（通过输入命令控制前进、后退、左转、右转）
            print("print(w:front,s:behind,a:left,d:right,q:quit,e:+rotate,r:-rotate,\n0:household,1:light on,2:light off,3:fan power control,\n4:fan enable control,5:fan rotate control):")
            word = input()
            if word == 'w':
                set_motor(0.1, 0.1, 100)
            elif word == 's':
                set_motor(-0.1, -0.1, 100)
            elif word == 'a':
                set_motor(-0.1, 0.1, 100)
            elif word == 'd':
                set_motor(0.1, -0.1, 100)
            elif word == 'e':
                set_rotation(100)
            elif word == 'r':
                set_rotation(-100)
            elif word == '0':
                set_household(0) 
            elif word == '1':
                set_household(1)
            elif word == '2':
                set_household(2)  
            elif word == '3':
                set_household(3)  
            elif word == '4':
                set_household(4)  
            elif word == '5':
                set_household(5)              
            elif word == 'q':
                set_motor(0, 0, 100)
                set_rotation(0)
                break
        except KeyboardInterrupt:
            break
__file__ = _run_file
_publish_namespace('function.control', _before_inline, top_level_aliases=('test3',))
del _before_inline

_before_inline = set(globals())
_run_file = __file__
__file__ = str(SKILL_DIR / 'embedded_sources' / 'llm/pet_camera.py')
import argparse
import os
import sys
import cv2
import numpy as np
import time
import math
import signal
import threading
import multiprocessing
import subprocess
from enum import Enum, auto
from typing import List, Optional
import warnings
import queue
from contextlib import contextmanager

_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CURRENT_DIR)
for _path in (_CURRENT_DIR, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from skills.speaker import speak
from rknn3lite.api import RKNN3Lite
from skills import runtime_config, runtime_models

warnings.filterwarnings("ignore")


def _single_function_emit_ready(skill_name, text):
    if os.getenv("SINGLE_FUNCTION_SPEECH_EVENTS", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    import json as _json
    print(_json.dumps({
        "event": "skill_ready",
        "skill_name": skill_name,
        "kind": "ready",
        "text": text,
    }, ensure_ascii=False), flush=True)


def _single_function_wait_start_gate(start_gate_path, should_stop=None):
    if not start_gate_path:
        return True
    gate = Path(start_gate_path)
    while True:
        if gate.exists():
            return True
        if callable(should_stop) and should_stop():
            return False
        time.sleep(0.05)


def _single_function_emit_progress(skill_name, **payload):
    if os.getenv("SINGLE_FUNCTION_SPEECH_EVENTS", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    import json as _json
    data = {"event": "skill_progress", "skill_name": skill_name, "kind": "progress"}
    data.update(payload)
    print(_json.dumps(data, ensure_ascii=False), flush=True)


class PetTrackingSystem:
    DETECTOR_MODEL = runtime_config.PET_DETECTOR_MODEL
    WEIGHT_MODEL = runtime_config.PET_DETECTOR_WEIGHT

    DET_CONF = 0.25

    TRACK_SEARCH_TIMEOUT_SEC = 18.0
    TRACK_RECORD_DURATION_SEC = 15.0

    TRACK_SEARCH_SPIN_SPEED = float(os.getenv("PERSON_TRACK_SEARCH_SPIN_SPEED", "0.22"))

    TRACK_OUTPUT_VIDEO_PATH = runtime_config.PET_TRACKING_OUTPUT_VIDEO
    TRACK_RESULT_PATH = runtime_config.PET_TRACKING_RESULT_PATH
    IDENTITY_EVENT_PATH = runtime_config.FACE_IDENTITY_EVENT_PATH
    IDENTITY_BINDING_PATH = runtime_config.PERSON_TRACKING_IDENTITY_BINDING_PATH
    _last_motor_debug_log = (0.0, None)

    GUI_AVAILABLE: Optional[bool] = None

    COCO80_NAMES = [
        "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
        "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
        "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
        "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
        "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
        "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
        "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
        "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
        "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
        "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
        "toothbrush"
    ]

    COCO_91 = {
        1: "person", 2: "bicycle", 3: "car", 4: "motorcycle", 5: "airplane",
        6: "bus", 7: "train", 8: "truck", 9: "boat", 10: "traffic light",
        11: "fire hydrant", 13: "stop sign", 14: "parking meter", 15: "bench",
        16: "bird", 17: "cat", 18: "dog", 19: "horse", 20: "sheep",
        21: "cow", 22: "elephant", 23: "bear", 24: "zebra", 25: "giraffe",
        27: "backpack", 28: "umbrella", 31: "handbag", 32: "tie", 33: "suitcase",
        34: "frisbee", 35: "skis", 36: "snowboard", 37: "sports ball", 38: "kite",
        39: "baseball bat", 40: "baseball glove", 41: "skateboard", 42: "surfboard",
        43: "tennis racket", 44: "bottle", 46: "wine glass", 47: "cup",
        48: "fork", 49: "knife", 50: "spoon", 51: "bowl", 52: "banana",
        53: "apple", 54: "sandwich", 55: "orange", 56: "broccoli", 57: "carrot",
        58: "hot dog", 59: "pizza", 60: "donut", 61: "cake", 62: "chair",
        63: "couch", 64: "potted plant", 65: "bed", 67: "dining table", 70: "toilet",
        72: "tv", 73: "laptop", 74: "mouse", 75: "remote", 76: "keyboard",
        77: "cell phone", 78: "microwave", 79: "oven", 80: "toaster",
        81: "sink", 82: "refrigerator", 84: "book", 85: "clock", 86: "vase",
        87: "scissors", 88: "teddy bear", 89: "hair drier", 90: "toothbrush"
    }

    class RectF:
        def __init__(self, left, top, right, bottom):
            self.left = float(left)
            self.top = float(top)
            self.right = float(right)
            self.bottom = float(bottom)

        def width(self):
            return max(0.0, self.right - self.left)

        def height(self):
            return max(0.0, self.bottom - self.top)

        def centerX(self):
            return (self.left + self.right) * 0.5

        def centerY(self):
            return (self.top + self.bottom) * 0.5

    class Detection:
        def __init__(self, title, confidence, rect):
            self.title = title
            self.confidence = float(confidence)
            self.rect = rect

    class TrackerState(Enum):
        IDLE = auto()
        TRACKING = auto()
        BUFFER_WAIT = auto()
        SEARCHING = auto()

    class DrawBox:
        def __init__(self, rect, title: str, score: float, is_target: bool):
            self.rect = rect
            self.title = title
            self.score = score
            self.is_target = is_target

    class FPSCounter:
        def __init__(self, smooth: float = 0.90):
            self.smooth = float(smooth)
            self.last_time = None
            self.fps = 0.0

        def update(self) -> float:
            now = time.perf_counter()

            if self.last_time is None:
                self.last_time = now
                return self.fps

            dt = now - self.last_time
            self.last_time = now

            if dt <= 1e-6:
                return self.fps

            instant_fps = 1.0 / dt

            if self.fps <= 0.0:
                self.fps = instant_fps
            else:
                self.fps = self.smooth * self.fps + (1.0 - self.smooth) * instant_fps

            return self.fps

    class DummyBoard:
        def set_motor_speed(self, speeds):
            PetTrackingSystem._debug_log(f"DummyBoard drop motor command: {speeds}")

    class MotorQueueProxy:
        def __init__(self, motor_mp_q):
            self.motor_mp_q = motor_mp_q

        def set_motor_speed(self, speeds):
            if self.motor_mp_q is None:
                PetTrackingSystem._debug_log(f"MotorQueueProxy missing queue, drop command: {speeds}")
                return
            try:
                enqueue_mono_ns = time.monotonic_ns()
                payload = {
                    "speeds": speeds,
                    "trace_id": f"{os.getpid()}-{enqueue_mono_ns}",
                    "enqueue_mono_ns": enqueue_mono_ns,
                }
                self.motor_mp_q.put(("set_motor_speed", payload), block=False)
                PetTrackingSystem._debug_log(f"MotorQueueProxy enqueue: {payload}")
            except Exception:
                PetTrackingSystem._debug_log(f"MotorQueueProxy enqueue failed: {speeds}")
                pass

    class Ros2CmdVelBoard:
        def __init__(self):
            self.topic = os.getenv(
                "PERSON_TRACKING_ROS_CMD_VEL_TOPIC",
                os.getenv("PET_ROS_CMD_VEL_TOPIC", os.getenv("ROBOT_CMD_VEL_TOPIC", "/cmd_vel_external")),
            )
            self.max_linear = float(os.getenv(
                "PERSON_TRACKING_ROS_MAX_LINEAR",
                os.getenv("PET_ROS_MAX_LINEAR", "0.55"),
            ))
            self.max_angular = float(os.getenv(
                "PERSON_TRACKING_ROS_MAX_ANGULAR",
                os.getenv("PET_ROS_MAX_ANGULAR", "1.30"),
            ))
            self.linear_sign = float(os.getenv(
                "PERSON_TRACKING_ROS_LINEAR_SIGN",
                os.getenv("ROBOT_CMD_LINEAR_SIGN", "1.0"),
            ))
            self.angular_sign = float(os.getenv(
                "PERSON_TRACKING_ROS_ANGULAR_SIGN",
                os.getenv("ROBOT_CMD_ANGULAR_SIGN", "1.0"),
            ))
            self.legacy_max_speed = float(os.getenv(
                "PERSON_TRACKING_LEGACY_MAX_SPEED",
                os.getenv("ROBOT_OLD_MOTOR_MAX_SPEED", "300.0"),
            ))
            self._closed = False

            import rclpy
            from rclpy.node import Node
            from geometry_msgs.msg import Twist

            self.rclpy = rclpy
            self.Twist = Twist

            if not rclpy.ok():
                try:
                    from rclpy.signals import SignalHandlerOptions
                    rclpy.init(args=None, signal_handler_options=SignalHandlerOptions.NO)
                except Exception:
                    rclpy.init(args=None)

            self.node = Node(f"person_tracking_cmd_vel_publisher_{os.getpid()}")
            self.pub = self.node.create_publisher(Twist, self.topic, 10)

            PetTrackingSystem._debug_log(
                f"Ros2CmdVelBoard init topic={self.topic}, "
                f"max_linear={self.max_linear}, max_angular={self.max_angular}, "
                f"linear_sign={self.linear_sign}, angular_sign={self.angular_sign}"
            )

        def set_motor_speed(self, speeds):
            if self._closed:
                return

            try:
                motor = {int(item[0]): float(item[1]) for item in speeds}

                right_norm = motor.get(1, 0.0) / max(self.legacy_max_speed, 1.0)
                left_norm = -motor.get(2, 0.0) / max(self.legacy_max_speed, 1.0)

                right_norm = max(-1.0, min(1.0, right_norm))
                left_norm = max(-1.0, min(1.0, left_norm))

                linear_x = (left_norm + right_norm) * 0.5 * self.max_linear
                angular_z = (right_norm - left_norm) * self.max_angular

                linear_x *= self.linear_sign
                angular_z *= self.angular_sign

                msg = self.Twist()
                msg.linear.x = float(linear_x)
                msg.linear.y = 0.0
                msg.linear.z = 0.0
                msg.angular.x = 0.0
                msg.angular.y = 0.0
                msg.angular.z = float(angular_z)

                self.pub.publish(msg)
                try:
                    self.rclpy.spin_once(self.node, timeout_sec=0.0)
                except Exception:
                    pass

                PetTrackingSystem._debug_log(
                    f"Ros2CmdVelBoard publish speeds={speeds}, "
                    f"left={left_norm:.3f}, right={right_norm:.3f}, "
                    f"linear_x={linear_x:.4f}, angular_z={angular_z:.4f}"
                )

            except Exception as e:
                PetTrackingSystem._debug_log(
                    f"Ros2CmdVelBoard set_motor_speed failed: {e!r}, speeds={speeds}"
                )

        def stop(self):
            try:
                repeat = int(os.getenv(
                    "PERSON_TRACKING_ROS_STOP_REPEAT",
                    os.getenv("PET_ROS_STOP_REPEAT", "8"),
                ))
                interval = float(os.getenv(
                    "PERSON_TRACKING_ROS_STOP_INTERVAL_SEC",
                    os.getenv("PET_ROS_STOP_INTERVAL_SEC", "0.04"),
                ))
                for _ in range(repeat):
                    msg = self.Twist()
                    msg.linear.x = 0.0
                    msg.angular.z = 0.0
                    self.pub.publish(msg)
                    try:
                        self.rclpy.spin_once(self.node, timeout_sec=0.0)
                    except Exception:
                        pass
                    time.sleep(interval)
                PetTrackingSystem._debug_log("Ros2CmdVelBoard stop published zero cmd_vel")
            except Exception as e:
                PetTrackingSystem._debug_log(f"Ros2CmdVelBoard stop failed: {e!r}")

        def close(self):
            if self._closed:
                return
            try:
                self.stop()
            except Exception:
                pass
            self._closed = True
            try:
                self.node.destroy_node()
            except Exception:
                pass

        def release(self):
            self.close()

        def shutdown(self):
            self.close()

    @staticmethod
    @contextmanager
    def _silence_native_output():
        if os.getenv("PET_TRACKING_SILENCE_CHILD_LOGS", "1").strip().lower() not in {"1", "true", "yes", "on"}:
            yield
            return

        devnull_fd = None
        saved_stdout_fd = None
        saved_stderr_fd = None

        try:
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            saved_stdout_fd = os.dup(1)
            saved_stderr_fd = os.dup(2)

            os.dup2(devnull_fd, 1)
            os.dup2(devnull_fd, 2)

            yield

        finally:
            if saved_stdout_fd is not None:
                os.dup2(saved_stdout_fd, 1)
                os.close(saved_stdout_fd)

            if saved_stderr_fd is not None:
                os.dup2(saved_stderr_fd, 2)
                os.close(saved_stderr_fd)

            if devnull_fd is not None:
                os.close(devnull_fd)

    @classmethod
    def _run_search_task_quietly(cls, *args):
        with cls._silence_native_output():
            return cls.background_pet_search_task(*args)

    @classmethod
    def _run_tracking_task_quietly(cls, *args):
        if os.getenv("SINGLE_FUNCTION_SPEECH_EVENTS", "0").strip().lower() in {"1", "true", "yes", "on"}:
            return cls.background_tracking_task(*args)
        with cls._silence_native_output():
            return cls.background_tracking_task(*args)

    class CameraReader:
        def __init__(self, src=r'/dev/video22', width=640, height=640):
            self.src = src
            self.cap = cv2.VideoCapture(src)
            self.is_file_source = self._is_file_source(src)
            self.flip_code = self._parse_flip_code(os.getenv("PERSON_TRACKING_CAMERA_FLIP", "none"))
            self.warmup_frames = max(0, int(os.getenv("PERSON_TRACKING_CAMERA_WARMUP_FRAMES", "10")))

            if not self.is_file_source:
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            if not self.cap.isOpened():
                raise RuntimeError(f"Cannot open video source: {src}")

            self._lock = threading.Lock()
            self._stop = threading.Event()
            self.ret = False
            self.frame = None
            self.thread = None

            if self.is_file_source:
                self._eof = False
            else:
                for _ in range(self.warmup_frames + 1):
                    self.ret, self.frame = self.cap.read()
                    if self.ret and self.frame is not None:
                        self.frame = self._apply_orientation(self.frame)
                self.thread = threading.Thread(target=self._update, daemon=True)
                self.thread.start()

        @staticmethod
        def _is_file_source(src):
            if isinstance(src, (int, np.integer)):
                return False
            text = str(src)
            if text.isdigit() or text.startswith('/dev/video'):
                return False
            if text.startswith(('rtsp://', 'rtmp://', 'http://', 'https://')):
                return False
            return os.path.exists(text)

        @staticmethod
        def _parse_flip_code(value):
            text = str(value or "none").strip().lower()
            if text in {"none", "no", "off", "false", "0"}:
                return None
            if text in {"vertical", "v", "updown", "y", "flip0"}:
                return 0
            if text in {"horizontal", "h", "leftright", "x", "flip1"}:
                return 1
            if text in {"both", "all", "180", "-1", "flip-1"}:
                return -1
            try:
                code = int(text)
            except ValueError:
                print(f"[PersonTracking] Unknown PERSON_TRACKING_CAMERA_FLIP={value!r}, using no flip")
                return None
            return code if code in {-1, 0, 1} else None

        def _apply_orientation(self, frame):
            if self.flip_code is None:
                return frame
            return cv2.flip(frame, self.flip_code)

        def _update(self):
            while not self._stop.is_set():
                ret, frame = self.cap.read()

                if ret and frame is not None:
                    with self._lock:
                        self.ret = ret
                        self.frame = self._apply_orientation(frame)
                else:
                    time.sleep(0.005)

            if self.cap.isOpened():
                self.cap.release()

        def read(self):
            if self.is_file_source:
                if self._eof or self._stop.is_set():
                    return False, None
                ret, frame = self.cap.read()
                if not ret or frame is None:
                    self._eof = True
                    return False, None
                return True, frame

            with self._lock:
                if self.frame is not None:
                    return self.ret, self.frame.copy()
            return False, None

        def isOpened(self):
            if self.is_file_source:
                return not self._stop.is_set() and not self._eof and self.cap.isOpened()
            return not self._stop.is_set() and self.cap.isOpened()

        def release(self):
            self._stop.set()

            if self.thread is not None and self.thread.is_alive():
                self.thread.join(timeout=2.0)

            if self.cap.isOpened():
                self.cap.release()

    class RKNNDetector:
        INPUT_W = 640
        INPUT_H = 640

        @staticmethod
        def _check_rknn3_device_access():
            if os.name != "posix" or os.geteuid() == 0:
                return

            required_paths = [
                "/dev/dma_heap/system",
                "/dev/dri/renderD128",
            ]
            inaccessible = [
                path for path in required_paths
                if os.path.exists(path) and not os.access(path, os.R_OK | os.W_OK)
            ]
            if inaccessible:
                raise PermissionError(
                    "RKNN3 backend requires NPU/DMA device access. "
                    "Do not run this skill as root; grant test access to /dev/dma_heap and /dev/dri/renderD*. "
                    f"Inaccessible devices: {', '.join(inaccessible)}"
                )

        def __init__(self, path, conf=0.25, core_mask=4):
            self.conf = conf

            mask_map = {
                1: 0x01,
                2: 0x02,
                3: 0x04,
                4: 0x07,
            }
            self._rknn_core = mask_map.get(core_mask, 0x01)

            self._check_rknn3_device_access()

            self.rknn = RKNN3Lite()

            if not os.path.exists(path):
                raise FileNotFoundError(path)

            if not os.path.exists(PetTrackingSystem.WEIGHT_MODEL):
                raise FileNotFoundError(PetTrackingSystem.WEIGHT_MODEL)

            print("[RKNNDetector] 准备 load_rknn")
            print("[RKNNDetector] model path:", path)
            print("[RKNNDetector] weight path:", PetTrackingSystem.WEIGHT_MODEL, flush=True)

            ret = self.rknn.load_rknn(path, PetTrackingSystem.WEIGHT_MODEL)
            print("[RKNNDetector] load_rknn 返回:", ret, flush=True)

            if ret != 0:
                raise RuntimeError(f"load_rknn failed: {ret}")

            print("[RKNNDetector] 准备 get_devices_id", flush=True)
            ids = self.rknn.get_devices_id()
            print("[RKNNDetector] get_devices_id 返回:", ids, flush=True)

            if not ids:
                raise RuntimeError("没有找到 RKNN 设备")

            print("[RKNNDetector] 准备 init_runtime", flush=True)
            ret = self.rknn.init_runtime(
                target="rk3588",
                core_mask=0x01,
                device_id=PERSON_TRACKING_RKNN_DEVICE.encode("ascii"),
            )

            print("[RKNNDetector] init_runtime 返回:", ret, flush=True)

            if ret != 0:
                raise RuntimeError(f"init_runtime failed: {ret}")

            print("[RKNNDetector] RKNN 初始化完成", flush=True)

        def _preprocess(self, bgr: np.ndarray):
            oh, ow = bgr.shape[:2]

            scale = min(self.INPUT_W / ow, self.INPUT_H / oh)
            nw = int(ow * scale)
            nh = int(oh * scale)

            resized = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)

            canvas = np.full((self.INPUT_H, self.INPUT_W, 3), 114, dtype=np.uint8)

            pad_left = (self.INPUT_W - nw) // 2
            pad_top = (self.INPUT_H - nh) // 2

            canvas[pad_top:pad_top + nh, pad_left:pad_left + nw] = resized

            inp = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
            inp = np.expand_dims(inp, 0)

            return inp, scale, pad_left, pad_top

        def _postprocess(self, outputs, scale, pad_left, pad_top, orig_w, orig_h, target_cls_ids: set):
            raw = np.array(outputs[0], dtype=np.float32)

            if raw.ndim == 3 and raw.shape[0] == 1:
                raw = raw[0]

            valid = np.any(raw != 0, axis=1)
            raw = raw[valid]

            if len(raw) == 0:
                return []

            scores = raw[:, 0]
            cls_ids = raw[:, 1].astype(np.int32)

            x1_lb = raw[:, 2]
            y1_lb = raw[:, 3]
            x2_lb = raw[:, 4]
            y2_lb = raw[:, 5]

            mask = (scores >= self.conf) & np.isin(cls_ids, list(target_cls_ids))

            if not mask.any():
                return []

            scores = scores[mask]
            cls_ids = cls_ids[mask]

            x1_lb = x1_lb[mask]
            y1_lb = y1_lb[mask]
            x2_lb = x2_lb[mask]
            y2_lb = y2_lb[mask]

            x1 = np.clip((x1_lb - pad_left) / scale, 0, orig_w)
            y1 = np.clip((y1_lb - pad_top) / scale, 0, orig_h)
            x2 = np.clip((x2_lb - pad_left) / scale, 0, orig_w)
            y2 = np.clip((y2_lb - pad_top) / scale, 0, orig_h)

            valid2 = (x2 > x1) & (y2 > y1)

            results = []

            for i in np.where(valid2)[0]:
                if cls_ids[i] < len(PetTrackingSystem.COCO80_NAMES):
                    cls_name = PetTrackingSystem.COCO80_NAMES[cls_ids[i]]
                else:
                    cls_name = f"class_{cls_ids[i]}"

                results.append(
                    PetTrackingSystem.Detection(
                        title=cls_name,
                        confidence=float(scores[i]),
                        rect=PetTrackingSystem.RectF(
                            float(x1[i]),
                            float(y1[i]),
                            float(x2[i]),
                            float(y2[i]),
                        )
                    )
                )

            return results

        def detect(self, bgr: np.ndarray, W: int, H: int, target_classes: List[str]):
            target_cls_ids = {
                i for i, name in enumerate(PetTrackingSystem.COCO80_NAMES)
                if name in target_classes
            }

            if not target_cls_ids:
                return []

            inp, scale, pad_left, pad_top = self._preprocess(bgr)

            outputs = self.rknn.inference(inputs=[inp])

            if outputs is None:
                return []

            return self._postprocess(
                outputs,
                scale,
                pad_left,
                pad_top,
                W,
                H,
                target_cls_ids,
            )

        def release(self):
            try:
                self.rknn.release()
            except Exception:
                pass

    class MultiBoxTracker:
        MIN_SIZE = 24.0
        BASE_SPEED = float(os.getenv("PERSON_TRACK_BASE_SPEED", "0.78"))
        STEERING_GAIN = float(os.getenv("PERSON_TRACK_STEERING_GAIN", "0.52"))
        MOVING_STEERING_GAIN = float(os.getenv("PERSON_TRACK_MOVING_STEERING_GAIN", "0.46"))
        DEAD_ZONE = float(os.getenv("PERSON_TRACK_DEAD_ZONE", "0.08"))
        MIN_STEER = float(os.getenv("PERSON_TRACK_MIN_STEER", "0.08"))
        SPEED_EMA_ALPHA = float(os.getenv("PERSON_TRACK_SPEED_EMA_ALPHA", "0.32"))
        MAX_SPEED_STEP = float(os.getenv("PERSON_TRACK_MAX_SPEED_STEP", "0.080"))
        STOP_SPEED_STEP = float(os.getenv("PERSON_TRACK_STOP_SPEED_STEP", "0.120"))
        OUTPUT_DEADBAND = 0.03
        WIDE_ERROR_TURN = float(os.getenv("PERSON_TRACK_WIDE_ERROR_TURN", "0.65"))
        CURVE_START_ERROR = float(os.getenv("PERSON_TRACK_CURVE_START_ERROR", "0.08"))
        MIN_CURVE_LINEAR_SCALE = float(os.getenv("PERSON_TRACK_MIN_CURVE_LINEAR_SCALE", "0.18"))
        BOX_EMA_ALPHA = max(0.0, min(1.0, float(os.getenv("PERSON_TRACK_BOX_EMA_ALPHA", "0.35"))))

        def __init__(self):
            self.currentState = PetTrackingSystem.TrackerState.IDLE
            self.lastKnownLocation = None
            self.lastMoveDirection = 0.0

            self.currentLeftSpeed = 0.0
            self.currentRightSpeed = 0.0

            self.frameWidth = 640
            self.frameHeight = 480

            self.drawBoxes = []

            self.lastSeenTime = time.time()
            self.searchStartTime = 0.0

            self.stationary_start_time = None
            self.is_backing_up = False

        @staticmethod
        def _clamp(value, low=-1.0, high=1.0):
            return max(low, min(high, value))

        @staticmethod
        def _deadband(value, threshold):
            if abs(value) < threshold:
                return 0.0
            return value

        @staticmethod
        def _limit_delta(prev, target, max_step):
            delta = target - prev

            if delta > max_step:
                delta = max_step
            elif delta < -max_step:
                delta = -max_step

            return prev + delta

        def _smooth_speed_pair(self, target_left, target_right):
            """
            强力速度平滑算法。

            流程：
            1. 限幅，防止目标速度超过 [-1, 1]；
            2. 小速度死区，避免微小误差导致电机抖动；
            3. EMA 低通滤波，削弱速度突变；
            4. 单帧速度变化限幅，限制加速度；
            5. 再次死区处理，让接近 0 的速度真正归零。

            这样可以同时解决：
            - 狗框抖动导致电机左右抽动；
            - 目标突然偏移导致速度突变；
            - 跟随速度过快导致机器人冲出去。
            """

            target_left = self._clamp(target_left)
            target_right = self._clamp(target_right)

            target_left = self._deadband(target_left, self.OUTPUT_DEADBAND)
            target_right = self._deadband(target_right, self.OUTPUT_DEADBAND)

            ema_left = self.currentLeftSpeed + self.SPEED_EMA_ALPHA * (target_left - self.currentLeftSpeed)
            ema_right = self.currentRightSpeed + self.SPEED_EMA_ALPHA * (target_right - self.currentRightSpeed)

            if abs(target_left) < self.OUTPUT_DEADBAND:
                left_step = self.STOP_SPEED_STEP
            else:
                left_step = self.MAX_SPEED_STEP

            if abs(target_right) < self.OUTPUT_DEADBAND:
                right_step = self.STOP_SPEED_STEP
            else:
                right_step = self.MAX_SPEED_STEP

            smooth_left = self._limit_delta(self.currentLeftSpeed, ema_left, left_step)
            smooth_right = self._limit_delta(self.currentRightSpeed, ema_right, right_step)

            smooth_left = self._clamp(smooth_left)
            smooth_right = self._clamp(smooth_right)

            smooth_left = self._deadband(smooth_left, self.OUTPUT_DEADBAND)
            smooth_right = self._deadband(smooth_right, self.OUTPUT_DEADBAND)

            return smooth_left, smooth_right

        @staticmethod
        def _rect_iou(a, b):
            ix1 = max(a.left, b.left)
            iy1 = max(a.top, b.top)
            ix2 = min(a.right, b.right)
            iy2 = min(a.bottom, b.bottom)
            iw = max(0.0, ix2 - ix1)
            ih = max(0.0, iy2 - iy1)
            inter = iw * ih
            area_a = max(0.0, a.width()) * max(0.0, a.height())
            area_b = max(0.0, b.width()) * max(0.0, b.height())
            denom = area_a + area_b - inter
            if denom <= 1e-6:
                return 0.0
            return inter / denom

        @classmethod
        def _dedupe_detections(cls, detections, iou_threshold=0.45):
            kept = []
            for det in sorted(detections, key=lambda item: item.confidence, reverse=True):
                if all(cls._rect_iou(det.rect, old.rect) < iou_threshold for old in kept):
                    kept.append(det)
            return kept

        def _select_single_target(self, valid):
            if not valid:
                return None

            if self.lastKnownLocation is None:
                frame_cx = self.frameWidth * 0.5
                frame_cy = self.frameHeight * 0.5
                diag = max(1.0, (self.frameWidth ** 2 + self.frameHeight ** 2) ** 0.5)

                def initial_score(det):
                    dx = det.rect.centerX() - frame_cx
                    dy = det.rect.centerY() - frame_cy
                    center_dist = (dx * dx + dy * dy) ** 0.5 / diag
                    area = max(1.0, det.rect.width() * det.rect.height())
                    area_ratio = min(1.0, area / max(1.0, self.frameWidth * self.frameHeight))
                    return 1.4 * det.confidence + 0.35 * area_ratio - 1.2 * center_dist

                return max(valid, key=initial_score)

            prev = self.lastKnownLocation
            diag = max(1.0, (self.frameWidth ** 2 + self.frameHeight ** 2) ** 0.5)

            def continuity_score(det):
                iou = self._rect_iou(det.rect, prev)
                dx = det.rect.centerX() - prev.centerX()
                dy = det.rect.centerY() - prev.centerY()
                center_dist = (dx * dx + dy * dy) ** 0.5 / diag
                area_prev = max(1.0, prev.width() * prev.height())
                area_now = max(1.0, det.rect.width() * det.rect.height())
                area_ratio = min(area_prev, area_now) / max(area_prev, area_now)
                return (
                    2.2 * iou
                    + 1.0 * det.confidence
                    + 0.4 * area_ratio
                    - 0.8 * center_dist
                )

            return max(valid, key=continuity_score)

        @staticmethod
        def _blend_rect(previous, current, alpha):
            if previous is None or alpha >= 1.0:
                return current
            if alpha <= 0.0:
                return previous
            return PetTrackingSystem.RectF(
                previous.left + alpha * (current.left - previous.left),
                previous.top + alpha * (current.top - previous.top),
                previous.right + alpha * (current.right - previous.right),
                previous.bottom + alpha * (current.bottom - previous.bottom),
            )

        def trackResults(self, results, currFrame=None):
            if currFrame is not None:
                self.frameWidth = currFrame.shape[1]
                self.frameHeight = currFrame.shape[0]

            self.drawBoxes.clear()

            valid = [
                r for r in results
                if r.rect.width() >= self.MIN_SIZE and r.rect.height() >= self.MIN_SIZE
            ]
            valid = self._dedupe_detections(valid)

            current_time = time.time()
            best = self._select_single_target(valid)

            if best is not None:
                rect = self._blend_rect(self.lastKnownLocation, best.rect, self.BOX_EMA_ALPHA)
                if self.lastKnownLocation is not None:
                    dx = rect.centerX() - self.lastKnownLocation.centerX()
                    self.lastMoveDirection = self.lastMoveDirection * 0.8 + dx * 0.2

                self.lastKnownLocation = rect
                self.currentState = PetTrackingSystem.TrackerState.TRACKING
                self.lastSeenTime = current_time

                self.drawBoxes.append(
                    PetTrackingSystem.DrawBox(
                        rect,
                        best.title,
                        best.confidence,
                        True,
                    )
                )

                return

            if self.currentState == PetTrackingSystem.TrackerState.TRACKING:
                self.currentState = PetTrackingSystem.TrackerState.BUFFER_WAIT

            elif self.currentState == PetTrackingSystem.TrackerState.BUFFER_WAIT:
                if current_time - self.lastSeenTime > 2.0:
                    self.currentState = PetTrackingSystem.TrackerState.SEARCHING
                    self.searchStartTime = current_time

            elif self.currentState == PetTrackingSystem.TrackerState.SEARCHING:
                if current_time - self.searchStartTime > 6.0:
                    self.currentState = PetTrackingSystem.TrackerState.IDLE
                    self.lastKnownLocation = None

        def updateTarget(self):
            target_left = 0.0
            target_right = 0.0

            state = PetTrackingSystem.TrackerState

            if self.currentState == state.TRACKING and self.lastKnownLocation is not None:
                error = 1.0 - 2.0 * self.lastKnownLocation.centerX() / float(self.frameWidth)
                abs_error = abs(error)

                area = (
                    self.lastKnownLocation.width() * self.lastKnownLocation.height()
                ) / float(self.frameWidth * self.frameHeight)

                height_ratio = self.lastKnownLocation.height() / float(self.frameHeight)

                forward = 0.0

                is_stationary = (
                    abs(self.currentLeftSpeed) < 0.05
                    and abs(self.currentRightSpeed) < 0.05
                )

                if is_stationary:
                    if self.stationary_start_time is None:
                        self.stationary_start_time = time.time()
                else:
                    self.stationary_start_time = None

                if self.is_backing_up:
                    if height_ratio < 0.80:
                        self.is_backing_up = False
                    else:
                        forward = -self.BASE_SPEED * 0.70
                else:
                    if height_ratio < 0.80 and area < 0.40:
                        if height_ratio <= 0.50:
                            raw_forward = self.BASE_SPEED
                        else:
                            raw_forward = self.BASE_SPEED * ((0.80 - height_ratio) / 0.30)

                        forward = max(0.0, raw_forward)

                    if self.stationary_start_time is not None:
                        if time.time() - self.stationary_start_time > 1.0:
                            if height_ratio > 0.80 or area > 0.55:
                                self.is_backing_up = True

                dynamic_dead_zone = self.DEAD_ZONE
                steer = 0.0

                if abs_error > dynamic_dead_zone:
                    steering_gain = self.MOVING_STEERING_GAIN if forward != 0.0 else self.STEERING_GAIN
                    steer = error * steering_gain

                    if forward < 0.0:
                        steer = -steer

                    if forward == 0.0:
                        min_steer = self.MIN_STEER * 1.5
                    else:
                        min_steer = self.MIN_STEER

                    if 0.0 < steer < min_steer:
                        steer = min_steer
                    elif -min_steer < steer < 0.0:
                        steer = -min_steer

                if abs_error <= self.CURVE_START_ERROR:
                    curve_scale = 1.0
                elif abs_error >= self.WIDE_ERROR_TURN:
                    curve_scale = 0.0
                else:
                    span = max(self.WIDE_ERROR_TURN - self.CURVE_START_ERROR, 1e-6)
                    curve_scale = (self.WIDE_ERROR_TURN - abs_error) / span
                    curve_scale = max(self.MIN_CURVE_LINEAR_SCALE, curve_scale)
                forward *= curve_scale

                target_left = forward - steer
                target_right = forward + steer

            elif self.currentState == state.BUFFER_WAIT:
                target_left = 0.0
                target_right = 0.0
                self.is_backing_up = False
                self.stationary_start_time = None

            elif self.currentState == state.SEARCHING:
                search_speed = PetTrackingSystem.TRACK_SEARCH_SPIN_SPEED

                if self.lastMoveDirection > 0:
                    target_left = search_speed
                    target_right = -search_speed
                else:
                    target_left = -search_speed
                    target_right = search_speed

                self.is_backing_up = False
                self.stationary_start_time = None

            elif self.currentState == state.IDLE:
                target_left = 0.0
                target_right = 0.0
                self.is_backing_up = False
                self.stationary_start_time = None

            self.currentLeftSpeed, self.currentRightSpeed = self._smooth_speed_pair(
                target_left,
                target_right,
            )

            return self.currentLeftSpeed, self.currentRightSpeed

    def __init__(self, model_path=None):
        self.model_path = model_path or self.DETECTOR_MODEL

        self.pid_file = "/tmp/pet_tracking_pid.txt"

        self._process = None

        self.motor_command_handler = None
        self.start_hook = None

        self._motor_mp_q = None
        self._motor_bridge_thread = None
        self._motor_bridge_stop = threading.Event()

    @staticmethod
    def _detect_gui_available() -> bool:
        gui_env = os.getenv("PET_CAMERA_GUI", "").strip().lower()

        if gui_env in {"0", "false", "off", "no"}:
            return False

        if gui_env in {"1", "true", "on", "yes"}:
            return True

        if not os.getenv("DISPLAY"):
            return False

        try:
            probe = subprocess.run(
                ["xdpyinfo"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                check=False,
            )
            return probe.returncode == 0

        except FileNotFoundError:
            return True

        except Exception:
            return False

    @classmethod
    def _gui_available(cls) -> bool:
        if cls.GUI_AVAILABLE is None:
            cls.GUI_AVAILABLE = cls._detect_gui_available()

            if not cls.GUI_AVAILABLE:
                print("[PetCamera] 未检测到可用图形显示环境，已启用无窗口模式")

        return cls.GUI_AVAILABLE

    @classmethod
    def _show_frame(cls, window_name: str, frame, delay: int = 1) -> int:
        if not cls._gui_available():
            return -1

        cv2.imshow(window_name, frame)

        if delay <= 0:
            return -1

        return cv2.waitKey(delay) & 0xFF

    @classmethod
    def _close_windows(cls) -> None:
        if not cls._gui_available():
            return

        try:
            cv2.destroyAllWindows()

            for _ in range(10):
                cv2.waitKey(1)

        except Exception:
            pass

    @staticmethod
    def _safe_remove_pid(pid_file: str) -> None:
        if not pid_file or not os.path.exists(pid_file):
            return

        try:
            os.remove(pid_file)

        except OSError:
            try:
                with open(pid_file, "w") as f:
                    f.write("")
            except OSError:
                pass

    @staticmethod
    def _debug_log(message: str) -> None:
        path = os.getenv("PERSON_TRACKING_DEBUG_LOG", "").strip()
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except Exception:
            pass

    @staticmethod
    def _create_board():
        """
        Create the chassis control object for standalone person tracking.

        The original skill wrote directly to the board serial controller. For the
        current robot bringup flow, the base/nav stack is already running and the
        standalone skill should publish geometry_msgs/Twist to /cmd_vel.
        """
        backend = os.getenv(
            "PERSON_TRACKING_MOTOR_BACKEND",
            os.getenv("PET_MOTOR_BACKEND", "ros2"),
        ).strip().lower()

        if os.getenv("PERSON_TRACKING_DISABLE_MOTOR", "0").strip().lower() in {"1", "true", "yes", "on"}:
            backend = "dummy"

        if backend in {"dummy", "none", "off", "0", "false"}:
            PetTrackingSystem._debug_log("PERSON_TRACKING_MOTOR_BACKEND=dummy, using DummyBoard")
            return PetTrackingSystem.DummyBoard()

        if backend in {"ros2", "cmd_vel", "cmdvel", "1", "true"}:
            try:
                return PetTrackingSystem.Ros2CmdVelBoard()
            except Exception as e:
                PetTrackingSystem._debug_log(
                    f"create Ros2CmdVelBoard failed: {e!r}; fallback DummyBoard"
                )
                print(f"[PersonTracking] ROS2 /cmd_vel init failed, fallback DummyBoard: {e}")
                return PetTrackingSystem.DummyBoard()

        PetTrackingSystem._debug_log(
            f"unknown PERSON_TRACKING_MOTOR_BACKEND={backend}, fallback DummyBoard"
        )
        return PetTrackingSystem.DummyBoard()

    @staticmethod
    def _ros2_backend_enabled():
        backend = os.getenv(
            "PERSON_TRACKING_MOTOR_BACKEND",
            os.getenv("PET_MOTOR_BACKEND", "ros2"),
        ).strip().lower()
        return backend in {"ros2", "cmd_vel", "cmdvel", "1", "true"}

    @classmethod
    def _publish_ros2_stop_once(cls):
        if not cls._ros2_backend_enabled():
            return
        try:
            board = cls.Ros2CmdVelBoard()
            board.stop()
            try:
                board._closed = True
                board.node.destroy_node()
            except Exception:
                pass
            cls._debug_log("parent published ROS2 zero cmd_vel stop")
        except Exception as e:
            cls._debug_log(f"parent ROS2 zero cmd_vel stop failed: {e!r}")

    @staticmethod
    def _release_board(board):
        if board is None:
            return

        if isinstance(board, PetTrackingSystem.MotorQueueProxy):
            try:
                PetTrackingSystem.set_motor(board, 0.0, 0.0)
            except Exception:
                pass
            return

        if isinstance(board, PetTrackingSystem.Ros2CmdVelBoard):
            try:
                board.close()
            except Exception:
                pass
            return

        try:
            PetTrackingSystem.set_motor(board, 0.0, 0.0)
            time.sleep(0.05)
        except Exception:
            pass

        try:
            if hasattr(board, "enable_recv"):
                board.enable_recv = False
        except Exception:
            pass

        for method_name in ("release", "close", "shutdown", "stop"):
            try:
                method = getattr(board, method_name, None)

                if callable(method):
                    method()
                    print(f"[PetTrackingSystem] Board.{method_name}() 已调用")
                    return

            except Exception as e:
                print(f"[PetTrackingSystem] 调用 Board.{method_name}() 失败: {e}")

        try:
            port = getattr(board, "port", None)

            if port is not None and hasattr(port, "close"):
                port.close()
                print("[PetTrackingSystem] Board 串口 port 已关闭")

        except Exception as e:
            print(f"[PetTrackingSystem] 关闭 Board 串口失败: {e}")

    @staticmethod
    def set_motor(board, speed_right, speed_left, max_speed=300):
        """
        设置底盘速度。

        修改点：
        原来 max_speed 默认是 300。
        现在改成 220，进一步降低底层电机输出，配合速度平滑算法使用。
        """
        if board is None:
            return

        try:
            speed_right = max(-1.0, min(1.0, float(speed_right)))
            speed_left = max(-1.0, min(1.0, float(speed_left)))

            board.set_motor_speed([
                [1, int(max_speed * speed_right)],
                [2, int(max_speed * speed_left * -1)],
            ])
            log_key = (round(speed_right, 2), round(speed_left, 2))
            log_now = time.time()
            last_log_at, last_log_key = PetTrackingSystem._last_motor_debug_log
            if log_key != last_log_key or log_now - last_log_at >= 1.0:
                PetTrackingSystem._debug_log(
                    f"set_motor right={speed_right:.3f} left={speed_left:.3f} "
                    f"real_right={int(max_speed * speed_right)} real_left={int(max_speed * speed_left * -1)}"
                )
                PetTrackingSystem._last_motor_debug_log = (log_now, log_key)

        except Exception as e:
            print(f"[PetTrackingSystem] 设置底轮速度失败: {e}")
            PetTrackingSystem._debug_log(f"set_motor failed: {e!r}")

    @staticmethod
    def _build_video_writer(frame_shape, output_path: str):
        h, w = frame_shape[:2]

        output_dir = os.path.dirname(output_path)

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        for codec in ("mp4v", "avc1", "H264", "XVID", "MJPG"):
            writer = cv2.VideoWriter(
                output_path,
                cv2.VideoWriter_fourcc(*codec),
                20.0,
                (w, h),
            )

            if writer.isOpened():
                return writer

            try:
                writer.release()
            except Exception:
                pass

        return None

    @staticmethod
    def _spd_bar(canvas, cx, cy, speed, label):
        bh, bw = 100, 26

        x0 = cx - bw // 2
        y0 = cy - bh // 2

        cv2.rectangle(
            canvas,
            (x0, y0),
            (x0 + bw, y0 + bh),
            (50, 50, 50),
            -1,
        )

        fill = int(abs(speed) * bh / 2)

        if speed >= 0:
            col = (50, 220, 50)
            cv2.rectangle(
                canvas,
                (x0 + 2, y0 + bh // 2 - fill),
                (x0 + bw - 2, y0 + bh // 2),
                col,
                -1,
            )
        else:
            col = (50, 50, 220)
            cv2.rectangle(
                canvas,
                (x0 + 2, y0 + bh // 2),
                (x0 + bw - 2, y0 + bh // 2 + fill),
                col,
                -1,
            )

        cv2.rectangle(
            canvas,
            (x0, y0),
            (x0 + bw, y0 + bh),
            (180, 180, 180),
            1,
        )

        cv2.line(
            canvas,
            (x0, cy),
            (x0 + bw, cy),
            (255, 255, 255),
            1,
        )

        cv2.putText(
            canvas,
            label,
            (cx - 8, y0 + bh + 16),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.44,
            (220, 220, 220),
            1,
        )

        cv2.putText(
            canvas,
            f"{speed:+.2f}",
            (cx - 22, y0 + bh + 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.44,
            (220, 220, 220),
            1,
        )

    @staticmethod
    def _read_face_identity_event():
        path = Path(PetTrackingSystem.IDENTITY_EVENT_PATH)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                event = json.load(f)
        except Exception:
            return None

        if event.get("status") != "matched":
            return None

        try:
            expires_at = float(event.get("expires_at", 0.0))
        except (TypeError, ValueError):
            expires_at = 0.0
        if expires_at and expires_at < time.time():
            return None

        name = event.get("name")
        person_id = event.get("person_id")
        if not name or not person_id:
            return None

        try:
            score = float(event.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0

        return {
            "name": str(name),
            "person_id": str(person_id),
            "score": score,
            "source": event.get("source", "face_recognition"),
            "timestamp": event.get("timestamp"),
        }

    @staticmethod
    def _write_identity_binding(identity, tracker_state: str, target_pet: str):
        if not identity:
            return None

        payload = {
            "status": "bound",
            "target": target_pet,
            "tracker_state": tracker_state,
            "identity": identity,
            "timestamp": time.time(),
        }

        path = Path(PetTrackingSystem.IDENTITY_BINDING_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as exc:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            print(f"Failed to initialize motor board: {exc}")
            return None
        return payload

    @classmethod
    def draw_tracking_ui(cls, frame, tracker, target_pet: str, fps: Optional[float] = None, identity=None):
        vis = frame.copy()

        H, W = vis.shape[:2]

        for db in tracker.drawBoxes:
            color = (0, 255, 0)
            status = "Target"

            x1 = int(db.rect.left)
            y1 = int(db.rect.top)
            x2 = int(db.rect.right)
            y2 = int(db.rect.bottom)

            cv2.rectangle(
                vis,
                (x1, y1),
                (x2, y2),
                color,
                3,
            )

            cv2.putText(
                vis,
                f"{db.title}|{status}({db.score:.2f})",
                (x1, max(20, y1 - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                color,
                2,
                cv2.LINE_AA,
            )

        cls._spd_bar(vis, 36, H - 75, tracker.currentLeftSpeed, "L")
        cls._spd_bar(vis, 76, H - 75, tracker.currentRightSpeed, "R")

        info_lines = [
            f"Mode: Follow [{target_pet}]",
            f"State: {tracker.currentState.name}",
            f"Smooth: alpha={tracker.SPEED_EMA_ALPHA:.2f}, step={tracker.MAX_SPEED_STEP:.3f}",
        ]

        if identity:
            info_lines.append(
                f"Identity: {identity['name']} score={identity['score']:.2f}"
            )

        if fps is not None and fps > 0:
            info_lines.append(f"FPS: {fps:.1f}")

        for i, text in enumerate(info_lines):
            cv2.putText(
                vis,
                text,
                (W - 360, 30 + i * 25),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (220, 220, 220),
                2,
                cv2.LINE_AA,
            )

        return vis

    @staticmethod
    def background_pet_search_task(video_source, model_path, target_pet, tts_mp_q=None, motor_mp_q=None):
        import speaker

        speaker.init_mp_queue(tts_mp_q)

        board = None
        board_owned = False

        if motor_mp_q is not None:
            board = PetTrackingSystem.MotorQueueProxy(motor_mp_q)
        else:
            board = PetTrackingSystem._create_board()
            board_owned = not isinstance(board, PetTrackingSystem.DummyBoard)

        detector, detector_owned = runtime_models.acquire_or_create(
            "pet_detector",
            lambda: PetTrackingSystem.RKNNDetector(
                model_path,
                conf=PetTrackingSystem.DET_CONF,
                core_mask=4,
            ),
            description="宠物检测 YOLO RKNN",
        )

        cap = None
        found = False

        pet_dict = {
            "cat": "小猫",
            "dog": "小狗",
            "person": "目标行人",
        }

        pet_name = pet_dict.get(target_pet, "宠物")

        if target_pet in {"cat", "dog", "person"}:
            target_classes = [target_pet]
        else:
            target_classes = ["cat", "dog"]

        try:
            cap = PetTrackingSystem.CameraReader(video_source)

            start_time = time.time()

            while cap.isOpened() and (time.time() - start_time) < 100000.0:
                ret, frame = cap.read()

                if not ret or frame is None:
                    time.sleep(0.01)
                    continue

                h, w = frame.shape[:2]

                dets = detector.detect(
                    frame,
                    w,
                    h,
                    target_classes=target_classes,
                )

                vis = frame.copy()

                cv2.putText(
                    vis,
                    f"Searching for [{pet_name}]...",
                    (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1,
                    (0, 165, 255),
                    2,
                )

                if dets:
                    found = True

                    PetTrackingSystem.set_motor(board, 0.0, 0.0)

                    for det in dets:
                        x1 = int(det.rect.left)
                        y1 = int(det.rect.top)
                        x2 = int(det.rect.right)
                        y2 = int(det.rect.bottom)

                        cv2.rectangle(
                            vis,
                            (x1, y1),
                            (x2, y2),
                            (0, 255, 0),
                            3,
                        )

                        cv2.putText(
                            vis,
                            "FOUND",
                            (x1, max(20, y1 - 10)),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            1,
                            (0, 255, 0),
                            2,
                        )

                    PetTrackingSystem._show_frame("Pet Search", vis, 800)
                    break

                PetTrackingSystem._show_frame("Pet Search", vis, 1)

                PetTrackingSystem.set_motor(
                    board,
                    speed_right=0.10,
                    speed_left=-0.10,
                )

        finally:
            PetTrackingSystem.set_motor(board, 0.0, 0.0)

            if board_owned:
                PetTrackingSystem._release_board(board)
                board = None

            if detector_owned:
                detector.release()

            if found:
                speak(f"这里有一只{pet_name}")
            else:
                speak(f"抱歉，我没有发现{pet_name}")

            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

            PetTrackingSystem._close_windows()

            time.sleep(0.2)

    @staticmethod
    def background_tracking_task(video_source, model_path, target_pet, pid_file_path, tts_mp_q=None, motor_mp_q=None, start_gate_path=None):
        import speaker

        result_path = PetTrackingSystem.TRACK_RESULT_PATH
        try:
            result_dir = os.path.dirname(result_path)
            if result_dir:
                os.makedirs(result_dir, exist_ok=True)
            if os.path.exists(result_path) and not os.access(result_path, os.W_OK):
                os.remove(result_path)
            with open(result_path, "w") as f:
                f.write("failure")
        except Exception as e:
            PetTrackingSystem._debug_log(f"tracking result init failed path={result_path}: {e!r}")
            raise

        speaker.init_mp_queue(tts_mp_q)

        is_running = True

        def handle_sigterm(signum, frame_obj):
            nonlocal is_running
            is_running = False

        signal.signal(signal.SIGTERM, handle_sigterm)
        signal.signal(signal.SIGINT, handle_sigterm)

        pet_dict = {
            "cat": "小猫",
            "dog": "小狗",
            "person": "目标行人",
        }

        pet_name = pet_dict.get(target_pet, "宠物")

        if target_pet in {"cat", "dog", "person"}:
            target_classes = [target_pet]
        else:
            target_classes = ["cat", "dog"]

        cap = None
        board = None
        board_owned = False
        detector = None
        detector_owned = False
        video_writer = None
        video_writer_failed = False
        recording_start_time = None

        try:
            PetTrackingSystem._debug_log(
                f"tracking start source={video_source} target={target_pet} model={model_path}"
            )
            if motor_mp_q is not None:
                board = PetTrackingSystem.MotorQueueProxy(motor_mp_q)
            else:
                board = PetTrackingSystem._create_board()
                board_owned = not isinstance(board, PetTrackingSystem.DummyBoard)

            detector, detector_owned = runtime_models.acquire_or_create(
                "pet_detector",
                lambda: PetTrackingSystem.RKNNDetector(
                    model_path,
                    conf=PetTrackingSystem.DET_CONF,
                    core_mask=4,
                ),
                description="宠物检测 YOLO RKNN",
            )

            tracker = PetTrackingSystem.MultiBoxTracker()
            cap = PetTrackingSystem.CameraReader(video_source)
            PetTrackingSystem._debug_log(
                f"camera opened source={video_source} flip={os.getenv('PERSON_TRACKING_CAMERA_FLIP', 'none')}"
            )

            fps_counter = PetTrackingSystem.FPSCounter()

            if start_gate_path:
                _single_function_emit_ready("person_tracking", "我开始寻找行人。")
                if not _single_function_wait_start_gate(start_gate_path, lambda: not is_running):
                    return

            has_tracked = False
            last_identity_id = None
            start_time = time.time()
            _single_function_emit_progress("person_tracking", state="searching", target=target_pet, started_at=start_time)

            if os.path.exists(PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH):
                try:
                    os.remove(PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH)
                except OSError:
                    pass

            while cap.isOpened() and is_running:
                now = time.time()

                ret, frame = cap.read()

                if not ret or frame is None:
                    time.sleep(0.01)
                    continue

                h, w = frame.shape[:2]

                dets = detector.detect(
                    frame,
                    w,
                    h,
                    target_classes=target_classes,
                )
                if dets:
                    PetTrackingSystem._debug_log(
                        "detections=" + ",".join(
                            f"{d.title}:{d.confidence:.2f}" for d in dets[:5]
                        )
                    )

                tracker.trackResults(dets, frame)

                current_identity = PetTrackingSystem._read_face_identity_event() if target_pet == "person" else None
                current_fps = fps_counter.update()

                vis = PetTrackingSystem.draw_tracking_ui(
                    frame,
                    tracker,
                    target_pet,
                    fps=current_fps,
                    identity=current_identity,
                )

                key = PetTrackingSystem._show_frame("Pet Follower", vis, 1)

                if video_writer is None and not video_writer_failed:
                    video_writer = PetTrackingSystem._build_video_writer(
                        vis.shape,
                        PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH,
                    )

                    if video_writer is None:
                        video_writer_failed = True
                        print(
                            f"警告: 无法创建带框宠物追踪视频，继续执行追踪: "
                            f"{PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH}"
                        )
                    else:
                        print(
                            f"开始录制带框宠物追踪视频: "
                            f"{PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH}"
                        )

                if video_writer is not None:
                    video_writer.write(vis)

                if not has_tracked:
                    if tracker.currentState == PetTrackingSystem.TrackerState.TRACKING:
                        has_tracked = True
                        recording_start_time = now
                        _single_function_emit_progress("person_tracking", state="tracking", target=target_pet, last_seen_at=now)

                        print(
                            f"已锁定{pet_name}，继续录制带框视频: "
                            f"{PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH}"
                        )

                        if current_identity:
                            PetTrackingSystem._write_identity_binding(
                                current_identity,
                                tracker.currentState.name,
                                target_pet,
                            )
                            last_identity_id = current_identity["person_id"]

                    else:
                        PetTrackingSystem.set_motor(
                            board,
                            speed_right=PetTrackingSystem.TRACK_SEARCH_SPIN_SPEED,
                            speed_left=-PetTrackingSystem.TRACK_SEARCH_SPIN_SPEED,
                        )

                        if now - start_time > PetTrackingSystem.TRACK_SEARCH_TIMEOUT_SEC:
                            _single_function_emit_progress("person_tracking", state="not_found", target=target_pet, elapsed_seconds=round(now - start_time, 2))
                            speak(f"对不起，我没找到{pet_name}")
                            print(f"寻找超时，未找到{pet_name}，跟踪进程结束")
                            break

                        if key in [ord("x"), ord("e"), ord("q"), 27]:
                            print("\n收到按键退出指令")
                            break

                        continue

                if current_identity and current_identity["person_id"] != last_identity_id:
                    PetTrackingSystem._write_identity_binding(
                        current_identity,
                        tracker.currentState.name,
                        target_pet,
                    )
                    last_identity_id = current_identity["person_id"]

                left_speed, right_speed = tracker.updateTarget()

                if tracker.currentState == PetTrackingSystem.TrackerState.IDLE:
                    PetTrackingSystem.set_motor(
                        board,
                        speed_right=PetTrackingSystem.TRACK_SEARCH_SPIN_SPEED,
                        speed_left=-PetTrackingSystem.TRACK_SEARCH_SPIN_SPEED,
                    )
                else:
                    PetTrackingSystem.set_motor(
                        board,
                        speed_right=right_speed,
                        speed_left=left_speed,
                    )
                    PetTrackingSystem._debug_log(
                        f"tracker_state={tracker.currentState.name} left={left_speed:.3f} right={right_speed:.3f}"
                    )

                if recording_start_time is not None:
                    if now - recording_start_time >= PetTrackingSystem.TRACK_RECORD_DURATION_SEC:
                        print(
                            f"视频录制完成（{PetTrackingSystem.TRACK_RECORD_DURATION_SEC:.0f}s）: "
                            f"{PetTrackingSystem.TRACK_OUTPUT_VIDEO_PATH}"
                        )

                        with open(PetTrackingSystem.TRACK_RESULT_PATH, "w") as f:
                            f.write("success")

                        break

                if key in [ord("x"), ord("e"), ord("q"), 27]:
                    print("\n收到按键退出指令")
                    break

        except Exception as e:
            print(f"异常: {e}")
            PetTrackingSystem._debug_log(f"tracking exception: {e!r}")

        finally:
            PetTrackingSystem.set_motor(board, 0.0, 0.0)

            if board_owned:
                PetTrackingSystem._release_board(board)
                board = None

            if detector is not None and detector_owned:
                detector.release()

            if video_writer is not None:
                try:
                    video_writer.release()
                except Exception:
                    pass

            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

            PetTrackingSystem._close_windows()

            PetTrackingSystem._safe_remove_pid(pid_file_path)

            time.sleep(0.2)

    def _ensure_motor_bridge(self, ctx):
        if not callable(self.motor_command_handler):
            return None

        if self._motor_mp_q is None:
            self._motor_mp_q = ctx.Queue()

        if self._motor_bridge_thread is None or not self._motor_bridge_thread.is_alive():
            self._motor_bridge_stop.clear()

            self._motor_bridge_thread = threading.Thread(
                target=self._motor_bridge_loop,
                daemon=True,
                name="pet-tracking-motor-bridge",
            )

            self._motor_bridge_thread.start()

        return self._motor_mp_q

    def _motor_bridge_loop(self):
        while not self._motor_bridge_stop.is_set():
            try:
                cmd, payload = self._motor_mp_q.get(timeout=0.2)

            except queue.Empty:
                continue

            except Exception:
                continue

            if cmd == "set_motor_speed" and callable(self.motor_command_handler):
                try:
                    self.motor_command_handler(payload)
                except Exception as e:
                    print(f"宠物追踪电机桥接失败: {e}")

    def _stop_motor_bridge(self):
        self._motor_bridge_stop.set()

        if self._motor_bridge_thread is not None and self._motor_bridge_thread.is_alive():
            self._motor_bridge_thread.join(timeout=1.0)

        self._motor_bridge_thread = None
        self._motor_mp_q = None

    def find_pet(self, video_source, target_pet):
        print(f"启动搜索进程: {target_pet}")

        import speaker

        ctx = runtime_config.get_mp_context()

        if speaker._mp_q is None:
            speaker.init_mp_queue(ctx.Queue())

        motor_mp_q = self._ensure_motor_bridge(ctx)

        p = ctx.Process(
            target=self.__class__._run_search_task_quietly,
            args=(
                video_source,
                self.model_path,
                target_pet,
                speaker._mp_q,
                motor_mp_q,
            ),
            daemon=True,
        )

        p.start()
        p.join()

        self._stop_motor_bridge()

        print("搜索进程已退出")

    def start_pet_tracking(self, video_source, target_pet, start_gate_path=None):
        print(f"启动进程跟踪目标: {target_pet}")

        if self._process is not None and self._process.is_alive():
            return

        self.__class__._safe_remove_pid(self.pid_file)

        import speaker

        ctx = runtime_config.get_mp_context()

        if speaker._mp_q is None:
            speaker.init_mp_queue(ctx.Queue())

        motor_mp_q = self._ensure_motor_bridge(ctx)

        p = ctx.Process(
            target=self.__class__._run_tracking_task_quietly,
            args=(
                video_source,
                self.model_path,
                target_pet,
                self.pid_file,
                speaker._mp_q,
                motor_mp_q,
                start_gate_path,
            ),
            daemon=True,
        )

        p.start()
        self._process = p

        if callable(self.start_hook):
            try:
                self.start_hook(target_pet)
            except Exception as e:
                print(f"宠物追踪启动钩子失败: {e}")

        try:
            with open(self.pid_file, "w") as f:
                f.write(str(p.pid))
        except OSError as e:
            print(f"写入 PID 文件失败: {e}")

    def stop_pet_tracking(self):
        try:
            self._stop_motor_bridge()
            self.__class__._publish_ros2_stop_once()

            stop_motion = getattr(self.motor_command_handler, "stop_motion", None)
            if callable(stop_motion):
                try:
                    stop_motion(accept_commands=False)
                except Exception:
                    pass
            elif callable(self.motor_command_handler):
                try:
                    self.motor_command_handler([[1, 0], [2, 0]])
                except Exception:
                    pass

            self._terminate_process()

        finally:
            self.__class__._safe_remove_pid(self.pid_file)

            stop_motion = getattr(self.motor_command_handler, "stop_motion", None)
            if callable(stop_motion):
                try:
                    stop_motion(accept_commands=False)
                except Exception:
                    pass
            elif callable(self.motor_command_handler):
                try:
                    self.motor_command_handler([[1, 0], [2, 0]])
                except Exception:
                    pass

            self.__class__._publish_ros2_stop_once()
            self._stop_motor_bridge()

        print("??????????")

    def _terminate_process(self):
        if self._process is not None:
            try:
                if self._process.is_alive():
                    self._process.terminate()
                    self._process.join(timeout=3.0)

                    if self._process.is_alive():
                        self._process.kill()
                        self._process.join(timeout=2.0)

            except Exception:
                pass

            finally:
                self._process = None

            return

        if not os.path.exists(self.pid_file):
            return

        try:
            with open(self.pid_file) as f:
                pid_str = f.read().strip()

            if not pid_str.isdigit():
                return

            pid = int(pid_str)

            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(3.0)

                try:
                    os.kill(pid, 0)
                    os.kill(pid, signal.SIGKILL)

                except ProcessLookupError:
                    pass

            except ProcessLookupError:
                pass

            except PermissionError:
                pass

        except Exception:
            pass

    @classmethod
    def smoke_test(cls):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)

        tracker = cls.MultiBoxTracker()

        detections = [
            cls.Detection(
                "dog",
                0.91,
                cls.RectF(230, 130, 410, 390),
            )
        ]

        tracker.trackResults(detections, frame)

        for i in range(20):
            left, right = tracker.updateTarget()
            print(f"step={i:02d}, left={left:.3f}, right={right:.3f}")

        vis = cls.draw_tracking_ui(frame, tracker, "dog")

        print("PetTrackingSystem smoke test passed")
        print(
            f"state={tracker.currentState.name}, "
            f"left={tracker.currentLeftSpeed:.3f}, "
            f"right={tracker.currentRightSpeed:.3f}, "
            f"frame_shape={vis.shape}"
        )

    @staticmethod
    def _normalize_video_source(value):
        if isinstance(value, int):
            return value

        text = str(value)

        return int(text) if text.isdigit() else text

    @classmethod
    def main(cls):
        parser = argparse.ArgumentParser(description="PetTrackingSystem 测试入口")

        parser.add_argument(
            "--mode",
            choices=["smoke", "find", "track", "stop"],
            default="track",
        )

        parser.add_argument(
            "--source",
            default=str(runtime_config.FACE_CAMERA_ID),
            help="摄像头编号或视频/RTSP 路径",
        )

        parser.add_argument(
            "--pet",
            choices=["cat", "dog", "person", "all"],
            default="dog",
        )

        parser.add_argument(
            "--duration",
            type=float,
            default=30.0,
            help="track 模式运行秒数，0 表示等待 Ctrl+C 或内部结束",
        )
        parser.add_argument(
            "--model",
            default=cls.DETECTOR_MODEL,
            help="RKNN 检测模型路径",
        )

        argv = []
        for item in sys.argv[1:]:
            if item == "--no-gui":
                os.environ["PET_CAMERA_GUI"] = "0"
                os.environ["PERSON_TRACKING_CAMERA_GUI"] = "0"
                continue
            argv.append(item)
        args = parser.parse_args(argv)

        if args.mode == "smoke":
            cls.smoke_test()
            return

        if args.pet != "all":
            target_pet = args.pet
        else:
            target_pet = "pet"

        source = cls._normalize_video_source(args.source)

        system = cls(model_path=args.model)

        if args.mode == "find":
            system.find_pet(source, target_pet)

        elif args.mode == "track":
            system.start_pet_tracking(source, target_pet)
            try:
                if args.duration > 0:
                    time.sleep(args.duration)
                    system.stop_pet_tracking()
                else:
                    while system._process is not None and system._process.is_alive():
                        time.sleep(0.5)
            except KeyboardInterrupt:
                system.stop_pet_tracking()
        elif args.mode == "stop":
            system.stop_pet_tracking()

if False and __name__ == "__main__":
    PetTrackingSystem.main()
__file__ = _run_file
_publish_namespace('skills.pet_camera', _before_inline, top_level_aliases=())
del _before_inline

import argparse


def _person_cli_backend_from_argv():
    backend = os.getenv("PERSON_TRACKING_MOTOR_BACKEND", os.getenv("PET_MOTOR_BACKEND", "ros2"))
    for index, arg in enumerate(sys.argv[1:], start=1):
        if arg == "--backend" and index + 1 < len(sys.argv):
            backend = sys.argv[index + 1]
        elif arg.startswith("--backend="):
            backend = arg.split("=", 1)[1]
    return str(backend or "ros2").strip().lower()


def _person_maybe_reexec_with_ros_env_for_cli():
    if "--help" in sys.argv or "-h" in sys.argv or "--dry-run" in sys.argv:
        return
    if os.environ.get("PERSON_TRACKING_ROS_ENV_REEXEC") == "1":
        return
    if _person_cli_backend_from_argv() in {"dummy", "none", "off", "0", "false"}:
        return
    try:
        import rclpy  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    env = os.environ.copy()
    env["PERSON_TRACKING_ROS_ENV_REEXEC"] = "1"
    car_ws = env.get("CAR_REAL_WS", "/home/test/Car_real_copy")
    setup_files = [
        "/opt/ros/humble/setup.bash",
        f"{car_ws}/install/setup.bash",
    ]
    source_lines = "\n".join(
        f'[ -f "{item}" ] && source "{item}"' for item in setup_files
    )
    script = (
        "set -e\n"
        + source_lines
        + "\nexport PERSON_TRACKING_ROS_ENV_REEXEC=1\n"
        + "exec \"$@\"\n"
    )
    print(
        "[PersonTracking] rclpy is not available in this shell; "
        "sourcing ROS2 setup files and restarting person_tracking/run.py",
        flush=True,
    )
    os.execvpe(
        "bash",
        ["bash", "-lc", script, "person-tracking-ros-env", sys.executable, *sys.argv],
        env,
    )


def _person_tracking_cli_main():
    parser = argparse.ArgumentParser(description="Person tracking skill using the COCO person class.")
    parser.add_argument("action", nargs="?", default="track", choices=["find", "track", "stop"])
    parser.add_argument("--camera", "--source", dest="camera", default=None)
    parser.add_argument("--duration", type=float, default=0.0, help="track duration in seconds; 0 means run until stopped")
    parser.add_argument("--base-speed", type=float, default=None, help="Normalized forward tracking speed.")
    parser.add_argument("--max-linear", type=float, default=None, help="Maximum ROS linear velocity in m/s.")
    parser.add_argument("--max-angular", type=float, default=None, help="Maximum ROS angular velocity in rad/s.")
    parser.add_argument("--steering-gain", type=float, default=None, help="Tracking steering gain.")
    parser.add_argument("--speed-ema-alpha", type=float, default=None, help="Speed response smoothing factor.")
    parser.add_argument("--max-speed-step", type=float, default=None, help="Maximum normalized speed change per frame.")
    parser.add_argument("--search-spin-speed", type=float, default=None, help="Normalized in-place search rotation speed.")
    parser.add_argument(
        "--backend",
        default=os.getenv("PERSON_TRACKING_MOTOR_BACKEND", os.getenv("PET_MOTOR_BACKEND", "ros2")),
        choices=["ros2", "cmd_vel", "cmdvel", "dummy", "none", "off"],
        help="Motor backend. Default: ros2.",
    )
    parser.add_argument(
        "--cmd-vel-topic",
        default=os.getenv(
            "PERSON_TRACKING_ROS_CMD_VEL_TOPIC",
            os.getenv("PET_ROS_CMD_VEL_TOPIC", os.getenv("ROBOT_CMD_VEL_TOPIC", "/cmd_vel_external")),
        ),
        help="ROS2 Twist topic. Default: /cmd_vel.",
    )
    parser.add_argument("--json", action="store_true", help="Print a JSON status line when the action finishes.")
    parser.add_argument("--start-gate", default=None, help="Path to a start gate file; tracking starts after the file appears.")
    parser.add_argument("--resume-from-interrupt", action="store_true", help="Accepted by the task runtime; tracking resumes by restoring scene and searching again.")
    parser.add_argument("--target", default=None, help="Optional target hint restored from interruption context.")

    argv = []
    for item in sys.argv[1:]:
        if item == "--no-gui":
            os.environ["PET_CAMERA_GUI"] = "0"
            os.environ["PERSON_TRACKING_CAMERA_GUI"] = "0"
            continue
        argv.append(item)
    args = parser.parse_args(argv)
    json_mode = args.json or os.getenv("SINGLE_FUNCTION_JSON", "0") == "1"

    duration = args.duration
    timeout_env = os.getenv("SINGLE_FUNCTION_TIMEOUT")
    if duration <= 0 and timeout_env:
        try:
            duration = float(timeout_env)
        except ValueError:
            duration = args.duration

    os.environ["PERSON_TRACKING_MOTOR_BACKEND"] = args.backend
    os.environ["PET_MOTOR_BACKEND"] = args.backend
    os.environ["PERSON_TRACKING_ROS_CMD_VEL_TOPIC"] = args.cmd_vel_topic
    os.environ["PET_ROS_CMD_VEL_TOPIC"] = args.cmd_vel_topic
    if args.base_speed is not None:
        PetTrackingSystem.MultiBoxTracker.BASE_SPEED = max(0.0, min(1.0, float(args.base_speed)))
        os.environ["PERSON_TRACK_BASE_SPEED"] = str(PetTrackingSystem.MultiBoxTracker.BASE_SPEED)
    if args.max_linear is not None:
        os.environ["PERSON_TRACKING_ROS_MAX_LINEAR"] = str(max(0.0, float(args.max_linear)))
    if args.max_angular is not None:
        os.environ["PERSON_TRACKING_ROS_MAX_ANGULAR"] = str(max(0.0, float(args.max_angular)))
    if args.steering_gain is not None:
        gain = max(0.0, float(args.steering_gain))
        PetTrackingSystem.MultiBoxTracker.STEERING_GAIN = gain
        PetTrackingSystem.MultiBoxTracker.MOVING_STEERING_GAIN = gain
        os.environ["PERSON_TRACK_STEERING_GAIN"] = str(gain)
        os.environ["PERSON_TRACK_MOVING_STEERING_GAIN"] = str(gain)
    if args.speed_ema_alpha is not None:
        value = max(0.0, min(1.0, float(args.speed_ema_alpha)))
        PetTrackingSystem.MultiBoxTracker.SPEED_EMA_ALPHA = value
        os.environ["PERSON_TRACK_SPEED_EMA_ALPHA"] = str(value)
    if args.max_speed_step is not None:
        value = max(0.001, float(args.max_speed_step))
        PetTrackingSystem.MultiBoxTracker.MAX_SPEED_STEP = value
        os.environ["PERSON_TRACK_MAX_SPEED_STEP"] = str(value)
    if args.search_spin_speed is not None:
        value = max(0.0, min(1.0, float(args.search_spin_speed)))
        PetTrackingSystem.TRACK_SEARCH_SPIN_SPEED = value
        os.environ["PERSON_TRACK_SEARCH_SPIN_SPEED"] = str(value)
    os.environ.setdefault("PET_CAMERA_GUI", "0")
    os.environ.setdefault("PERSON_TRACKING_CAMERA_GUI", "0")

    source = args.camera or DEFAULT_CAMERA

    print("=" * 80)
    print("[PersonTracking]")
    print(f"mode    : {args.action}")
    print(f"source  : {source}")
    print("target  : person")
    print(f"backend : {args.backend}")
    print(f"cmd_vel : {args.cmd_vel_topic}")
    print("=" * 80)

    system = PetTrackingSystem()
    if args.action == "find":
        system.find_pet(source, "person")
    elif args.action == "track":
        system.start_pet_tracking(source, "person", start_gate_path=args.start_gate)
        try:
            if duration > 0:
                deadline = time.monotonic() + duration
                while system._process is not None and system._process.is_alive() and time.monotonic() < deadline:
                    system._process.join(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
                system.stop_pet_tracking()
            else:
                while system._process is not None and system._process.is_alive():
                    time.sleep(0.5)
        except KeyboardInterrupt:
            system.stop_pet_tracking()
    else:
        system.stop_pet_tracking()

    if json_mode:
        print(json.dumps({
            "ok": True,
            "skill": "person_tracking",
            "mode": args.action,
            "source": str(source),
            "target": "person",
            "backend": args.backend,
            "cmd_vel_topic": args.cmd_vel_topic,
        }, ensure_ascii=False))


if __name__ == "__main__":
    _person_maybe_reexec_with_ros_env_for_cli()
    _person_tracking_cli_main()
