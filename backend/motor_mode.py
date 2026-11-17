"""
B601-DM 达妙电机控制模式校验与设置模块 (Motor Mode Verification)
================================================================

【模块背景与定位】
在工业机械臂控制与大功率无刷伺服电机（如达妙 DM 系列电机）通信中，通过 CAN/串口总线
下发电机运行模式切换（例如：从 MIT 力矩模式切换到 POS_VEL 位置速度模式）属于高风险操作。
部分硬件或总线桥接芯片在密集通讯时可能会丢失“写寄存器确认帧（Register-Write ACK）”，
但实际上电机的内部寄存器可能已经写入成功；亦或者写操作失败。若仅凭是否收到写 ACK 或盲目重试，
极易导致：
1. 误报通信失败，导致初始化流程意外中断；
2. 电机并未真正切换模式就强行下发使能指令，引发电机暴冲或硬件异常。

【核心保护机制】
本模块提供带超时限制与回读校验的双重保护函数 `ensure_mode_checked`：
1. 下发模式切换指令，捕获写入异常。
2. 若捕获到“register 10 write ack not received”（寄存器 10 写入确认未收到）错误，
   不盲目认定失败，而是立即向电机主动回读 RID 10（控制模式寄存器）。
3. 只有回读到的当前模式值与期望的目标模式完全一致时，才确认切换成功。
4. 提供指数/线性退避重试机制（attempts），若最终仍无法通过回读验证，严格抛出异常，
   坚决禁止未确认模式的电机进入使能状态，保障人身与硬件安全。
"""
import time


def ensure_mode_checked(motor, mode, *, attempts=3, sleep=time.sleep):
    """
    确保达妙 (DM) 电机正确切换至指定的控制模式，并通过寄存器 10 严格回读验证。

    【工作流程】
    1. 尝试调用 motor.ensure_mode(mode) 下发模式变更命令（默认超时 1500ms）。
    2. 若指令成功收到 ACK，直接返回。
    3. 若抛出异常且属于写 ACK 丢失（"register 10 write ack not received"）：
       - 主动读取电机内部 RID 10 (控制模式寄存器) 的 32 位无符号整型值。
       - 比对寄存器实际值与期望模式值：若匹配则判定模式切换真实生效，安全返回；
       - 若读取失败或值不匹配，记录异常并进入下一次重试。
    4. 若属于其他严重错误（如串口断开、CAN 总线严重错误），立即向上抛出。
    5. 重试之间执行步进延时退避（0.08s * attempt），防止总线瞬态拥塞。
    6. 达到最大重试次数仍未验证成功，抛出 RuntimeError，坚决阻断后续使能。

    Args:
        motor: 电机控制对象（需实现 ensure_mode, get_register_u32 等接口，如 motorbridge 或 DM SDK 电机驱动）。
        mode (int or Enum): 期望设置的电机工作模式枚举或整型值（例如 MIT 模式、POS_VEL 位置速度模式等）。
        attempts (int, optional): 最大尝试验证轮数，默认为 3 次。
        sleep (callable, optional): 延时函数，默认为 time.sleep，可在测试中注入 mock。

    Raises:
        RuntimeError: 当达到最大尝试次数仍无法通过寄存器 10 确认控制模式时抛出，阻止盲目使能。
        Exception: 捕获到非 ACK 丢失的致命总线通信异常时直接向外冒泡。
    """
    last = None
    for attempt in range(attempts):
        try:
            # 步骤 1：向电机下发控制模式切换指令，等待下位机写入并回复 ACK
            motor.ensure_mode(mode, timeout_ms=1500)
            return
        except Exception as error:
            last = error
            # 步骤 2：部分达妙串口/CAN桥接器在修改寄存器时会偶发丢失写ACK帧。
            # 如果异常不是“寄存器 10 写 ACK 未接收”，说明是通信中断等致命错误，直接向上抛出。
            if 'register 10 write ack not received' not in str(error).lower():
                raise
            # 步骤 3：针对丢写ACK的特殊情况，主动发起读取查询寄存器 10 (RID 10: 控制模式寄存器)
            try:
                # 达妙电机寄存器 10 (0x0A) 存储当前电机的运行控制模式
                if motor.get_register_u32(10, timeout_ms=1500) == int(mode):
                    # 寄存器数值匹配，证明模式实际已变更成功，可以安全放行
                    return
            except Exception as read_error:
                # 回读也发生异常，记录为最后错误
                last = read_error
            # 步骤 4：未确认成功且未耗尽重试次数时，执行线性退避延时 (0.08s, 0.16s...)
            if attempt + 1 < attempts:
                sleep(0.08 * (attempt + 1))

    # 步骤 5：所有重试用尽，仍无法确认模式切换，安全熔断
    raise RuntimeError(f'控制模式无法通过寄存器 10 实时回读确认；未允许使能：{last}') from last
