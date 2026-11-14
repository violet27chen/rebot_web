"""Hardware-free, velocity/acceleration bounded minimum-jerk homing samples.

This file is mirrored into the standalone vendor drivers for their CLI entrypoints.
A scheduler delay never skips samples or jumps to the final target.
"""
import math

HOME_SPEED_RAD_S=.15
HOME_ACCEL_RAD_S2=.20
HOME_HZ=50.


def trajectory(start,target,*,speed=HOME_SPEED_RAD_S,acceleration=HOME_ACCEL_RAD_S2,hz=HOME_HZ,min_duration=2.):
    start=tuple(float(v) for v in start);target=tuple(float(v) for v in target)
    if len(start)!=len(target) or not start or not all(math.isfinite(v) for v in start+target):
        raise ValueError('归零需要完整的有限实测角度')
    if not all(math.isfinite(v) for v in (speed,acceleration,hz,min_duration)) or not 0<speed<=.3 or not 0<acceleration<=.4 or not 10<=hz<=100 or min_duration<0:
        raise ValueError('归零速度、加速度或采样频率无效')
    delta=max(abs(b-a) for a,b in zip(start,target))
    # Peak first derivative=1.875, peak second derivative=10/sqrt(3).
    duration=max(min_duration,1.875*delta/speed,math.sqrt((10/math.sqrt(3))*delta/acceleration))
    steps=max(1,math.ceil(duration*hz));interval=duration/steps
    for step in range(steps+1):
        s=step/steps;blend=s*s*s*(10+s*(-15+6*s))
        yield interval,tuple(a+(b-a)*blend for a,b in zip(start,target))
