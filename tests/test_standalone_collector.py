"""Single-file distribution and hardware-free native configuration checks."""
import importlib.util
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/'data_collector/collect.py'
spec=importlib.util.spec_from_file_location('standalone_collector_test',SOURCE)
collector=importlib.util.module_from_spec(spec);spec.loader.exec_module(collector)


class DistributionTests(unittest.TestCase):
    def test_copied_file_runs_without_project_imports_or_working_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'collect.py';shutil.copy2(SOURCE,file)
            command=[sys.executable,str(file),'--repo-id','qa/standalone','--root',str(Path(folder)/'data')]
            result=subprocess.run(command+['--dry-run'],cwd=folder,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            config=json.loads(result.stdout);self.assertEqual(len(config['cameras']),3)
            self.assertEqual(config['size'],[640,360]);self.assertEqual(config['codec'],'h264')
            self.assertFalse((Path(folder)/'data').exists())
            result=subprocess.run(command+['--check'],cwd=folder,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr);self.assertIn('未打开任何设备',result.stdout)
    def test_resume_preserves_original_camera_shape_codec_and_fps(self):
        with tempfile.TemporaryDirectory() as folder:
            info=Path(folder)/'meta/info.json';info.parent.mkdir()
            features={'observation.images.left_'+name:{'shape':[720,1280,3],'info':{'video.codec':'av1'}} for name,_ in collector.DEFAULT_CAMERAS}
            info.write_text(json.dumps({'fps':20,'features':features}))
            args=collector.arguments(['--repo-id','qa/resume','--root',folder,'--resume','--dry-run'])
            self.assertEqual(args.size,(1280,720));self.assertEqual(args.fps,20);self.assertEqual(args.codec,'libsvtav1')


class NativeAdapterTests(unittest.TestCase):
    def test_native_record_receives_four_correct_ports_and_three_shared_rgb_frames(self):
        import numpy as np
        native = collector.recording_module()
        from lerobot.datasets import lerobot_dataset
        from lerobot_teleoperator_rebot_arm_102 import BiRebotArm102Leader
        with tempfile.TemporaryDirectory() as folder:
            args=collector.arguments(['--repo-id','qa/adapter','--root',str(Path(folder)/'data'),'--dry-run'])
            service=collector.CaptureService(args)
            service.process=NS(is_alive=lambda:True)
            for i,node in enumerate(service.nodes):
                with node['condition']:
                    np.frombuffer(node['pixels'],np.uint8)[:]=i+7
                    node['sequence'].value=1;node['stamp'].value=time.monotonic()
            checked=[]
            def native_record(cfg):
                self.assertFalse(cfg.display_data);self.assertFalse(cfg.play_sounds)
                self.assertEqual(cfg.dataset.vcodec,'h264');self.assertEqual(cfg.dataset.num_image_writer_threads_per_camera,2)
                robot=native.make_robot_from_config(cfg.robot)
                self.assertEqual(len(robot.action_features),14)
                self.assertEqual(robot.left_arm.config.port,'/dev/ttyACM0')
                self.assertEqual(robot.right_arm.config.port,'/dev/ttyACM1')
                self.assertEqual(set(robot.left_arm.cameras),{n for n,_ in collector.DEFAULT_CAMERAS})
                for i,camera in enumerate(robot.left_arm.cameras.values()):
                    camera.connect();frame=camera.async_read(timeout_ms=1)
                    self.assertEqual(frame.shape,(360,640,3));self.assertTrue(np.all(frame==i+7));camera.disconnect()
                checked.append(True)
            try:
                with patch.object(native,'make_robot_from_config'),patch.object(native,'make_teleoperator_from_config'),patch.object(native,'init_keyboard_listener'),patch.object(lerobot_dataset.LeRobotDataset,'save_episode'),patch.object(BiRebotArm102Leader,'connect'),patch.object(native,'record',side_effect=native_record):
                    collector.record(args,service)
                self.assertEqual(checked,[True]);self.assertFalse(service.ready.is_set())
            finally:service.process=None;service.close()


if __name__=='__main__':unittest.main()
