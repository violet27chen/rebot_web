"""面单朝上 → 向左推走 的决策、几何与流程编排，不需要真实设备。"""
import unittest
import numpy as np
from backend.sorting.geometry import SortingError, PackageError
from backend.sorting.policy import decide, PUSH_LEFT, RESCAN, FLIP, FAIL
from backend.sorting.pushleft import (left_direction, rpy_matrix, gripper_down_ok,
                                      push_left_poses, verify_left)
from backend.sorting.cycle import run_cycle, run_session
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
    """桌面模拟：packages 为 [(x, 扫描脚本)]；推走即从桌面移除，jam 的包裹推不动。"""
    def __init__(self, packages, jam=()):
        self.p = [{'id': str(i), 'name': 'box', 'xyz_m': [x, 0., .05], 'valid': True, 'scans': list(sc)}
                  for i, (x, sc) in enumerate(packages)]
        self.jam, self.log, self.cur = set(jam), [], None
        self.hidden = 0

    def check(self): pass
    def phase(self, n): self.log.append(n)
    def retreat(self): self.log.append('retreat')
    def scene(self):
        if self.hidden:
            self.hidden -= 1
            return {'scene_id': 's', 'targets': [dict(t, valid=False) for t in self.p]}
        return {'scene_id': 's', 'targets': [dict(t) for t in self.p]}
    def pick(self, scene, prev):
        near = [t for t in scene['targets'] if t.get('valid') and abs(t['xyz_m'][0] - prev['xyz_m'][0]) < .12]
        return near[0] if near else None
    def _find(self, t): return next(q for q in self.p if q['id'] == t['id'])
    def plan(self, scene, target, action):
        self.cur = self._find(target); self.log.append('plan:%s:%s' % (action, target['id']))
        return {'paths': [], 'side': 'left' if target['xyz_m'][0] <= 0 else 'right'}
    def execute(self, plan): pass
    def scan(self, side=None):
        self.log.append('scan:' + str(side))
        return self.cur['scans'].pop(0) if len(self.cur['scans']) > 1 else dict(self.cur['scans'][0])
    def act_flip(self, target): self.log.append('flip:' + target['id'])
    def verify_after_push(self, target):
        q = self._find(target)
        if q['id'] in self.jam: return dict(q)
        self.p.remove(q); return None


OK = {'status': 'ok', 'codes': [{'text': 'SF1'}], 'sharpness': 200}
NF = {'status': 'not_found', 'codes': [], 'sharpness': 200}
BL = {'status': 'blurry', 'codes': [], 'sharpness': 5}


class CycleTests(unittest.TestCase):
    def test_face_up_pushes_left_directly(self):
        io = FakeIO([(0, [OK])])
        r = run_cycle(io, {}, io.scene()['targets'][0])
        self.assertEqual(r['status'], 'done'); self.assertEqual(r['flips'], 0)
        self.assertFalse([l for l in io.log if l.startswith('flip')])

    def test_face_down_flips_then_pushes(self):
        io = FakeIO([(0, [NF, NF, OK])])
        r = run_cycle(io, {}, io.scene()['targets'][0])
        self.assertEqual(r['flips'], 1)
        flips = [i for i, l in enumerate(io.log) if l.startswith('flip')]
        push = [i for i, l in enumerate(io.log) if ':push_left:' in l]
        self.assertTrue(flips and push and flips[0] < push[0])

    def test_unreadable_is_flagged_and_still_cleared_by_default(self):
        io = FakeIO([(0, [NF])])
        r = run_cycle(io, {'max_flips': 1}, io.scene()['targets'][0])
        self.assertEqual(r['status'], 'unreadable'); self.assertEqual(io.p, [])

    def test_unreadable_can_be_refused(self):
        io = FakeIO([(0, [NF])])
        with self.assertRaises(PackageError): run_cycle(io, {'push_unreadable': False}, io.scene()['targets'][0])
        self.assertFalse([l for l in io.log if ':push_left:' in l])

    def test_blurry_never_flips(self):
        io = FakeIO([(0, [BL])])
        r = run_cycle(io, {}, io.scene()['targets'][0])
        self.assertEqual(r['status'], 'unreadable')
        self.assertFalse([l for l in io.log if l.startswith('flip')])


class SessionTests(unittest.TestCase):
    def test_clears_whole_table_leftmost_first(self):
        io = FakeIO([(.1, [OK]), (-.2, [NF, NF, OK]), (0, [OK]), (.2, [NF])])
        r = run_session(io, {'max_flips': 1})
        self.assertEqual(io.p, [])
        self.assertEqual(r['stopped_because'], 'cleared')
        self.assertEqual(r['done'] + r['unreadable'], 4); self.assertEqual(r['unreadable'], 1)
        order = [l.split(':')[2] for l in io.log if l.startswith('plan:push_left')]
        self.assertEqual(order[0], '1')   # x=-.2 最靠左，最先推

    def test_empty_table_ends_immediately(self):
        r = run_session(FakeIO([]), {})
        self.assertEqual(r['stopped_because'], 'cleared'); self.assertEqual(r['done'], 0)

    def test_stuck_package_does_not_stall_others(self):
        io = FakeIO([(0, [OK]), (.3, [OK])], jam=['0'])
        r = run_session(io, {})
        self.assertEqual(r['done'], 1); self.assertEqual(len(r['failed']), 1)
        self.assertEqual(r['stopped_because'], 'only_failed_remain')   # 不无限重试，明确报告

    def test_temporarily_unlocatable_is_retried_then_reported(self):
        io = FakeIO([(0, [OK])]); io.hidden = 2
        self.assertEqual(run_session(io, {})['done'], 1)
        io = FakeIO([(0, [OK])]); io.hidden = 9
        self.assertEqual(run_session(io, {})['stopped_because'], 'unlocatable')

    def test_max_packages_bound(self):
        io = FakeIO([(i * .01, [OK]) for i in range(5)])
        r = run_session(io, {}, max_packages=2)
        self.assertEqual(r['stopped_because'], 'max_packages'); self.assertEqual(len(io.p), 3)

    def test_retreat_optional(self):
        io = FakeIO([(0, [OK])]); run_session(io, {'retreat_to_start': True})
        self.assertIn('retreat', io.log)


class ConfigTests(unittest.TestCase):
    def test_route_roles(self):
        skills = [{'name': 'a', 'class_name': 'box'}, {'name': 'f', 'class_name': 'box', 'role': 'flip'}]
        self.assertEqual(route({'name': 'box'}, skills)['name'], 'a')
        self.assertEqual(route({'name': 'box'}, skills, 'flip')['name'], 'f')
        with self.assertRaises(SortingError): route({'name': 'bag'}, skills, 'flip')

    def test_cycle_settings_validation(self):
        validate_settings({'cycle': {'left_direction': [-1, 0, 0], 'left_push_m': .1, 'arm': 'auto',
                                     'wrist_cameras': {'left': '/dev/video0', 'right': '/dev/video2'}}})
        for bad in ({'left_direction': [0, 0, 1]}, {'left_push_m': 1}, {'arm': 'x'}, {'max_flips': 9},
                    {'wrist_cameras': {'mid': 'x'}}, {'wrist_cameras': 'bad'}):
            with self.assertRaises(SortingError): validate_settings({'cycle': bad})


if __name__ == '__main__':
    unittest.main()
