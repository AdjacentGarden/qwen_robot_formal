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
import subprocess  # 子进程库，用于执行系统命令
import sys         # 系统库

# rpm 最大转速 约为0.7m/s. 最大支持300
MAX_SPEED=200
MAX_SPEED_STEP=100
WHEEL_R=0.035

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
    PACKET_FUNC_SYS = 0            # 系统指令 (如获取电池电压)
    PACKET_FUNC_LED = 1            # LED控制
    PACKET_FUNC_BUZZER = 2         # 蜂鸣器控制
    PACKET_FUNC_MOTOR = 3          # 电机控制
    PACKET_FUNC_SPEAKER = 4        # 语音控制
    PACKET_FUNC_WKUP = 5           # 语音唤醒信号
    PACKET_FUNC_KEY = 6            # 获取按键状态
    PACKET_FUNC_HOUSEHOLD = 7      # 家电控制
    PACKET_FUNC_GP2Y = 8           # 获取距离传感器数据
    PACKET_FUNC_LEARN = 9          # 家电学习模式
    PACKET_FUNC_MOTOR_SPEED = 10   # 获取电机速度数据
    PACKET_FUNC_FAN = 11           # 风扇控制
    PACKET_FUNC_NONE = 12      # 无效/空功能

    PACKET_FUNC_PWM_SERVO = 20  # PWM舵机控制, 板子上从里到外依次为1-4
    PACKET_FUNC_BUS_SERVO = 21  # 总线舵机控制
    PACKET_FUNC_IMU = 23  # IMU获取
    PACKET_FUNC_GAMEPAD = 24  # 手柄获取
    PACKET_FUNC_SBUS = 25  # 航模遥控获取
    PACKET_FUNC_OLED = 26 # OLED 显示内容设置

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

    def _kill_serial_processes(self, device):
        """
        杀死所有占用指定串口设备的进程
        :param device: 串口设备路径，如 "/dev/ttyS0"
        :return: 是否成功清理
        """
        try:
            # 使用 lsof 查找占用串口的进程PID
            result = subprocess.run(
                ['lsof', '-t', device],
                capture_output=True,
                text=True
            )

            if result.stdout.strip():
                pids = result.stdout.strip().split('\n')
                print(f"[Board] 发现占用 {device} 的进程: {pids}")

                for pid in pids:
                    if pid:
                        try:
                            # 先尝试温和终止
                            subprocess.run(['kill', '-15', pid], capture_output=True)
                            print(f"[Board] 已发送终止信号到进程 {pid}")
                            time.sleep(0.5)

                            # 检查进程是否还存在
                            check = subprocess.run(
                                ['kill', '-0', pid],
                                capture_output=True
                            )
                            if check.returncode == 0:
                                # 进程仍在运行，强制杀死
                                subprocess.run(['kill', '-9', pid], capture_output=True)
                                print(f"[Board] 已强制杀死进程 {pid}")
                        except Exception as e:
                            print(f"[Board] 杀死进程 {pid} 时出错: {e}")

                # 等待进程完全退出
                time.sleep(1)
                return True
            else:
                print(f"[Board] 没有发现占用 {device} 的进程")
                return True

        except FileNotFoundError:
            print("[Board] 警告: 未找到 lsof 命令，无法检查进程占用")
            print("[Board] 请先安装 lsof: sudo apt-get install lsof (Linux) 或 brew install lsof (macOS)")
            return True  # 继续尝试打开串口
        except Exception as e:
            print(f"[Board] 检查进程占用时出错: {e}")
            return True  # 继续尝试打开串口

    def __init__(self, device="/dev/ttyS0", baudrate=115200, timeout=5, auto_kill=True):
        """
        初始化开发板控制对象
        :param device: 串口设备路径
        :param baudrate: 波特率
        :param timeout: 串口超时时间
        :param auto_kill: 是否自动杀死占用串口的进程
        """
        # 如果需要，清理占用串口的进程
        if auto_kill:
            self._kill_serial_processes(device)

        self.enable_recv = False  # 接收线程使能标志
        self.frame = []           # 用于暂存接收到的单个数据包
        self.recv_count = 0       # 记录当前接收到的数据长度

        # 打开串口，默认为 /dev/ttyS0, 波特率 115200
        try:
            self.port = serial.Serial(device, baudrate, timeout=timeout)
            print(f"[Board] 成功打开串口 {device}, 波特率 {baudrate}")
        except serial.SerialException as e:
            print(f"[Board] 打开串口失败: {e}")
            print(f"[Board] 请检查:")
            print(f"  1. 串口设备 {device} 是否存在")
            print(f"  2. 是否有权限访问该设备 (尝试: sudo chmod 666 {device})")
            print(f"  3. 是否被其他程序占用")
            raise

        self.state = PacketControllerState.PACKET_CONTROLLER_STATE_STARTBYTE1  # 初始化状态机

        # 为每种传感器/数据定义一个容量为1的队列，确保读取时获取的是最新的一帧数据
        self.sys_queue = queue.Queue(maxsize=1)
        self.key_queue = queue.Queue(maxsize=1)
        self.imu_queue = queue.Queue(maxsize=1)
        self.wkup_queue = queue.Queue(maxsize=1)
        self.motor_speed_queue = queue.Queue(maxsize=1)

        # 注册各功能码对应的数据处理回调函数
        self.parsers = {
            PacketFunction.PACKET_FUNC_SYS: self.packet_report_sys,
            PacketFunction.PACKET_FUNC_KEY: self.packet_report_key,
            PacketFunction.PACKET_FUNC_WKUP: self.packet_report_wkup,
            PacketFunction.PACKET_FUNC_MOTOR_SPEED: self.packet_report_motor_speed
        }


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

    def packet_report_motor_speed(self, data):
        try:
            self.motor_speed_queue.put_nowait(data)
        except queue.Full:
            pass

    def packet_report_wkup(self, data):
        try:
            self.wkup_queue.put_nowait(data)
        except queue.Full:
            pass



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


    def get_motor_speed(self):
        # 获取电机速度数据
        if self.enable_recv:
            try:
                data = self.motor_speed_queue.get(block=False)
                left_speed_low = data[0]
                left_speed_high = data[1]
                right_speed_low = data[4]
                right_speed_high = data[5]

                left_speed = struct.unpack('<h', bytes([left_speed_low, left_speed_high]))[0]
                right_speed = -1 * struct.unpack('<h', bytes([right_speed_low, right_speed_high]))[0]

                return left_speed, right_speed

                # return {'left_wheel_speed': left_speed,'right_wheel_speed': right_speed*-1}

            except queue.Empty:
                return None
        else:
            print('enable reception first!')
            return None


    # ---- 以下是通信发送核心逻辑 ----
    def buf_write(self, func, data):
        # 通用数据包封装发送函数
        buf = [0xAA, 0x55, int(func)]  # 帧头1，帧头2，功能码
        buf.append(len(data))          # 写入数据长度
        buf.extend(data)               # 写入实际数据
        buf.append(checksum_crc8(bytes(buf[2:])))  # 计算并写入功能码+长度+数据的 CRC8 校验值
        self.port.write(bytes(buf))    # 通过串口发送出去（注意转为bytes）


    # ---- 以下是开发板各硬件的控制命令 ----
    def set_motor_speed(self, speed_left, speed_right, max_speed=MAX_SPEED):
        """
        设置电机速度
        :param speed_right: 右电机速度，范围 -MAX_SPEED 到 MAX_SPEED
        :param speed_left: 左电机速度，范围 -MAX_SPEED 到 MAX_SPEED
        :param max_speed: 最大速度值，默认200
        """
        # 计算实际速度值
        # 限幅到 [-max_speed, max_speed]
        speed_left_real = int(max(-max_speed, min(max_speed, speed_left)))
        speed_right_real = int(max(-max_speed, min(max_speed, speed_right))) * (-1)

        # 准备电机控制数据：[[电机ID, 速度], ...]
        speeds = [[1, speed_left_real], [2, speed_right_real]]

        # 打包数据: 0x01为控制子命令，后面跟要控制的电机数量
        data = [0x01, len(speeds)] # 0x01为控制子命令，后面跟要控制的电机数量
        for i in speeds:
            # 电机ID通常习惯1开始，底层接收从1开始。速度为浮点数
            data.extend(struct.pack("<Bf", int(i[0]), float(i[1])))
        self.buf_write(PacketFunction.PACKET_FUNC_MOTOR, data)

    def set_fan_state(self, state):
        """
        设置风扇状态
        :param state: 0 低转速状态, 1 高转速转速状态
        """
        if state not in (0, 1):
            return
        self.buf_write(PacketFunction.PACKET_FUNC_FAN, struct.pack("<B", state))

    def set_step_motor_speed(self, speed, max_speed=MAX_SPEED_STEP):
        """
        设置步进电机速度
        :param speed: 电机速度
        :param max_speed: 最大速度值，默认100
        """
        data =[0x00]
        speed_real = int(max(-max_speed, min(max_speed, speed)))
        data.extend(struct.pack("<Bf", 3, speed_real)) # 3为步进电机ID
        self.buf_write(PacketFunction.PACKET_FUNC_MOTOR, data)

    # ---- 串口数据后台接收与解析任务 ----
    def enable_reception(self):
        # 开启接收功能，启动一个守护线程后台运行 recv_task
        self.enable_recv = True
        threading.Thread(target=self.recv_task, daemon=True).start()
        print("[Board] 串口接收线程已启动")

    def recv_task(self):
        # 后台死循环：不断从串口读取数据，并通过状态机解析数据包
        while self.enable_recv:
            try:
                recv_data = self.port.read(1)  # 读取1字节数据
                if recv_data is None or len(recv_data) == 0:
                  # 没有读取到数据，继续下一次循环
                  continue
                if recv_data:
                    dat = recv_data[0]  # 获取字节值
                    # 状态机：寻找第一个帧头 0xAA
                    if self.state == PacketControllerState.PACKET_CONTROLLER_STATE_STARTBYTE1:
                        if dat == 0xAA:
                            self.state = PacketControllerState.PACKET_CONTROLLER_STATE_STARTBYTE2
                        continue
                    # 状态机：寻找第二个帧头 0x55
                    elif self.state == PacketControllerState.PACKET_CONTROLLER_STATE_STARTBYTE2:
                        if dat == 0x55:
                            self.state = PacketControllerState.PACKET_CONTROLLER_STATE_FUNCTION
                        else:
                            self.state = PacketControllerState.PACKET_CONTROLLER_STATE_STARTBYTE1 # 头错误，重置状态
                        continue
                    # 状态机：记录功能码 Function ID
                    elif self.state == PacketControllerState.PACKET_CONTROLLER_STATE_FUNCTION:
                        if dat < int(PacketFunction.PACKET_FUNC_NONE): # 校验功能码合法性
                            self.frame = [dat, 0] # 记录功能码，准备记录长度
                            self.state = PacketControllerState.PACKET_CONTROLLER_STATE_LENGTH
                        else:
                            self.frame = []
                            self.state = PacketControllerState.PACKET_CONTROLLER_STATE_STARTBYTE1 # 错误功能码，重置
                        continue
                    # 状态机：记录数据长度
                    elif self.state == PacketControllerState.PACKET_CONTROLLER_STATE_LENGTH:
                        self.frame[1] = dat
                        self.recv_count = 0
                        if dat == 0:
                            self.state = PacketControllerState.PACKET_CONTROLLER_STATE_CHECKSUM # 若无数据，直接验证校验码
                        else:
                            self.state = PacketControllerState.PACKET_CONTROLLER_STATE_DATA # 进入数据接收状态
                        continue
                    # 状态机：接收数据体
                    elif self.state == PacketControllerState.PACKET_CONTROLLER_STATE_DATA:
                        self.frame.append(dat)
                        self.recv_count += 1
                        if self.recv_count >= self.frame[1]: # 数据接收完毕
                            self.state = PacketControllerState.PACKET_CONTROLLER_STATE_CHECKSUM
                        continue
                    # 状态机：接收校验码并验证
                    elif self.state == PacketControllerState.PACKET_CONTROLLER_STATE_CHECKSUM:
                        crc8 = checksum_crc8(bytes(self.frame)) # 计算本地校验和 (包含功能码、长度、数据)
                        if crc8 == dat: # 校验通过
                            func = PacketFunction(self.frame[0]) # 获取功能码
                            data = bytes(self.frame[2:])         # 切片出纯数据体
                            if func in self.parsers:
                                self.parsers[func](data)         # 调用注册好的回调函数放入相应队列
                        else:
                            print(f"[Board] 校验失败: 计算CRC=0x{crc8:02X}, 接收CRC=0x{dat:02X}")
                        # 无论校验成功与否，一帧处理完毕，重置状态机寻找下一个帧头
                        self.state = PacketControllerState.PACKET_CONTROLLER_STATE_STARTBYTE1
                        continue
            except Exception as e:
                print(f"[Board] 接收数据时出错: {e}")
                continue

        # 退出循环后关闭串口
        if hasattr(self, 'port') and self.port.is_open:
            self.port.close()
        print("[Board] 串口接收线程结束")


