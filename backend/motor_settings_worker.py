"""
B601-DM 达妙电机高级参数（速度/加速度寄存器）读写 Worker 子进程
==================================================================

【模块背景与定位】
在工业机械臂执行精细控制或高速运动任务时，需对各关节电机的内部底层运动参数进行微调。
本模块作为独立的子进程执行工具，通过串口 CAN 总线安全读写达妙电机的底层寄存器：
- RID 4: 加速度 (ACC, 单位: rad/s²)
- RID 5: 减速度 (DEC, 单位: rad/s²，下位机要求为负数)
- RID 6: 最大角速度 (MAX_SPD, 单位: rad/s)
- RID 10: 当前控制模式 (CTRL_MODE)

【安全与稳定性准则】
1. 写入安全校验：对下发的速度、加速度严格做物理界限限制（(0, 100] rad/s），杜绝误输入导致失控。
2. Flash 烧写互锁：若指定 `persist=True` 将参数永久持久化至电机 Flash，必须严格检查整臂所有电机
   均处于“失能（status_code == 0，即释放力矩）”状态，严禁在使能受力或运动过程中擦写 Flash，
   防止下位机固件因写 Flash 中断控制循环造成暴冲。
3. 进程隔离：独立子进程执行，避免阻塞 FastAPI 主事件循环，并通过标准输出协议返回 JSON 结果。
"""
import json
import math
from pathlib import Path
import sys
import time

try:
    from .zero_adjust_worker import FeedbackUnavailable, collect_feedback
except ImportError:
    from zero_adjust_worker import FeedbackUnavailable, collect_feedback

# 达妙电机内部寄存器地址 (Register ID) 定义
RID_ACC = 4         # 加速度寄存器 (f32)
RID_DEC = 5         # 减速度寄存器 (f32, 需为负数)
RID_MAX_SPD = 6     # 最大运行角速度寄存器 (f32)
RID_CTRL_MODE = 10  # 当前控制模式寄存器 (u32)


def _read_joint_params(motor, *, timeout_ms=1000):
    """
    从指定电机的只读/可读寄存器中批量拉取当前运动参数。

    Args:
        motor: 达妙电机控制对象。
        timeout_ms (int, optional): 单次寄存器读取超时时间（毫秒），默认 1000ms。

    Returns:
        dict: 包含 max_speed, acceleration, deceleration, ctrl_mode 的字典。
    """
    max_speed = float(motor.get_register_f32(RID_MAX_SPD, timeout_ms=timeout_ms))
    acceleration = float(motor.get_register_f32(RID_ACC, timeout_ms=timeout_ms))
    deceleration = float(motor.get_register_f32(RID_DEC, timeout_ms=timeout_ms))
    ctrl_mode = int(motor.get_register_u32(RID_CTRL_MODE, timeout_ms=timeout_ms))
    return {
        'max_speed': max_speed,
        'acceleration': acceleration,
        'deceleration': deceleration,
        'ctrl_mode': ctrl_mode,
    }


def motor_rows(params_by_id, states_by_id):
    """
    将电机参数与实时物理反馈状态合并为便于前端展示的行数据结构。

    Args:
        params_by_id (dict): 电机 ID 到寄存器参数字典的映射。
        states_by_id (dict): 电机 ID 到实时状态对象（包含 status_code）的映射。

    Returns:
        list[dict]: 格式化后的电机信息列表。
    """
    rows = []
    for i in sorted(params_by_id):
        p = params_by_id[i]
        st = states_by_id.get(i)
        rows.append({
            'id': i,
            'max_speed': p['max_speed'],
            'acceleration': p['acceleration'],
            'deceleration': p['deceleration'],
            'ctrl_mode': p['ctrl_mode'],
            'disabled': (int(st.status_code) == 0) if st is not None else None,
            'status_code': int(st.status_code) if st is not None else None,
        })
    return rows


