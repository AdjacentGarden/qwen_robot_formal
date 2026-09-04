#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Range
import serial
import binascii

def calculate_crc16(data):
    """计算Modbus CRC16校验码"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return crc.to_bytes(2, byteorder='little')

class TOF400FNode(Node):
    def __init__(self):
        super().__init__('tof400f_node')

        # 声明参数（可在launch文件中配置）
        self.declare_parameter('port', '/dev/ttyS9')
        self.declare_parameter('baudrate', 115200)
        self.declare_parameter('frame_id', 'tof_link')
        self.declare_parameter('min_range_m', 0.01)    # 最小量程 10 mm
        self.declare_parameter('max_range_m', 4.0)     # 最大量程 4 m
        self.declare_parameter('field_of_view_rad', 0.01)  # 非常窄的光束

        port = self.get_parameter('port').get_parameter_value().string_value
        baudrate = self.get_parameter('baudrate').get_parameter_value().integer_value
        frame_id = self.get_parameter('frame_id').get_parameter_value().string_value
        min_range = self.get_parameter('min_range_m').get_parameter_value().double_value
        max_range = self.get_parameter('max_range_m').get_parameter_value().double_value
        fov = self.get_parameter('field_of_view_rad').get_parameter_value().double_value

        # 初始化串口
        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1
            )
        except serial.SerialException as e:
            self.get_logger().error(f"无法打开串口 {port}: {e}")
            raise SystemExit(1)

        if self.ser.is_open:
            self.get_logger().info(f"串口 {port} 打开成功")
            # 发送高精度模式命令
            # high_precision_cmd = bytes.fromhex('01 06 00 04 00 00 48 CB')
            # self.ser.write(high_precision_cmd)
            # self.get_logger().info("已发送高精度模式命令")
        else:
            self.get_logger().error("串口打开失败，程序退出")
            raise SystemExit(1)

        # 创建发布者
        self.publisher = self.create_publisher(Range, '/tof400f/range', 10)

        # 预配置 Range 消息的不变字段
        self.range_msg = Range()
        self.range_msg.header.frame_id = frame_id
        self.range_msg.radiation_type = Range.INFRARED   # 红外/激光测距
        self.range_msg.field_of_view = fov
        self.range_msg.min_range = min_range
        self.range_msg.max_range = max_range

        # 接收缓冲区（用于处理粘包）
        self.buffer = b''

        # 定时器，周期性读取串口数据（10ms间隔，降低CPU占用）
        self.timer = self.create_timer(0.01, self.timer_callback)

        self.get_logger().info("TOF400F 测距节点已启动")

    def timer_callback(self):
        """定时读取串口数据、解析并发布距离"""
        try:
            # 读取可用数据
            if self.ser.in_waiting > 0:
                self.buffer += self.ser.read(self.ser.in_waiting)

            # 处理完整帧（7字节）
            while len(self.buffer) >= 7:
                # 检查帧头（地址0x01，功能码0x03，字节数0x02）
                if self.buffer[0] == 0x01 and self.buffer[1] == 0x03 and self.buffer[2] == 0x02:
                    frame = self.buffer[:7]
                    data_part = frame[:5]
                    received_crc = frame[5:7]

                    # 验证CRC
                    calculated_crc = calculate_crc16(data_part)
                    if received_crc == calculated_crc:
                        distance_mm = (frame[3] << 8) | frame[4]
                        if distance_mm == 65535:
                            # self.get_logger().warning("测距失败", throttle_duration_sec=1.0)
                            # 测距失败时发布 NaN
                            self.range_msg.range = 4.0  # 超出最大量程，发布一个大于 max_range 的值
                        else:
                            # 转换为米
                            self.range_msg.range = distance_mm / 1000.0

                        # 更新时间戳并发布
                        self.range_msg.header.stamp = self.get_clock().now().to_msg()
                        self.publisher.publish(self.range_msg)
                    else:
                        self.get_logger().error(
                            f"CRC校验失败: 收到 {binascii.b2a_hex(received_crc)}, "
                            f"计算 {binascii.b2a_hex(calculated_crc)}",
                            throttle_duration_sec=1.0
                        )

                    # 移除已处理的帧
                    self.buffer = self.buffer[7:]
                else:
                    # 帧头不匹配，丢弃第一个字节继续查找
                    self.buffer = self.buffer[1:]

        except serial.SerialException as e:
            self.get_logger().error(f"串口读取错误: {e}")
        except Exception as e:
            self.get_logger().error(f"数据处理异常: {e}")

    def close_serial(self):
        """安全关闭串口"""
        if hasattr(self, 'ser') and self.ser.is_open:
            self.ser.close()
            self.get_logger().info("串口已关闭")

    def destroy_node(self):
        self.close_serial()
        super().destroy_node()

def main(args=None):
    rclpy.init(args=args)
    node = TOF400FNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

if __name__ == "__main__":
    main()