# 主程序执行入口
if __name__ == "__main__":
    SERIAL_DEVICE = "/dev/ttyS0"  # 根据实际情况修改串口设备

    try:
        # 实例化底层控制对象（会自动清理占用串口的进程）
        board = Board(device=SERIAL_DEVICE, baudrate=115200, timeout=5, auto_kill=True)

        # 必须先开启串口接收守护线程
        board.enable_reception()
        print("START...")

        # 主循环 - 示例：读取各种传感器数据
        loop_count = 0
        while True:
            # 示例1: 获取语音唤醒信号
            wkup = board.get_wkup()
            if wkup is not None:
                print(f"语音唤醒: {wkup}")

            # 示例2: 获取电机速度
            motor_speed = board.get_motor_speed()
            if motor_speed is not None:
                left_speed, right_speed = motor_speed
                print(f"电机速度 - 左: {left_speed}, 右: {right_speed}")

            # 每10个循环控制一次LED或蜂鸣器示例
            loop_count += 1
            if loop_count >= 10:  # 大约5秒一次（如果sleep 0.1秒）
                # 可以在这里添加控制代码
                board.set_motor_speed(10,20)  # 前进
                pass

            time.sleep(0.2)  # 100ms 轮询间隔

    except KeyboardInterrupt:
        print("\n用户中断程序")
    except Exception as e:
        print(f"程序运行错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 清理资源
        if 'board' in locals():
            if hasattr(board, 'enable_recv'):
                board.enable_recv = False  # 停止接收线程
            if hasattr(board, 'port') and board.port.is_open:
                board.port.close()
                print("串口已关闭")
        print("程序退出")