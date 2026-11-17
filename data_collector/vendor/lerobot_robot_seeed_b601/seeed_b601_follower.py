import logging
import math
import time
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from lerobot.cameras import CameraConfig
from lerobot.cameras.utils import make_cameras_from_configs
from lerobot.motors import MotorCalibration
from motorbridge import Controller as MotorBridgeController, Mode as MotorBridgeMode
from lerobot.processor import RobotAction, RobotObservation
from lerobot.utils.errors import DeviceAlreadyConnectedError, DeviceNotConnectedError

from lerobot.robots.robot import Robot
from lerobot.robots.utils import ensure_safe_goal_position


@dataclass
class SeeedB601FollowerConfigBase:
    """Base configuration for the Seeed B601 Follower arm."""

    # Communication port for CAN adapter (e.g., "can0" for SocketCAN, or "/dev/ttyACM0" for Damiao serial bridge)
    port: str
    
    # CAN adapter type:
    #   "socketcan"  - SocketCAN based adapters (PCAN, slcan, embedded can controller, etc.)
    #   "damiao"     - Damiao dedicated serial bridge
    #   "robstride"  - RobStride dedicated adapter (placeholder, not yet supported)
    can_adapter: str = "socketcan"

    # Baud rate for Damiao serial bridge (only used when can_adapter="damiao")
    dm_serial_baud: int = 921600

    disable_torque_on_disconnect: bool = True

    # Max relative target for joint movements, in degrees
    max_relative_target: float | dict[str, float] | None = None

    cameras: dict[str, CameraConfig] = field(default_factory=dict)
    
    # Motor configuration must be provided by concrete subclasses.
    # Maps motor names to (send_can_id, recv_can_id)
    motor_can_ids: dict[str, tuple[int, int]] = field(default_factory=dict)

    # Control parameters are defined by concrete subclasses so different motor families
    # can keep their own defaults.
    ## Default target velocity for joints running in POS_VEL mode, in degrees/s.
    pos_vel_velocity: float | list[float] = field(default_factory=list)

    ## Default torque/current ration for gripper's FORCE_POS mode, in range [0,1].
    force_pos_torque_ration: float = 0.1

    # Soft joint limits in degrees. Concrete subclasses should define defaults.
    joint_limits: dict[str, tuple[float, float]] = field(default_factory=dict)

    # Per-joint action direction/scale applied before joint-limit clipping.
    # Use -1 for sign flip, 1 for no flip, and other values when scaling is required.
    joint_directions: dict[str, float] = field(default_factory=dict)

    # Gravity-compensation feedforward added to MIT tau for the 6 arm joints.
    # Disabled by default; enable in the concrete follower config (e.g. RS).
    gravity_compensation: bool = False
    # URDF for gravity calc. Set by concrete subclasses (e.g. RS follower).
    gravity_urdf_path: str | None = None

    # Temperature protection thresholds (degrees Celsius, read from each motor's t_mos).
    # Concrete subclasses may override per motor family.
    temp_alarm_threshold_c: float = 80.0              # print HIGH TEMP warning above this
    temp_overheat_threshold_c: float = 100.0           # raise KeyboardInterrupt above this
    temp_emergency_disable_threshold_c: float = 135.0  # safe_zero emergency disable-torque limit


logger = logging.getLogger(__name__)


FOLLOWER_GRIPPER_MOTOR = "gripper"
LONG_TIMEOUT_SEC = 0.1
MEDIUM_TIMEOUT_SEC = 0.01

