"""Bounded DM mode verification; a missing write ACK is never proof of success."""
import time


def ensure_mode_checked(motor, mode, *, attempts=3, sleep=time.sleep):
    last=None
    for attempt in range(attempts):
        try:
            motor.ensure_mode(mode, timeout_ms=1500)
            return
        except Exception as error:
            last=error
            # Some DM bridges lose the register-write ACK while the register
            # itself changes. Only accept a fresh, matching RID10 response.
            if 'register 10 write ack not received' not in str(error).lower():
                raise
            try:
                if motor.get_register_u32(10, timeout_ms=1500) == int(mode):
                    return
            except Exception as read_error:
                last=read_error
            if attempt+1<attempts:sleep(.08*(attempt+1))
    raise RuntimeError(f'控制模式无法通过寄存器 10 实时回读确认；未允许使能：{last}') from last
