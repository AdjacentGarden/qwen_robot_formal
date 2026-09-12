import struct
import pytest
from robot_graph.wheel_guard import validate_head_packet, validate_zero_wheel_stop

@pytest.mark.parametrize('left,right',[(0,0),(10,20),(-100,100),(200,200)])
def test_every_wheel_packet_rejected(left,right):
    data=bytes([1,2])+struct.pack('<BfBf',1,float(left),2,float(right))
    with pytest.raises(PermissionError):validate_head_packet(3,data,3)

@pytest.mark.parametrize('speed',[-40,-10,0,10,40])
def test_head_only(speed):
    data=bytes([0,3])+struct.pack('<f',speed)
    assert validate_head_packet(3,data,3)==data

@pytest.mark.parametrize('data',[b'',b'\x00\x01'+struct.pack('<f',10),b'\x00\x02'+struct.pack('<f',0),b'\x00\x03'+struct.pack('<f',41),b'\x00\x03'+struct.pack('<f',float('nan'))])
def test_other_packets_rejected(data):
    with pytest.raises(PermissionError):validate_head_packet(3,data,3)


def test_exact_zero_wheel_stop_is_allowed():
    assert validate_zero_wheel_stop(0,0)==(0.0,0.0)


@pytest.mark.parametrize('left,right',[(1,0),(0,-1),(True,0),(0,None),(float('nan'),0)])
def test_wheel_motion_or_invalid_stop_is_rejected(left,right):
    with pytest.raises(PermissionError):validate_zero_wheel_stop(left,right)
