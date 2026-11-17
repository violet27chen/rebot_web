import ast
from contextlib import nullcontext
import logging
import math
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from backend.leader_connection import fresh_positions, read_action_positions
from backend.record_audio import install

NATIVE = Path('/home/legion/rebot_lerobot/lerobot/src/lerobot')
LEADER = Path('/home/legion/miniconda3/envs/lerobot/lib/python3.12/site-packages/lerobot_teleoperator_rebot_arm_102/rebot_arm_102_leader.py')
FOLLOWER = Path('/home/legion/rebot_lerobot/lerobot/lerobot-robot-seeed-b601/lerobot_robot_seeed_b601/seeed_b601_follower.py')


def function(path, name, **scope):
    tree = ast.parse(path.read_text())
    func = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)
    func.decorator_list = []
    env = dict(scope)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[func], type_ignores=[])), str(path), 'exec'), env)
    return env[name]


class SpeechTests(unittest.TestCase):
    def test_web_entry_logging_never_spawns_speech_even_when_requested(self):
        recorder = NS(log_say=Mock(side_effect=AssertionError('speech must be disabled')))
        install(recorder)
        with self.assertLogs(level='INFO') as logs:
            recorder.log_say('Stop recording', True, blocking=True)
        self.assertIn('Stop recording', logs.output[0])

    @unittest.skipUnless(NATIVE.exists(), 'native recorder not installed')
    def test_legacy_speech_functions_are_log_only_even_when_requested(self):
        for name, args in [('say', {'blocking': True}),
                           ('log_say', {'play_sounds': True, 'blocking': True})]:
            logger = Mock()
            speak = function(NATIVE/'utils/utils.py', name, logging=logger)
            speak('Stop recording', **args)
            logger.info.assert_called_once_with('Stop recording')


@unittest.skipUnless(NATIVE.exists(), 'native recorder not installed')
class NativeCleanupTests(unittest.TestCase):
    def cleanup(self, *, finalize_error=None, robot_error=None):
        tree = ast.parse((NATIVE/'scripts/lerobot_record.py').read_text())
        record = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'record')
        final = next(node.finalbody for node in record.body if isinstance(node, ast.Try))
        events = []
        def call(name, error=None):
            def run(*args, **kwargs):
                events.append(name)
                if error:
                    raise error
            return run
        logger = Mock()
        logger.info.side_effect = lambda message: events.append(message) if message in ('Stop recording', 'Exiting') else None
        env = dict(logging=logger, finish_cleanup=nullcontext,
                   dataset=NS(finalize=call('finalize', finalize_error)),
                   robot=NS(is_connected=True, disconnect=call('follower', robot_error)),
                   teleop=NS(is_connected=True, disconnect=call('leader')),
                   is_headless=lambda: True, listener=None, cfg=NS(play_sounds=False, dataset=NS(push_to_hub=False)))
        try:
            exec(compile(ast.fix_missing_locations(ast.Module(body=final, type_ignores=[])), '<native cleanup>', 'exec'), env)
        except RuntimeError as exc:
            events.append(str(exc))
        logger.info.assert_any_call('Stop recording')
        return events

    def test_stop_closes_data_and_both_devices_in_order(self):
        self.assertEqual(self.cleanup(), ['Stop recording', 'finalize', 'follower', 'leader', 'Exiting'])

    def test_disk_error_still_runs_robot_and_leader_cleanup(self):
        self.assertEqual(self.cleanup(finalize_error=RuntimeError('disk error')),
                         ['Stop recording', 'finalize', 'follower', 'leader', 'disk error'])

    def test_follower_cleanup_error_still_releases_leader(self):
        self.assertEqual(self.cleanup(robot_error=RuntimeError('homing obstruction')),
                         ['Stop recording', 'finalize', 'follower', 'leader', 'homing obstruction'])


class FreshFeedbackTests(unittest.TestCase):
    def test_monitor_requires_complete_fresh_finite_angles(self):
        joints = {'shoulder': 0, 'elbow': 1}
        good = NS(angle_deg=13.0, reliable=True)
        self.assertEqual(fresh_positions({0: good, 1: good}, joints), {'shoulder': 13., 'elbow': 13.})
        for bad in [None, NS(angle_deg=13., reliable=False), NS(angle_deg=float('nan'), reliable=True)]:
            with self.assertRaisesRegex(RuntimeError, 'elbow'):
                fresh_positions({0: good, 1: bad}, joints)
        with self.assertRaisesRegex(RuntimeError, 'elbow'):
            fresh_positions({0: good}, joints)

    def test_transient_read_is_retried_but_persistent_loss_cannot_replay_old_angles(self):
        read = Mock(side_effect=[RuntimeError('cache'), {'elbow': 12.}])
        self.assertEqual(read_action_positions(read, sleep=lambda _: None), {'elbow': 12.})
        read = Mock(side_effect=RuntimeError('no fresh feedback'))
        with self.assertRaisesRegex(RuntimeError, '未沿用旧角度'):
            read_action_positions(read, sleep=lambda _: None)
        self.assertEqual(read.call_count, 3)

    @unittest.skipUnless(LEADER.exists(), 'leader not installed')
    def test_native_get_action_propagates_loss_instead_of_returning_last_action(self):
        get = function(LEADER, 'get_action', RobotAction=dict, time=NS(perf_counter=lambda: 0.),
                       read_action_positions=read_action_positions, logger=Mock(), DeviceNotConnectedError=RuntimeError)
        obj = NS(is_connected=True, _last_raw_positions={'shoulder': 32.},
                 _read_raw_positions=Mock(side_effect=RuntimeError('lost')))
        with self.assertRaisesRegex(RuntimeError, '未沿用旧角度'):
            get(obj)
        self.assertEqual(obj._last_raw_positions, {'shoulder': 32.})

    @unittest.skipUnless(FOLLOWER.exists(), 'follower not installed')
    def test_missing_follower_state_is_not_zero_observation(self):
        get = function(FOLLOWER, 'get_observation', RobotObservation=dict, Any=object,
                       time=NS(perf_counter=lambda: 0.), math=math, logger=Mock(), DeviceNotConnectedError=RuntimeError)
        for state in [None, NS(pos=float('nan'), vel=0., torq=0.)]:
            obj = NS(is_connected=True, config=NS(port='/dev/qa'), motors={'elbow': NS(request_feedback=Mock(), get_state=lambda: state)},
                     bus=NS(poll_feedback_once=Mock()), cameras={})
            with self.assertRaisesRegex(RuntimeError, 'elbow.*反馈缺失或无效'):
                get(obj)


