"""双臂 reBot 102 主臂复合遥操作器：左右两个主臂组合成一个 teleoperator。"""
from __future__ import annotations

from functools import cached_property
import logging

from lerobot.processor import RobotAction
from lerobot.teleoperators.teleoperator import Teleoperator

from .config_bi_rebot_arm_102_leader import BiRebotArm102LeaderConfig
from .config_rebot_arm_102_leader import RebotArm102LeaderConfig
from .rebot_arm_102_leader import RebotArm102Leader

logger = logging.getLogger(__name__)


class BiRebotArm102Leader(Teleoperator):
    config_class = BiRebotArm102LeaderConfig
    name = "bi_rebot_arm_102_leader"

    def __init__(self, config: BiRebotArm102LeaderConfig):
        super().__init__(config)
        self.config = config
        self.left_arm = RebotArm102Leader(RebotArm102LeaderConfig(
            port=config.left_arm.port,
            id=config.left_arm.id,
        ))
        self.right_arm = RebotArm102Leader(RebotArm102LeaderConfig(
            port=config.right_arm.port,
            id=config.right_arm.id,
        ))

    @cached_property
    def action_features(self) -> dict[str, type]:
        return {
            **{f"left_{k}": v for k, v in self.left_arm.action_features.items()},
            **{f"right_{k}": v for k, v in self.right_arm.action_features.items()},
        }

    @property
    def feedback_features(self) -> dict[str, type]:
        return {}

    @property
    def is_connected(self) -> bool:
        return self.left_arm.is_connected and self.right_arm.is_connected

    def connect(self, calibrate: bool = True) -> None:
        try:
            self.left_arm.connect(calibrate)
            self.right_arm.connect(calibrate)
        except BaseException:
            # The composite is_connected is false after a partial connection;
            # callers cannot use it to discover and release the surviving arm.
            for arm in (self.left_arm, self.right_arm):
                if arm.is_connected:
                    try:
                        arm.disconnect()
                    except Exception:
                        logger.exception('Failed to release a partially connected leader')
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

    def send_feedback(self, feedback) -> None:
        raise NotImplementedError

    def get_action(self) -> RobotAction:
        return {
            **{f"left_{k}": v for k, v in self.left_arm.get_action().items()},
            **{f"right_{k}": v for k, v in self.right_arm.get_action().items()},
        }

    def disconnect(self) -> None:
        errors = []
        for arm in (self.left_arm, self.right_arm):
            if arm.is_connected:
                try:
                    arm.disconnect()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise errors[0]
