"""
只读 CAN 遥测观察器进程 (Read-Only CAN Telemetry Observer)
=============================================================

【模块背景与核心定位】
在 Web 前端或上位机未启动遥操作、数据录制或模型评估等控制任务时，从臂总线处于“空闲”状态。
为了让前端监控看板、3D 机械臂数字孪生页面能实时显示机械臂实际关节角度与受力，同时
【绝对不干扰硬件安全】，本模块作为独立的后台观察进程运行。

【工业安全设计准则】
1. 纯只读（Read-Only）：全过程唯一调用的电机操作是 `request_feedback()` 与 `poll_feedback_once()`。
2. 零控制副作用：严禁调用 `enable()`（使能）、`mode_pos_vel()`（改模式）、下发速度/位置/力矩目标、
   修改看门狗或下发零点指令。
3. 退出安全：进程退出时仅执行 `bus.close()` 释放串口句柄，绝对不发送 `disable()`，防止误切力矩导致掉臂。
4. 独占感知：仅在无控制进程占用串口时启动；控制任务启动前必须优先杀灭本进程。
"""
import argparse
import signal
import time

import yaml
from motorbridge import Controller
from telemetry_client import FeedbackPublisher


def observe(port, hardware, publish, stopping):
    """
    以约 30Hz 频率主动轮询达妙 CAN 总线关节反馈，并通过 IPC 广播发布遥测数据。

    Args:
        port (str): 达妙电机总线所在的串口设备路径（如 /dev/ttyACM0）。
        hardware (dict): 机械臂硬件配置文件内容（包含各关节 motor_id, feedback_id, model 等）。
        publish (FeedbackPublisher): 遥测数据发布客户端，将实测数据通过 Socket 发送至中央遥测服务。
        stopping (callable): 停止检查谓词函数，返回 True 时优雅退出循环。
    """
    # 步骤 1：打开达妙串口通讯总线（波特率 921600）
    bus = Controller.from_dm_serial(port, 921600)
    motors = []
    try:
        # 步骤 2：注册 6 个手臂关节电机和 1 个夹爪电机，建立电机驱动实例
        for joint in hardware['joints']:
            if joint['name'] in hardware['groups']['arm']['joints'] or joint['name'] == 'gripper':
                motors.append(bus.add_damiao_motor(joint['motor_id'], joint['feedback_id'], joint['model']))
                
        # 步骤 3：周期性轮询循环（目标帧率 30Hz）
        while not stopping():
            started = time.monotonic()
            # 向总线上所有电机并发广播请求反馈帧 (0x7FF)
            for motor in motors:
                motor.request_feedback()
            # 留出 4ms 供各电机在 CAN 总线上仲裁并响应回传
            time.sleep(0.004)
            # 轮询接收缓冲区中的 CAN 响应帧
            bus.poll_feedback_once()
            
            # 读取当前所有电机的已解析状态缓存
            states = [motor.get_state() for motor in motors]
            # 严格校验：必须同时拿到全部 7 个关节的有效非空状态才广播
            if len(states) == 7 and all(state is not None for state in states):
                publish.send(
                    port,
                    [s.pos for s in states[:6]],       # 1~6 轴当前物理关节弧度
                    [s.torq for s in states[:6]],      # 1~6 轴当前估计外力矩 (N·m)
                    gripper=states[6].pos             # 7 轴夹爪当前开度弧度
                )
            # 动态休眠补齐 30Hz (每帧约 33.3ms)
            time.sleep(max(0, 1 / 30 - (time.monotonic() - started)))
    finally:
        # 关键安全保证：仅释放 USB 虚拟串口底层传输句柄，绝对不能调用 disable() 或 disconnect()！
        bus.close()


def main():
    """CLI 命令行入口，负责解析参数、注册 POSIX 终止信号并启动观察器。"""
    parser = argparse.ArgumentParser(description="只读 CAN 遥测观察器后台进程")
    parser.add_argument('--port', required=True, help="从臂串口设备路径")
    parser.add_argument('--hardware', required=True, help="机械臂硬件 yaml 配置文件路径")
    args = parser.parse_args()
    
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    # 捕获 SIGTERM 与 SIGINT 实现优雅退出
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # 读取并解析硬件配置
    with open(args.hardware, encoding='utf-8') as stream:
        hardware = yaml.safe_load(stream)
        
    observe(args.port, hardware, FeedbackPublisher(), lambda: stopped)


if __name__ == '__main__':
    main()
