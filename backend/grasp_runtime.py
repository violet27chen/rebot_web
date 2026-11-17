"""
视觉抓取运行时动力学控制器模块 (Visual Grasping Runtime Controller)
====================================================================

【模块背景与定位】
本模块继承官方轨迹控制器 `RebotArmEndPose`，并专门定制了“受限力矩夹爪闭合”与“实时重力前馈补偿”。
仅在经过明确确认的真实物理抓取场景下才被导入与实例化；
纯粹的正逆运动学（FK/IK）计算绝不导入本运行时，亦不打开任何硬件总线。

【核心安全机制与防护设计】
1. 夹爪受限力矩控制（Bounded Gripper Torque）：
   替代官方高层 SDK 中无界限的硬位置 PD 控制，采用 `clipped_gripper_torque` 算法，
   通过阻尼与比例项计算力矩，并严格限制在 [0.05, 0.5] N·m 范围内，杜绝夹碎纸箱或损坏抓取物品。
2. 电机温升保护熔断（Thermal Protection）：
   在每个控制周期（约 100Hz）严格回读所有电机（1~6 轴手臂 + 7 轴夹爪）的功率管温度（t_mos）。
   一旦发现任何一个电机 MOS 温度超过 80°C，立即停止抓取任务并对整臂失能（disable_all），防止硬件烧毁。
3. 官方 URDF 刚体动力学重力补偿：
   工作在 MIT 力矩控制模式下，利用 Pinocchio 模型实时计算各连杆重力力矩并进行前馈叠加，
   使机械臂在抓取移动过程中极其柔顺平稳。
4. 遥测广播解耦：
   在控制循环内部以 30Hz 频率节流将关节角度与力矩广播至遥测服务，无需外部轮询。
"""
import time
import numpy as np
from reBotArm_control_py.controllers import RebotArmEndPose
from telemetry_client import FeedbackPublisher


def clipped_gripper_torque(target, position, velocity, limit=0.3):
    """
    计算经过刚度、阻尼调节并严格截断在安全区间的夹爪目标输出力矩。

    【控制规律】
    tau = Kp * (target - position) - Kd * velocity
    其中：
    - Kp = 4.0: 适中的位置追踪刚度系数；
    - Kd = 0.15: 速度阻尼系数，防止高速撞击产生强烈弹跳；
    - limit: 力矩上下限截断阈值（默认 ±0.3 N·m）。

    Args:
        target (float): 目标角度 (rad)。
        position (float): 当前实测物理角度 (rad)。
        velocity (float): 当前实测角速度 (rad/s)。
        limit (float, optional): 最大允许力矩绝对值 (N·m)，默认 0.3。

    Returns:
        float: 经过硬限幅后的目标输出力矩 (N·m)。
    """
    return float(np.clip(4.0 * (target - position) - 0.15 * velocity, -limit, limit))


