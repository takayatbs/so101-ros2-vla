import json
import os
import select
import threading
import time
import pytest
from so101_core.feetech import ServoBus, ServoStatusError, TICK_RAD, encode_value, decode_value, packet
from so101_core.hardware import SO101Bus


class ReplySerial:
    def __init__(self, *args, **kwargs):
        self.buffer = bytearray()
        self.sent = []
        self.fault = None
        self.status = {}
    @property
    def in_waiting(self): return len(self.buffer)
    def reset_input_buffer(self): self.buffer.clear()
    def close(self): pass
    def write(self, data):
        self.sent.append(bytes(data))
        motor, instruction, params = data[2], data[4], data[5:-1]
        ids = params[2:] if instruction == 0x82 else [motor]
        if instruction == 0x83: return len(data)
        size = params[1] if instruction in (2, 0x82) else 0
        for index, i in enumerate(ids):
            if self.fault == 'missing' and index == 1: continue
            reply = bytearray(packet(i, 4 if self.fault == 'error' else self.status.get(i, 0), (1200+i).to_bytes(size,'little') if size else b''))
            if self.fault == 'checksum': reply[-1] ^= 1
            self.buffer.extend(reply)
        return len(data)
    def read(self, n):
        if not self.buffer: time.sleep(.001)
        result = bytes(self.buffer[:min(n,3)])
        del self.buffer[:len(result)]
        return result


def test_packet_and_signed_magnitude_reference():
    assert packet(1, 2, bytes([56, 2])) == bytes.fromhex('ff ff 01 04 02 38 02 be')
    assert packet(1, 3, bytes([40, 0])) == bytes.fromhex('ff ff 01 04 03 28 00 cf')
    assert encode_value('Homing_Offset', -1792) == 3840
    assert decode_value('Homing_Offset', 3840) == -1792
    assert decode_value('Present_Load', 1027) == -3
    with pytest.raises(ValueError): encode_value('Homing_Offset', 2048)


@pytest.mark.parametrize('fault,exception', [('checksum',IOError),('error',IOError),('missing',TimeoutError)])
def test_corrupt_errors_and_missing_ids_fail_closed(fault,exception):
    fake=ReplySerial(); fake.fault=fault
    bus=ServoBus('unused', {'a':1,'b':2}, timeout_s=.01, serial_factory=lambda *a,**kw: fake)
    bus.connect()
    with pytest.raises(exception): bus.read_all('Present_Position')
    bus.close()


def test_fragmented_sync_read_and_write():
    fake=ReplySerial()
    bus=ServoBus('unused', {'a':1,'b':2}, serial_factory=lambda *a,**kw: fake)
    bus.connect()
    assert bus.read_all('Present_Position') == {'a':1201,'b':1202}
    bus.write_all('Goal_Position', {'a':1024,'b':2048})
    assert fake.sent[-1][4:-1] == bytes([0x83,42,2,1,0,4,2,0,8])
    bus.close()


def test_gripper_overload_preserves_feedback_and_reports_transient_alarm():
    fake = ReplySerial()
    fake.status = {6: 0x20}
    bus = ServoBus('unused', {'shoulder': 1, 'gripper': 6}, serial_factory=lambda *a, **kw: fake)
    bus.connect()
    # Handshake/configuration are strict, even for the gripper.
    with pytest.raises(ServoStatusError, match='overload alarm'):
        bus.read_all('Present_Position')
    bus.allowed_status = {6: 0x20}
    assert bus.read_all('Present_Position') == {'shoulder': 1201, 'gripper': 1206}
    # An opening command is possible while the servo reports overload.
    bus.write('Torque_Enable', 'gripper', 1)
    bus.write_all('Goal_Position', {'shoulder': 1201, 'gripper': 1800})
    fake.status = {}
    bus.read_all('Present_Position')
    assert bus.consume_status() == {6: 0x20}
    assert bus.consume_status() == {}
    fake.status = {6: 0x20}
    bus.read_all('Present_Position')
    assert bus.consume_status() == {6: 0x20}
    assert bus.consume_status() == {6: 0x20}  # Still present in the latest reply.
    bus.close()


@pytest.mark.parametrize('motor,status', [(1, 0x20), (6, 0x01), (6, 0x04), (6, 0x08),
                                         (6, 0x21), (6, 0x24), (6, 0x28), (6, 0x40)])
