"""Camera ownership and configuration regressions; no hardware is opened."""
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import cv2
import numpy as np

from backend.camera_manager import CameraError, CameraManager, source_key, validate_configs


class Capture:
    def __init__(self, source):
        self.source, self.props, self.released = source, {}, False
        self.read_count = 0

    def isOpened(self):
        return True

    def set(self, prop, value):
        self.props[prop] = value
        return True

    def get(self, prop):
        return self.props.get(prop, 0)

    def read(self):
        assert self.props[cv2.CAP_PROP_FOURCC] == cv2.VideoWriter_fourcc(*'MJPG')
        assert self.props[cv2.CAP_PROP_FPS] == 30
        self.read_count += 1
        return True, np.full((48, 64, 3), self.read_count % 255, dtype=np.uint8)

    def release(self):
        self.released = True


class CamerasTest(unittest.TestCase):
    def setUp(self):
        self.opened = []
        def create(source):
            c = Capture(source)
            self.opened.append(c)
            return c
        self.manager = CameraManager(create, idle_seconds=.15)

    def tearDown(self):
        self.manager.close_all()

    def test_three_devices_stream_simultaneously(self):
        leases = [self.manager.acquire(n) for n in [2, 4, 6]]
        with ThreadPoolExecutor(3) as pool:
            frames = list(pool.map(lambda lease: lease.jpeg(), leases))
        self.assertTrue(all(f.startswith(b'\xff\xd8') for f in frames))
        self.assertEqual(len(self.opened), 3)
        self.assertTrue(all(s['frames'] for s in self.manager.status()['sessions']))
        for lease in leases:
            lease.release()

    def test_two_tabs_and_snapshot_share_one_physical_capture(self):
        a = self.manager.acquire(2)
        b = self.manager.acquire('/dev/video2')
        snapshot = self.manager.acquire('2')
        self.assertEqual(a.jpeg(), snapshot.jpeg())
        self.assertEqual(len(self.opened), 1)
        a.release(); a.release()  # release is idempotent
        self.assertEqual(self.manager.status()['sessions'][0]['clients'], 2)
        self.assertTrue(b.jpeg(fresh=True))
        b.release(); snapshot.release()

    def test_stable_alias_resolves_to_existing_capture(self):
        with patch('backend.camera_manager.os.path.realpath', return_value='/dev/video4'):
            a = self.manager.acquire('/dev/v4l/by-path/camera')
            b = self.manager.acquire(4)
            a.jpeg(); b.jpeg()
        self.assertEqual(len(self.opened), 1)
        a.release(); b.release()

    def test_last_client_releases_device_and_reconnects(self):
        a = self.manager.acquire(2); a.jpeg(); a.release()
        time.sleep(.4)
        self.assertTrue(self.opened[0].released)
        b = self.manager.acquire(2); b.jpeg(); b.release()
        self.assertEqual(len(self.opened), 2)

    def test_handoff_closes_preview_and_rejects_new_clients(self):
        a = self.manager.acquire(2); a.jpeg()
        self.manager.close_all(exclusive=True)
        self.assertTrue(self.opened[0].released)
        with self.assertRaises(CameraError):
            self.manager.acquire(4)
        with self.assertRaises(CameraError):
            a.jpeg()
        a.release()

    def test_capture_failure_propagates_and_releases_device(self):
        class Broken(Capture):
            def read(self):
                return False, None
        self.manager.factory = Broken
        a = self.manager.acquire(4)
        with self.assertRaisesRegex(CameraError, 'USB'):
            a.jpeg()
        a.release()
        self.manager.close_all()

    def test_first_frame_wait_has_bounded_timeout(self):
        class Slow(Capture):
            def read(self):
                time.sleep(.2)
                return super().read()
        self.manager.factory = Slow
        a = self.manager.acquire(2)
        with self.assertRaisesRegex(CameraError, '超时'):
            a.session.wait(timeout=.01)
        a.release()

    def test_invalid_source_is_never_silently_camera_zero(self):
        for source in ['', 'bad-camera', '-1', 'http://example.com', '/etc/passwd', '/dev/video1x']:
            with self.subTest(source=source), self.assertRaises(CameraError):
                source_key(source)

    def test_duplicate_source_alias_and_bad_config_rejected(self):
        c = {'name': 'top', 'index_or_path': 4, 'width': 640, 'height': 480, 'fps': 30}
        validate_configs([c])
        with self.assertRaises(CameraError):
            validate_configs([c, dict(c, name='wrist', index_or_path='/dev/video4')])
        for patch_config in [{'name': ''}, {'fps': 0}, {'fourcc': 'BAD'}, {'width': True}]:
            with self.subTest(config=patch_config), self.assertRaises(CameraError):
                validate_configs([dict(c, **patch_config)])


if __name__ == '__main__':
    unittest.main()
