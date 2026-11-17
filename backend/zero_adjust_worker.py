"""
B601-DM 达妙从臂机械零点校准与力矩释放 Worker 子进程 (Zero Adjustment Worker)
==================================================================================

【模块背景与定位】
在工业机械臂使用过程中，因装配、撞击或重置后，关节绝对零点可能发生偏移。
本模块作为独立的子进程执行机械臂的“零点读取”、“力矩释放”与“机械零点标定持久化”操作。

【关键安全机制】
1. 只读节奏轮询（Paced Polling）：采集电机状态时，绝不发送使能、运动或改模式命令，纯只读探测。
2. 冷启动诊断：在第 10 次重试时主动诊断 RID 10（模式）与 RID 7（反馈 ID），精确定位是总线断线还是 ID 不匹配。
3. 设零前力矩释放硬校验：执行 `set-zero` 前，强制要求机械臂所有电机均处于失能（status_code == 0）状态，
   严禁在电机通电带载时设零，防止机械剧烈冲击损坏减速器。
4. 闭环验证与双重确认：设零后逐个回读，确保当前物理角度已真正处于 0 附近（< 0.02 rad），
   随后写入 Flash（store_parameters），最后再次全量回读校验，不符合要求绝不报告成功。
5. LeRobot 标定元数据自动同步与备份：当整臂（1~7轴）全部完成设零时，自动生成官方兼容的
   LeRobot MotorCalibration 配置文件，并在覆盖前对旧标定文件自动进行毫秒级时间戳备份。
"""
import json
import math
from pathlib import Path
import sys
import time


class FeedbackUnavailable(RuntimeError):
    """
    当从臂指定关节未在超时时间内返回有效物理反馈时抛出的专用异常。
    包含已成功采集的 states 字典和缺失反馈的 missing 电机 ID 列表。
    """
    def __init__(self, message, states, missing):
        super().__init__(message)
        self.states = states
        self.missing = missing


def feedback_rows(states):
    """
    将电机实时底层状态字典转化为结构化的数据列表，便于前端可视化展示。

    Args:
        states (dict): 电机 ID 到电机状态对象的映射字典。

    Returns:
        list[dict]: 包含 id, degrees, disabled, status_code, reported_motor_id, feedback_can_id 的字典列表。
    """
    return [
        {
            'id': i,
            'degrees': math.degrees(st.pos),
            'disabled': st.status_code == 0,
            'status_code': int(st.status_code),
            'reported_motor_id': getattr(st, 'can_id', None),
            'feedback_can_id': getattr(st, 'arbitration_id', None),
        }
        for i, st in sorted(states.items())
    ]


