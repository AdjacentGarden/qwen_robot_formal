import smbus2
import time
from smbus2 import i2c_msg

# 常量定义（与原C代码一致）
I2C_BUS_NUMBER = 2          # /dev/i2c-2 对应总线编号2
I2C_SLAVE_ADDR = 0x33       # 设备I2C地址
RECV_BUF_LEN = 256          # 接收缓冲区长度

def i2c_send_at(bus, at_cmd: str) -> str:
    """
    模拟原C代码的i2c_send_at函数：发送AT指令 + 延时1秒 + 接收回复
    :param bus: smbus2.SMBus实例
    :param at_cmd: 要发送的AT指令字符串
    :return: 设备返回的字符串（去除空字符）
    """
    # 1. 转换AT指令为字节流（ASCII编码）
    cmd_bytes = at_cmd.encode('ascii')
    
    # 发送AT指令（I2C写操作）
    write_msg = i2c_msg.write(I2C_SLAVE_ADDR, cmd_bytes)
    try:
        bus.i2c_rdwr(write_msg)  # 执行写操作
    except Exception as e:
        raise RuntimeError(f"I2C发送指令失败: {e}")
    
    # 延时1秒（与原代码sleep(1)一致）
    time.sleep(1)
    
    # 2. 读取设备返回数据（I2C读操作）
    read_msg = i2c_msg.read(I2C_SLAVE_ADDR, RECV_BUF_LEN)
    try:
        bus.i2c_rdwr(read_msg)  # 执行读操作
    except Exception as e:
        raise RuntimeError(f"I2C接收数据失败: {e}")
    
    # 转换字节流为字符串，去除末尾空字符（原C代码缓冲区初始化为0）
    recv_data = bytes(read_msg).decode('ascii', errors='ignore').rstrip('\0')
    return recv_data

# 打开光机
def light_on():
    # 打开I2C总线（对应原C代码的open(I2C_DEVICE)）
    try:
        bus = smbus2.SMBus(I2C_BUS_NUMBER)
    except Exception as e:
        print(f"打开I2C设备失败: {e}")
        return -1
    
    # AT指令（与原代码一致，\r对应<CR>）
    at_cmd = "AT+LightSource=ON\r"
    # 如需关闭光源，替换为：at_cmd = "AT+LightSource=OFF\r"
    
    print(f"Send: {at_cmd}")
    
    # 发送指令并接收回复
    try:
        recv_buf = i2c_send_at(bus, at_cmd)
        print(f"Recv: {recv_buf}")
    except RuntimeError as e:
        print(e)
        bus.close()
        return -1
    
    # 关闭I2C总线（对应原C代码的close(fd)）
    bus.close()
    return 0

def light_off():
    try:
        bus = smbus2.SMBus(I2C_BUS_NUMBER)
    except Exception as e:
        print(f"打开I2C设备失败: {e}")
        return -1
    
    at_cmd = "AT+LightSource=OFF\r"
    
    print(f"Send: {at_cmd}")
    
    try:
        recv_buf = i2c_send_at(bus, at_cmd)
        print(f"Recv: {recv_buf}")
    except RuntimeError as e:
        print(e)
        bus.close()
        return -1
    
    bus.close()
    return 0

if __name__ == "__main__":
    exit(light_off())