class GraspController(RebotArmEndPose):
    """
    专为自动化物料抓取定制的机械臂动力学末端位姿控制器。
    工作在 MIT 模式下，结合 URDF 刚体动力学重力前馈以及受限力矩夹爪闭合控制。
    """

    def __init__(self, arm, joints, torque_limit=0.3):
        """
        初始化抓取控制器。

        Args:
            arm: RebotArm 底层机械臂通信实例。
            joints (Sequence[float]): 臂部 6 关节初始物理角度列表。
            torque_limit (float, optional): 夹爪抓取力矩限制 (N·m)，必须在 0.05~0.5 之间。

        Raises:
            RuntimeError: 当夹爪未获取到物理反馈时抛出。
            ValueError: 当指定的力矩限制超出安全边界时抛出。
        """
        super().__init__(arm, arm_control_mode='mit', use_gravity_ff=True)
        # 禁用官方 SDK 内部自带的无限制夹爪 PD 逻辑，改由本类在控制循环中精确控制
        self._has_gripper = False
        self._q_target[:] = joints
        self.gripper = arm._motor_map['gripper']
        state = self.gripper.get_state()
        if state is None:
            raise RuntimeError('夹爪没有实测反馈')
        self.gripper_target = state.pos
        self.limit = float(torque_limit)
        if not (0.05 <= self.limit <= 0.5):
            raise ValueError('夹爪力矩限制需要在 0.05～0.5 Nm 之间')
        self.error = None
        self.publisher = FeedbackPublisher()
        self.last_publish = 0

    def start(self):
        """启动控制器：首先将夹爪切换为 MIT 力矩模式，随后启动父类运动学后台控制循环。"""
        self.rebotarm.groups['gripper'].mode_mit()
        super().start()

    def _loop_cb(self, arm, dt):
        """
        控制器内部高频调度回调（约 100Hz）：
        执行硬件状态读取、电机温度保护、重力前馈计算、夹爪力矩计算以及遥测广播。
        """
        try:
            # 步骤 1：在控制循环周期内请求实测反馈
            arm.get_state()
            motors = [arm._motor_map[name] for name in self._arm_group.joint_names]
            states = [motor.get_state() for motor in motors]
            grip = self.gripper.get_state()
            if grip is None or any(s is None for s in states):
                raise RuntimeError('关节反馈缺失')

            # 步骤 2：核心硬件安全保护——实时监测所有电机 MOS 管温度
            if any(s.t_mos > 80 for s in states + [grip]):
                raise RuntimeError('电机温度超过 80°C，已停止抓取')

            # 步骤 3：调用官方控制器计算笛卡尔轨迹插补与 URDF 重力前馈力矩
            super()._loop_cb(arm, dt)

            # 步骤 4：通过限幅阻尼算法计算夹爪力矩并在 MIT 模式下下发
            torque = clipped_gripper_torque(self.gripper_target, grip.pos, grip.vel, self.limit)
            self.gripper.send_mit(grip.pos, 0, 0, 0, torque)

            # 步骤 5：以 30Hz 频率向下游发布实测角度与力矩遥测
            if time.monotonic() - self.last_publish >= 1 / 30:
                self.last_publish = time.monotonic()
                self.publisher.send(arm._channel, [s.pos for s in states], [s.torq for s in states])
        except Exception as exc:
            # 捕获任何异常，记录错误并安全中止控制线程与整臂失能
            self.error = exc
            self._stop_send.set()
            arm._running = False
            arm.disable_all()

    def check(self):
        """主动检查控制后台循环是否抛出内部异常。"""
        if self.error:
            raise RuntimeError(str(self.error))

    def move(self, x, y, z, roll, pitch, yaw, duration=2):
        """
        向指定末端笛卡尔坐标执行平滑轨迹运动，并阻塞等待运动完全到位。

        Args:
            x, y, z (float): 目标位置坐标 (米)。
            roll, pitch, yaw (float): 目标欧拉角姿态 (rad)。
            duration (float, optional): 规划运行时间 (秒)，默认 2.0s。

        Raises:
            RuntimeError: 当 IK 求解失败或控制循环报错时抛出。
            TimeoutError: 当在安全截止时间内未完成运动时抛出。
        """
        self.check()
        if not self.move_to_traj(x, y, z, roll, pitch, yaw, duration=duration):
            raise RuntimeError('轨迹求解失败')
        deadline = time.monotonic() + duration + 8
        while self._moving:
            self.check()
            if time.monotonic() > deadline:
                raise TimeoutError('轨迹执行超时')
            time.sleep(0.02)
        self.check()
        time.sleep(0.2)

    def open_gripper(self):
        """
        以标定张开角度（-3.05 rad）张开夹爪，并等待物理到位。

        Raises:
            TimeoutError: 当 4 秒内夹爪未能张开到位时抛出。
        """
        self.gripper_target = -3.05
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.check()
            state = self.gripper.get_state()
            if state and abs(state.pos - self.gripper_target) < 0.12:
                return
            time.sleep(0.02)
        raise TimeoutError('夹爪未能张开到目标，已停止抓取')

    def grasp(self):
        """
        执行闭合抓取动作，并智能判断是否真正夹持住物体。

        【判定机制】
        闭合目标为 0.0 rad。在 4 秒预算内观察状态：
        1. 若位置接近 0 rad（< 0.1 rad），说明完全合拢成空夹，返回 False（未抓到物品）。
        2. 若角速度极小（< 0.05 rad/s）且检测到持续外力矩（> 0.05 N·m）连续稳定 10 帧（约 0.3s），
           证明夹爪已受阻合拢贴紧物体，返回 True（抓取成功）。

        Returns:
            bool: True 表示成功夹住物体；False 表示空夹闭合。

        Raises:
            TimeoutError: 4 秒内无明确状态判定时抛出。
        """
        self.gripper_target = 0
        deadline = time.monotonic() + 4
        stable = 0
        while time.monotonic() < deadline:
            self.check()
            state = self.gripper.get_state()
            if state:
                # 夹爪已经几乎完全闭合至 0，说明中间没有障碍物
                if abs(state.pos) < 0.1:
                    return False
                # 速度停顿且存在反作用力矩
                stable = stable + 1 if abs(state.vel) < 0.05 and abs(state.torq) > 0.05 else 0
                if stable >= 10:
                    return True
            time.sleep(0.03)
        raise TimeoutError('夹爪闭合反馈不明确，已停止抓取')

    def stop(self):
        """优雅停止抓取控制器，等待后台插值线程退出，并终止机械臂内部控制循环。"""
        self._stop_send.set()
        if self._send_thread:
            self._send_thread.join(timeout=2)
        self.rebotarm.stop_control_loop()
        self._running = False
