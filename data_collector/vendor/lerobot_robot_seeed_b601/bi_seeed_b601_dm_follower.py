"""双臂 Seeed B601-DM 复合机器人：把左右两台单臂组合成一个 Robot。

LeRobot 的 dataset/record/policy 都按“一个 robot”处理，复合后在同一个进程里
同步读取两臂的 observation、下发两臂的 action，天然保证左右帧对齐。
特征名沿用 LeRobot bi_* 约定：左臂 `left_*`、右臂 `right_*`。
"""
from __future__ import annotations

import logging
from functools import cached_property

from lerobot.processor import RobotAction, RobotObservation
from lerobot.robots.robot import Robot

from .config_bi_seeed_b601_dm_follower import BiSeeedB601DMFollowerConfig
from .config_seeed_b601_dm_follower import SeeedB601DMFollowerConfig
from .seeed_b601_dm_follower import SeeedB601DMFollower

logger = logging.getLogger(__name__)


class BiSeeedB601DMFollower(Robot):
    config_class = BiSeeedB601DMFollowerConfig
    name = "bi_seeed_b601_dm_follower"

    def __init__(self, config: BiSeeedB601DMFollowerConfig):
        super().__init__(config)
        self.config = config

        def _make(side: str, arm_cfg, cameras):
            arm_id = arm_cfg.id or (f"{config.id}_{side}" if getattr(config, "id", None) else None)
            return SeeedB601DMFollower(SeeedB601DMFollowerConfig(
                port=arm_cfg.port,
                id=arm_id,
                can_adapter=arm_cfg.can_adapter or "damiao",
                force_pos_torque_ration=arm_cfg.force_pos_torque_ration,
                cameras=cameras,
            ))

        self.left_arm = _make("left", config.left_arm, config.cameras)
        self.right_arm = _make("right", config.right_arm, {})

        self.cameras = {**self.left_arm.cameras, **self.right_arm.cameras}

    @property
    def _motors_ft(self) -> dict[str, type]:
        return {
            **{f"left_{k}": v for k, v in self.left_arm._motors_ft.items()},
            **{f"right_{k}": v for k, v in self.right_arm._motors_ft.items()},
        }

    @property
    def _cameras_ft(self) -> dict[str, tuple]:
        return {
            **{f"left_{k}": v for k, v in self.left_arm._cameras_ft.items()},
            **{f"right_{k}": v for k, v in self.right_arm._cameras_ft.items()},
        }

    @cached_property
    def observation_features(self) -> dict[str, type | tuple]:
        return {**self._motors_ft, **self._cameras_ft}

    @cached_property
    def action_features(self) -> dict[str, type]:
        return self._motors_ft

    @property
    def is_connected(self) -> bool:
        return self.left_arm.is_connected and self.right_arm.is_connected

    def connect(self, calibrate: bool = True) -> None:
        try:
            self.left_arm.connect(calibrate)
            self.right_arm.connect(calibrate)
        except Exception:
            # A failed startup must not trigger motion on either follower.
            self.left_arm._skip_safe_zero_on_disconnect = True
            self.right_arm._skip_safe_zero_on_disconnect = True
            try:
                self.disconnect()
            except Exception:
                logger.exception("Bimanual arm cleanup failed after connection failure")
            raise

    @property
    def is_calibrated(self) -> bool:
        return self.left_arm.is_calibrated and self.right_arm.is_calibrated

    def calibrate(self) -> None:
        self.left_arm.calibrate()
        self.right_arm.calibrate()

    def configure(self) -> None:
        self.left_arm.configure()
        self.right_arm.configure()

    def setup_motors(self) -> None:
        self.left_arm.setup_motors()
        self.right_arm.setup_motors()

    def get_observation(self) -> RobotObservation:
        obs = {}
        obs.update({f"left_{k}": v for k, v in self.left_arm.get_observation().items()})
        obs.update({f"right_{k}": v for k, v in self.right_arm.get_observation().items()})
        return obs

    def send_action(self, action: RobotAction) -> RobotAction:
        left = {k.removeprefix("left_"): v for k, v in action.items() if k.startswith("left_")}
        right = {k.removeprefix("right_"): v for k, v in action.items() if k.startswith("right_")}
        sent_left = self.left_arm.send_action(left)
        sent_right = self.right_arm.send_action(right)
        return {
            **{f"left_{k}": v for k, v in sent_left.items()},
            **{f"right_{k}": v for k, v in sent_right.items()},
        }

    def disconnect(self):
        """Coordinated parallel shutdown on the two independent CAN buses."""
        from .coordinated_shutdown import parallel_shutdown
        arms = [arm for arm in (self.left_arm, self.right_arm)
                if getattr(arm, 'bus', None) is not None or arm.is_connected]
        homing = [arm for arm in arms if getattr(arm, '_connection_ready', False)
                  and not getattr(arm, '_skip_safe_zero_on_disconnect', False)
                  and not getattr(arm, '_emergency_disable_requested', False)
                  and not getattr(arm, '_in_safe_zero', False)]

        def finish(arm, group):
            arm._coordinated_shutdown = group
            try:
                arm.disconnect()
            finally:
                del arm._coordinated_shutdown

        logger.info('Bimanual shutdown: both followers return concurrently; coordinated stage/disable checks')
        parallel_shutdown(arms, finish, checkpoint_count=max(1, len(homing)))
