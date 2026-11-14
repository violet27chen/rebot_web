#!/usr/bin/env python3
"""lerobot-record 的安全收尾包装。

默认的 disconnect() 会执行 safe_zero（把大臂/小臂放回 0°），在桌面上
有电子设备时容易砸到东西。这里改为：结束时**保持当前姿态、保持力矩**，
不再回零下压；等用户手动回零或电机超时。
"""
from __future__ import annotations

import collections
import os
import sys
import time

os.environ.setdefault("REBOT_KEEP_POSE", "1")

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

from lerobot.scripts import lerobot_record  # noqa: E402
from lerobot.scripts.lerobot_record import main  # noqa: E402


def _install_continuous_sort_hook() -> None:
    """当开启 REBOT_CONTINUOUS_SORT=1 时，实时监测机械臂关节与画面变化：
    - 有快递在分拣时持续运行，不受 30 秒单集时长限制；
    - 当桌面所有快递分拣完毕、机械臂回到待机姿态连续静止满 REBOT_IDLE_STOP_S 秒后，
      自动触发 exit_early / stop_recording 正常结束任务。
    """
    if os.environ.get("REBOT_CONTINUOUS_SORT") != "1":
        return

    try:
        idle_stop_s = max(0.0, float(os.environ.get("REBOT_IDLE_STOP_S", "8")))
    except ValueError:
        idle_stop_s = 8.0
    try:
        idle_deg = max(0.3, float(os.environ.get("REBOT_IDLE_DEG", "1.8")))
    except ValueError:
        idle_deg = 1.8

    orig_record_loop = lerobot_record.record_loop

    def _wrapped_record_loop(*args, **kwargs):
        robot = kwargs.get("robot") if "robot" in kwargs else (args[0] if len(args) > 0 else None)
        events = kwargs.get("events") if "events" in kwargs else (args[1] if len(args) > 1 else None)
        if robot is None or not isinstance(events, dict):
            return orig_record_loop(*args, **kwargs)

        orig_get_obs = robot.get_observation
        history: collections.deque = collections.deque()
        start_t = time.monotonic()
        last_active_t = start_t
        last_log_t = start_t
        has_moved = False

        print(
            f"[持续分拣] 已开启连续分拣模式（不按 30s 截断；"
            + (
                f"桌面清空/机械臂连续静止 {idle_stop_s:.0f}s 自动停机，关节阈值 {idle_deg:.1f}°）"
                if idle_stop_s > 0
                else "持续运行直到手动点击急停或达到安全时长上限）"
            ),
            flush=True,
        )

        def _monitored_get_obs(*o_args, **o_kwargs):
            nonlocal last_active_t, last_log_t, has_moved
            obs = orig_get_obs(*o_args, **o_kwargs)
            now = time.monotonic()
            if isinstance(obs, dict):
                q_vals = [
                    float(v)
                    for k, v in obs.items()
                    if str(k).endswith(".pos") and isinstance(v, (int, float))
                ]
                if q_vals:
                    history.append((now, q_vals))
                    while history and (now - history[0][0]) > 2.0:
                        history.popleft()
                    if len(history) >= 5 and (now - history[0][0]) >= 1.2:
                        num_j = len(q_vals)
                        max_span = 0.0
                        for j in range(num_j):
                            col = [item[1][j] for item in history if len(item[1]) > j]
                            if col:
                                span = max(col) - min(col)
                                if span > max_span:
                                    max_span = span
                        if max_span >= idle_deg:
                            has_moved = True
                            last_active_t = now
                        elapsed = now - start_t
                        idle_for = now - last_active_t
                        if now - last_log_t >= 10.0:
                            last_log_t = now
                            print(
                                f"[持续分拣] 运行中（已持续 {int(elapsed)}s · 近2s关节活动度 {max_span:.2f}° · 静止计时 {idle_for:.1f}s）",
                                flush=True,
                            )
                        # 启动至少 8 秒后再判定静止停机（若机械臂已动作过并回到待机位，或开局即空桌面静止满 idle_stop_s）
                        warmup_ok = (has_moved and elapsed >= 6.0) or (elapsed >= max(10.0, idle_stop_s + 2.0))
                        if idle_stop_s > 0 and warmup_ok and idle_for >= idle_stop_s:
                            print(
                                f"[持续分拣] 检测到机械臂已连续静止 {idle_for:.1f}s（关节波动 {max_span:.2f}° < {idle_deg:.1f}°），所有快递已分拣完毕，自动结束！",
                                flush=True,
                            )
                            events["exit_early"] = True
                            events["stop_recording"] = True
            return obs

        robot.get_observation = _monitored_get_obs
        try:
            return orig_record_loop(*args, **kwargs)
        finally:
            robot.get_observation = orig_get_obs

    lerobot_record.record_loop = _wrapped_record_loop


_install_continuous_sort_hook()


if __name__ == "__main__":
    sys.argv = ["lerobot-record"] + sys.argv[1:]
    sys.exit(main())
