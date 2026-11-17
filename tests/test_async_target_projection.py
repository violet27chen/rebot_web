"""Policy-goal projection must never relax measured limits or command guards."""
import unittest
import numpy as np

from backend.async_action_buffer import AsyncActionError, TargetProjectionWatchdog, limit_action


class ProjectionTests(unittest.TestCase):
    def test_reported_elbow_target_is_projected_to_zero_without_exiting(self):
        status = {}
        result = limit_action([6.27], [-.47], [-.47], [1],
                              joint_names=['left_elbow_flex.pos'], motor_limits=[[-200, 0]],
                              max_lead=6, diagnostics=status)
        np.testing.assert_allclose(result, [0])
        self.assertEqual(status['projected_targets'][0]['raw_motor_deg'], 6.27)
        self.assertEqual(status['projected_targets'][0]['projected_motor_deg'], 0)

    def test_projection_does_not_jump_straight_from_a_distant_pose_to_zero(self):
        result = limit_action([6.27], [-20], [-20], [1], motor_limits=[[-200, 0]], max_lead=6)
        np.testing.assert_allclose(result, [-18])

    def test_inverted_shoulder_and_geared_gripper_are_projected_in_motor_coordinates(self):
        status = {}
        value = limit_action([-8.94, -1], [.03, .1], [.03, .1], [-1, -6],
                             joint_names=['left_shoulder_lift.pos', 'gripper.pos'],
                             motor_limits=[[-170, 0], [-270, 0]], max_lead=6, diagnostics=status)
        np.testing.assert_allclose(value, [0, 0], atol=1e-6)
        self.assertEqual(len(status['projected_targets']), 2)

    def test_measured_overflow_is_not_treated_as_a_clippable_prediction(self):
        with self.assertRaisesRegex(AsyncActionError, '实测反馈超出物理限位'):
            limit_action([0], [6.27], [6.27], [1], motor_limits=[[-200, 0]], max_lead=6)

    def test_severely_invalid_goal_nan_and_tracking_loss_still_stop(self):
        with self.assertRaisesRegex(AsyncActionError, '策略目标严重超出物理限位'):
            limit_action([31], [-.47], [-.47], [1], motor_limits=[[-200, 0]], max_lead=6)
        with self.assertRaises(AsyncActionError):
            limit_action([np.nan], [0], [0], [1], motor_limits=[[-200, 0]], max_lead=6)
        with self.assertRaisesRegex(AsyncActionError, '上一命令已失跟'):
            limit_action([-10], [-50], [-10], [1], motor_limits=[[-200, 0]], max_lead=6)

    def test_sustained_large_projection_is_not_silently_masked(self):
        watch = TargetProjectionWatchdog()
        bad = [dict(joint='left_elbow_flex.pos', raw_motor_deg=6.27,
                    projected_motor_deg=0., overshoot_motor_deg=6.27)]
        watch.update(bad, 10.)
        watch.update(bad, 11.9)
        with self.assertRaisesRegex(AsyncActionError, '模型持续预测越界目标'):
            watch.update(bad, 12.)
        watch.reset()
        watch.update(bad, 20.)
        watch.update([], 21.)
        watch.update(bad, 22.)
        watch.update(bad, 23.9)

    def test_valid_prediction_clears_the_previous_projection_diagnostics(self):
        status = {}
        limit_action([6.27], [-.47], [-.47], [1], motor_limits=[[-200, 0]], diagnostics=status)
        self.assertTrue(status['projected_targets'])
        limit_action([-1], [-.47], [-.47], [1], motor_limits=[[-200, 0]], diagnostics=status)
        self.assertEqual(status['projected_targets'], [])


if __name__ == '__main__':
    unittest.main()