def apply_motor_settings(
    bus,
    motors,
    action,
    selected,
    *,
    max_speed=None,
    acceleration=None,
    deceleration=None,
    persist=False,
    sleep=time.sleep,
    clock=time.monotonic,
    log=None,
):
    """
    执行电机参数的读取（read）或修改写入（write），支持选择性保存到 Flash。

    【执行流程与防护】
    1. 参数校验：检查操作动作是否合法，选中的关节列表是否有效。
    2. 反馈采集：调用 `collect_feedback` 确认选中的电机（或在持久化时要求全臂电机）在线通信正常。
    3. 写入前物理范围限制：
       - max_speed ∈ (0, 100.0] rad/s
       - acceleration ∈ (0, 100.0] rad/s²
       - deceleration ∈ [-100.0, 0) rad/s²（若未传则默认取 -acceleration）
    4. Flash 互锁：若 persist=True，必须严格要求全臂电机处于失能状态（status_code == 0），
       否则抛出 RuntimeError 拒绝写入！
    5. 逐个关节写入寄存器，写入后微延时 20ms；若持久化则调用 `store_parameters()` 并延时 80ms。
    6. 最终统一回读并返回最新的电机参数，供前端确认生效。

    Args:
        bus: 达妙总线控制器实例。
        motors (dict): 电机 ID 到控制器驱动对象的字典。
        action (str): 操作类型，'read' 或 'write'。
        selected (list[int]): 目标操作的电机 ID 列表。
        max_speed (float, optional): 最大速度 (rad/s)。
        acceleration (float, optional): 加速度 (rad/s²)。
        deceleration (float, optional): 减速度 (rad/s²，需为负数)。
        persist (bool, optional): 是否烧写保存至 Flash，默认 False。
        sleep (callable, optional): 延时函数。
        clock (callable, optional): 单调时钟函数。
        log (callable, optional): 日志输出回调。

    Returns:
        dict: 包含执行结果 ok、action、selected、persist 及最新 joints 参数的字典。

    Raises:
        ValueError: 输入参数不合法或超出物理安全范围时抛出。
        RuntimeError: 在使能带电状态下尝试持久化到 Flash 时抛出。
    """
    if action not in ('read', 'write') or not selected or any(isinstance(i, bool) or i not in motors for i in selected):
        raise ValueError('无效的电机参数操作或关节选择')
    selected = sorted(set(selected))
    
    # 若需写入 Flash，必须全臂电机在线且失能；若仅读取或临时写入，只需目标关节在线
    required = set(motors) if (action == 'write' and persist) else set(selected)
    states = collect_feedback(bus, motors, required, sleep=sleep, clock=clock, log=log)

    if action == 'write':
        try:
            spd = float(max_speed)
            acc = float(acceleration)
            dec = -acc if deceleration is None else float(deceleration)
        except (TypeError, ValueError) as exc:
            raise ValueError('速度、加速度与减速度必须为有效数字') from exc
            
        # 严格数值安全边界校验
        if not all(math.isfinite(v) for v in (spd, acc, dec)):
            raise ValueError('速度、加速度与减速度必须为有限数值')
        if not (0 < spd <= 100.0):
            raise ValueError('最大速度 (MAX_SPD) 需在 (0, 100] rad/s 范围内')
        if not (0 < acc <= 100.0):
            raise ValueError('加速度 (ACC) 需在 (0, 100] rad/s² 范围内')
        if not (-100.0 <= dec < 0):
            raise ValueError('减速度 (DEC) 需在 [-100, 0) rad/s² 范围内（负值）')
            
        # 核心安全互锁：持久化写 Flash 绝不允许在使能状态下进行
        if persist and any(int(states[i].status_code) != 0 for i in motors):
            raise RuntimeError('写入 Flash 永久保存需要整臂处于失能状态，请先释放力矩')

        # 逐个对所选电机写入寄存器
        for i in selected:
            m = motors[i]
            m.write_register_f32(RID_MAX_SPD, spd)
            sleep(0.02)
            m.write_register_f32(RID_ACC, acc)
            sleep(0.02)
            m.write_register_f32(RID_DEC, dec)
            sleep(0.02)
            if persist:
                m.store_parameters()
                sleep(0.08)
            if log:
                log(f'[电机高级设置] 关节 {i} 已写入 MAX_SPD={spd:.2f}, ACC={acc:.2f}, DEC={dec:.2f}'
                    + ('（已保存到 Flash）' if persist else '（临时生效）'), flush=True)

    # 写入后（或读取操作）重新回读各寄存器，确保真实状态与界面展示完全一致
    params = {}
    for i in selected:
        params[i] = _read_joint_params(motors[i])

    return {
        'ok': True,
        'action': action,
        'selected': selected,
        'persist': bool(persist),
        'joints': motor_rows(params, states),
    }


def main():
    """
    Worker 进程 CLI 入口：接收 JSON 命令行参数，执行电机参数读写并输出标准响应。
    """
    from motorbridge import Controller
    import yaml

    payload = json.loads(sys.argv[1])
    bus = None
    motors = {}
    try:
        # 读取硬件连接与关节映射配置
        hardware = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / 'rebot_grasp/sdk/reBotArm_control_py/config/rebotarm_dm.yaml').read_text()
        )
        bus = Controller.from_dm_serial(payload['port'], 921600)
        for j in hardware['joints']:
            motors[int(j['motor_id'])] = bus.add_damiao_motor(j['motor_id'], j['feedback_id'], j['model'])
            
        result = apply_motor_settings(
            bus,
            motors,
            payload['action'],
            payload['joints'],
            max_speed=payload.get('max_speed'),
            acceleration=payload.get('acceleration'),
            deceleration=payload.get('deceleration'),
            persist=bool(payload.get('persist', False)),
            log=print,
        )
        result.update(side=payload['side'], port=payload['port'], timestamp=time.time())
        # 输出结构化协议结果供主进程捕获
        print('REBOT_MOTOR_RESULT ' + json.dumps(result, ensure_ascii=False), flush=True)
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
            detail.update(missing_joints=error.missing)
        print('REBOT_MOTOR_ERROR ' + json.dumps(detail, ensure_ascii=False), flush=True)
        print(f'[电机高级设置失败] {error}', flush=True)
        return 1
    finally:
        # 退出清理：安全关闭各电机对象与底层总线
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
