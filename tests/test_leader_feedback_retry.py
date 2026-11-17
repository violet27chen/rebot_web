import ast
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from backend.leader_connection import read_fresh_feedback
from backend.shutdown_signals import finish_cleanup


def sample(value, reliable=True):
    return NS(angle_deg=value, filtered_deg=value, raw_deg=9999., reliable=reliable)


class Clock:
    def __init__(self):
        self.now = 0.
    def __call__(self):
        return self.now
    def sleep(self, dt):
        self.now += dt


class FeedbackRetryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.joints = {'shoulder': 0, 'elbow': 1}
        self.bus = NS(sync_monitor=Mock(return_value={0: sample(12.), 1: sample(9., False)}),
                      read_angle=Mock(return_value=sample(-40.)), set_loss_threshold=Mock())
    def read(self, **kwargs):
        return read_fresh_feedback(self.bus, self.joints, '/dev/leader-qa',
                                   clock=self.clock, sleep=self.clock.sleep, **kwargs)
    def test_good_bulk_feedback_requires_no_extra_reads(self):
        self.bus.sync_monitor.return_value = {0: sample(12.), 1: sample(-40.)}
        self.assertEqual(self.read(), {'shoulder': 12., 'elbow': -40.})
        self.bus.read_angle.assert_not_called()
        self.bus.set_loss_threshold.assert_called_once_with(1)
    def test_only_missing_joint_is_reread_using_filtered_angle(self):
        recovered = Mock()
        self.assertEqual(self.read(on_recovery=recovered), {'shoulder': 12., 'elbow': -40.})
        self.bus.read_angle.assert_called_once_with(1)
        recovered.assert_called_once_with(['elbow'], 0.)
    def test_combine_fresh_replies_across_rounds_without_demanding_simultaneous_success(self):
        self.bus.sync_monitor.return_value = {0: None, 1: None}
        responses = {0: [sample(12.)], 1: [sample(9., False), sample(-40.)]}
        self.bus.read_angle.side_effect = lambda sid: responses[sid].pop(0)
        self.assertEqual(self.read(), {'shoulder': 12., 'elbow': -40.})
        self.assertEqual([c.args[0] for c in self.bus.read_angle.call_args_list], [0, 1, 1])
    def test_failed_bulk_query_can_recover_with_actual_individual_feedback(self):
        self.bus.sync_monitor.side_effect = OSError('USB packet loss')
        self.bus.read_angle.side_effect = lambda sid: sample(12. if sid == 0 else -40.)
        self.assertEqual(self.read(), {'shoulder': 12., 'elbow': -40.})
    def test_stale_nan_and_missing_feedback_never_turn_into_valid_angles(self):
        for bad in [sample(99., False), sample(float('nan')), None]:
            self.bus.read_angle.return_value = bad
            with self.assertRaisesRegex(RuntimeError, r'/dev/leader-qa.*elbow\(ID 1\).*未使用缓存'):
                self.read()
        self.assertGreaterEqual(self.bus.read_angle.call_count, 3)
        self.assertLessEqual(self.bus.read_angle.call_count, 153)
    def test_zero_filter_is_not_mistaken_for_transport_loss(self):
        self.bus.sync_monitor.return_value = {0: sample(0., False), 1: sample(0., False)}
        self.bus.read_angle.return_value = NS(raw_deg=0., filtered_deg=0., reliable=False)
        self.assertEqual(self.read(), {'shoulder': 0., 'elbow': 0.})
        # Consecutive read misses must RAISE instead of returning SDK's held cache.
        self.bus.set_loss_threshold.assert_called_once_with(1)
    def test_zero_filter_keeps_previous_filtered_pose_without_accepting_raw_zero(self):
        self.bus.read_angle.return_value = NS(raw_deg=0., filtered_deg=-40., reliable=False)
        self.assertEqual(self.read(), {'shoulder': 12., 'elbow': -40.})
    def test_float32_zero_threshold_matches_native_sdk_in_both_directions(self):
        for raw in [.20000000298023224, -.20000000298023224]:
            self.bus.read_angle.return_value = NS(raw_deg=raw, filtered_deg=raw, reliable=False)
            self.assertEqual(self.read(), {'shoulder': 12., 'elbow': raw})
        self.bus.read_angle.return_value = NS(raw_deg=.21, filtered_deg=.2, reliable=False)
        with self.assertRaisesRegex(RuntimeError, 'raw=0.21'):
            self.read()
    def test_transport_errors_during_zero_filter_still_timeout(self):
        self.bus.read_angle.side_effect = RuntimeError('servo lost 1 consecutive responses')
        with self.assertRaisesRegex(RuntimeError, '未使用缓存角度.*lost 1'):
            self.read()
        self.assertGreaterEqual(self.clock.now, .25)
    def test_recovery_after_four_fast_rounds_still_gets_full_time_budget(self):
        self.bus.read_angle.side_effect = [RuntimeError('packet loss')]*8 + [sample(-40.)]
        self.assertEqual(self.read(), {'shoulder': 12., 'elbow': -40.})
    def test_slow_query_exceeding_budget_does_not_accept_expired_batch(self):
        def slow_read(sid):
            self.clock.sleep(.26)
            return sample(-40.)
        self.bus.read_angle.side_effect = slow_read
        with self.assertRaisesRegex(RuntimeError, '超过时效预算'):
            self.read()
    def test_no_previous_call_cache_is_used_after_first_success(self):
        self.read()
        self.bus.sync_monitor.return_value = {0: sample(12., False), 1: None}
        self.bus.read_angle.side_effect = RuntimeError('offline')
        with self.assertRaisesRegex(RuntimeError, r'shoulder\(ID 0\).*elbow\(ID 1\)'):
            self.read()
    def test_invalid_time_budget_is_rejected(self):
        for budget in [0, -1, float('nan')]:
            with self.assertRaises(ValueError):
                self.read(timeout=budget)


