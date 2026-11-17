from dataclasses import dataclass, field

from lerobot.cameras import CameraConfig
from lerobot.robots.robot import RobotConfig


@dataclass
class BiSeeedArmConfig:
    """单侧子臂的连接参数（与平台双臂页面的 left_arm/right_arm 字段对应）。"""

    type: str = "seeed_b601_dm_follower"
    port: str = ""
    id: str | None = None
    can_adapter: str = "damiao"
    force_pos_torque_ration: float = 0.12

    def __post_init__(self):
        if not 0 < self.force_pos_torque_ration <= 1:
            raise ValueError("夹爪力矩上限必须大于 0 且不超过 1")


@RobotConfig.register_subclass("bi_seeed_b601_dm_follower")
@dataclass
class BiSeeedB601DMFollowerConfig(RobotConfig):
    """双臂 Seeed B601-DM 复合机器人配置：左右两个子臂共用一个进程。"""

    left_arm: BiSeeedArmConfig = field(default_factory=BiSeeedArmConfig)
    right_arm: BiSeeedArmConfig = field(default_factory=BiSeeedArmConfig)
    # 共享相机（俯视/侧视），统一挂到左臂上，避免两个子臂重复打开同一个相机
    cameras: dict[str, CameraConfig] = field(default_factory=dict)
