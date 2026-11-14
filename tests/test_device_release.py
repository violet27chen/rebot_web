"""Release a disposable external process on a pseudoterminal, never robot hardware."""
import os
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from backend.device_release import device_paths, kill_device_owners, owners


class ReleaseTests(unittest.TestCase):
    def test_kills_external_owner_and_leaves_unrelated_process_alive(self):
        master, slave = os.openpty(); device = os.ttyname(slave)
        child = subprocess.Popen([sys.executable,'-u','-c',
            'import os,sys,time; f=os.open(sys.argv[1],os.O_RDWR);print("ready",flush=True);time.sleep(60)',device],stdout=subprocess.PIPE,text=True)
        unrelated = subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
        try:
            self.assertEqual(child.stdout.readline().strip(),'ready'); os.close(slave); slave=None
            self.assertIn(child.pid,[p['pid'] for p in owners([device])])
            report=kill_device_owners([device]);child.wait(timeout=3)
            self.assertEqual(child.returncode,-signal.SIGKILL)
            self.assertEqual(report['remaining'],[])
            self.assertEqual([p['pid'] for p in report['killed']],[child.pid])
            self.assertIsNone(unrelated.poll())
            self.assertEqual(kill_device_owners([device])['killed'],[])
        finally:
            if slave is not None:os.close(slave)
            os.close(master)
            if child.poll() is None:child.kill()
            child.wait(); child.stdout.close(); unrelated.kill();unrelated.wait()

    def test_protects_service_itself(self):
        master,slave=os.openpty()
        try:
            report=kill_device_owners([os.ttyname(slave)],timeout=.01)
            self.assertIn(os.getpid(),[p['pid'] for p in report['remaining']])
            self.assertTrue(report['errors'])
        finally:os.close(master);os.close(slave)

    def test_config_cannot_target_arbitrary_files_or_keyboard_usb(self):
        with patch('backend.device_release.glob.glob',return_value=[]),patch('backend.device_release.Path.glob',return_value=[]):
            result=device_paths({'follower':{'port':'/etc/passwd'},'cameras':[{'index_or_path':'/home/secret'}]})
        self.assertEqual(result,[])

class ReleaseEndpointTests(unittest.TestCase):
    def test_stops_external_owners_persists_pause_and_never_homes(self):
        import json
        import tempfile
        from pathlib import Path
        from fastapi.testclient import TestClient
        import backend.main as main
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'config.json';file.write_text(json.dumps(main.DEFAULT_CONFIG))
            paused=main.DEVICES_RELEASED;telemetry_paused=main.TELEMETRY.paused
            flags=(main.ENV.connected,main.ENV.dual_connected,main.ENV.dual_ports)
            report={'devices':['/dev/qa'],'killed':[{'pid':123456,'name':'external','devices':['/dev/qa']}],'remaining':[],'errors':[]}
            try:
                with patch.object(main,'CONFIG_FILE',file),patch.object(main,'device_paths',return_value=['/dev/qa']),patch.object(main,'kill_device_owners',return_value=report) as kill,patch.object(main,'stop_all') as stop,patch.object(main.TELEMETRY,'stop_observers'),patch.object(main.DEPTH,'close'),patch.object(main.CAMERAS,'close_all'),patch.object(main,'_ARM_PROC',None),patch.object(main,'_arm_send') as motion:
                    response=TestClient(main.app).post('/api/devices/disconnect-all')
                    self.assertEqual(response.status_code,200,response.text);self.assertTrue(response.json()['released'])
                    self.assertEqual(response.json()['killed'][0]['pid'],123456)
                    self.assertTrue(main.load_config()['device_session']['paused']);self.assertTrue(main.DEVICES_RELEASED)
                    stop.assert_called_once_with(force=True);motion.assert_not_called();self.assertEqual(kill.call_count,2)
                    self.assertEqual(TestClient(main.app).get('/api/camera/snapshot?device=2').status_code,409)
                    self.assertEqual(TestClient(main.app).post('/api/devices/resume').status_code,200)
                    self.assertFalse(main.DEVICES_RELEASED)
            finally:
                main.DEVICES_RELEASED=paused;main.TELEMETRY.paused=telemetry_paused
                main.ENV.connected,main.ENV.dual_connected,main.ENV.dual_ports=flags

class DeletedDeviceTests(unittest.TestCase):
    def test_unplugged_configured_serial_fd_is_still_reported(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            entry=Path(directory)/'123456';(entry/'fd').mkdir(parents=True)
            (entry/'fd/7').symlink_to('/dev/null')
            (entry/'stat').write_text(Path('/proc/self/stat').read_text())
            (entry/'comm').write_text('detached-device-reader')
            with patch('pathlib.Path.readlink',return_value=Path('/dev/ttyUSB999 (deleted)')):
                result=owners(['/dev/ttyUSB999'],proc_root=directory)
            self.assertEqual(result[0]['devices'],['/dev/ttyUSB999'])
            self.assertEqual(result[0]['pid'],123456)

    def test_mapping_only_owner_is_reported_without_open_fd(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            entry=Path(directory)/'123456';(entry/'fd').mkdir(parents=True)
            (entry/'stat').write_text(Path('/proc/self/stat').read_text())
            (entry/'comm').write_text('mapped-camera-reader')
            (entry/'maps').write_text('0001-1000 rw-s 00000000 00:06 1234 /dev/video999 (deleted)\n')
            result=owners(['/dev/video999'],proc_root=directory)
            self.assertEqual(result[0]['devices'],['/dev/video999'])