def test_gripper_exception_does_not_hide_other_motor_or_combined_alarms(motor, status):
    fake = ReplySerial()
    fake.status = {motor: status}
    bus = ServoBus('unused', {'shoulder': 1, 'gripper': 6}, serial_factory=lambda *a, **kw: fake)
    bus.allowed_status = {6: 0x20}
    bus.connect()
    with pytest.raises(ServoStatusError) as caught:
        bus.read_all('Present_Position')
    assert caught.value.motor_id == motor and caught.value.status == status
    bus.close()


@pytest.mark.parametrize('fault,exception', [('checksum', IOError), ('missing', TimeoutError)])
def test_gripper_warning_never_accepts_corrupt_or_incomplete_packets(fault, exception):
    fake = ReplySerial()
    fake.status, fake.fault = {6: 0x20}, fault
    bus = ServoBus('unused', {'shoulder': 1, 'gripper': 6}, timeout_s=.01,
                   serial_factory=lambda *a, **kw: fake)
    bus.allowed_status = {6: 0x20}
    bus.connect()
    with pytest.raises(exception):
        bus.read_all('Present_Position')
    bus.close()


def test_gripper_overload_with_wrong_payload_length_is_rejected():
    class ShortReply(ReplySerial):
        def write(self, data):
            self.buffer.extend(packet(6, 0x20, b'\x01'))
            return len(data)
    bus = ServoBus('unused', {'gripper': 6}, serial_factory=ShortReply)
    bus.allowed_status = {6: 0x20}
    bus.connect()
    with pytest.raises(IOError, match='wrong data length'):
        bus.read_all('Present_Position')
    bus.close()


def test_pyserial_transport_with_pty_servos_and_read_only_driver(tmp_path):
    pytest.importorskip('serial')
    master, slave = os.openpty()
    port = os.ttyname(slave)
    motors = {'shoulder_pan':1, 'shoulder_lift':2, 'elbow_flex':3, 'wrist_flex':4, 'wrist_roll':5, 'gripper':6}
    raw={j:{'id':i,'drive_mode':0,'homing_offset':-50,'range_min':500,'range_max':3500} for j,i in motors.items()}
    path=tmp_path/'arm.json';path.write_text(json.dumps(raw))
    registers={9:500,11:3500,31:2098,56:2000,40:0}
    stop, writes, errors = threading.Event(), [], []
    def emulate():
        buffer=bytearray()
        try:
            while not stop.is_set():
                if not select.select([master],[],[],.01)[0]: continue
                buffer.extend(os.read(master,1024))
                while len(buffer)>=4 and len(buffer)>=buffer[3]+4:
                    total=buffer[3]+4
                    request=bytes(buffer[:total]);del buffer[:total]
                    i,inst,p=request[2],request[4],request[5:-1]
                    if inst not in (2,0x82): writes.append(request); continue
                    address,size=p[:2]
                    ids=p[2:] if inst==0x82 else [i]
                    response=b''.join(packet(m,0,registers.get(address,0).to_bytes(size,'little')) for m in ids)
                    os.write(master,response)
        except Exception as error: errors.append(error)
    thread=threading.Thread(target=emulate);thread.start()
    driver=None
    try:
        driver=SO101Bus(port,path,list(motors),{'closed_rad':0.,'open_rad':3000*TICK_RAD}, motors=motors, read_only=True)
        assert driver.calibration_matches()
        assert driver.read().shape==(6,)
        with pytest.raises(RuntimeError): driver.enable(True)
        with pytest.raises(RuntimeError): driver.write([0]*6)
        with pytest.raises(RuntimeError): driver.configure({})
        driver.close();driver=None
        assert not writes and not errors
    finally:
        if driver is not None: driver.close()
        stop.set();thread.join(timeout=2)
        os.close(master);os.close(slave)


def test_address_change_write_does_not_wait_for_old_address_ack():
    fake=ReplySerial();fake.fault='error'
    bus=ServoBus('unused',{'motor':9},serial_factory=lambda *a,**kw:fake)
    bus.connect()
    bus.write('ID','motor',1,expect_reply=False)
    assert fake.sent[-1] == packet(9,3,bytes([5,1]))
    bus.close()
