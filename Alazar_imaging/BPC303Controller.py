import os
import time


class BPC303Controller:
    """
    Thin pythonnet wrapper for a Thorlabs BPC303/BPC30x benchtop piezo controller.

    The three BPC303 channels are exposed as x/y/z by default:
      x -> channel 1
      y -> channel 2
      z -> channel 3

    Position moves use Kinesis closed-loop piezo position APIs. For BPC30x this
    requires a compatible closed-loop piezo stage/sensor on the channel.
    """

    DEFAULT_KINESIS_DIR = r"C:\Program Files\Thorlabs\Kinesis"
    DEFAULT_SERIAL_NO = "71241834"

    def __init__(
        self,
        serial_no=DEFAULT_SERIAL_NO,
        kinesis_dir=DEFAULT_KINESIS_DIR,
        channels=(1, 2, 3),
        axis_map=None,
        polling_ms=250,
        startup_delay_s=0.25,
        simulation=False,
        auto_connect=True,
    ):
        self.serial_no = str(serial_no)
        self.kinesis_dir = kinesis_dir
        self.channel_ids = tuple(channels)
        self.axis_map = axis_map or {"x": 1, "y": 2, "z": 3}
        self.polling_ms = polling_ms
        self.startup_delay_s = startup_delay_s
        self.simulation = simulation

        self.device = None
        self.channels = {}
        self._loaded = False
        self._simulation_started = False

        if auto_connect:
            self.connect()

    def _load_kinesis(self):
        if self._loaded:
            return

        if not os.path.isdir(self.kinesis_dir):
            raise RuntimeError(f"Kinesis directory not found: {self.kinesis_dir}")

        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(self.kinesis_dir)

        try:
            import clr
        except ImportError as exc:
            raise RuntimeError(
                "pythonnet is required for BPC303Controller. Install it with "
                "`pip install pythonnet` in the Python environment used to run PAM_Main_Nanomax.py."
            ) from exc

        clr.AddReference(os.path.join(self.kinesis_dir, "Thorlabs.MotionControl.DeviceManagerCLI.dll"))
        clr.AddReference(os.path.join(self.kinesis_dir, "Thorlabs.MotionControl.GenericPiezoCLI.dll"))
        clr.AddReference(os.path.join(self.kinesis_dir, "Thorlabs.MotionControl.Benchtop.PiezoCLI.dll"))

        from System import Decimal
        from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI, SimulationManager
        from Thorlabs.MotionControl.Benchtop.PiezoCLI import BenchtopPiezo
        from Thorlabs.MotionControl.GenericPiezoCLI import Piezo

        self._Decimal = Decimal
        self._DeviceManagerCLI = DeviceManagerCLI
        self._SimulationManager = SimulationManager
        self._BenchtopPiezo = BenchtopPiezo
        self._Piezo = Piezo
        self._loaded = True

    def connect(self):
        self._load_kinesis()

        if self.simulation and not self._simulation_started:
            self._SimulationManager.Instance.InitializeSimulations()
            self._simulation_started = True

        self._DeviceManagerCLI.BuildDeviceList()
        self.device = self._BenchtopPiezo.CreateBenchtopPiezo(self.serial_no)
        if self.device is None:
            raise RuntimeError(f"{self.serial_no} is not a BenchtopPiezo device")

        self.device.Connect(self.serial_no)

        for channel_id in self.channel_ids:
            channel = self.device.GetChannel(int(channel_id))
            if channel is None:
                raise RuntimeError(f"BPC303 channel {channel_id} is unavailable")

            if not channel.IsSettingsInitialized():
                channel.WaitForSettingsInitialized(10000)
                if not channel.IsSettingsInitialized():
                    raise RuntimeError(f"BPC303 channel {channel_id} settings failed to initialize")

            channel.StartPolling(self.polling_ms)
            time.sleep(self.startup_delay_s)
            channel.EnableDevice()
            time.sleep(self.startup_delay_s)

            # Load device settings before issuing position commands.
            channel.GetPiezoConfiguration(channel.DeviceID)
            self._set_closed_loop(channel)
            self.channels[int(channel_id)] = channel

        return self

    def _set_closed_loop(self, channel):
        if not hasattr(channel, "SetPositionControlMode"):
            return

        mode = None
        mode_type = getattr(self._Piezo, "PiezoControlModeTypes", None)
        if mode_type is not None and hasattr(mode_type, "CloseLoop"):
            mode = mode_type.CloseLoop
        else:
            try:
                current_mode = channel.GetPositionControlMode()
                mode = current_mode.CloseLoop
            except Exception:
                mode = None

        if mode is not None:
            channel.SetPositionControlMode(mode)
            time.sleep(self.startup_delay_s)

    def _channel_id(self, axis):
        if isinstance(axis, str):
            key = axis.lower()
            if key not in self.axis_map:
                raise ValueError(f"Unknown BPC303 axis: {axis}")
            return int(self.axis_map[key])
        return int(axis)

    def _channel(self, axis):
        channel_id = self._channel_id(axis)
        if channel_id not in self.channels:
            raise RuntimeError(f"BPC303 channel {channel_id} is not connected")
        return self.channels[channel_id]

    def _decimal(self, value):
        return self._Decimal(str(value))

    def move_axis(self, axis, position, wait=False, settle_time_ms=0, tolerance=0.05):
        channel = self._channel(axis)
        if not hasattr(channel, "SetPosition"):
            raise RuntimeError(
                "This Kinesis PiezoChannel does not expose SetPosition. "
                "BPC closed-loop position moves require a compatible closed-loop piezo channel."
            )
        channel.SetPosition(self._decimal(position))
        if wait:
            self.wait_until_axis_settled(axis, position, settle_time_ms=settle_time_ms, tolerance=tolerance)

    def move_x(self, position, wait=False, settle_time_ms=0, tolerance=0.05):
        self.move_axis("x", position, wait=wait, settle_time_ms=settle_time_ms, tolerance=tolerance)

    def move_y(self, position, wait=False, settle_time_ms=0, tolerance=0.05):
        self.move_axis("y", position, wait=wait, settle_time_ms=settle_time_ms, tolerance=tolerance)

    def move_z(self, position, wait=False, settle_time_ms=0, tolerance=0.05):
        self.move_axis("z", position, wait=wait, settle_time_ms=settle_time_ms, tolerance=tolerance)

    def move_xyz(self, x=None, y=None, z=None, wait=False, settle_time_ms=0, tolerance=0.05):
        targets = {"x": x, "y": y, "z": z}
        for axis, value in targets.items():
            if value is not None:
                self.move_axis(axis, value)

        if wait:
            self.wait_until_settled(
                x,
                y,
                target_z=z,
                settle_time_ms=settle_time_ms,
                tolerance_step=tolerance,
            )

    def set_position(self, position):
        if len(position) == 2:
            self.move_xyz(x=position[0], y=position[1])
        elif len(position) == 3:
            self.move_xyz(x=position[0], y=position[1], z=position[2])
        else:
            raise ValueError("position must be [x, y] or [x, y, z]")

    def get_axis_position(self, axis):
        channel = self._channel(axis)
        if not hasattr(channel, "GetPosition"):
            raise RuntimeError("This Kinesis PiezoChannel does not expose GetPosition")
        return float(channel.GetPosition())

    def get_position_values(self):
        values = []
        for axis in ("x", "y", "z"):
            try:
                values.append(self.get_axis_position(axis))
            except Exception:
                values.append(0.0)
        return values

    def get_position(self):
        return ",".join(str(v) for v in self.get_position_values())

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
                raise TimeoutError(f"BPC303 axis {axis} did not settle at {target}")

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
            values = {axis: self.get_axis_position(axis) for axis in targets}
            if all(abs(values[axis] - float(target)) <= tolerance_step for axis, target in targets.items()):
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
                raise TimeoutError(f"BPC303 did not settle at {targets}")

    def set_output_voltage(self, axis, voltage):
        channel = self._channel(axis)
        channel.SetOutputVoltage(self._decimal(voltage))

    def get_output_voltage(self, axis):
        return float(self._channel(axis).GetOutputVoltage())

    def set_voltage_xyz(self, x=None, y=None, z=None):
        for axis, value in {"x": x, "y": y, "z": z}.items():
            if value is not None:
                self.set_output_voltage(axis, value)

    def get_max_travel(self, axis):
        channel = self._channel(axis)
        if not hasattr(channel, "GetMaxTravel"):
            raise RuntimeError("This Kinesis PiezoChannel does not expose GetMaxTravel")
        return float(channel.GetMaxTravel())

    def get_max_output_voltage(self, axis):
        return float(self._channel(axis).GetMaxOutputVoltage())

    def connect_sdk(self):
        # Compatibility with PAM_Main_Nanomax cleanup code.
        if self.device is None:
            self.connect()

    def close(self):
        for channel in list(self.channels.values()):
            try:
                channel.StopPolling()
            except Exception:
                pass
        self.channels.clear()

        if self.device is not None:
            try:
                self.device.Disconnect(True)
            except TypeError:
                self.device.Disconnect()
            finally:
                self.device = None

        if self.simulation and self._simulation_started:
            try:
                self._SimulationManager.Instance.UninitializeSimulations()
            finally:
                self._simulation_started = False

    def disconnect(self):
        self.close()

    def stage_deinitial(self):
        self.close()

    def __enter__(self):
        if self.device is None:
            self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
