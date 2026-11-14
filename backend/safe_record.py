#!/usr/bin/env python3
"""lerobot-record 的安全收尾包装。

默认的 disconnect() 会执行 safe_zero（把大臂/小臂放回 0°），在桌面上
有电子设备时容易砸到东西。这里改为：结束时**保持当前姿态、保持力矩**，
不再回零下压；等用户手动回零或电机超时。
"""
from __future__ import annotations

import os
import sys

os.environ["REBOT_KEEP_POSE"] = "1"

try:
    from lerobot_robot_seeed_b601.seeed_b601_follower import (  # noqa: E402
        SeeedB601FollowerBase,
    )
except Exception:  # noqa: BLE001
    SeeedB601FollowerBase = None  # type: ignore[assignment]

_orig_disconnect = SeeedB601FollowerBase.disconnect if SeeedB601FollowerBase else None


def _keep_pose_disconnect(self):
    if os.environ.get("REBOT_KEEP_POSE") == "1":
        try:
            for _name, motor in self.motors.items():
                try:
                    motor.set_can_timeout_ms(600000)
                except Exception:
                    pass
            print("[safe_record] 结束：保持当前姿态与力矩，不回零（避免下压桌面设备）",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[safe_record] 保持姿态失败：{exc}", flush=True)
        # 必须像原生 disconnect 一样清理总线/电机/相机，否则进程退出时会 abort(-6)。
        # 注意：不调用 motor.disable()，让电机靠 CAN 超时继续保持在原位。
        for _name, motor in list(self.motors.items()):
            try:
                motor.clear_error()
            except Exception:
                pass
            try:
                motor.close()
            except Exception:
                pass
        try:
            if self.bus is not None:
                self.bus.close()
        except Exception:
            pass
        self.bus = None
        try:
            for cam in list(self.cameras.values()):
                try:
                    cam.disconnect()
                except Exception:
                    pass
        except Exception:
            pass
        print("[safe_record] 总线与相机已关闭，电机保持力矩", flush=True)
        return
    return _orig_disconnect(self)


if SeeedB601FollowerBase is not None:
    SeeedB601FollowerBase.disconnect = _keep_pose_disconnect

from lerobot.scripts.lerobot_record import main  # noqa: E402


if __name__ == "__main__":
    sys.argv = ["lerobot-record"] + sys.argv[1:]
    sys.exit(main())
