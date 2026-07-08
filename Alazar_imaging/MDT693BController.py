import os
import re
import time
from ctypes import POINTER, c_char_p, c_double, c_int, cdll, create_string_buffer


class MDT693BController:
    """
    ctypes wrapper for the Thorlabs MDT693B three-axis open-loop piezo driver.

    Important: MDT693B control is voltage control. It does not provide closed-loop
    stage position feedback. Optional um_per_volt support is only a user-supplied
    calibration convenience, not a measured position.
    """

    DEFAULT_DLL_NAME = "MDT_COMMAND_LIB_x64.dll"

    def __init__(
        self,
        serial_no=None,
        dll_path=None,
        baud=115200,
        timeout_s=3,
        safe_max_voltage=75.0,
        axis_map=None,
        um_per_volt=None,
        backend="serial",
        serial_port=None,
        auto_connect=True,
    ):
        self.serial_no = str(serial_no) if serial_no else None
        self.dll_path = dll_path or os.path.join(os.path.dirname(__file__), self.DEFAULT_DLL_NAME)
        self.baud = int(baud)
        self.timeout_s = int(timeout_s)
        self.safe_max_voltage = None if safe_max_voltage is None else float(safe_max_voltage)
        self.axis_map = axis_map or {"x": "x", "y": "y", "z": "z"}
        self.um_per_volt = um_per_volt
        self.backend = str(backend).lower()
        self.serial_port = serial_port

        self._lib = None
        self._handle = None
        self._serial_conn = None
        self._active_backend = None
        self._limit_voltage = None
        self._device_id = None

        if auto_connect:
            self.connect()

    def _import_serial(self):
        try:
            import serial
            from serial.tools import list_ports
        except Exception as exc:
            raise RuntimeError("pyserial is required for MDT693B serial control") from exc
        return serial, list_ports

    def _load_library(self):
        if self._lib is not None:
            return

        if not os.path.isfile(self.dll_path):
            raise RuntimeError(f"MDT DLL not found: {self.dll_path}")

        dll_dir = os.path.dirname(os.path.abspath(self.dll_path))
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(dll_dir)

        self._lib = cdll.LoadLibrary(os.path.abspath(self.dll_path))
        self._bind_functions()

    def _bind(self, name, restype, argtypes):
        func = getattr(self._lib, name)
        func.restype = restype
        func.argtypes = argtypes
        return func

    def _bind_functions(self):
        self._open = self._bind("Open", c_int, [c_char_p, c_int, c_int])
        self._is_open = self._bind("IsOpen", c_int, [c_char_p])
        self._close = self._bind("Close", c_int, [c_int])
        self._list = self._bind("List", c_int, [c_char_p, c_int])
        self._get_id = self._bind("GetId", c_int, [c_int, c_char_p])
        self._get_limit_voltage = self._bind("GetLimitVoltage", c_int, [c_int, POINTER(c_double)])

        self._get_x_voltage = self._bind("GetXAxisVoltage", c_int, [c_int, POINTER(c_double)])
        self._set_x_voltage = self._bind("SetXAxisVoltage", c_int, [c_int, c_double])
        self._get_y_voltage = self._bind("GetYAxisVoltage", c_int, [c_int, POINTER(c_double)])
        self._set_y_voltage = self._bind("SetYAxisVoltage", c_int, [c_int, c_double])
        self._get_z_voltage = self._bind("GetZAxisVoltage", c_int, [c_int, POINTER(c_double)])
        self._set_z_voltage = self._bind("SetZAxisVoltage", c_int, [c_int, c_double])
        self._get_xyz_voltage = self._bind(
            "GetXYZAxisVoltage",
            c_int,
            [c_int, POINTER(c_double), POINTER(c_double), POINTER(c_double)],
        )
        self._set_xyz_voltage = self._bind(
            "SetXYZAxisVoltage",
            c_int,
            [c_int, c_double, c_double, c_double],
        )

    def _check_connected(self):
        if self._active_backend == "serial":
            if self._serial_conn is None or not self._serial_conn.is_open:
                raise RuntimeError("MDT693B is not connected")
            return
        if self._handle is None or self._handle < 0:
            raise RuntimeError("MDT693B is not connected")

    def _check_result(self, result, action):
        if result < 0:
            raise RuntimeError(f"{action} failed with MDT error code {result}")
        return result

    def _axis_key(self, axis):
        key = axis.lower() if isinstance(axis, str) else str(axis).lower()
        if key not in self.axis_map:
            raise ValueError(f"Unknown MDT axis: {axis}")
        mapped = str(self.axis_map[key]).lower()
        if mapped not in ("x", "y", "z"):
            raise ValueError(f"MDT axis {axis} maps to invalid output {mapped}")
        return mapped

    def _effective_max_voltage(self):
        candidates = []
        if self._limit_voltage is not None and self._limit_voltage > 0:
            candidates.append(float(self._limit_voltage))
        if self.safe_max_voltage is not None:
            candidates.append(float(self.safe_max_voltage))
        if not candidates:
            return None
        return min(candidates)

    def _validate_voltage(self, voltage):
        value = float(voltage)
        max_voltage = self._effective_max_voltage()
        if value < 0:
            raise ValueError(f"MDT voltage must be >= 0 V, got {value}")
        if max_voltage is not None and value > max_voltage:
            raise ValueError(
                f"MDT voltage {value} V exceeds safe limit {max_voltage} V "
                f"(device limit={self._limit_voltage}, configured safe_max_voltage={self.safe_max_voltage})"
            )
        return value

    def list_devices(self):
        devices = []
        if self.backend in ("serial", "auto"):
            try:
                _, list_ports = self._import_serial()
                for port_info in list_ports.comports():
                    if port_info.vid == 0x1313 and port_info.pid == 0x1003:
                        serial_no = port_info.serial_number or ""
                        devices.append([serial_no, "MDT693B", port_info.device])
                if devices or self.backend == "serial":
                    return devices
            except Exception:
                if self.backend == "serial":
                    raise

        self._load_library()
        raw = create_string_buffer(10240)
        self._check_result(self._list(raw, 10240), "MDT device list")
        fields = raw.raw.decode("utf-8", errors="replace").rstrip("\x00").split(",")
        for i in range(0, len(fields) - 1, 2):
            serial = fields[i].strip()
            model_info = fields[i + 1].strip()
            model = model_info
            if "MDT693B" in model_info:
                model = "MDT693B"
            elif "MDT694B" in model_info:
                model = "MDT694B"
            devices.append([serial, model])
        return devices

    def connect(self):
        if self.backend not in ("serial", "dll", "auto"):
            raise ValueError("MDT backend must be 'serial', 'dll', or 'auto'")
        if self.backend in ("serial", "auto"):
            try:
                return self._connect_serial()
            except Exception:
                if self.backend == "serial":
                    raise
                self.close()
        return self._connect_dll()

    def _find_serial_port(self):
        if self.serial_port:
            return self.serial_port
        _, list_ports = self._import_serial()
        candidates = []
        for port_info in list_ports.comports():
            if port_info.vid == 0x1313 and port_info.pid == 0x1003:
                candidates.append(port_info)
        if self.serial_no:
            for port_info in candidates:
                serial_number = port_info.serial_number or ""
                hwid = port_info.hwid or ""
                if self.serial_no == serial_number or self.serial_no in hwid:
                    return port_info.device
        if len(candidates) == 1:
            return candidates[0].device
        if candidates:
            listed = [f"{item.device}({item.serial_number or item.hwid})" for item in candidates]
            raise RuntimeError(f"Multiple MDT serial ports found; set serial_port explicitly: {listed}")
        raise RuntimeError("No MDT693B serial port found for VID_1313/PID_1003")

    def _connect_serial(self):
        serial, _ = self._import_serial()
        port = self._find_serial_port()
        self._serial_conn = serial.Serial(
            port=port,
            baudrate=self.baud,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=float(self.timeout_s),
            write_timeout=float(self.timeout_s),
        )
        self._active_backend = "serial"
        self.serial_port = port

        self._serial_command("ECHO=0")
        reported_serial = self._clean_response(self._serial_command("SERIAL?"))
        if self.serial_no is None:
            self.serial_no = reported_serial
        elif reported_serial and self.serial_no != reported_serial:
            raise RuntimeError(
                f"MDT693B serial mismatch on {port}: expected {self.serial_no}, got {reported_serial}"
            )

        self._device_id = self._clean_response(self._serial_command("ID?"))
        self._limit_voltage = self._parse_first_number(self._serial_command("VLIMIT?"))
        print(
            f"MDT693B connected via {port}: serial={self.serial_no}, "
            f"device_limit={self._limit_voltage} V, safe_limit={self._effective_max_voltage()} V"
        )
        return self

    def _connect_dll(self):
        self._load_library()
        if self.serial_no is None:
            devices = self.list_devices()
            three_axis_devices = [dev for dev in devices if dev[1] == "MDT693B"]
            if not three_axis_devices:
                raise RuntimeError(f"No MDT693B device found. Detected MDT devices: {devices}")
            self.serial_no = three_axis_devices[0][0]

        self._handle = self._open(self.serial_no.encode("utf-8"), self.baud, self.timeout_s)
        self._check_result(self._handle, f"Open MDT693B {self.serial_no}")

        is_open = self._is_open(self.serial_no.encode("utf-8"))
        if is_open != 1:
            raise RuntimeError(f"MDT693B {self.serial_no} did not report open state")

        id_buffer = create_string_buffer(1024)
        self._check_result(self._get_id(self._handle, id_buffer), "Get MDT693B id")
        self._device_id = id_buffer.raw.decode("utf-8", errors="replace").rstrip("\x00")

        limit = c_double(0)
        self._check_result(self._get_limit_voltage(self._handle, limit), "Get MDT693B limit voltage")
        self._limit_voltage = float(limit.value)

        print(
            f"MDT693B connected: serial={self.serial_no}, id={self._device_id}, "
            f"device_limit={self._limit_voltage} V, safe_limit={self._effective_max_voltage()} V"
        )
        self._active_backend = "dll"
        return self

    def _serial_command(self, command):
        self._check_connected()
        self._serial_conn.reset_input_buffer()
        self._serial_conn.write((str(command).strip() + "\r").encode("ascii"))
        self._serial_conn.flush()
        deadline = time.time() + float(self.timeout_s)
        chunks = []
        while time.time() < deadline:
            pending = self._serial_conn.in_waiting
            data = self._serial_conn.read(pending or 1)
            if data:
                chunks.append(data)
                if b">" in data:
                    break
            else:
                time.sleep(0.01)
        raw = b"".join(chunks).decode("ascii", errors="replace")
        if not raw:
            raise TimeoutError(f"MDT693B serial command {command!r} returned no response")
        if "CMD_NOT_DEFINED" in raw or "CMD_ARG_INVALID" in raw:
            raise RuntimeError(f"MDT693B rejected command {command!r}: {raw!r}")
        return raw

    def _clean_response(self, response):
        text = str(response).replace(">", "").replace("\r", "\n")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)

    def _parse_first_number(self, response):
        match = re.search(r"[-+]?\d+(?:\.\d+)?", str(response))
        if not match:
            raise RuntimeError(f"Could not parse MDT voltage response: {response!r}")
        return float(match.group(0))

    @property
    def handle(self):
        return self._handle

    @property
    def limit_voltage(self):
        return self._limit_voltage

    @property
    def device_id(self):
        return self._device_id

    def get_voltage_axis(self, axis):
        self._check_connected()
        mapped = self._axis_key(axis)
        if self._active_backend == "serial":
            return self._parse_first_number(self._serial_command(f"{mapped.upper()}VOLTAGE?"))
        value = c_double(0)
        getter = {
            "x": self._get_x_voltage,
            "y": self._get_y_voltage,
            "z": self._get_z_voltage,
        }[mapped]
        self._check_result(getter(self._handle, value), f"Get MDT {mapped}-axis voltage")
        return float(value.value)

    def get_axis_max_voltage(self, axis):
        self._check_connected()
        if self._active_backend != "serial":
            raise RuntimeError("Axis max voltage query is only implemented for the MDT serial backend")
        mapped = self._axis_key(axis)
        return self._parse_first_number(self._serial_command(f"{mapped.upper()}MAX?"))

    def set_axis_max_voltage(self, axis, voltage):
        self._check_connected()
        if self._active_backend != "serial":
            raise RuntimeError("Axis max voltage setting is only implemented for the MDT serial backend")
        mapped = self._axis_key(axis)
        value = self._validate_voltage(voltage)
        self._serial_command(f"{mapped.upper()}MAX={value:.6f}")
        return self.get_axis_max_voltage(mapped)

    def _get_raw_voltage_xyz(self):
        self._check_connected()
        if self._active_backend == "serial":
            return {axis: self.get_voltage_axis(axis) for axis in ("x", "y", "z")}
        x = c_double(0)
        y = c_double(0)
        z = c_double(0)
        self._check_result(self._get_xyz_voltage(self._handle, x, y, z), "Get MDT xyz voltages")
        return {"x": float(x.value), "y": float(y.value), "z": float(z.value)}

    def get_voltage_xyz(self):
        raw_values = self._get_raw_voltage_xyz()
        return [raw_values[self._axis_key(axis)] for axis in ("x", "y", "z")]

    def get_voltage(self):
        return ",".join(str(v) for v in self.get_voltage_xyz())

    def set_voltage_axis(self, axis, voltage):
        self._check_connected()
        mapped = self._axis_key(axis)
        value = self._validate_voltage(voltage)
        if self._active_backend == "serial":
            self._serial_command(f"{mapped.upper()}VOLTAGE={value:.6f}")
            return
        setter = {
            "x": self._set_x_voltage,
            "y": self._set_y_voltage,
            "z": self._set_z_voltage,
        }[mapped]
        self._check_result(setter(self._handle, value), f"Set MDT {mapped}-axis voltage")

    def set_voltage_xyz(self, x=None, y=None, z=None, wait=False, settle_time_ms=0):
        self._check_connected()
        targets = self._get_raw_voltage_xyz()
        logical_targets = self.get_voltage_xyz()
        requested = {"x": x, "y": y, "z": z}
        for logical_index, (logical_axis, voltage) in enumerate(requested.items()):
            if voltage is not None:
                value = self._validate_voltage(voltage)
                targets[self._axis_key(logical_axis)] = value
                logical_targets[logical_index] = value

        if self._active_backend == "serial":
            for mapped_axis in ("x", "y", "z"):
                if abs(targets[mapped_axis] - self.get_voltage_axis(mapped_axis)) > 1e-9:
                    self._serial_command(f"{mapped_axis.upper()}VOLTAGE={targets[mapped_axis]:.6f}")
            if wait:
                self.wait_until_voltage_settled(logical_targets, settle_time_ms=settle_time_ms)
            return

        ordered_targets = [targets["x"], targets["y"], targets["z"]]
        self._check_result(
            self._set_xyz_voltage(self._handle, ordered_targets[0], ordered_targets[1], ordered_targets[2]),
            "Set MDT xyz voltages",
        )
        if wait:
            self.wait_until_voltage_settled(logical_targets, settle_time_ms=settle_time_ms)

    def wait_until_voltage_settled(self, targets, settle_time_ms=0, tolerance_v=0.25, timeout_s=5.0):
        self._check_connected()
        target_values = [float(v) for v in targets]
        start = time.time()
        stable_once = False
        while True:
            values = self.get_voltage_xyz()
            if all(abs(values[i] - target_values[i]) <= tolerance_v for i in range(3)):
                if stable_once:
                    if settle_time_ms:
                        time.sleep(settle_time_ms / 1000.0)
                    return True
                stable_once = True
                time.sleep(0.02)
            else:
                stable_once = False
                time.sleep(0.02)

            if time.time() - start > timeout_s:
                raise TimeoutError(f"MDT voltages did not settle at {target_values}")

    def voltage_step_from_um(self, step_um):
        if self.um_per_volt is None:
            raise RuntimeError("um_per_volt calibration is required for um-to-voltage conversion")
        return float(step_um) / float(self.um_per_volt)

    def close(self):
        if self._serial_conn is not None:
            try:
                self._serial_conn.close()
            finally:
                self._serial_conn = None
                self._active_backend = None
        if self._handle is not None and self._handle >= 0:
            try:
                self._close(self._handle)
            finally:
                self._handle = None
                self._active_backend = None

    def disconnect(self):
        self.close()

    def __enter__(self):
        if self._handle is None and self._serial_conn is None:
            self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
