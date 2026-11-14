import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import backend.main as main


class WorkflowConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'config.json'
        self.path.write_text(json.dumps(main.DEFAULT_CONFIG))
        self.file_patch = patch.object(main, 'CONFIG_FILE', self.path)
        self.file_patch.start()
        self.client = TestClient(main.app)
        self.flags = (main.ENV.connected, main.ENV.dual_connected, dict(main.ENV.dual_ports))

    def tearDown(self):
        main.ENV.connected, main.ENV.dual_connected, main.ENV.dual_ports = self.flags
        self.file_patch.stop()
        self.directory.cleanup()

    def test_parallel_partial_autosaves_preserve_all_fields(self):
        with ThreadPoolExecutor(12) as pool:
            list(pool.map(lambda n: main.save_config({'test_parallel': {str(n): n}}), range(40)))
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['test_parallel'], {str(n): n for n in range(40)})

    def test_noop_write_keeps_revision_and_does_not_publish(self):
        before = self.path.stat().st_mtime_ns
        with patch.object(main.HUB, 'publish') as publish:
            main.save_config({})
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        publish.assert_not_called()

    def test_changed_config_publishes_revision_without_values(self):
        with patch.object(main.HUB, 'publish') as publish:
            main.save_config({'llm': {'api_key': 'test-only-value'}})
        event = publish.call_args.args[0]
        self.assertEqual(event['type'], 'config')
        self.assertEqual(event['sections'], ['llm'])
        self.assertNotIn('test-only-value', json.dumps(event))
        self.assertEqual(event['revision'], self.client.get('/api/config/revision').json()['revision'])

    def test_failed_replace_preserves_previous_file(self):
        before = self.path.read_bytes()
        with patch.object(main.os, 'replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(main.ApiError):
                main.save_config({'dataset': {'single_task': 'should not replace'}})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('.config-*')), [])

    def test_old_dual_page_redirects_to_devices(self):
        result = self.client.get('/p/dual', follow_redirects=False)
        self.assertEqual(result.status_code, 307)
        self.assertEqual(result.headers['location'], '/p/connect')

    def test_generic_preview_supports_both_modes(self):
        for enabled in (False, True):
            main.save_config({'dual_arm': {'enabled': enabled}})
            with patch.object(main, 'build_command', return_value=['verified-command']):
                for kind in ('teleoperate', 'record', 'train', 'eval'):
                    response = self.client.get('/api/commands/preview', params={'kind': kind})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['enabled'], enabled)
                    self.assertIn('verified-command', response.json()['command'])

    def test_idempotent_mode_update_preserves_connection(self):
        main.save_config({'dual_arm': {'enabled': True}})
        main.ENV.dual_connected = True
        response = self.client.post('/api/dual/mode', json={'enabled': True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(main.ENV.dual_connected)

    def test_busy_task_rejects_mode_and_device_edits(self):
        main.save_config({'dual_arm': {'enabled': False}})
        before = self.path.read_bytes()
        with patch.object(main.STATE, 'running', return_value=True):
            self.assertEqual(self.client.post('/api/dual/mode', json={'enabled': True}).status_code, 409)
            self.assertEqual(self.client.post('/api/config', json={'leader': {'id': 'changed'}}).status_code, 409)
        self.assertEqual(self.path.read_bytes(), before)

    def test_device_edit_invalidates_stale_connection(self):
        main.ENV.connected = main.ENV.dual_connected = True
        response = self.client.post('/api/config', json={'leader': {'port': '/dev/qa-port'}})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(main.ENV.connected)
        self.assertFalse(main.ENV.dual_connected)

    def test_camera_name_edit_preserves_capture_sessions(self):
        camera = {'name':'top','type':'opencv','index_or_path':2,'width':640,'height':480,'fps':30,'fourcc':'MJPG'}
        main.save_config({'cameras':[camera]})
        with patch.object(main.CAMERAS, 'close_devices') as close:
            response = self.client.post('/api/config', json={'cameras':[dict(camera, name='renamed')]})
            self.assertEqual(response.status_code, 200)
            close.assert_called_once_with(set())
        with patch.object(main.CAMERAS, 'close_devices') as close:
            response = self.client.post('/api/config', json={'cameras':[dict(camera, name='renamed',width=800)]})
            self.assertEqual(response.status_code, 200)
            close.assert_called_once_with({'/dev/video2'})

    def test_dataset_features_on_training_endpoint(self):
        root = Path(self.directory.name) / 'dataset'
        (root / 'meta').mkdir(parents=True)
        (root / 'meta/info.json').write_text(json.dumps({'features':{
            'action':{'shape':[14]}, 'observation.state':{'shape':[14]},
            'observation.images.top':{'shape':[480,640,3]}}}))
        response = self.client.get('/api/datasets/features', params={'dataset_dir':str(root)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['action_dim'], 14)
        self.assertEqual(response.json()['cameras'], ['top'])


if __name__ == '__main__':
    unittest.main()
