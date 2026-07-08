import ctypes
import os
import time
from ctypes import c_bool, c_char_p, c_int, c_short, c_ushort, create_string_buffer


class BPC303NativeController:
    """
    Native Kinesis C API wrapper for a Thorlabs BPC303/BPC30x controller.

    This backend avoids the pythonnet BenchtopPiezo.CreateBenchtopPiezo path,
    which can fail when Kinesis static device settings are incomplete. Position
    commands use the native BPC closed-loop units:

      0..32767 device counts == 0..100% of the channel maximum travel.

    For MAX311D the reported maximum travel should be 20 um, so this class
    converts between um and native counts per channel.
    """

    DEFAULT_KINESIS_DIR = r"C:\Program Files\Thorlabs\Kinesis"
    DEFAULT_SERIAL_NO = "71241834"
    DEVICE_TYPE_ID_71 = 71
    CLOSED_LOOP_MODE = 2
    CLOSED_LOOP_SMOOTH_MODE = 4
    DEVICE_MAX_COUNT = 32767
    STATUS_ZEROED = 0x00000010
    STATUS_ZEROING = 0x00000020

    def __init__(
        self,
        serial_no=DEFAULT_SERIAL_NO,
        kinesis_dir=DEFAULT_KINESIS_DIR,
        channels=(1, 2, 3),
        axis_map=None,
        polling_ms=200,
        startup_delay_s=0.25,
        enable_channels=True,
        force_closed_loop=True,
        safe_max_output_voltage=75.0,
        log_callback=None,
        auto_connect=True,
    ):
        self.serial_no = str(serial_no)
        self.serial_bytes = self.serial_no.encode("ascii")
        self.kinesis_dir = kinesis_dir
        self.channel_ids = tuple(int(ch) for ch in channels)
        self.axis_map = axis_map or {"x": 1, "y": 2, "z": 3}
        self.polling_ms = int(polling_ms)
        self.startup_delay_s = float(startup_delay_s)
        self.enable_channels = bool(enable_channels)
        self.force_closed_loop = bool(force_closed_loop)
        self.safe_max_output_voltage = float(safe_max_output_voltage)
        self.log_callback = log_callback

        self._dll = None
        self._connected = False
        self._polling_channels = set()
        self._travel_um = {}
        self._max_output_voltage = {}

        if auto_connect:
            self.connect()

    def _log(self, event, **fields):
        if self.log_callback is None:
            return
        try:
            self.log_callback(event, **fields)
        except Exception:
            pass

    def _load_library(self):
        if self._dll is not None:
            return
        if not os.path.isdir(self.kinesis_dir):
            raise RuntimeError(f"Kinesis directory not found: {self.kinesis_dir}")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(self.kinesis_dir)
        dll_path = os.path.join(self.kinesis_dir, "Thorlabs.MotionControl.Benchtop.Piezo.dll")
        if not os.path.isfile(dll_path):
            raise RuntimeError(f"BPC native DLL not found: {dll_path}")
        self._dll = ctypes.CDLL(dll_path)
        self._bind_functions()

    def _bind(self, name, restype, argtypes):
        func = getattr(self._dll, name)
        func.restype = restype
        func.argtypes = argtypes
        return func

    def _bind_functions(self):
        self._TLI_BuildDeviceList = self._bind("TLI_BuildDeviceList", c_short, [])
        self._TLI_GetDeviceListByTypeExt = self._bind(
            "TLI_GetDeviceListByTypeExt",
            c_short,
            [ctypes.c_char_p, ctypes.c_ulong, c_int],
        )
        self._PBC_Open = self._bind("PBC_Open", c_short, [c_char_p])
        self._PBC_Close = self._bind("PBC_Close", None, [c_char_p])
        self._PBC_CheckConnection = self._bind("PBC_CheckConnection", c_bool, [c_char_p])
        self._PBC_MaxChannelCount = self._bind("PBC_MaxChannelCount", c_int, [c_char_p])
        self._PBC_GetNumChannels = self._bind("PBC_GetNumChannels", c_short, [c_char_p])
        self._PBC_IsChannelValid = self._bind("PBC_IsChannelValid", c_bool, [c_char_p, c_short])
        self._PBC_StartPolling = self._bind("PBC_StartPolling", c_bool, [c_char_p, c_short, c_int])
        self._PBC_StopPolling = self._bind("PBC_StopPolling", None, [c_char_p, c_short])
        self._PBC_EnableChannel = self._bind("PBC_EnableChannel", c_short, [c_char_p, c_short])
        self._PBC_SetPositionControlMode = self._bind(
            "PBC_SetPositionControlMode",
            c_short,
            [c_char_p, c_short, c_short],
        )
        self._PBC_RequestPositionControlMode = self._bind(
            "PBC_RequestPositionControlMode",
            c_bool,
            [c_char_p, c_short],
        )
        self._PBC_GetPositionControlMode = self._bind("PBC_GetPositionControlMode", c_short, [c_char_p, c_short])
        self._PBC_RequestMaximumTravel = self._bind("PBC_RequestMaximumTravel", c_bool, [c_char_p, c_short])
        self._PBC_GetMaximumTravel = self._bind("PBC_GetMaximumTravel", c_ushort, [c_char_p, c_short])
        self._PBC_RequestMaxOutputVoltage = self._bind("PBC_RequestMaxOutputVoltage", c_bool, [c_char_p, c_short])
        self._PBC_GetMaxOutputVoltage = self._bind("PBC_GetMaxOutputVoltage", c_short, [c_char_p, c_short])
        self._PBC_RequestActualPosition = self._bind("PBC_RequestActualPosition", c_short, [c_char_p, c_short])
        self._PBC_RequestPosition = self._bind("PBC_RequestPosition", c_short, [c_char_p, c_short])
        self._PBC_GetPosition = self._bind("PBC_GetPosition", c_short, [c_char_p, c_short])
        self._PBC_SetPosition = self._bind("PBC_SetPosition", c_short, [c_char_p, c_short, c_short])
        self._PBC_RequestStatusBits = self._bind("PBC_RequestStatusBits", c_short, [c_char_p, c_short])
        self._PBC_GetStatusBits = self._bind("PBC_GetStatusBits", ctypes.c_ulong, [c_char_p, c_short])
        self._PBC_SetZero = self._bind("PBC_SetZero", c_short, [c_char_p, c_short])

    def _check_result(self, result, action):
        if result != 0:
            raise RuntimeError(f"{action} failed with BPC native error code {result}")

    def _channel_id(self, axis):
        if isinstance(axis, str):
            key = axis.lower()
            if key not in self.axis_map:
                raise ValueError(f"Unknown BPC303 axis: {axis}")
            return int(self.axis_map[key])
        return int(axis)

    def _axis_name_for_channel(self, channel_id):
        for axis, mapped_channel in self.axis_map.items():
            if int(mapped_channel) == int(channel_id):
                return axis
        return str(channel_id)

    def _device_list_by_type(self, type_id):
        buffer = create_string_buffer(1024)
        ret = self._TLI_GetDeviceListByTypeExt(buffer, 1024, int(type_id))
        self._check_result(ret, f"TLI_GetDeviceListByTypeExt({type_id})")
        raw = buffer.value.decode("ascii", errors="replace")
        return [item for item in raw.split(",") if item]

    def connect(self):
        self._log("BPC_CONNECT_LOAD_LIBRARY_BEGIN", serial=self.serial_no)
        self._load_library()
        self._log("BPC_CONNECT_LOAD_LIBRARY_DONE", serial=self.serial_no)

        self._log("BPC_CONNECT_BUILD_DEVICE_LIST_BEGIN", serial=self.serial_no)
        ret = self._TLI_BuildDeviceList()
        self._log("BPC_CONNECT_BUILD_DEVICE_LIST_DONE", serial=self.serial_no, result=ret)
        self._check_result(ret, "TLI_BuildDeviceList")

        self._log("BPC_CONNECT_ENUMERATE_BEGIN", serial=self.serial_no, device_type=self.DEVICE_TYPE_ID_71)
        serials = self._device_list_by_type(self.DEVICE_TYPE_ID_71)
        self._log(
            "BPC_CONNECT_ENUMERATE_DONE",
            serial=self.serial_no,
            device_type=self.DEVICE_TYPE_ID_71,
            serials=",".join(serials) if serials else "[]",
        )
        if self.serial_no not in serials:
            raise RuntimeError(f"BPC303 serial {self.serial_no} not found in type 71 device list: {serials}")

        self._log("BPC_CONNECT_OPEN_BEGIN", serial=self.serial_no)
        ret = self._PBC_Open(self.serial_bytes)
        self._log("BPC_CONNECT_OPEN_DONE", serial=self.serial_no, result=ret)
        self._check_result(ret, f"PBC_Open({self.serial_no})")
        self._connected = True
        self._log("BPC_CONNECT_CHECK_CONNECTION_BEGIN", serial=self.serial_no)
        is_connected = self._PBC_CheckConnection(self.serial_bytes)
        self._log("BPC_CONNECT_CHECK_CONNECTION_DONE", serial=self.serial_no, connected=is_connected)
        if not is_connected:
            raise RuntimeError(f"BPC303 {self.serial_no} did not report a valid USB connection")

        max_channels = int(self._PBC_MaxChannelCount(self.serial_bytes))
        num_channels = int(self._PBC_GetNumChannels(self.serial_bytes))
        for channel_id in self.channel_ids:
            self._log("BPC_CHANNEL_SETUP_BEGIN", serial=self.serial_no, channel=channel_id)
            if channel_id > max_channels or channel_id > num_channels:
                raise RuntimeError(
                    f"BPC303 channel {channel_id} unavailable "
                    f"(max_channel_count={max_channels}, num_channels={num_channels})"
                )
            if not self._PBC_IsChannelValid(self.serial_bytes, channel_id):
                raise RuntimeError(f"BPC303 channel {channel_id} is not valid")

            self._log("BPC_CHANNEL_START_POLLING_BEGIN", serial=self.serial_no, channel=channel_id, polling_ms=self.polling_ms)
            polling_started = self._PBC_StartPolling(self.serial_bytes, channel_id, self.polling_ms)
            self._log("BPC_CHANNEL_START_POLLING_DONE", serial=self.serial_no, channel=channel_id, started=polling_started)
            if not polling_started:
                raise RuntimeError(f"BPC303 channel {channel_id} failed to start polling")
            self._polling_channels.add(channel_id)
            time.sleep(self.startup_delay_s)

            if self.enable_channels:
                self._log("BPC_CHANNEL_ENABLE_BEGIN", serial=self.serial_no, channel=channel_id)
                ret = self._PBC_EnableChannel(self.serial_bytes, channel_id)
                self._log("BPC_CHANNEL_ENABLE_DONE", serial=self.serial_no, channel=channel_id, result=ret)
                self._check_result(ret, f"PBC_EnableChannel({channel_id})")
                time.sleep(self.startup_delay_s)

            if self.force_closed_loop:
                self._log("BPC_CHANNEL_CLOSED_LOOP_BEGIN", serial=self.serial_no, channel=channel_id)
                self.ensure_closed_loop_axis(channel_id, timeout_s=10.0)
                self._log("BPC_CHANNEL_CLOSED_LOOP_DONE", serial=self.serial_no, channel=channel_id)

            self._log("BPC_CHANNEL_LIMITS_BEGIN", serial=self.serial_no, channel=channel_id)
            self._cache_channel_limits(channel_id)
            self._log(
                "BPC_CHANNEL_LIMITS_DONE",
                serial=self.serial_no,
                channel=channel_id,
                travel_um=self._travel_um.get(channel_id),
                max_output_voltage=self._max_output_voltage.get(channel_id),
            )

        self._log("BPC_CONNECT_DONE", serial=self.serial_no)
        return self

    def _cache_channel_limits(self, channel_id):
        self._PBC_RequestMaximumTravel(self.serial_bytes, channel_id)
        self._PBC_RequestMaxOutputVoltage(self.serial_bytes, channel_id)
        self._PBC_RequestPositionControlMode(self.serial_bytes, channel_id)
        time.sleep(0.1)

        max_travel_raw = int(self._PBC_GetMaximumTravel(self.serial_bytes, channel_id))
        max_voltage_raw = int(self._PBC_GetMaxOutputVoltage(self.serial_bytes, channel_id))
        mode = int(self._PBC_GetPositionControlMode(self.serial_bytes, channel_id))

        travel_um = max_travel_raw * 0.1
        max_voltage = max_voltage_raw / 10.0
        if travel_um <= 0:
            raise RuntimeError(f"BPC303 channel {channel_id} reported invalid max travel: {max_travel_raw}")
        if max_voltage > self.safe_max_output_voltage + 1e-9:
            raise RuntimeError(
                f"BPC303 channel {channel_id} max output {max_voltage} V exceeds safe limit "
                f"{self.safe_max_output_voltage} V"
            )
        if self.force_closed_loop and mode != self.CLOSED_LOOP_MODE:
            raise RuntimeError(f"BPC303 channel {channel_id} is not in closed-loop mode after setup (mode={mode})")

        self._travel_um[channel_id] = travel_um
        self._max_output_voltage[channel_id] = max_voltage

    def _position_to_count(self, axis, position_um):
        channel_id = self._channel_id(axis)
        travel_um = self._travel_um[channel_id]
        value = float(position_um)
        if value < 0 or value > travel_um:
            raise ValueError(
                f"BPC303 axis {axis} target {value} um outside valid native range [0, {travel_um}] um"
            )
        return int(round(value / travel_um * self.DEVICE_MAX_COUNT))

    def _count_to_position(self, axis, count):
        channel_id = self._channel_id(axis)
        clamped = max(0, int(count))
        return clamped / float(self.DEVICE_MAX_COUNT) * self._travel_um[channel_id]

    def get_axis_position_raw(self, axis):
        channel_id = self._channel_id(axis)
        self._PBC_RequestActualPosition(self.serial_bytes, channel_id)
        self._PBC_RequestPosition(self.serial_bytes, channel_id)
        time.sleep(0.02)
        return int(self._PBC_GetPosition(self.serial_bytes, channel_id))

    def get_axis_position(self, axis):
        return self._count_to_position(axis, self.get_axis_position_raw(axis))

    def get_position_values(self):
        return [self.get_axis_position(axis) for axis in ("x", "y", "z")]

    def get_position(self):
        return ",".join(str(v) for v in self.get_position_values())

    def get_max_travel(self, axis):
        return self._travel_um[self._channel_id(axis)]

    def get_max_output_voltage(self, axis):
        return self._max_output_voltage[self._channel_id(axis)]

    def get_position_control_mode(self, axis):
        channel_id = self._channel_id(axis)
        if not self._PBC_RequestPositionControlMode(self.serial_bytes, channel_id):
            raise RuntimeError(f"PBC_RequestPositionControlMode({channel_id}) failed")
        time.sleep(0.02)
        return int(self._PBC_GetPositionControlMode(self.serial_bytes, channel_id))

    def ensure_closed_loop_axis(self, axis, timeout_s=15.0):
        channel_id = self._channel_id(axis)
        start = time.time()
        last_mode = None
        while True:
            self._check_result(
                self._PBC_SetPositionControlMode(self.serial_bytes, channel_id, self.CLOSED_LOOP_MODE),
                f"PBC_SetPositionControlMode({channel_id}, closed loop)",
            )
            time.sleep(0.1)
            last_mode = self.get_position_control_mode(channel_id)
            if last_mode == self.CLOSED_LOOP_MODE:
                return True
            if time.time() - start > timeout_s:
                raise TimeoutError(
                    f"BPC303 channel {channel_id} did not enter closed-loop mode; mode={last_mode}"
                )
            time.sleep(0.2)

    def ensure_closed_loop_axes(self, axes=("x", "y", "z"), timeout_s=15.0):
        for axis in axes:
            self.ensure_closed_loop_axis(axis, timeout_s=timeout_s)

    def get_status_bits(self, axis):
        channel_id = self._channel_id(axis)
        self._check_result(self._PBC_RequestStatusBits(self.serial_bytes, channel_id), f"PBC_RequestStatusBits({channel_id})")
        time.sleep(0.02)
        return int(self._PBC_GetStatusBits(self.serial_bytes, channel_id))

    def set_zero_axis(self, axis, wait=True, settle_time_ms=0, timeout_s=60.0):
        """
        Run the BPC zero routine for one axis.

        Thorlabs documents PBC_SetZero as setting the output voltage to zero and
        defining the ensuing actuator position as zero. This changes the datum.
        """
        channel_id = self._channel_id(axis)
        self._check_result(self._PBC_SetZero(self.serial_bytes, channel_id), f"PBC_SetZero({channel_id})")
        if wait:
            self.wait_until_axis_zeroed(axis, settle_time_ms=settle_time_ms, timeout_s=timeout_s)
        if self.force_closed_loop:
            # Zeroing can leave the channel out of closed-loop position control;
            # PBC_SetPosition is ignored unless the channel is closed-loop again.
            self.ensure_closed_loop_axis(channel_id, timeout_s=timeout_s)

    def set_zero_axes(self, axes=("x", "y"), wait=True, settle_time_ms=0, timeout_s=60.0):
        for axis in axes:
            self.set_zero_axis(axis, wait=wait, settle_time_ms=settle_time_ms, timeout_s=timeout_s)

    def wait_until_axis_zeroed(self, axis, settle_time_ms=0, timeout_s=60.0, zeroing_stuck_grace_s=2.0):
        start = time.time()
        while True:
            status = self.get_status_bits(axis)
            zeroed = bool(status & self.STATUS_ZEROED)
            zeroing = bool(status & self.STATUS_ZEROING)
            # This BPC303 can keep the zeroing bit set after the zeroed bit is
            # already asserted, so do not wait forever for bit 0x20 to clear.
            if zeroed and (not zeroing or time.time() - start >= zeroing_stuck_grace_s):
                if settle_time_ms:
                    time.sleep(settle_time_ms / 1000.0)
                return True
            if time.time() - start > timeout_s:
                raise TimeoutError(f"BPC303 axis {axis} did not report zeroed status; status=0x{status:08X}")
            time.sleep(0.05)

    def move_axis(self, axis, position, wait=False, settle_time_ms=0, tolerance=0.05, timeout_s=10.0):
        channel_id = self._channel_id(axis)
        target_count = self._position_to_count(axis, position)
        self._check_result(
            self._PBC_SetPosition(self.serial_bytes, channel_id, target_count),
            f"PBC_SetPosition(axis={axis}, count={target_count})",
        )
        if wait:
            self.wait_until_axis_settled(axis, position, settle_time_ms, tolerance, timeout_s)

    def move_xyz(self, x=None, y=None, z=None, wait=False, settle_time_ms=0, tolerance=0.05, timeout_s=10.0):
        for axis, value in {"x": x, "y": y, "z": z}.items():
            if value is not None:
                self.move_axis(axis, value)
        if wait:
            self.wait_until_settled(x, y, target_z=z, settle_time_ms=settle_time_ms, tolerance_step=tolerance, timeout_s=timeout_s)

    def set_position(self, position):
        if len(position) == 2:
            self.move_xyz(x=position[0], y=position[1])
        elif len(position) == 3:
            self.move_xyz(x=position[0], y=position[1], z=position[2])
        else:
            raise ValueError("position must be [x, y] or [x, y, z]")

    def wait_until_axis_settled(self, axis, target, settle_time_ms=0, tolerance=0.05, timeout_s=10.0):
        start = time.time()
        stable_once = False
        target = float(target)
        while True:
            current = self.get_axis_position(axis)
            if abs(current - target) <= tolerance:
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
                raise TimeoutError(f"BPC303 native axis {axis} did not settle at {target} um")

    def wait_until_settled(
        self,
        target_x,
        target_y,
        target_z=None,
        settle_time_ms=0,
        tolerance_step=0.05,
        timeout_s=10.0,
    ):
        targets = {"x": target_x, "y": target_y}
        if target_z is not None:
            targets["z"] = target_z
        start = time.time()
        stable_once = False
        while True:
            values = {axis: self.get_axis_position(axis) for axis in targets if targets[axis] is not None}
            if all(abs(values[axis] - float(target)) <= tolerance_step for axis, target in targets.items() if target is not None):
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
                raise TimeoutError(f"BPC303 native did not settle at {targets}; current={values}")

    def close(self):
        for channel_id in list(self._polling_channels):
            try:
                self._PBC_StopPolling(self.serial_bytes, channel_id)
            except Exception:
                pass
        self._polling_channels.clear()
        if self._connected:
            try:
                self._PBC_Close(self.serial_bytes)
            finally:
                self._connected = False

    def disconnect(self):
        self.close()

    def __enter__(self):
        if not self._connected:
            self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
