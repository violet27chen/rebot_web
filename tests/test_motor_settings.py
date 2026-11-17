import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import main
from backend.motor_settings_worker import (
    RID_ACC,
    RID_CTRL_MODE,
    RID_DEC,
    RID_MAX_SPD,
    apply_motor_settings,
)


class _FakeBus:
    def poll_feedback_once(self):
        return None


class _FakeMotor:
    def __init__(self, status_code=0, max_speed=5.0, acc=5.0, dec=-5.0, ctrl_mode=2):
        self.status_code = status_code
        self.f32 = {RID_MAX_SPD: float(max_speed), RID_ACC: float(acc), RID_DEC: float(dec)}
        self.u32 = {RID_CTRL_MODE: int(ctrl_mode)}
        self.stored = 0

    def request_feedback(self):
        return None

    def get_state(self):
        return SimpleNamespace(pos=0.0, vel=0.0, torq=0.0, status_code=self.status_code)

    def get_register_f32(self, rid, timeout_ms=1000):
        return self.f32[rid]

    def get_register_u32(self, rid, timeout_ms=1000):
        return self.u32[rid]

    def write_register_f32(self, rid, value):
        self.f32[rid] = float(value)

    def store_parameters(self):
        self.stored += 1


class MotorSettingsWorkerTests(unittest.TestCase):
    def test_read_returns_current_speed_and_accel_without_mutating(self):
        bus = _FakeBus()
        motors = {i: _FakeMotor(status_code=1, max_speed=4.0 + i, acc=3.0, dec=-3.0) for i in range(1, 8)}
        res = apply_motor_settings(bus, motors, 'read', [1, 7], sleep=lambda _: None)
        self.assertTrue(res['ok'])
        self.assertEqual([j['id'] for j in res['joints']], [1, 7])
        self.assertEqual(res['joints'][0]['max_speed'], 5.0)
        self.assertEqual(res['joints'][1]['max_speed'], 11.0)
        self.assertEqual(motors[1].stored, 0)

    def test_write_temp_updates_registers_without_store(self):
        bus = _FakeBus()
        motors = {i: _FakeMotor(status_code=1) for i in range(1, 8)}
        res = apply_motor_settings(
            bus, motors, 'write', [1, 2], max_speed=8.5, acceleration=6.0, persist=False, sleep=lambda _: None
        )
        self.assertTrue(res['ok'])
        self.assertFalse(res['persist'])
        for i in (1, 2):
            self.assertEqual(motors[i].f32[RID_MAX_SPD], 8.5)
            self.assertEqual(motors[i].f32[RID_ACC], 6.0)
            self.assertEqual(motors[i].f32[RID_DEC], -6.0)
            self.assertEqual(motors[i].stored, 0)
        self.assertEqual(motors[3].f32[RID_MAX_SPD], 5.0)

    def test_write_persist_requires_disabled_and_stores_parameters(self):
        bus = _FakeBus()
        motors = {i: _FakeMotor(status_code=1) for i in range(1, 8)}
        with self.assertRaisesRegex(RuntimeError, '失能'):
            apply_motor_settings(
                bus, motors, 'write', [1], max_speed=6.0, acceleration=4.0, persist=True, sleep=lambda _: None
            )
        for m in motors.values():
            m.status_code = 0
        res = apply_motor_settings(
            bus, motors, 'write', [1, 3], max_speed=6.0, acceleration=4.0, deceleration=-3.5,
            persist=True, sleep=lambda _: None
        )
        self.assertTrue(res['persist'])
        self.assertEqual(motors[1].stored, 1)
        self.assertEqual(motors[3].stored, 1)
        self.assertEqual(motors[1].f32[RID_DEC], -3.5)

    def test_write_rejects_invalid_bounds(self):
        bus = _FakeBus()
        motors = {i: _FakeMotor(status_code=0) for i in range(1, 8)}
        with self.assertRaises(ValueError):
            apply_motor_settings(bus, motors, 'write', [1], max_speed=0, acceleration=4.0, sleep=lambda _: None)
        with self.assertRaises(ValueError):
            apply_motor_settings(bus, motors, 'write', [1], max_speed=4.0, acceleration=4.0, deceleration=1.0, sleep=lambda _: None)


class MotorSettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg_path = self.root / 'config.json'
        self.cfg = {
            'dual_arm': {'enabled': False},
            'follower': {'type': main.FOLLOWER_TYPE, 'port': '/dev/ttyACM0', 'id': 'f1', 'can_adapter': 'damiao'},
            'cameras': [],
        }
        self.patches = [
            patch.object(main, 'load_config', lambda: dict(self.cfg)),
            patch.object(main, 'DEVICES_RELEASED', False),
            patch.object(main, 'require_env', lambda: None),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_capabilities_endpoint(self):
        r = self.client.get('/api/devices/motor-settings/capabilities')
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['actions'], ['read', 'write'])
        self.assertTrue(data['worker_available'])

    def test_start_validation_and_spawn(self):
        # Missing confirm on write
        r = self.client.post('/api/devices/motor-settings/start', json={
            'side': 'left', 'action': 'write', 'joints': [1], 'max_speed': 5.0, 'acceleration': 5.0,
        })
        self.assertEqual(r.status_code, 400)

        # Invalid positive deceleration
        r = self.client.post('/api/devices/motor-settings/start', json={
            'side': 'left', 'action': 'write', 'joints': [1], 'max_speed': 5.0,
            'acceleration': 5.0, 'deceleration': 2.0, 'confirm': True,
        })
        self.assertEqual(r.status_code, 400)

        # Valid write spawns worker
        fake_job = SimpleNamespace(job_id='job-m1', finished=SimpleNamespace(is_set=lambda: False), exit_code=None, result=None, last_line='')
        with patch.object(main.STATE, 'running', return_value=False), \
             patch.object(main.STATE, 'job', fake_job), \
             patch.object(main, '_py', return_value=['python']), \
             patch.object(main, '_spawn', return_value={'ok': True, 'job_id': 'job-m1'}) as spawn_mock, \
             patch('backend.device_release.owners', return_value=[]):
            r = self.client.post('/api/devices/motor-settings/start', json={
                'side': 'left', 'action': 'write', 'joints': [1, 2],
                'max_speed': 7.5, 'acceleration': 6.0, 'persist': False, 'confirm': True,
            })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['job_id'], 'job-m1')
        self.assertEqual(spawn_mock.call_args.args[0], 'motor-settings')


if __name__ == '__main__':
    unittest.main()
