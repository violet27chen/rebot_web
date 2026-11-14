"""Read-only CAN observer used only while no control process owns the port.

The only motor operation is request_feedback(). No enable, mode change,
position/velocity command, watchdog change, homing or torque command is issued.
"""
import argparse
import signal
import time

import yaml
from motorbridge import Controller
from telemetry_client import FeedbackPublisher


def observe(port, hardware, publish, stopping):
    bus = Controller.from_dm_serial(port, 921600)
    motors = []
    try:
        for joint in hardware['joints']:
            if joint['name'] in hardware['groups']['arm']['joints'] or joint['name']=='gripper':
                motors.append(bus.add_damiao_motor(joint['motor_id'], joint['feedback_id'], joint['model']))
        while not stopping():
            started = time.monotonic()
            for motor in motors: motor.request_feedback()
            time.sleep(.004)
            bus.poll_feedback_once()
            states = [motor.get_state() for motor in motors]
            if len(states) == 7 and all(state is not None for state in states):
                publish.send(port, [s.pos for s in states[:6]], [s.torq for s in states[:6]], gripper=states[6].pos)
            time.sleep(max(0, 1/30-(time.monotonic()-started)))
    finally:
        bus.close()  # Transport release only; never call disable()/disconnect().


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--port', required=True); parser.add_argument('--hardware', required=True)
    args = parser.parse_args(); stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    with open(args.hardware) as stream: hardware = yaml.safe_load(stream)
    observe(args.port, hardware, FeedbackPublisher(), lambda:stopped)


if __name__ == '__main__': main()