class CleanupSignalsTests(unittest.TestCase):
    def test_guard_restores_sigint_and_leaves_force_stop_handler_untouched(self):
        before_int = signal.getsignal(signal.SIGINT)
        before_term = signal.getsignal(signal.SIGTERM)
        with self.assertRaisesRegex(RuntimeError, 'failed'):
            with finish_cleanup():
                self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
                self.assertEqual(signal.getsignal(signal.SIGTERM), before_term)
                raise RuntimeError('failed')
        self.assertEqual(signal.getsignal(signal.SIGINT), before_int)
    def test_child_finishes_cleanup_after_repeated_normal_stop(self):
        code = """from backend.shutdown_signals import finish_cleanup
import time
with finish_cleanup():
    print('READY', flush=True)
    time.sleep(.25)
    print('FINISHED', flush=True)
"""
        child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'READY')
            child.send_signal(signal.SIGINT)
            child.send_signal(signal.SIGINT)
            out, err = child.communicate(timeout=3)
            self.assertEqual(child.returncode, 0, err)
            self.assertEqual(out.strip(), 'FINISHED')
        finally:
            if child.poll() is None: child.kill(); child.wait()
    def test_teleop_cleanup_releases_leader_after_unexpected_robot_interrupt(self):
        path = Path('/home/legion/rebot_lerobot/lerobot/src/lerobot/scripts/lerobot_teleoperate.py')
        if not path.exists(): self.skipTest('native teleoperation not installed')
        tree = ast.parse(path.read_text())
        func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'teleoperate')
        body = next(n.finalbody for n in func.body if isinstance(n, ast.Try))
        leader = NS(is_connected=True, disconnect=Mock())
        scope = dict(finish_cleanup=finish_cleanup, cfg=NS(display_data=False), logging=Mock(),
                     robot=NS(disconnect=Mock(side_effect=KeyboardInterrupt())), teleop=leader)
        with self.assertRaises(KeyboardInterrupt):
            exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), '<cleanup>', 'exec'), scope)
        leader.disconnect.assert_called_once()


if __name__ == '__main__':
    unittest.main()
