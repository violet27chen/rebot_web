#!/usr/bin/env python3
"""常驻从臂控制守护进程：连一次串口，持续保持力矩，按行接收 JSON 指令。

协议：stdin 每行一个 {"id": N, "action": "...", "payload": {...}}
输出：stdout 每行一个 {"id": N, "ok": true/false, ...}
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time

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

HOLD_MS = 600000  # 10 分钟；每次指令后刷新


def reply(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main() -> None:
    model = load_robot_model()
    arm = RebotArm()
    try:
        arm.connect()
    except Exception as exc:  # noqa: BLE001
        reply({"ok": False, "error": f"连接从臂失败：{exc}"})
        return
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
        nonlocal ctl_started
        if not ctl_started:
            ctl.start()
            ctl_started = True

    def arm_q() -> np.ndarray:
        q, _dq, _tau = arm.get_state()
        return np.array([q[names.index(n)] for n in arm_names], dtype=float)

    def fk(qa: np.ndarray):
        pos, rot, _ = compute_fk(model, pad_q_for_model(model, qa, len(qa)))
        rpy = pin.rpy.matrixToRpy(rot)
        return [float(x) for x in pos], [float(x) for x in rpy]

    def snapshot() -> dict:
        q, _dq, _tau = arm.get_state()
        qa = arm_q()
        grip = float(q[names.index("gripper")])
        xyz, rpy = fk(qa)
        return {"joints": [float(x) for x in qa], "gripper": grip,
                "xyz": xyz, "rpy": rpy}

    def hold() -> None:
        for m in arm._motor_map.values():
            try:
                m.set_can_timeout_ms(HOLD_MS)
            except Exception:
                pass

    def handle(action: str, p: dict) -> dict:
        with state_lock:
            if action == "state":
                return {"ok": True, **snapshot()}

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
                for i in range(1, 21):
                    cmd = qa + (target - qa) * i / 20
                    g.send_pos_vel([float(x) for x in cmd], vlim=[1.0])
                    time.sleep(0.03)
                hold()
                return {"ok": True, **snapshot()}

            if action == "gripper":
                target = p.get("target")
                if target == "open":
                    target = -3.05   # 张开 = 负方向（机械止点实测约 -174°）
                elif target == "close":
                    target = 0.0     # 闭合 = 0°
                else:
                    target = float(target)
                q, _dq, _tau = arm.get_state()
                cur = float(q[names.index("gripper")])
                # 距离目标够近就不动，避免每次都“重新握持”造成抽动
                if abs(target - cur) < 0.08:
                    hold()
                    return {"ok": True, "gripper": cur, "changed": False}
                closing = target > cur   # 0° 在正方向：往 0 走就是闭合
                # 闭合时如果给了力矩上限，用 FORCE_POS 限力闭合，防止夹坏快递
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
                            return {"ok": True, "gripper": final, "changed": True,
                                    "gripped": abs(final - target) > 0.15,
                                    "force_limited": ratio}
                    except Exception as exc:  # noqa: BLE001
                        return {"ok": False, "error": f"限力闭合失败：{exc}"}
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
                    if i % 4 == 0:
                        q2, _dq2, _tau2 = arm.get_state()
                        pos2 = float(q2[names.index("gripper")])
                        # 闭合时：指令还在走、实际位置不再跟上 = 夹到东西或卡住
                        if closing and abs(pos2 - moving) < 0.015:
                            stalled += 1
                            if stalled >= 2:
                                hold()
                                return {"ok": True, "gripper": pos2, "changed": True,
                                        "gripped": True,
                                        "note": "检测到阻力，已停在夹持位置（没有继续硬压）"}
                        else:
                            stalled = 0
                        moving = pos2
                time.sleep(1.0)
                hold()
                q, _dq, _tau = arm.get_state()
                final = float(q[names.index("gripper")])
                if abs(final - cur) < 0.02 and abs(target - cur) > 0.3:
                    return {"ok": False, "error":
                            f"夹爪没有动（{cur:.2f}→{final:.2f} rad）。"
                            "可能电机未使能或被占用，请重试一次"}
                return {"ok": True, "gripper": final, "changed": True,
                        "gripped": bool(closing and abs(final - target) > 0.15)}

            if action == "pose":
                x, y, z = float(p["x"]), float(p["y"]), float(p["z"])
                ensure_ctl()
                # move_to_traj：IK + 笛卡尔轨迹 + 后台线程发送；False 多为 IK 不可达
                ok = ctl.move_to_traj(
                    x, y, z,
                    float(p.get("roll", 0.0)), float(p.get("pitch", 0.0)),
                    float(p.get("yaw", 0.0)),
                    duration=float(p.get("duration", 1.5)))
                if not ok:
                    # 回退：保持当前姿态，只做平移重试一次（边界处 IK 种子敏感）
                    _xyz0, _rpy0 = fk(arm_q())
                    ok = ctl.move_to_traj(
                        x, y, z, _rpy0[0], _rpy0[1], _rpy0[2],
                        duration=float(p.get("duration", 1.5)))
                if not ok:
                    snap = snapshot()
                    snap["ok"] = False
                    snap["error"] = (f"移动失败：目标 ({x:.2f},{y:.2f},{z:.2f}) 超出可达范围"
                                     "（试试先向上/向后挪几步再试）")
                    return snap
                # 轨迹是后台线程发的；等运动结束再回——期间真正到位才说成功
                time.sleep(float(p.get("duration", 1.5)) + 0.8)
                # 连续读 5 次确认位置稳定（也在动的话再等）
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

            if action == "home":
                ensure_ctl()
                ctl.safe_home()
                time.sleep(0.5)
                hold()
                return {"ok": True, **snapshot()}

            if action == "hold":
                hold()
                return {"ok": True, **snapshot()}

            if action == "quit":
                # 收工：先安全回零，再卸力，然后让守护进程退出（释放串口）
                try:
                    ensure_ctl()
                    ctl.safe_home()
                    time.sleep(0.5)
                except Exception:
                    pass
                try:
                    arm.disable_all()
                except Exception:
                    pass
                return {"ok": True, "closed": True, "bye": True}

            return {"ok": False, "error": f"未知指令：{action}"}

    # Push cached measured joints; request feedback only when no control loop polls it.
    from telemetry_client import FeedbackPublisher
    publisher=FeedbackPublisher()
    observer_stopped=threading.Event()
    def publish_feedback():
        while not observer_stopped.wait(1/30):
            try:
                if not arm.control_loop_active: arm.get_state()
                motors=[arm._motor_map[n] for n in arm_names]
                states=[m.get_state() for m in motors]
                if all(st is not None for st in states):
                    publisher.send(arm._channel,[st.pos for st in states],[st.torq for st in states])
            except Exception:pass
    threading.Thread(target=publish_feedback,daemon=True,name='manual-feedback-push').start()
    reply({"ok": True, "event": "ready"})
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
