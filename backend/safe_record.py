#!/usr/bin/env python3
"""
安全收尾与连续分拣录制启动器 (Safe Record & Continuous Sorting Wrapper)
========================================================================

【模块背景与核心安全机制】
官方原版 LeRobot 从臂驱动在执行 `disconnect()` 时，默认会调用 `safe_zero` 强制将机械臂
的大臂与小臂硬性旋转回 0° 机械原点。
在工业装配桌面或实际工位上，机械臂正下方通常摆放有相机三脚架、待分拣快递包裹或标定板，
一旦盲目自动回零下压，会以较大力矩直接“砸”在桌面设备上，导致工件损毁或减速器受损。

【核心改造与特性】
1. 保持姿态安全断开 (`_keep_pose_disconnect`):
   在断开连接退出时，将所有电机的硬件 CAN 超时设为 10 分钟（600000ms），
   并正常关闭总线与相机句柄，【坚决不调用 motor.disable()】，使机械臂依靠电机自身保持力矩
   在当前空间姿态平稳悬停，绝不下压回零，彻底杜绝砸坏桌面贵重设备。
2. 连续分拣无截断模式 (`_install_continuous_sort_hook`):
   支持通过环境变量 `REBOT_CONTINUOUS_SORT=1` 开启持续分拣。不再按单次 30 秒剧集强制截断，
   而是实时监听机械臂关节活动度：若检测到连续一段时间（如 8 秒）关节波动均低于阈值（如 1.8°），
   判定本次作业已自动完成，平稳收尾退出。
3. 异步 ACT 策略支持:
   若启用 `REBOT_ASYNC_ACT=1`，自动挂接异步进程化推理引擎。
"""
from __future__ import annotations

import collections
import os
from pathlib import Path
import sys
import time

# 将项目根目录添加至模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 默认启用姿态保持标志
os.environ.setdefault("REBOT_KEEP_POSE", "1")

try:
    from lerobot_robot_seeed_b601.seeed_b601_follower import (  # noqa: E402
        SeeedB601FollowerBase,
    )
except Exception:  # noqa: BLE001
    SeeedB601FollowerBase = None  # type: ignore[assignment]

_orig_disconnect = SeeedB601FollowerBase.disconnect if SeeedB601FollowerBase else None


def _keep_pose_disconnect(self):
    """
    重写从臂断开连接函数：保持力矩与当前姿态，绝不自动回零下压。
    """
    if os.environ.get("REBOT_KEEP_POSE") == "1":
        try:
            # 将每个电机的底层硬件看门狗/通信超时设为 10 分钟
            for _name, motor in self.motors.items():
                try:
                    motor.set_can_timeout_ms(600000)
                except Exception:
                    pass
            print("[safe_record] 结束：保持当前姿态与力矩，不回零（避免下压桌面设备）", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[safe_record] 保持姿态失败：{exc}", flush=True)

        # 清理总线与电机句柄（但不调用 disable），避免 Python 退出时由于析构顺序异常导致 abort(-6)
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

        # 释放相机句柄
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


# 注入断开方法
if SeeedB601FollowerBase is not None:
    SeeedB601FollowerBase.disconnect = _keep_pose_disconnect

from lerobot.scripts import lerobot_record  # noqa: E402
from lerobot.scripts.lerobot_record import main  # noqa: E402
from backend.record_audio import install as disable_speech  # noqa: E402

disable_speech(lerobot_record)
from backend.record_controls import install as install_controls
_close_controls = install_controls(lerobot_record)


def _install_continuous_sort_hook() -> None:
    """
    安装连续分拣动态监测钩子。
    基于机械臂各关节运动波动跨度（max - min）监测静止状态，
    达到设定超时后自动设置 exit_early 正常结束录制。
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
        policy = kwargs.get('policy', args[8] if len(args) > 8 else None)
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
                f"机械臂连续静止 {idle_stop_s:.0f}s 自动结束，关节阈值 {idle_deg:.1f}°；不代表视觉确认桌面清空）"
                if idle_stop_s > 0
                else "持续运行直到手动点击急停或达到安全时长上限）"
            ),
            flush=True,
        )

        def _monitored_get_obs(*o_args, **o_kwargs):
            nonlocal last_active_t, last_log_t, has_moved
            obs = orig_get_obs(*o_args, **o_kwargs)
            now = time.monotonic()
            # 若异步推理正在等待下一块动作块，暂不计入静止时间
            if getattr(policy, 'waiting_for_prediction', False):
                last_active_t = now
                history.clear()
                return obs
            if isinstance(obs, dict):
                q_vals = [
                    float(v)
                    for k, v in obs.items()
                    if str(k).endswith(".pos") and isinstance(v, (int, float))
                ]
                if q_vals:
                    history.append((now, q_vals))
                    # 维护近 2 秒历史窗口
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
                        # 若任何一个关节在 2 秒内的运动跨度超过阈值，刷新活跃时间戳
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
                        # 满足热身条件且连续静止超时，触发退出
                        warmup_ok = (has_moved and elapsed >= 6.0) or (elapsed >= max(10.0, idle_stop_s + 2.0))
                        if idle_stop_s > 0 and warmup_ok and idle_for >= idle_stop_s:
                            print(
                                f"[持续分拣] 检测到机械臂已连续静止 {idle_for:.1f}s（关节波动 {max_span:.2f}° < {idle_deg:.1f}°），按待机设置结束；此条件不代表已视觉确认桌面清空。",
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

from lerobot.datasets import lerobot_dataset
from backend.record_progress import install as install_save_progress, install_loop_progress
install_save_progress(lerobot_dataset)
install_loop_progress(lerobot_record)

_close_async = lambda: None
if os.environ.get('REBOT_ASYNC_ACT') == '1':
    from backend.async_record_hook import install
    _close_async = install(lerobot_record)


if __name__ == "__main__":
    sys.argv = ["lerobot-record"] + sys.argv[1:]
    try:
        sys.exit(main())
    finally:
        _close_controls()
        _close_async()
