"""面单朝上 → 向左推走 的决策、几何与流程编排，不需要真实设备。"""
import unittest
import numpy as np
from backend.sorting.geometry import SortingError
from backend.sorting.policy import decide, PUSH_LEFT, RESCAN, FLIP, FAIL
from backend.sorting.pushleft import (left_direction, rpy_matrix, gripper_down_ok,
                                      push_left_poses, verify_left)
from backend.sorting.cycle import run_cycle
from backend.sorting.skills import route
from backend.sorting.service import validate_settings

EYE = np.eye(4).tolist()


class PolicyTests(unittest.TestCase):
    def test_decisions(self):
        self.assertEqual(decide('ok', 0, 0), PUSH_LEFT)
        self.assertEqual(decide('blurry', 0, 0), RESCAN)
        self.assertEqual(decide('blurry', 2, 0), FAIL)
        self.assertEqual(decide('not_found', 0, 0), RESCAN)   # 先排除角度问题
        self.assertEqual(decide('not_found', 1, 0), FLIP)
        self.assertEqual(decide('not_found', 1, 2), FAIL)
        with self.assertRaises(SortingError): decide('?', 0, 0)


class GeometryTests(unittest.TestCase):
    def test_left_direction_is_horizontal_unit(self):
        d = left_direction([-3, 4, 9])
        self.assertAlmostEqual(np.linalg.norm(d), 1); self.assertEqual(d[2], 0)
        with self.assertRaises(SortingError): left_direction([0, 0, 1])

    def test_gripper_down(self):
        down = [np.pi, 0, 0]          # 绕 X 旋转 180°：工具 Z 轴朝下
        self.assertTrue(gripper_down_ok(down, [0, 0, 1], EYE))
        self.assertFalse(gripper_down_ok([0, 0, 0], [0, 0, 1], EYE))
        self.assertFalse(gripper_down_ok([np.pi - .6, 0, 0], [0, 0, 1], EYE, .35))
        np.testing.assert_allclose(rpy_matrix([0, 0, 0]), np.eye(3), atol=1e-12)

    def test_push_left_poses_go_left(self):
        poses = push_left_poses([0, 0, .05], [.2, .15, .1], .1, [-1, 0, 0], .12, .1, 'right', 'left')
        self.assertEqual([p[0] for p in poses], ['left', 'left', 'right', 'right', 'right', 'right', 'left'])
        contact, end = poses[3][1], poses[4][1]
        self.assertAlmostEqual(end[0] - contact[0], -.12)   # 向 X 负方向（左）
        self.assertGreater(contact[0], 0)                   # 从包裹右侧接触
        with self.assertRaises(SortingError): push_left_poses([0, 0, 0], [.1, .1, 0], .1, [-1, 0, 0], .5, .1, 'a', 'b')

    def test_verify_left(self):
        self.assertTrue(verify_left([0, 0, 0], None, [-1, 0, 0], .08)['removed'])
        self.assertTrue(verify_left([0, 0, 0], {'xyz_m': [-.1, 0, 0]}, [-1, 0, 0], .08)['reached'])
        self.assertFalse(verify_left([0, 0, 0], {'xyz_m': [.1, 0, 0]}, [-1, 0, 0], .08)['reached'])
        self.assertFalse(verify_left([0, 0, 0], {'xyz_m': [-.02, 0, 0]}, [-1, 0, 0], .08)['reached'])


class FakeIO:
    def __init__(self, scans, pushes):
        self.scans, self.pushes, self.log = list(scans), list(pushes), []
        self.pos = [0., 0., .05]

    def check(self): pass
    def phase(self, n): self.log.append(n)
    def scene(self): return {'scene_id': 's'}
    def pick(self, scene, prev):
        return None if getattr(self, 'gone', False) else {'id': '0', 'name': 'box', 'xyz_m': list(self.pos)}
    def plan(self, scene, target, action): self.log.append('plan:' + action); return {'paths': []}
    def execute(self, plan): pass
    def scan(self): return self.scans.pop(0)
    def act_flip(self, target): self.log.append('act_flip')
    def verify_after_push(self, target):
        r = self.pushes.pop(0)
        if r is None: self.gone = True; return None
        self.pos = r; return {'id': '0', 'name': 'box', 'xyz_m': r}


OK = {'status': 'ok', 'codes': [{'text': 'SF1'}], 'sharpness': 200}
NF = {'status': 'not_found', 'codes': [], 'sharpness': 200}
BL = {'status': 'blurry', 'codes': [], 'sharpness': 5}


class CycleTests(unittest.TestCase):
    def test_face_up_pushes_left_directly(self):
        io = FakeIO([OK], [[-.2, 0, .05]])
        r = run_cycle(io, {})
        self.assertEqual(r['status'], 'done'); self.assertEqual(r['flips'], 0)
        self.assertNotIn('act_flip', io.log); self.assertIn('plan:push_left', io.log)

    def test_face_down_flips_then_pushes(self):
        io = FakeIO([NF, NF, OK], [None])
        r = run_cycle(io, {})
        self.assertEqual(r['flips'], 1); self.assertTrue(r['removed'])
        self.assertEqual(io.log.count('act_flip'), 1)
        self.assertLess(io.log.index('act_flip'), io.log.index('plan:push_left'))

    def test_never_pushes_without_readable_label(self):
        io = FakeIO([NF] * 20, [])
        with self.assertRaises(SortingError): run_cycle(io, {'max_flips': 2})
        self.assertNotIn('plan:push_left', io.log)
        self.assertEqual(io.log.count('act_flip'), 2)

    def test_blurry_gives_up(self):
        io = FakeIO([BL] * 5, [])
        with self.assertRaises(SortingError): run_cycle(io, {})
        self.assertNotIn('act_flip', io.log)

    def test_insufficient_push_retries_then_fails(self):
        io = FakeIO([OK], [[-.01, 0, .05], [-.02, 0, .05]])
        with self.assertRaises(SortingError): run_cycle(io, {})


class ConfigTests(unittest.TestCase):
    def test_route_roles(self):
        skills = [{'name': 'a', 'class_name': 'box'}, {'name': 'f', 'class_name': 'box', 'role': 'flip'}]
        self.assertEqual(route({'name': 'box'}, skills)['name'], 'a')
        self.assertEqual(route({'name': 'box'}, skills, 'flip')['name'], 'f')
        with self.assertRaises(SortingError): route({'name': 'bag'}, skills, 'flip')

    def test_cycle_settings_validation(self):
        validate_settings({'cycle': {'left_direction': [-1, 0, 0], 'left_push_m': .1, 'arm': 'auto'}})
        for bad in ({'left_direction': [0, 0, 1]}, {'left_push_m': 1}, {'arm': 'x'}, {'max_flips': 9}):
            with self.assertRaises(SortingError): validate_settings({'cycle': bad})


if __name__ == '__main__':
    unittest.main()
