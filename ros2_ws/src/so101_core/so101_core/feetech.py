"""Project-owned STS3215 protocol-0 transport. Only pyserial is required.

Register/instruction reference: https://github.com/ftservo/FTServo_Python
All transactions have one deadline, validate checksums and servo error bits,
and reject missing/duplicate replies. An explicitly allowed status is retained
for diagnostics; all other alarm bits raise. Connection and close never write registers.
"""
import math
import time

REGISTERS = {
    "Firmware_Major_Version": (0, 1), "Firmware_Minor_Version": (1, 1),
    "Model_Number": (3, 2), "ID": (5, 1), "Baud_Rate": (6, 1),
    "Return_Delay_Time": (7, 1), "Min_Position_Limit": (9, 2),
    "Max_Position_Limit": (11, 2), "Max_Torque_Limit": (16, 2),
    "Phase": (18, 1), "P_Coefficient": (21, 1), "D_Coefficient": (22, 1),
    "I_Coefficient": (23, 1), "Protection_Current": (28, 2),
    "Homing_Offset": (31, 2), "Operating_Mode": (33, 1),
    "Overload_Torque": (36, 1), "Torque_Enable": (40, 1),
    "Acceleration": (41, 1), "Goal_Position": (42, 2), "Lock": (55, 1),
    "Present_Position": (56, 2), "Present_Load": (60, 2),
    "Present_Voltage": (62, 1), "Present_Temperature": (63, 1),
    "Present_Current": (69, 2), "Maximum_Acceleration": (85, 1),
}
BAUD_CODES = {1000000: 0, 500000: 1, 250000: 2, 128000: 3,
              115200: 4, 76800: 5, 57600: 6, 38400: 7}
TICK_RAD = 2 * math.pi / 4095


class ServoStatusError(IOError):
    """A checksum-valid reply reporting a servo alarm, not transport corruption."""
    def __init__(self, motor_id, status):
        self.motor_id, self.status = motor_id, status
        alarms = [label for bit, label in [(0x01, "input voltage alarm"), (0x20, "overload alarm")]
                  if status & bit]
        detail = " (" + ", ".join(alarms) + ")" if alarms else ""
        super().__init__(f"Servo ID {motor_id} status error 0x{status:02x}{detail}")


def encode_value(register, value):
    value = int(value)
    if register in ("Homing_Offset", "Goal_Position"):
        bit = 11 if register == "Homing_Offset" else 15
        if abs(value) >= 1 << bit:
            raise ValueError(f"{register} must fit signed-magnitude {bit} bits")
        return abs(value) | ((1 << bit) if value < 0 else 0)
    if not 0 <= value < 1 << (8 * REGISTERS[register][1]):
        raise ValueError(f"Invalid {register}: {value}")
    return value


def decode_value(register, value):
    bit = {"Homing_Offset": 11, "Present_Position": 15, "Goal_Position": 15,
           "Present_Load": 10}.get(register)
    if bit is not None:
        return -(value & ((1 << bit)-1)) if value & (1 << bit) else value
    return value


def packet(motor_id, instruction, params=b""):
    body = bytes([motor_id, len(params)+2, instruction]) + bytes(params)
    return b"\xff\xff" + body + bytes([(~sum(body)) & 255])


