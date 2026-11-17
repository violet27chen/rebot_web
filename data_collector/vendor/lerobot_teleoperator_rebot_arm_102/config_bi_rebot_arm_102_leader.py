from dataclasses import dataclass, field

from lerobot.teleoperators.config import TeleoperatorConfig


@dataclass
class BiRebotArmConfig:
    """单侧主臂连接参数（对应平台双臂页 left_arm/right_arm 字段）。"""

    type: str = "rebot_arm_102_leader"
    port: str = ""
    id: str | None = None


@TeleoperatorConfig.register_subclass("bi_rebot_arm_102_leader")
@dataclass
class BiRebotArm102LeaderConfig(TeleoperatorConfig):
    left_arm: BiRebotArmConfig = field(default_factory=BiRebotArmConfig)
    right_arm: BiRebotArmConfig = field(default_factory=BiRebotArmConfig)
