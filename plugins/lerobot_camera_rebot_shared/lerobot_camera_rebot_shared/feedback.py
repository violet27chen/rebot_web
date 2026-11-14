"""Publish feedback after a real observation; no extra CAN reader or cache timer."""
import os


def install():
    if not os.environ.get('REBOT_TELEMETRY_SOCKET'): return
    from telemetry_client import FeedbackPublisher
    from lerobot_robot_seeed_b601.seeed_b601_follower import SeeedB601FollowerBase
    if getattr(SeeedB601FollowerBase, '_rebot_feedback_hook', False): return
    original = SeeedB601FollowerBase.get_observation
    publisher = FeedbackPublisher()
    def observe(self, *args, **kwargs):
        observation = original(self, *args, **kwargs)
        try:
            names = [n for n in self.motor_names if n != 'gripper']
            states = [self.motors[n].get_state() for n in names if n in self.motors]
            if len(states) == 6 and all(s is not None for s in states):
                gripper=self.motors.get('gripper')
                g=gripper.get_state() if gripper else None
                publisher.send(self.config.port, [s.pos for s in states], [s.torq for s in states], gripper=g.pos if g else None)
        except Exception: pass
        return observation
    SeeedB601FollowerBase.get_observation = observe
    SeeedB601FollowerBase._rebot_feedback_hook = True