class SeeedB601FollowerBase(Robot):
    """
    Base class for Seeed B601 Follower Arms (DM and RS variants).
    Uses CAN bus communication via motorbridge.
    """

    motor_type: str = ""

    def __init__(self, config: SeeedB601FollowerConfigBase):
        super().__init__(config)
        self.config = config
        self.bus = None
        self.motors = {}
        self.motor_names = list(config.motor_can_ids.keys())
        self._in_safe_zero = False
        self._connection_ready = False
        self._emergency_disable_requested = False
        # motor_name -> URDF q_idx, built once in connect(); None disables ff.
        self._gravity_pairing = None
        # When True, disconnect() skips safe_zero(). Set by calibrate() so the
        # lerobot-calibrate entrypoint (which calls calibrate() then immediately
        # disconnect()) does not move the arm back to zero. connect() resets this
        # after its internal calibrate() call so normal use still safe-zeros.
        self._skip_safe_zero_on_disconnect = False

        # Initialize cameras
        self.cameras = make_cameras_from_configs(config.cameras)

    @property
    def _motors_ft(self) -> dict[str, type]:
        """Motor features for observation and action spaces."""
        features: dict[str, type] = {}
        for motor in self.motor_names:
            features[f"{motor}.pos"] = float
            # features[f"{motor}.vel"] = float
            # features[f"{motor}.torque"] = float
        return features

    @property
    def _cameras_ft(self) -> dict[str, tuple]:
        """Camera features for observation space."""
        return {
            cam: (self.config.cameras[cam].height, self.config.cameras[cam].width, 3)
            for cam in self.cameras
        }

    @cached_property
    def observation_features(self) -> dict[str, type | tuple]:
        """Combined observation features from motors and cameras."""
        return {**self._motors_ft, **self._cameras_ft}

    @cached_property
    def action_features(self) -> dict[str, type]:
        """Action features."""
        return self._motors_ft

    @property
    def is_connected(self) -> bool:
        """Check if robot is connected."""
        return self.bus is not None and all(cam.is_connected for cam in self.cameras.values())

    def _add_motors_to_bus(self):
        """Must be implemented by subclasses to add specific motor types to self.bus."""
        raise NotImplementedError

    def connect(self, calibrate: bool = True) -> None:
        """Connect to the follower arm and optionally calibrate."""
        if self.is_connected:
            raise DeviceAlreadyConnectedError(f"{self} already connected")

        self._connection_ready = False
        logger.info(f"Connecting arm on {self.config.port} (adapter={self.config.can_adapter})...")
        if self.config.can_adapter == "damiao":
            self.bus = MotorBridgeController.from_dm_serial(
                serial_port=self.config.port,
                baud=self.config.dm_serial_baud,
            )
        elif self.config.can_adapter == "robstride":
            raise NotImplementedError(
                "RobStride dedicated USB-to-CAN adapter is not yet supported in motorbridge Python SDK."
            )
        else:
            # Default: socketcan (PCAN, slcan, etc.)
            self.bus = MotorBridgeController(channel=self.config.port)
        
        self._add_motors_to_bus()

        if not self.is_calibrated and calibrate:
            logger.info(
                "Mismatch between calibration values in the motor and the calibration file or no calibration file found"
            )
            self.calibrate()
            # calibrate() set _skip_safe_zero_on_disconnect for the calibrate
            # script's benefit; clear it here so a later disconnect (after
            # normal use of this connected arm) still runs safe_zero().
            self._skip_safe_zero_on_disconnect = False

        for cam in self.cameras.values():
            cam.connect()

        import sys
        entry = sys.argv[0].replace(chr(92), "/").rsplit("/", 1)[-1].removesuffix(".exe").removesuffix(".py").replace("-", "_")
        calibration_only = not calibrate and entry == "lerobot_calibrate"
        if not calibration_only:
            self._skip_safe_zero_on_disconnect = False
            self.configure()
        else:
            # Only the calibration CLI is torque-off; replay can use calibrate=False.
            self.bus.disable_all()
            self._skip_safe_zero_on_disconnect = True

        # Skip during the calibrate flow (calibrate=False); the gripper
        # may not be homed yet and the probe would block connect().
        if calibrate:
            self.detect_gripper_zero()

        self._build_gravity_pairing()

        self._connection_ready = not calibration_only
        logger.info(f"{self} connected.")

    @property
    def is_calibrated(self) -> bool:
        """Check if robot is calibrated."""
        return bool(self.calibration)

    def calibrate(self) -> None:
        """Calibration procedure for B601."""
        # Mark so that the disconnect() following this (as in the
        # lerobot-calibrate entrypoint) skips safe_zero(). connect() clears
        # this flag after its internal calibrate() call.
        self._skip_safe_zero_on_disconnect = True
        if self.calibration:
            user_input = input(
                f"Press ENTER to use provided calibration file associated with the id {self.id}, or type 'c' and press ENTER to run calibration: "
            )
            if user_input.strip().lower() != "c":
                logger.info(f"Using calibration file associated with the id {self.id}")
                return

        logger.info(f"\nRunning calibration for {self}")
        
        self.bus.disable_all()

        print(
            "\nCalibration: Set Zero Position\n"
            "Please MANUALLY move the robot to its ZERO POSITION, and close its gripper.\n"
            "Reference the B601 manual for Zero Pose (generally the default sit-down position).\n"
        )
        input("Press ENTER when ready...")

        for motor in self.motors.values():
            motor.set_zero_position()
            time.sleep(LONG_TIMEOUT_SEC)
        # Damiao 的 set_zero_position 只把零点写进 RAM：不写 flash 的话，重新上电或
        # 通信超时后零点会回到旧值，夹爪零点检测就会一直报同一个偏移。
        if self.motor_type != "rs":
            for motor in self.motors.values():
                try:
                    motor.store_parameters()
                    time.sleep(MEDIUM_TIMEOUT_SEC)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("store_parameters failed: %s", exc)
        # 读回零点：正常应全部接近 0°，用于确认零点真的写进去了
        for motor in self.motors.values():
            motor.request_feedback()
        try:
            self.bus.poll_feedback_once()
        except Exception as exc:  # noqa: BLE001
            logger.warning("zero read-back poll failed: %s", exc)
        for motor_name, motor in self.motors.items():
            state = motor.get_state()
            if state is None:
                print(f"  zero read-back: {motor_name} = <no feedback>")
            else:
                print(f"  zero read-back: {motor_name} = {math.degrees(state.pos):.2f}°")

        logger.info("Arm zero position set.")

        logger.info("Setting range: -90° to +90° by default for all joints")
        self.calibration = {}
        for motor_name, (send_id, recv_id) in self.config.motor_can_ids.items():
            self.calibration[motor_name] = MotorCalibration(
                id=send_id,
                drive_mode=0,
                homing_offset=0,
                range_min=-90,
                range_max=90,
            )

        self._save_calibration()
        print(f"Calibration saved to {self.calibration_fpath}")

    def configure(self) -> None:
        """Configure motors with appropriate settings."""
        # Keep torque off while switching modes, then enable after all motors are configured.
        self.bus.disable_all()
        from .mode_safety import ensure_mode_checked
        for motor_name, motor in self.motors.items():
            target_mode = MotorBridgeMode.MIT if self.motor_type == "rs" else (
                MotorBridgeMode.FORCE_POS if motor_name == FOLLOWER_GRIPPER_MOTOR else MotorBridgeMode.POS_VEL
            )
            try:
                ensure_mode_checked(motor, target_mode)
            except Exception as exc:
                raise RuntimeError(f"{motor_name} mode {int(target_mode)} verification failed: {exc}") from exc
            logger.info(f"{motor_name} ensure mode {target_mode}")
        if self.motor_type != "rs":
            # Even a recovered ACK must not bypass measured-position seeding.
            for motor in self.motors.values():
                motor.request_feedback()
            time.sleep(0.03)
            self.bus.poll_feedback_once()
            states = {name: motor.get_state() for name, motor in self.motors.items()}
            if any(state is None or not math.isfinite(state.pos) for state in states.values()):
                raise RuntimeError("Measured positions missing before enable; startup aborted")
            for name, motor in self.motors.items():
                if name == FOLLOWER_GRIPPER_MOTOR:
                    motor.send_force_pos(states[name].pos, 0.35, self.config.force_pos_torque_ration)
                else:
                    motor.send_pos_vel(states[name].pos, 0.35)
        self.bus.enable_all()

    def disable_torque(self) -> None:
        """Disable follower motor torque so the arm can be moved by hand during read-only debugging."""
        if not self.is_connected:
            raise DeviceNotConnectedError(f"{self} is not connected.")

        self.bus.disable_all()
        logger.info(f"{self} torque disabled.")

    def detect_gripper_zero(
        self,
        tolerance_deg: float = 12.0,
        force_ratio: float = 0.012,
        mit_tau: float = 0.3,
    ) -> dict:
        """校验夹爪读数是否落在关节限位内。

        这台 B601 的夹爪零点就是完全闭合位 0°，张开方向为负（约 -220°）。
        原实现用 +10° 推挤后要求停在 0° 附近，仅在"连接时夹爪恰好闭合"才通过；
        如果上一次结束停在张开位，再连接就会被误判（实测 start=-216° 直接中止）。
        因此改为只读取当前位置并检查是否在 joint_limits 内：既不妨碍正常姿态，
        又能拦住真正超出机械行程的错位。
        """
        if not self.is_connected:
            raise DeviceNotConnectedError(f"{self} is not connected.")
        motor = self.motors.get(FOLLOWER_GRIPPER_MOTOR)
        if motor is None:
            return {"error": "gripper motor not found"}
        lo, hi = self.config.joint_limits[FOLLOWER_GRIPPER_MOTOR]

        motor.request_feedback()
        try:
            self.bus.poll_feedback_once()
        except Exception:
            pass
        state = motor.get_state()
        if state is None:
            raise RuntimeError("gripper zero-detect: no feedback from gripper motor")
        pos = math.degrees(state.pos)
        margin = max(10.0, float(tolerance_deg))
        print(f"  gripper zero-detect: pos = {pos:.2f}°, limits = [{lo:.1f}, {hi:.1f}]")
        if lo - margin <= pos <= hi + margin:
            logger.info("gripper zero-detect: %.2f° within [%.1f, %.1f], passed", pos, lo, hi)
            return {"gripper_deg": pos, "range": (lo, hi)}
        raise RuntimeError(
            "gripper zero-detect: "
            f"{pos:.2f}° outside joint limits [{lo:.1f}, {hi:.1f}]; "
            "please check gripper calibration or mechanics"
        )

    def _read_motor_temperatures(self) -> dict[str, float]:
        """Read per-motor MOS temperatures once and return available values."""
        for motor in self.motors.values():
            motor.request_feedback()
        try:
            self.bus.poll_feedback_once()
        except Exception:
            logger.warning("Temperature check poll feedback failed.")

        temps: dict[str, float] = {}
        for motor_name, motor in self.motors.items():
            state = motor.get_state()
            if state is not None:
                temps[motor_name] = state.t_mos

        return temps

    def _check_motor_temperatures(
        self,
        alarm_threshold_c: float,
        overheat_threshold_c: float,
        context: str = "",
    ) -> dict[str, float]:
        """Read motor MOS temperatures once, print a HIGH TEMP warning for any
        motor above ``alarm_threshold_c``, and raise ``KeyboardInterrupt`` if any
        motor exceeds ``overheat_threshold_c`` (aborts the control loop).

        Returns the dict of ``{motor_name: t_mos_c}`` that was read.
        """
        temperatures = self._read_motor_temperatures()
        logger.debug(
            "motor temps (°C): %s",
            " | ".join(
                f"{n}={temperatures[n]:.1f}" if n in temperatures else f"{n}=--"
                for n in self.motors
            ),
        )
        label = f" in {context}" if context else ""
        for motor_name, temp_c in temperatures.items():
            if temp_c > alarm_threshold_c:
                print(
                    f"[HIGH TEMP] {motor_name} t_mos={temp_c:.2f}C > {alarm_threshold_c:.2f}C"
                )
            if temp_c > overheat_threshold_c:
                logger.error(
                    "Overheat detected%s: %s t_mos=%.2fC > %.2fC.",
                    label,
                    motor_name,
                    temp_c,
                    overheat_threshold_c,
                )
                raise KeyboardInterrupt("Overheat detected")
        return temperatures

    def mit_output_torque_limit(
        self,
        motor: Any,
        pos_target_rad: float,
    ) -> float | None:
        """Compute MIT torque command from target position and motor state."""
        return 0.0

    def _build_gravity_pairing(self) -> None:
        """Build motor_name -> URDF q_idx pairing once at connect().

        Loads and warms the gravity model, then pairs follower arm motors
        (config order, excluding gripper) with URDF revolute joints (DFS
        order). On any failure sets self._gravity_pairing = None so
        _gravity_feedforward no-ops.
        """
        if not (self.config.gravity_compensation and self.motor_type == "rs"):
            self._gravity_pairing = None
            return
        urdf = self.config.gravity_urdf_path
        if not urdf:
            logger.warning("gravity_urdf_path not set, feedforward disabled")
            self._gravity_pairing = None
            return
        try:
            from .gravity import load_dynamics_model
        except Exception as exc:  # pragma: no cover
            logger.warning("gravity import failed, feedforward disabled: %s", exc)
            self._gravity_pairing = None
            return
        model = load_dynamics_model(urdf_path=urdf)
        arm_motors = [n for n in self.motor_names if n != FOLLOWER_GRIPPER_MOTOR]
        arm_qidx = [j.q_idx for j in model.joints if j.jtype in ("revolute", "continuous")]
        if len(arm_motors) != len(arm_qidx):
            logger.warning(
                "gravity feedforward disabled: arm motor count %d != URDF revolute %d",
                len(arm_motors), len(arm_qidx),
            )
            self._gravity_pairing = None
            return
        self._gravity_pairing = dict(zip(arm_motors, arm_qidx))

    def _gravity_feedforward(self) -> dict[str, float]:
        """Per-arm-motor gravity feedforward torque (N·m); {} if disabled."""
        pairing = self._gravity_pairing
        if not pairing:
            return {}
        import numpy as np
        from .gravity import load_dynamics_model, compute_generalized_gravity
        model = load_dynamics_model(urdf_path=self.config.gravity_urdf_path)
        q = np.zeros(model.nq)
        for motor_name, qi in pairing.items():
            motor = self.motors.get(motor_name)
            if motor is None:
                continue
            state = motor.get_state()
            if state is not None:
                q[qi] = float(state.pos)
        tau = compute_generalized_gravity(model=model, q=q)
        return {name: float(tau[qi]) for name, qi in pairing.items()}

    def safe_zero(self, step_interval_s: float = 0.02, exit_on_complete: bool = True) -> None:
        """Slow measured-pose return; never use send_action's runtime speed."""
        from .homing_profile import trajectory, HOME_SPEED_RAD_S
        sync=getattr(self,'_coordinated_shutdown',None)
        publisher=None
        try:
            from telemetry_client import FeedbackPublisher
            publisher=FeedbackPublisher()
        except ImportError:pass
        if not self.is_connected:raise DeviceNotConnectedError(f"{self} is not connected.")
        if self._in_safe_zero:return
        if not math.isfinite(step_interval_s) or step_interval_s<0:raise ValueError('Invalid homing interval')
        self._in_safe_zero=True;self._safe_zero_incomplete=True
        ids={send:name for name,(send,_) in self.config.motor_can_ids.items()}
        try:
            if set(ids)!=set(range(1,8)):raise RuntimeError('safe_zero requires all 7 motor mappings')
            def read():
                if sync:sync.check()
                for name in ids.values():self.motors[name].request_feedback()
                time.sleep(.015);self.bus.poll_feedback_once()
                states={i:self.motors[name].get_state() for i,name in ids.items()}
                for i,st in states.items():
                    if st is None or not all(math.isfinite(float(getattr(st,k))) for k in ('pos','vel','torq','t_mos','t_rotor')):
                        raise RuntimeError(f'safe_zero: motor {i} missing or invalid feedback')
                    if st.status_code not in (0,1):raise RuntimeError(f'safe_zero: motor {i} fault {st.status_code}')
                    if max(st.t_mos,st.t_rotor)>80:raise RuntimeError(f'safe_zero: motor {i} over 80C')
                if publisher:
                    try:publisher.send(getattr(self.config,'port',''),[states[i].pos for i in range(1,7)],[states[i].torq for i in range(1,7)],gripper=states[7].pos)
                    except Exception:pass
                return states
            def send(q):
                if sync:sync.check()
                if self.motor_type=='rs':
                    action={}
                    for i,name in ids.items():
                        direction=self.config.joint_directions.get(name)
                        if not direction:raise RuntimeError('safe_zero: missing direction for '+name)
                        action[name+'.pos']=math.degrees(q[i])/direction
                    self.send_action(action);return
                for i,name in ids.items():
                    motor=self.motors[name]
                    if i==7:motor.send_force_pos(q[i],HOME_SPEED_RAD_S,min(.05,self.config.force_pos_torque_ration))
                    else:motor.send_pos_vel(q[i],HOME_SPEED_RAD_S)
            states=read()
            if any(abs(st.pos)>4*math.pi for st in states.values()):raise RuntimeError('safe_zero: multi-turn offset outside homing range')
            if sync:sync.checkpoint()
            # Preserve the established CAN-ID stage order and park target.
            goal={i:0. for i in ids};goal[7]=math.radians(float(getattr(self.config,'gripper_park_deg',0.) or 0.))
            for stage,active in enumerate(((1,4,5,6),(2,3),(7,)),1):
                states=read();target={i:st.pos for i,st in states.items()};previous=dict(target)
                logger.info('safe_zero stage%s: measured pose, max %.1f deg/s, smooth acceleration',stage,math.degrees(HOME_SPEED_RAD_S))
                def hold():
                    read();send(target)
                duration=sync.movement_duration([target[i] for i in active],[goal[i] for i in active],hold=hold) if sync else 2.
                for interval,point in trajectory([target[i] for i in active],[goal[i] for i in active],hz=1/max(.02,min(.1,step_interval_s or .02)),min_duration=duration):
                    states=read()
                    if any(abs(states[i].pos-previous[i])>.10 for i in active):
                        send({i:st.pos for i,st in states.items()});raise RuntimeError('safe_zero following error; holding measured pose')
                    for i,v in zip(active,point):target[i]=v
                    send(target);previous=dict(target);time.sleep(interval)
                deadline=time.monotonic()+8;settled=0
                while time.monotonic()<deadline:
                    states=read();send(target)
                    settled=settled+1 if all(abs(states[i].pos-goal[i])<.035 and abs(states[i].vel)<.05 for i in active) else 0
                    if settled>=8:break
                    time.sleep(.02)
                else:raise RuntimeError('safe_zero not settled; keep torque and do not disable')
                if sync:
                    sync.checkpoint(hold)
            states=read()
            if any(abs(st.pos-goal[i])>=.035 or abs(st.vel)>=.05 for i,st in states.items()):raise RuntimeError('safe_zero final feedback not settled')
            if sync:sync.checkpoint(lambda:(read(),send(goal)))
            self._safe_zero_incomplete=False
            logger.info('safe_zero: measured zero settled; holding 3 seconds before disconnect')
            time.sleep(3.)
            if exit_on_complete:raise KeyboardInterrupt('safe_zero completed')
        except Exception:
            # Never drop an arm mid-return after missing feedback/obstruction.
            self._safe_zero_incomplete=True
            if sync:sync.abort()
            raise
        finally:self._in_safe_zero=False

    def get_observation(self) -> RobotObservation:
        """Get current observation from robot."""
        start = time.perf_counter()

        if not self.is_connected:
            raise DeviceNotConnectedError(f"{self} is not connected.")

        obs_dict: dict[str, Any] = {}

        # Request and poll feedback from motorbridge
        for motor in self.motors.values():
            motor.request_feedback()
        # A missing sample is not a real zero; do not record or control from fabricated data.
        self.bus.poll_feedback_once()
        for motor_name, motor in self.motors.items():
            state = motor.get_state()
            if state is None or not all(math.isfinite(float(v)) for v in
                                        (state.pos, state.vel, state.torq)):
                raise RuntimeError(f"{self.config.port} {motor_name}: 从臂反馈缺失或无效，未使用默认零值")
            # motorbridge works natively in radians. Convert to degrees.
            obs_dict[f"{motor_name}.pos"] = math.degrees(state.pos)
            obs_dict[f"{motor_name}.vel"] = math.degrees(state.vel)
            obs_dict[f"{motor_name}.torque"] = state.torq

        # Capture images
        for cam_key, cam in self.cameras.items():
            obs_dict[cam_key] = cam.async_read()

        dt_ms = (time.perf_counter() - start) * 1e3
        logger.debug(f"{self} get_observation took: {dt_ms:.1f}ms")
        # logger.debug(f"Observation: {obs_dict}")

        return obs_dict

    def send_action(
        self,
        action: RobotAction
    ) -> RobotAction:
        """Send action command to robot."""
        if not self.is_connected:
            raise DeviceNotConnectedError(f"{self} is not connected.")

        if not self._in_safe_zero:
            self._check_motor_temperatures(
                self.config.temp_alarm_threshold_c,
                self.config.temp_overheat_threshold_c,
                context="send_action",
            )

        goal_pos = {key.removesuffix(".pos"): val for key, val in action.items() if key.endswith(".pos")}

        # Apply per-joint direction/scale mapping before clipping.
        for motor_name, position in goal_pos.items():
            direction = self.config.joint_directions.get(motor_name, 0.0)
            position = position * direction
            # print(f"motor_name: {motor_name}, position: {position}")
            if motor_name in self.config.joint_limits:
                min_limit, max_limit = self.config.joint_limits[motor_name]
                clipped_position = max(min_limit, min(max_limit, position))
                if clipped_position != position:
                    logger.debug(f"Clipped {motor_name} from {position:.2f} to {clipped_position:.2f}")
                position = clipped_position

            goal_pos[motor_name] = position

        # To tolerate 6-DOF leader arms that don't have a wrist_yaw joint, we can allow the follower to ignore missing wrist_yaw commands by treating them as 0.
        if 'wrist_yaw' not in goal_pos:
            goal_pos['wrist_yaw'] = 0.0

        # Safety: Cap relative target
        if self.config.max_relative_target is not None:
            # We need current position in degrees to compare against relative limit safely
            present_pos = {}
            for motor_name, motor in self.motors.items():
                state = motor.get_state()
                if state is None or not math.isfinite(float(state.pos)):
                    raise RuntimeError(f"{self.config.port} {motor_name}: 无实测角度，无法限制相对位移")
                present_pos[motor_name] = math.degrees(state.pos)
            
            goal_present_pos = {key: (g_pos, present_pos.get(key, g_pos)) for key, g_pos in goal_pos.items()}
            goal_pos = ensure_safe_goal_position(goal_present_pos, self.config.max_relative_target)

        # Prepare and send commands
        gravity_tau = (
            self._gravity_feedforward()
            if (self.config.gravity_compensation and self.motor_type == "rs")
            else {}
        )

        for motor_name, position_degrees in goal_pos.items():
            try:
                idx = self.motor_names.index(motor_name)
            except ValueError:
                idx = 0 # Fallback

            # 排查回放时夹爪不动：只在目标变化 >=2° 时打印，避免刷屏
            if motor_name == FOLLOWER_GRIPPER_MOTOR:
                # 记录本次会话命令过的最闭合位置（数值最小），断开时把夹爪停到这里
                self._gripper_closed_cmd = min(
                    getattr(self, "_gripper_closed_cmd", position_degrees), position_degrees)
                last_cmd = getattr(self, "_dbg_gripper_cmd", None)
                if (not self._in_safe_zero and
                        (last_cmd is None or abs(position_degrees - last_cmd) >= 2.0)):
                    print(f"  gripper cmd: {position_degrees:.1f}°")
                    self._dbg_gripper_cmd = position_degrees

            # Convert target position from degrees to radians for motorbridge
            pos_rad = math.radians(position_degrees)
            vel_deg_s = (
                self.config.pos_vel_velocity[idx]
                if isinstance(self.config.pos_vel_velocity, list)
                else self.config.pos_vel_velocity
            )
            vel_rad = math.radians(vel_deg_s)

            motor = self.motors.get(motor_name)
            if motor is not None:
                if motor_name == FOLLOWER_GRIPPER_MOTOR:
                    if self.motor_type == "rs":
                        tau_ff = self.mit_output_torque_limit(motor, pos_rad)
                        if tau_ff is None:
                            tau_ff = 0.0
                        motor.send_mit(0, 0, 0, 1.5, tau_ff)
                        logger.debug(
                            f"Sent MIT command to {motor_name}: pos={position_degrees:.2f}°, "
                            f"tau_ff={tau_ff:.2f}"
                        )
                    else:
                        motor.send_force_pos(pos_rad, vel_rad, self.config.force_pos_torque_ration)
                        logger.debug(f"Sent FORCE_POS command to {motor_name}: pos={position_degrees:.2f}°, vel={vel_deg_s:.2f}°/s, ratio={0.1}")
                else:
                    if self.motor_type == "rs":
                        if self.config.gravity_compensation:
                            kp = getattr(self.config, "gravity_mit_kp", {}).get(motor_name, 0.0)
                            kd = getattr(self.config, "gravity_mit_kd", {}).get(motor_name, 0.0)
                        else:
                            kp = getattr(self.config, "mit_kp", {}).get(motor_name, 0.0)
                            kd = getattr(self.config, "mit_kd", {}).get(motor_name, 0.0)
                        tau = gravity_tau.get(motor_name, 0.0)
                        motor.send_mit(pos_rad, 0, kp, kd, tau)
                        logger.debug(
                            f"Sent MIT command to {motor_name}: "
                            f"pos={position_degrees:.2f}°, kp={kp}, kd={kd}, tau_ff={tau:.3f}"
                        )
                    else:
                        motor.send_pos_vel(pos_rad, 32)
                        logger.debug(f"Sent POS_VEL command to {motor_name}: target={pos_rad:.2f},pos={position_degrees:.2f}°, vel={vel_deg_s:.2f}°/s")

        # motorbridge sends packets mostly synchronously here over loop, 
        # so we don't need a bulk send command through ctypes.

        return {f"{motor}.pos": val for motor, val in goal_pos.items()}

    def disconnect(self):
        """Disconnect from robot."""
        if self.bus is None:
            return
        if not getattr(self, "_connection_ready", False):
            self._skip_safe_zero_on_disconnect = True

        if (
            not self._in_safe_zero
            and not self._emergency_disable_requested
            and not self._skip_safe_zero_on_disconnect
        ):
            try:
                self.safe_zero(exit_on_complete=False)
            except Exception:
                logger.exception("safe_zero during disconnect failed.")
        elif self._skip_safe_zero_on_disconnect:
            logger.info(
                "safe_zero skipped on disconnect: calibrate context "
                "(_skip_safe_zero_on_disconnect=True)."
            )

        hold_gripper = bool(getattr(self.config, "hold_gripper_on_disconnect", False))
        incomplete = bool(getattr(self, "_safe_zero_incomplete", False))
        sync=getattr(self,'_coordinated_shutdown',None)
        if sync and sync.cancelled.is_set():incomplete=True
        if incomplete:
            logger.warning("disconnect: safe_zero incomplete, keeping torque (skip disable)")
        for name, motor in self.motors.items():
            try:
                if self.config.disable_torque_on_disconnect and not incomplete:
                    if name == FOLLOWER_GRIPPER_MOTOR and hold_gripper and self._connection_ready:
                        motor.send_force_pos(math.radians(float(getattr(self.config, "gripper_park_deg", 0.0))), math.radians(20.0), 0.05)
                    else:
                        motor.disable()
            except Exception:
                logger.exception("Motor %s cleanup failed", name)
            finally:
                try: motor.close()
                except Exception: logger.exception("Motor %s transport close failed", name)
        self._safe_zero_incomplete = False
        try:
            self.bus.close()
        finally:
            self.bus = None
            self._connection_ready = False
        for cam in self.cameras.values():
            if cam.is_connected:
                try: cam.disconnect()
                except Exception: logger.exception("Camera disconnect failed")

        self._emergency_disable_requested = False
        logger.info(f"{self} disconnected.")