class ServoBus:
    def __init__(self, port, motors, baudrate=1000000, timeout_s=.05, serial_factory=None):
        if not motors or len(set(motors.values())) != len(motors):
            raise ValueError("Motor IDs must be present and unique on each arm")
        if any(type(i) is not int or not 1 <= i <= 253 for i in motors.values()):
            raise ValueError("Motor IDs must be integers in 1..253")
        if baudrate not in BAUD_CODES or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Unsupported baudrate or timeout")
        self.port, self.motors = port, dict(motors)
        self.baudrate, self.timeout_s = baudrate, timeout_s
        self.serial_factory, self.serial = serial_factory, None
        # Strict by default, including handshake, calibration, and configuration.
        self.allowed_status = {}
        self.last_status, self.status_events = {}, {}

    def consume_status(self):
        """Accepted alarm bits seen since the last report, including current bits."""
        status = dict(self.status_events)
        for motor_id, flags in self.last_status.items():
            if flags:
                status[motor_id] = status.get(motor_id, 0) | flags
        self.status_events.clear()
        return status

    @property
    def is_connected(self):
        return self.serial is not None

    def connect(self):
        if self.serial is not None:
            raise RuntimeError("Already connected")
        if self.serial_factory is None:
            import serial
            factory = serial.Serial
        else:
            factory = self.serial_factory
        self.serial = factory(self.port, baudrate=self.baudrate, timeout=.005,
                              write_timeout=self.timeout_s, exclusive=True)

    def close(self):
        if self.serial is not None:
            self.serial.close()
            self.serial = None

    def _transaction(self, motor_id, instruction, params, expected):
        if self.serial is None:
            raise RuntimeError("Serial port is closed")
        self.serial.reset_input_buffer()
        outgoing = packet(motor_id, instruction, params)
        if self.serial.write(outgoing) != len(outgoing):
            raise IOError("Incomplete servo packet write")
        if not expected:
            return {}
        deadline, buffer, result = time.monotonic()+self.timeout_s, bytearray(), {}
        while len(result) < len(expected) and time.monotonic() < deadline:
            buffer.extend(self.serial.read(max(1, self.serial.in_waiting)))
            while len(buffer) >= 4:
                start = buffer.find(b"\xff\xff")
                if start < 0:
                    buffer[:] = buffer[-1:] if buffer[-1] == 255 else b""
                    break
                del buffer[:start]
                if len(buffer) < 4:
                    break
                length = buffer[3]
                if not 2 <= length <= 64:
                    raise IOError("Invalid servo reply length")
                total = length + 4
                if len(buffer) < total:
                    break
                reply = bytes(buffer[:total])
                del buffer[:total]
                if sum(reply[2:]) & 255 != 255:
                    raise IOError("Servo reply checksum mismatch")
                servo_id, error = reply[2], reply[4]
                if servo_id not in expected or servo_id in result:
                    raise IOError(f"Unexpected/duplicate servo ID {servo_id}")
                payload = reply[5:-1]
                if len(payload) != expected[servo_id]:
                    raise IOError(f"Servo ID {servo_id} returned wrong data length")
                if error & ~self.allowed_status.get(servo_id, 0):
                    raise ServoStatusError(servo_id, error)
                self.last_status[servo_id] = error
                if error:
                    self.status_events[servo_id] = self.status_events.get(servo_id, 0) | error
                result[servo_id] = payload
        if len(result) != len(expected):
            raise TimeoutError(f"Missing motor IDs: {sorted(set(expected)-set(result))}")
        return result

    def read(self, register, name):
        address, size = REGISTERS[register]
        motor_id = self.motors[name]
        data = self._transaction(motor_id, 2, bytes([address, size]), {motor_id: size})[motor_id]
        return decode_value(register, int.from_bytes(data, "little"))

    def read_all(self, register, names=None):
        names = list(self.motors) if names is None else list(names)
        address, size = REGISTERS[register]
        ids = [self.motors[name] for name in names]
        data = self._transaction(254, 0x82, bytes([address, size, *ids]), {i: size for i in ids})
        return {name: decode_value(register, int.from_bytes(data[i], "little"))
                for name, i in zip(names, ids, strict=True)}

    def write(self, register, name, value, expect_reply=True):
        address, size = REGISTERS[register]
        motor_id = self.motors[name]
        encoded = encode_value(register, value).to_bytes(size, "little")
        self._transaction(motor_id, 3, bytes([address])+encoded, {motor_id: 0} if expect_reply else {})

    def write_all(self, register, values):
        address, size = REGISTERS[register]
        params = bytearray([address, size])
        for name, value in values.items():
            params.append(self.motors[name])
            params.extend(encode_value(register, value).to_bytes(size, "little"))
        # Protocol sync-write has no status response; read-back is needed for settings.
        self._transaction(254, 0x83, params, {})

    def disable_torque(self):
        errors = []
        for name in self.motors:
            try:
                self.write("Torque_Enable", name, 0)
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise IOError("Torque disable failed: " + "; ".join(errors))

    def read_calibration(self):
        return {name: {"id": motor_id, "drive_mode": 0,
                       "homing_offset": self.read("Homing_Offset", name),
                       "range_min": self.read("Min_Position_Limit", name),
                       "range_max": self.read("Max_Position_Limit", name)}
                for name, motor_id in self.motors.items()}

    def check_motors(self):
        models = self.read_all("Model_Number")
        if any(value != 777 for value in models.values()):
            raise ValueError(f"Expected STS3215 model 777: {models}")
        major = self.read_all("Firmware_Major_Version")
        minor = self.read_all("Firmware_Minor_Version")
        if len({(major[j], minor[j]) for j in self.motors}) != 1:
            raise ValueError("Motors on one arm have different firmware versions")

    def write_calibration(self, calibration):
        for name, item in calibration.items():
            self.write("Lock", name, 0)
            for key, register in [("homing_offset", "Homing_Offset"),
                                  ("range_min", "Min_Position_Limit"), ("range_max", "Max_Position_Limit")]:
                self.write(register, name, item[key])
        if self.read_calibration() != calibration:
            raise IOError("Calibration read-back mismatch")