def collect_feedback(bus, motors, required, *, sleep=time.sleep, clock=time.monotonic,
                     timeout_s=2.5, max_attempts=80, log=None):
    """
    带节奏控制与总线诊断的有界只读反馈轮询函数。

    【核心流程】
    1. 纯只读轮询：向 required 指定的电机循环广播 `request_feedback()`，并调用 `poll_feedback_once()`。
    2. 解决冷串口与丢包：多达 80 轮微步重试（每轮约 25ms），总预算 2.5 秒。
    3. 智能诊断探针：在第 10 轮重试时，如果仍缺少反馈，主动对首个缺失的电机发起 RID 10（控制模式）
       和 RID 7（反馈 ID）只读寄存器探测，区分是通信总线无响应还是 ID 仲裁不匹配。
    4. 成功条件：仅当 required 中的所有电机均采集到有限合法角度时才视为成功返回。

    Args:
        bus: 达妙总线控制器实例。
        motors (dict): 所有注册的电机对象字典。
        required (set[int]): 本次必须获得反馈的电机 ID 集合。
        sleep (callable, optional): 延时函数。
        clock (callable, optional): 单调时间函数。
        timeout_s (float, optional): 最大超时时间（秒），默认 2.5s。
        max_attempts (int, optional): 最大尝试轮数，默认 80 次。
        log (callable, optional): 诊断日志打印回调。

    Returns:
        dict: 电机 ID 到有效状态对象的映射。

    Raises:
        FeedbackUnavailable: 当超时或耗尽尝试后仍有 required 关节未响应时抛出。
    """
    started = clock()
    deadline = started + timeout_s
    errors = {}
    states = {}

    for attempt in range(max_attempts):
        # 步骤 1：向所有必需的关节请求反馈
        for i in sorted(required):
            try:
                motors[i].request_feedback()
                sleep(0.002)
                bus.poll_feedback_once()
            except Exception as exc:
                errors[i] = str(exc)

        # 步骤 2：达妙底层驱动单次 poll 只取一帧，此处多次轮询确保抽空接收队列
        for _ in range(max(2, len(motors))):
            try:
                bus.poll_feedback_once()
            except Exception as exc:
                errors['bus'] = str(exc)
                break

        # 步骤 3：收集各电机最新解析的状态
        states = {}
        for i, motor in motors.items():
            try:
                value = motor.get_state()
                if value is not None and math.isfinite(float(value.pos)):
                    states[i] = value
            except Exception as exc:
                errors[i] = str(exc)

        missing = sorted(set(required) - states.keys())
        if not missing:
            return states

        # 步骤 4：在第 10 次尝试时执行深度只读寄存器探测，帮助排查硬件故障
        if log and attempt == 9:
            probe = missing[0]
            for rid, label in ((10, '控制模式'), (7, '反馈ID')):
                try:
                    value = motors[probe].get_register_u32(rid, timeout_ms=200)
                    log(f'[零点读取诊断] CAN {probe} {label}={value}', flush=True)
                except Exception as exc:
                    errors[probe] = f'只读寄存器 {rid} 未响应：{exc}'
                    log(f'[零点读取诊断] CAN {probe} ' + errors[probe], flush=True)

        # 打印关键轮次的等待日志
        if log and attempt in (0, 9, 29, 59):
            log('[零点读取] 等待实测反馈 · 缺少 CAN ID ' + ','.join(map(str, missing)) + f' · 第 {attempt + 1} 次', flush=True)

        if clock() >= deadline:
            break
        sleep(0.025)

    # 步骤 5：超时未全到位，抛出详细诊断异常
    missing = sorted(set(required) - states.keys())
    detail = '；'.join(f'{i}: {errors[i]}' for i in missing if i in errors)
    if errors.get('bus'):
        detail += '；总线轮询：' + errors['bus']
    raise FeedbackUnavailable(
        '所选关节反馈缺失（CAN ID ' + ','.join(map(str, missing)) +
        f'，已重试 {attempt + 1} 次 / {clock() - started:.2f}s）' +
        '。请检查该臂电机供电、CAN 接线 / ID 与串口映射；未发送使能、运动或设零指令' +
        ('；' + detail if detail else ''),
        states,
        missing
    )


def adjust_zero(bus, motors, action, selected, *, sleep=time.sleep, publish=None, clock=time.monotonic, log=None):
    """
    执行零点调整核心逻辑：读取 (read)、释放力矩 (release)、或设置并持久化机械零点 (set-zero)。

    Args:
        bus: 总线控制器对象。
        motors (dict): 所有关节驱动对象字典。
        action (str): 操作类型 ('read', 'release', 'set-zero')。
        selected (list[int]): 本次选中的关节 ID 列表。
        sleep (callable, optional): 延时函数。
        publish (callable, optional): 遥测广播回调。
        clock (callable, optional): 单调时钟。
        log (callable, optional): 日志函数。

    Returns:
        dict: 操作执行结果字典。

    Raises:
        ValueError: 当参数不合法时抛出。
        RuntimeError: 当设零前未失能、设零后回读不归零或保存失败时抛出。
    """
    if action not in ('read', 'release', 'set-zero') or not selected or any(isinstance(i, bool) or i not in motors for i in selected):
        raise ValueError('无效的操作或关节选择')

    # 读取操作仅需目标关节在线即可；而释放力矩或设零必须要求整臂全部在线并受监视
    required = set(selected) if action == 'read' else set(motors)

    def read():
        states = collect_feedback(bus, motors, required, sleep=sleep, clock=clock, log=log)
        # 若提供了遥测广播回调，实时向系统广播最新角度与力矩
        if publish and all(i in states for i in range(1, 7)):
            try:
                publish([states[i].pos for i in range(1, 7)], [states[i].torq for i in range(1, 7)])
            except Exception:
                pass
        return states

    states = read()
    before = {i: math.degrees(st.pos) for i, st in states.items()}

    # 动作分支 1：释放力矩 (release)
    if action == 'release':
        for motor in motors.values():
            motor.disable()
        # 轮询确认全部电机确实已进入失能状态 (status_code == 0)
        for _ in range(20):
            states = read()
            if all(st.status_code == 0 for st in states.values()):
                break
        else:
            raise RuntimeError('未确认全部电机失能，请勿手动摆动')

    # 动作分支 2：设置零点 (set-zero)
    if action == 'set-zero':
        # 安全互锁：必须先确认全臂电机均已完全失能！
        if any(states[i].status_code != 0 for i in motors):
            raise RuntimeError('所选从臂尚未全部失能，请先托住机械臂并点击“释放力矩”')

        # 预热并读取模式寄存器
        for i in selected:
            motors[i].get_register_u32(10, timeout_ms=1500)

        completed = []
        zeroed = []
        try:
            for i in selected:
                # 下发设零指令
                motors[i].set_zero_position()
                zeroed.append(i)
                # 轮询回读，验证电机当前位置是否已经归零 (< 0.02 rad)
                for _ in range(25):
                    states = read()
                    if abs(states[i].pos) < 0.02:
                        break
                else:
                    raise RuntimeError(f'关节 {i} 设零后读数没有接近 0')

                # 将当前参数烧录至电机 Flash 存储器中永久保存
                motors[i].store_parameters()
                sleep(0.1)
                # 读回模式确认电机就绪
                motors[i].get_register_u32(10, timeout_ms=1500)
                completed.append(i)
        except Exception as error:
            raise RuntimeError(f'零点未全部完成；已执行设零 {zeroed}，已确认保存 {completed}，其余结果不确定，请核查：{error}') from error

    # 最终回读并双重验证
    states = read()
    if action == 'set-zero' and any(abs(states[i].pos) >= 0.02 for i in selected):
        raise RuntimeError('持久化后零点回读未通过，未报告成功')

    return {
        'ok': True,
        'action': action,
        'selected': selected,
        'before_deg': before,
        'missing_joints': sorted(set(motors) - states.keys()),
        'joints': feedback_rows(states),
    }


