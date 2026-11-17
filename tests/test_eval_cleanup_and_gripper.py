import ast
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock
import numpy as np
from backend.async_action_buffer import limit_action, AsyncActionError


class GripperTargetTests(unittest.TestCase):
    def test_valid_absolute_gripper_close_is_slewed_not_rejected_as_arm_jump(self):
        value=limit_action([0],[46/6],[46/6],[-6],joint_names=['right_gripper.pos'],motor_limits=[[-270,0]])
        self.assertAlmostEqual(float(value[0]*-6),-44,places=4)
    def test_actual_command_follow_error_still_fails_with_joint_diagnostics(self):
        with self.assertRaisesRegex(AsyncActionError,r'left_shoulder_pan.pos.*本帧 42.00.*实测 0.00.*30'):
            limit_action([50],[0],[40],[1],joint_names=['left_shoulder_pan.pos'],motor_limits=[[-145,145]])
    def test_reported_right_shoulder_absolute_goal_is_limited_before_follow_error(self):
        value=limit_action([35.4],[3.86],[3.86],[-1],max_step=2,
                           joint_names=['right_shoulder_lift.pos'],motor_limits=[[-170,0]])
        self.assertAlmostEqual(float(value[0]*-1),-5.86,places=4)
    def test_blocked_arm_cannot_accumulate_unlimited_commands(self):
        previous=[0.]
        for _ in range(15):
            previous=limit_action([80],[0],previous,[1],max_step=2)
        with self.assertRaisesRegex(AsyncActionError,'实际待发送命令偏离实测'):
            limit_action([80],[0],previous,[1],max_step=2)
    def test_distant_goal_converges_without_exceeding_per_frame_step(self):
        measured=previous=[0.]
        for _ in range(40):
            value=limit_action([60],measured,previous,[1],max_step=2)
            self.assertLessEqual(abs(value[0]-measured[0]),2.00001)
            measured=previous=value
        self.assertAlmostEqual(float(measured[0]),60.)
    def test_gripper_cannot_escape_physical_stroke(self):
        with self.assertRaisesRegex(AsyncActionError,'物理限位'):
            limit_action([60],[0],[0],[-6],joint_names=['gripper.pos'],motor_limits=[[-270,0]])
    def test_small_model_overshoot_is_clipped_like_native_follower(self):
        value=limit_action([-.1],[0],[0],[-1],joint_names=['shoulder_lift.pos'],motor_limits=[[-170,0]])
        np.testing.assert_allclose(value,[0])


class VideoCleanupTests(unittest.TestCase):
    def exit_method(self):
        file=Path('/home/legion/rebot_lerobot/lerobot/src/lerobot/datasets/video_utils.py')
        if not file.exists():self.skipTest('LeRobot not installed')
        source=ast.parse(file.read_text());cls=next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=='VideoEncodingManager')
        function=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='__exit__')
        env={'logging':logging}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[])),str(file),'exec'),env)
        return env['__exit__']
    def test_writer_is_joined_before_encoding_and_failed_frames_are_preserved(self):
        calls=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);image=root/'images/cam/episode-000000/frame-0.png';image.parent.mkdir(parents=True);image.write_bytes(b'pending image')
            ds=NS(root=root,episodes_since_last_encoding=1,num_episodes=1,
                  _wait_image_writer=lambda:calls.append('drain'),stop_image_writer=lambda:calls.append('stop'),
                  _batch_save_episode_video=lambda *_:calls.append('encode'),finalize=lambda:calls.append('finalize'))
            result=self.exit_method()(NS(dataset=ds),RuntimeError,RuntimeError('policy deviation'),None)
            self.assertFalse(result);self.assertEqual(calls,['drain','stop','encode','finalize']);self.assertTrue(image.exists())
    def test_cleanup_error_cannot_mask_original_policy_error(self):
        ds=NS(_wait_image_writer=Mock(side_effect=OSError('disk error')))
        self.assertFalse(self.exit_method()(NS(dataset=ds),RuntimeError,RuntimeError('policy deviation'),None))
        with self.assertRaisesRegex(OSError,'disk error'):self.exit_method()(NS(dataset=ds),None,None,None)


if __name__=='__main__':unittest.main()
