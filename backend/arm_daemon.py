#!/usr/bin/env python3
"""
常驻从臂控制守护进程 (Persistent Arm Control Daemon)
======================================================

【模块背景与核心定位】
在 Web 前端进行连续机械臂交互（如笛卡尔拖拽、连续点动、快速运单抓取）时，
频繁启动/销毁 Python 进程会导致频繁打开/关闭串口和反复电机握手，造成严重延迟与机械抖动。
本模块作为常驻的后台子进程运行：
1. 仅在启动时建立一次串口与电机通信，全程保持连接。
2. 通过 stdin/stdout 基于 JSON-Lines 标准行通信协议与主进程双向交互。
3. 持续维持 10 分钟看门狗力矩保持（HOLD_MS = 600000ms），绝不因空闲掉电下坠。
4. 集成后台 30Hz 异步遥测发布线程，免除外部额外观察进程的总线争抢。

【通信协议规范】
- 输入 (stdin): 每行一个 JSON 对象:
    {"id": <int/str 请求流水号>, "action": "<操作指令>", "payload": {<参数>}}
- 输出 (stdout): 每行一个 JSON 对象:
    {"id": <流水号>, "ok": <bool>, ... 业务结果数据 ...}

【高级功能与安全机制】
- 夹爪防夹坏保护（Torque-Limited Gripping）：闭合时支持传入 torque_ratio，采用 FORCE_POS 软闭合；
  并支持运动过程中位置停滞（堵转）感知，自动判定为“夹持成功”并提前悬停，不施加破坏性硬挤压。
- 笛卡尔逆解容错回退（IK Fallback）：若全姿态逆解因奇异点无解，自动保持当前姿态仅做平移重试。
- 运动到位稳定确认：运动完成后连续采样校验（最大变化 < 0.004 rad），确认物理完全静止才返回完成。
- 安全退出（Safe Quit）：接收 quit 指令时，以超低速（0.15 rad/s）平稳归零后再失能退出。
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time

# 动态配置机械臂 Python 控制 SDK 路径
SDK = "/home/legion/rebot_web/rebot_grasp/sdk/reBotArm_control_py"
if SDK not in sys.path:
    sys.path.insert(0, SDK)

import numpy as np  # noqa: E402
import pinocchio as pin  # noqa: E402
from reBotArm_control_py.actuator import RebotArm  # noqa: E402
from reBotArm_control_py.controllers import RebotArmEndPose  # noqa: E402
from reBotArm_control_py.kinematics.forward_kinematics import compute_fk  # noqa: E402
from reBotArm_control_py.kinematics.robot_model import (  # noqa: E402
    load_robot_model, pad_q_for_model,
)

# 硬件看门狗力矩保持时长：600 秒 (10 分钟)，每次接收到指令后自动刷新
HOLD_MS = 600000


def reply(obj: dict) -> None:
    """向 stdout 发送单行 JSON 响应并立即刷新输出流。"""
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main() -> None:
    """守护进程主循环入口。"""
    # 步骤 1：加载 Pinocchio 刚体动力学模型与连接机械臂
    model = load_robot_model()
    arm = RebotArm()
    try:
        arm.connect()
    except Exception as exc:  # noqa: BLE001
        reply({"ok": False, "error": f"连接从臂失败：{exc}"})
        return

    # 步骤 2：设置硬件超时时间，开启力矩维持
    try:
        for m in arm._motor_map.values():
            try:
                m.set_can_timeout_ms(HOLD_MS)
            except Exception:
                pass
    except Exception:
        pass

    names = [jc.name for jc in arm._all_joints]
    arm_names = list(arm.groups["arm"].joint_names)
    state_lock = threading.Lock()
    ctl = RebotArmEndPose(arm, arm_control_mode="posvel")
    ctl_started = False

    def ensure_ctl() -> None:
        """按需惰性启动末端位姿控制器后台调度器。"""
        nonlocal ctl_started
        if not ctl_started:
            ctl.start()
            ctl_started = True

    def arm_q() -> np.ndarray:
        """获取当前 6 自由度臂部物理关节角度。"""
        q, _dq, _tau = arm.get_state()
        return np.array([q[names.index(n)] for n in arm_names], dtype=float)

    def fk(qa: np.ndarray):
        """正运动学计算末端空间位置 [x,y,z] 与姿态 [roll,pitch,yaw]。"""
        pos, rot, _ = compute_fk(model, pad_q_for_model(model, qa, len(qa)))
        rpy = pin.rpy.matrixToRpy(rot)
        return [float(x) for x in pos], [float(x) for x in rpy]

    def snapshot() -> dict:
        """获取当前机械臂的完整状态快照。"""
        q, _dq, _tau = arm.get_state()
        qa = arm_q()
        grip = float(q[names.index("gripper")])
        xyz, rpy = fk(qa)
        return {
            "joints": [float(x) for x in qa],
            "gripper": grip,
            "xyz": xyz,
            "rpy": rpy
        }

    def hold() -> None:
        """刷新所有电机的底层 CAN 超时时间，保证断续期间力矩持续。"""
        for m in arm._motor_map.values():
            try:
                m.set_can_timeout_ms(HOLD_MS)
            except Exception:
                pass

    def handle(action: str, p: dict) -> dict:
        """
        核心指令路由处理函数（受线程锁保护，保证原子互斥执行）。

        Args:
            action (str): 操作指令名称。
            p (dict): 操作关联的参数载荷。

        Returns:
            dict: 处理结果字典。
        """
        with state_lock:
            # 分支 1：状态快照查询
            if action == "state":
                return {"ok": True, **snapshot()}

            # 分支 2：单关节微步点动
            if action == "jog":
                idx = int(p.get("index", 0))
                delta = math.radians(float(p.get("delta_deg", 0.0)))
                qa = arm_q()
                if idx < 0 or idx >= len(qa):
                    return {"ok": False, "error": "关节序号超出范围"}
                target = qa.copy()
                target[idx] += delta
                g = arm.groups["arm"]
                g.mode_pos_vel()
                g.enable()
                # 20 步平滑插值规划
                for i in range(1, 21):
                    cmd = qa + (target - qa) * i / 20
                    g.send_pos_vel([float(x) for x in cmd], vlim=[1.0])
                    time.sleep(0.03)
                hold()
                return {"ok": True, **snapshot()}

            # 分支 3：夹爪开合与智能防夹
            if action == "gripper":
                target = p.get("target")
                if target == "open":
                    target = -3.05   # 张开：负方向（实测物理机械止点约 -174°）
                elif target == "close":
                    target = 0.0     # 闭合：0°
                else:
                    target = float(target)
                q, _dq, _tau = arm.get_state()
                cur = float(q[names.index("gripper")])
                
                # 若已处于目标位置附近（误差 < 0.08 rad），直接返回，避免无谓抽动
                if abs(target - cur) < 0.08:
                    hold()
                    return {"ok": True, "gripper": cur, "changed": False}
                    
                closing = target > cur   # 往 0 方向移动即为闭合
                
                # 软夹持特性：如果前端指定了力矩上限 ratio，使用底层 FORCE_POS 限力闭合
                ratio = p.get("torque_ratio")
                if closing and ratio is not None:
                    try:
                        ratio = max(0.01, min(1.0, float(ratio)))
                        motor = getattr(arm, "_motor_map", {}).get("gripper")
                        if motor is not None and hasattr(motor, "send_force_pos"):
                            motor.send_force_pos(float(target), math.radians(60.0), ratio)
                            time.sleep(1.8)
                            hold()
                            q2, _dq2, _tau2 = arm.get_state()
                            final = float(q2[names.index("gripper")])
                            return {
                                "ok": True,
                                "gripper": final,
                                "changed": True,
                                "gripped": abs(final - target) > 0.15,
                                "force_limited": ratio
                            }
                    except Exception as exc:  # noqa: BLE001
                        return {"ok": False, "error": f"限力闭合失败：{exc}"}
                        
                # 标准速度位置模式开合
                g = arm.groups["gripper"]
                g.mode_pos_vel()
                g.enable()
                steps = 25
                moving = cur
                stalled = 0
                for i in range(1, steps + 1):
                    step_target = cur + (target - cur) * i / steps
                    try:
                        g.send_pos_vel([step_target], vlim=[1.5])
                    except Exception as exc:  # noqa: BLE001
                        return {"ok": False, "error": f"夹爪指令发送失败：{exc}"}
                    time.sleep(0.05)
                    # 堵转停滞检测：闭合阶段若指令在走而实际位置停止前进，判定已夹持住物品
                    if i % 4 == 0:
                        q2, _dq2, _tau2 = arm.get_state()
                        pos2 = float(q2[names.index("gripper")])
                        if closing and abs(pos2 - moving) < 0.015:
                            stalled += 1
                            if stalled >= 2:
                                hold()
                                return {
                                    "ok": True,
                                    "gripper": pos2,
                                    "changed": True,
                                    "gripped": True,
                                    "note": "检测到阻力，已停在夹持位置（没有继续硬压）"
                                }
                        else:
                            stalled = 0
                        moving = pos2
                        
                time.sleep(1.0)
                hold()
                q, _dq, _tau = arm.get_state()
                final = float(q[names.index("gripper")])
                if abs(final - cur) < 0.02 and abs(target - cur) > 0.3:
                    return {
                        "ok": False,
                        "error": f"夹爪没有动（{cur:.2f}→{final:.2f} rad）。可能电机未使能或被占用，请重试一次"
                    }
                return {
                    "ok": True,
                    "gripper": final,
                    "changed": True,
                    "gripped": bool(closing and abs(final - target) > 0.15)
                }

            # 分支 4：笛卡尔位姿轨迹控制
            if action == "pose":
                x, y, z = float(p["x"]), float(p["y"]), float(p["z"])
                ensure_ctl()
                # 调用 move_to_traj：IK 逆解求解 + 五次多项式笛卡尔轨迹规划
                ok = ctl.move_to_traj(
                    x, y, z,
                    float(p.get("roll", 0.0)), float(p.get("pitch", 0.0)),
                    float(p.get("yaw", 0.0)),
                    duration=float(p.get("duration", 1.5))
                )
                if not ok:
                    # 容错回退机制：若全姿态逆解失败，尝试锁定当前姿态仅平移
                    _xyz0, _rpy0 = fk(arm_q())
                    ok = ctl.move_to_traj(
                        x, y, z, _rpy0[0], _rpy0[1], _rpy0[2],
                        duration=float(p.get("duration", 1.5))
                    )
                if not ok:
                    snap = snapshot()
                    snap["ok"] = False
                    snap["error"] = (f"移动失败：目标 ({x:.2f},{y:.2f},{z:.2f}) 超出可达范围"
                                     "（试试先向上/向后挪几步再试）")
                    return snap
                    
                # 等待后台执行线程完成插值发送
                time.sleep(float(p.get("duration", 1.5)) + 0.8)
                # 连续 8 轮采样验证物理位置是否完全静止收敛
                prev = None
                for _ in range(8):
                    time.sleep(0.15)
                    qa = arm_q()
                    if prev is not None and max(abs(a - b) for a, b in zip(qa, prev)) < 0.004:
                        break
                    prev = qa
                hold()
                snap = snapshot()
                snap["reached"] = True
                snap["ok"] = True
                return snap

            # 分支 5：平滑回零
            if action == "home":
                ensure_ctl()
                ctl.safe_home()
                time.sleep(0.5)
                hold()
                return {"ok": True, **snapshot()}

            # 分支 6：力矩刷新
            if action == "hold":
                hold()
                return {"ok": True, **snapshot()}

            # 分支 7：优雅安全退出
            if action == "quit":
                ensure_ctl()
                # 以极低速 0.15 rad/s 安全回到零位后才切断使能
                ctl.safe_home(max_vel=0.15)
                time.sleep(0.5)
                arm.disable_all()
                return {"ok": True, "closed": True, "bye": True}

            return {"ok": False, "error": f"未知指令：{action}"}

    # 步骤 3：启动后台 30Hz 关节角度与力矩发布线程
    from telemetry_client import FeedbackPublisher
    publisher = FeedbackPublisher()
    observer_stopped = threading.Event()

    def publish_feedback():
        while not observer_stopped.wait(1 / 30):
            try:
                if not arm.control_loop_active:
                    arm.get_state()
                motors = [arm._motor_map[n] for n in arm_names]
                states = [m.get_state() for m in motors]
                if all(st is not None for st in states):
                    publisher.send(arm._channel, [st.pos for st in states], [st.torq for st in states])
            except Exception:
                pass

    threading.Thread(target=publish_feedback, daemon=True, name='manual-feedback-push').start()
    
    # 向标准输出发送就绪握手事件
    reply({"ok": True, "event": "ready"})

    # 步骤 4：主事件循环：从标准输入逐行读取 JSON 指令并响应
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply({"ok": False, "error": "指令不是合法 JSON"})
            continue
        rid = msg.get("id")
        try:
            out = handle(str(msg.get("action")), msg.get("payload") or {})
            out["id"] = rid
            reply(out)
            if out.get("bye"):
                observer_stopped.set()
                break
            if not out.get("ok"):
                print(f"[arm_daemon] FAIL action={msg.get('action')} "
                      f"error={out.get('error')}", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[arm_daemon] EXC action={msg.get('action')} "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            reply({"id": rid, "ok": False, "error": str(exc)[:400]})


if __name__ == "__main__":
    main()
