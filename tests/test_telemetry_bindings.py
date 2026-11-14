"""Current canonical follower ports, not cached side labels, own model feedback."""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from backend.telemetry import Telemetry


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.left=Path(self.temp.name)/'tty-left';self.right=Path(self.temp.name)/'tty-right'
        self.left.touch();self.right.touch();self.alias=Path(self.temp.name)/'stable-left';self.alias.symlink_to(self.left)
        self.telemetry=Telemetry(self.temp.name);self.telemetry.publish=Mock()
        self.telemetry.configure({'left':str(self.left),'right':str(self.right)})
    def tearDown(self):
        self.telemetry.close();shutil.rmtree(self.telemetry.directory,ignore_errors=True);self.temp.cleanup()
    def packet(self,port,side='left',q=.3,**kwargs):
        return dict(token=self.telemetry.token,side=side,port=str(port),joints=[q]*6,timestamp=time.time(),**kwargs)
    def test_port_routes_stale_or_wrong_side_label_to_correct_arm(self):
        self.assertTrue(self.telemetry.accept(self.packet(self.right,'left')))
        state=self.telemetry.snapshot()['arms'];self.assertNotIn('left',state)
        self.assertEqual(state['right']['port'],str(self.right));self.assertEqual(state['right']['side'],'right')
    def test_swap_clears_old_poses_and_reassigns_by_actual_port(self):
        self.telemetry.accept(self.packet(self.left));old=self.telemetry.snapshot()['binding_generation']
        queued=self.packet(self.left)
        self.telemetry.configure({'left':str(self.right),'right':str(self.left)})
        self.assertEqual(self.telemetry.snapshot()['arms'],{})
        self.assertFalse(self.telemetry.accept(queued))
        self.assertTrue(self.telemetry.accept(self.packet(self.left,'left',q=.8)))
        state=self.telemetry.snapshot();self.assertEqual(state['binding_generation'],old+1)
        self.assertEqual(state['arms']['right']['joints'],[.8]*6)
    def test_stable_path_matches_real_serial_device_and_retains_display_path(self):
        self.telemetry.configure({'left':str(self.alias)})
        self.assertTrue(self.telemetry.accept(self.packet(self.left,'right')))
        self.assertEqual(self.telemetry.snapshot()['arms']['left']['binding_port'],str(self.alias))
        bindings=self.telemetry.snapshot()['bindings'];self.assertEqual(bindings['left']['canonical_port'],str(self.left))
    def test_single_arm_can_use_previous_right_port_without_right_model(self):
        self.telemetry.configure({'left':str(self.right)})
        self.assertTrue(self.telemetry.accept(self.packet(self.right,'right')))
        self.assertEqual(set(self.telemetry.snapshot()['arms']),{'left'})
        self.assertFalse(self.telemetry.accept(self.packet(self.left)))
    def test_duplicate_aliases_never_arbitrarily_pick_a_side(self):
        self.telemetry.configure({'left':str(self.left),'right':str(self.alias)})
        self.assertFalse(self.telemetry.accept(self.packet(self.left)))
        self.assertTrue(all(b['ambiguous'] for b in self.telemetry.snapshot()['bindings'].values()))
    def test_old_timestamp_unknown_port_and_nonfinite_joint_do_not_replace_pose(self):
        self.telemetry.accept(self.packet(self.left,q=.2))
        old=self.packet(self.left,q=.1);old['timestamp']-=.1
        self.assertFalse(self.telemetry.accept(old))
        self.assertFalse(self.telemetry.accept(self.packet('/dev/not-configured')))
        self.assertFalse(self.telemetry.accept(self.packet(self.left,q=float('nan'))))
        self.assertEqual(self.telemetry.snapshot()['arms']['left']['joints'],[.2]*6)
    def test_unchanged_binding_does_not_clear_latest_on_environment_refresh(self):
        self.telemetry.accept(self.packet(self.left))
        env=self.telemetry.environment({'left':str(self.left),'right':str(self.right)})
        self.assertEqual(json.loads(env['REBOT_TELEMETRY_PORTS'])[str(self.right)],'right')
        self.assertIn('left',self.telemetry.snapshot()['arms'])


class SupervisorTests(unittest.TestCase):
    def test_training_keeps_idle_readonly_feedback_but_arm_jobs_have_exclusive_ports(self):
        import backend.main as main
        from types import SimpleNamespace
        from unittest.mock import patch
        for kind,observe in [('train',True),('teleoperate',False),('zero-adjust',False)]:
            with self.subTest(kind=kind),patch.object(main.TELEMETRY.stop,'wait',side_effect=[False,True]),                 patch.object(main.TELEMETRY,'configure'),patch.object(main.TELEMETRY,'observe_idle') as idle,                 patch.object(main.TELEMETRY,'stop_observers') as stop,patch.object(main.STATE,'running',return_value=True),                 patch.object(main.STATE,'job',SimpleNamespace(kind=kind)),patch.object(main,'DEVICES_RELEASED',False):
                main.observer_supervisor()
                self.assertEqual(idle.call_count,1 if observe else 0)
                self.assertEqual(stop.call_count,0 if observe else 1)


if __name__=='__main__':unittest.main()