def main():
    """CLI 主入口：解析执行参数并调用零点校准逻辑。"""
    from motorbridge import Controller
    from telemetry_client import FeedbackPublisher
    import yaml

    payload = json.loads(sys.argv[1])
    bus = None
    motors = {}
    try:
        hardware = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / 'rebot_grasp/sdk/reBotArm_control_py/config/rebotarm_dm.yaml').read_text()
        )
        bus = Controller.from_dm_serial(payload['port'], 921600)
        for j in hardware['joints']:
            motors[int(j['motor_id'])] = bus.add_damiao_motor(j['motor_id'], j['feedback_id'], j['model'])
            
        publisher = FeedbackPublisher()
        result = adjust_zero(
            bus,
            motors,
            payload['action'],
            payload['joints'],
            publish=lambda q, t: publisher.send(
                payload['port'], q, t,
                gripper=motors[7].get_state().pos if motors[7].get_state() else None
            ),
            log=print,
        )

        # 若整臂全部完成标定，自动更新生成 LeRobot 官方格式标定文件
        if payload['action'] == 'set-zero' and set(payload['joints']) == set(range(1, 8)):
            from lerobot_robot_seeed_b601.seeed_b601_dm_follower import SeeedB601DMFollower
            from lerobot_robot_seeed_b601.config_seeed_b601_dm_follower import SeeedB601DMFollowerConfig
            from lerobot.motors import MotorCalibration
            
            robot = SeeedB601DMFollower(SeeedB601DMFollowerConfig(port=payload['port'], id=payload['id'], can_adapter='damiao'))
            robot.calibration = {
                name: MotorCalibration(id=ids[0], drive_mode=0, homing_offset=0, range_min=-90, range_max=90)
                for name, ids in robot.config.motor_can_ids.items()
            }
            # 自动备份旧标定文件，防止误操作丢失历史标定
            if robot.calibration_fpath.exists():
                import shutil
                shutil.copy2(robot.calibration_fpath, str(robot.calibration_fpath) + '.before-zero-' + str(time.time_ns()))
            robot._save_calibration()

        result.update(side=payload['side'], port=payload['port'], timestamp=time.time())
        print('REBOT_ZERO_RESULT ' + json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        detail = {
            'ok': False,
            'error': str(error),
            'side': payload.get('side'),
            'port': payload.get('port'),
            'timestamp': time.time(),
        }
        if isinstance(error, FeedbackUnavailable):
            detail.update(joints=feedback_rows(error.states), missing_joints=error.missing)
        print('REBOT_ZERO_ERROR ' + json.dumps(detail, ensure_ascii=False), flush=True)
        print(f'[零点调整失败] {error}', flush=True)
        return 1
    finally:
        for motor in motors.values():
            try:
                motor.close()
            except Exception:
                pass
        if bus:
            try:
                bus.close()
            except Exception:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