class WebCommandTests(unittest.TestCase):
    def test_record_and_eval_disable_audio_and_have_contextual_silent_progress(self):
        from backend import main
        import copy
        cfg = copy.deepcopy(main.DEFAULT_CONFIG)
        cfg['cameras'] = []
        cfg['eval'].update(policy_path='/qa/model', async_inference=False)
        with patch.object(main, 'eval_policy_cameras', return_value=[]):
            for kind in ['record', 'eval']:
                self.assertIn('--play_sounds=false', main.build_command(kind, cfg))
        self.assertIn('收尾', main._silence_hint({'kind': 'record', 'recording': {'phase': 'closing'}}))
        self.assertNotIn('下载', main._silence_hint({'kind': 'record'}))


class ManualStopDatasetSafetyTests(unittest.TestCase):
    def test_record_loop_keyboard_interrupt_preserves_recorded_frames(self):
        from backend.record_progress import install_loop_progress
        events = {'stop_recording': False, 'exit_early': False, 'rerecord_episode': True}
        dataset = NS(episode_buffer={'size': 42})
        def failing_loop(*args, **kwargs):
            raise KeyboardInterrupt()

        module = NS(record_loop=failing_loop)
        install_loop_progress(module)
        res = module.record_loop(events=events, dataset=dataset)
        self.assertIsNone(res)
        self.assertTrue(events['stop_recording'])
        self.assertTrue(events['exit_early'])
        self.assertFalse(events['rerecord_episode'])

    def test_record_loop_keyboard_interrupt_with_zero_frames_sets_rerecord(self):
        from backend.record_progress import install_loop_progress
        events = {'stop_recording': False, 'exit_early': False, 'rerecord_episode': False}
        dataset = NS(episode_buffer={'size': 0})
        def failing_loop(*args, **kwargs):
            raise KeyboardInterrupt()

        module = NS(record_loop=failing_loop)
        install_loop_progress(module)
        res = module.record_loop(events=events, dataset=dataset)
        self.assertIsNone(res)
        self.assertTrue(events['stop_recording'])
        self.assertTrue(events['exit_early'])
        self.assertTrue(events['rerecord_episode'])

    def test_dataset_finalize_auto_saves_uncommitted_buffer(self):
        from backend.record_progress import install
        saved = []
        finalized = []
        class FakeDataset:
            _wait_image_writer = lambda self: None
            _save_episode_data = lambda self: {}
            def save_episode(self, *args, **kwargs):
                saved.append(self.episode_buffer['size'])
                self.episode_buffer['size'] = 0
            def finalize(self):
                finalized.append(True)

        dataset_mod = NS(LeRobotDataset=FakeDataset)
        install(dataset_mod)

        inst = FakeDataset()
        inst.episode_buffer = {'size': 25}
        inst.num_episodes = 1
        inst.finalize()

        self.assertEqual(saved, [25])
        self.assertEqual(finalized, [True])

    def test_shutdown_worker_live_modes_accepts_get_register_u32(self):
        from backend.shutdown_worker import home_arm
        class RealMotorbridgeMock:
            def __init__(self, mode):
                self._mode = mode
            def get_register_u32(self, rid, timeout_ms=1000):
                assert rid == 10
                return self._mode
            def request_feedback(self): pass
            def get_state(self):
                return NS(pos=0.01, vel=0.01, torq=0.0, t_mos=35.0, t_rotor=35.0, status_code=0)
            def ensure_mode(self, mode, **kw): self._mode = int(mode)
            def send_pos_vel(self, pos, speed): pass
            def send_force_pos(self, pos, speed, ratio): pass
            def enable(self): pass
            def disable(self): pass

        bus = NS(poll_feedback_once=lambda: None)
        motors = {i: RealMotorbridgeMock(2 if i != 7 else 3) for i in range(1, 8)}
        home_arm(bus, motors, position_mode=2, force_mode=3, sleep=lambda _: None, clock=lambda: 0.0)

    def test_save_episode_shields_against_sigint(self):
        import signal
        from backend.record_progress import install
        completed = []
        class FakeDataset:
            _wait_image_writer = lambda self: None
            _save_episode_data = lambda self: {}
            def save_episode(self, *args, **kwargs):
                # 模拟在保存期间收到 SIGINT
                signal.raise_signal(signal.SIGINT)
                completed.append(True)
            def finalize(self): pass

        dataset_mod = NS(LeRobotDataset=FakeDataset)
        install(dataset_mod)

        inst = FakeDataset()
        inst.meta = NS(video_keys=[])
        inst.vcodec = 'h264'
        with self.assertRaises(KeyboardInterrupt):
            inst.save_episode()
        self.assertEqual(completed, [True])


if __name__ == '__main__':
    unittest.main()
