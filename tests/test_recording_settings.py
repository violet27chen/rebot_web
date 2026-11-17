import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import backend.main as main
from backend.recording_settings import recording_cameras, recording_options


class RecordingSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.cfg=copy.deepcopy(main.DEFAULT_CONFIG)
        self.cfg['cameras']=[{'name':name,'index_or_path':n,'width':1920,'height':1080,'fps':30} for name,n in [('左',0),('顶部',4),('右',6)]]
    def test_only_recording_outputs_resize_and_all_three_views_remain(self):
        cams=recording_cameras(self.cfg,self.root)
        self.assertEqual(len(cams),3)
        self.assertTrue(all((c['width'],c['height'],c['_resize_to_config'])==(640,360,True) for c in cams))
        self.assertTrue(all(c['width']==1920 for c in self.cfg['cameras']))
        cmd=main.build_command('record',self.cfg)
        self.assertIn('--dataset.vcodec=h264',cmd)
        self.assertIn('--dataset.video_encoding_batch_size=1',cmd)
        self.assertTrue(any('resize_to_config: true' in arg for arg in cmd))
    def test_resume_preserves_dataset_image_sizes(self):
        self.cfg['dataset'].update(resume=True,repo_id='qa/demo')
        self.cfg['dual_arm'].update(enabled=True,robot_type='bi_seeed_b601_dm_follower')
        info=self.root/'qa/demo/meta/info.json';info.parent.mkdir(parents=True)
        features={'observation.images.left_'+c['name']:{'shape':[720,1280,3]} for c in self.cfg['cameras']}
        info.write_text(json.dumps({'features':features}))
        cams=recording_cameras(self.cfg,self.root)
        self.assertTrue(all((c['width'],c['height'])==(1280,720) for c in cams))
        self.assertEqual(self.cfg['cameras'][0]['width'],1920)
        info.unlink()
        with self.assertRaisesRegex(ValueError,'续录'):recording_cameras(self.cfg,self.root)
    def test_storage_reports_video_and_unencoded_frames_separately(self):
        (self.root/'videos').mkdir();(self.root/'images').mkdir()
        (self.root/'videos/test.mp4').write_bytes(b'x'*1048576)
        (self.root/'images/frame.png').write_bytes(b'x'*2097152)
        storage=main._dataset_storage(self.root)
        self.assertEqual(storage['total_mb'],3);self.assertEqual(storage['videos_mb'],1);self.assertEqual(storage['images_mb'],2)

    def test_resume_keeps_original_codec_and_fps_instead_of_mixing_av1_h264(self):
        self.cfg['dataset'].update(resume=True,repo_id='qa/demo')
        info=self.root/'qa/demo/meta/info.json';info.parent.mkdir(parents=True)
        info.write_text(json.dumps({'fps':20,'features':{'observation.images.left':{'dtype':'video','info':{'video.codec':'av1'}}}}))
        self.assertEqual(recording_options(self.cfg,self.root),('libsvtav1',20))
        info.write_text(json.dumps({'fps':20,'features':{'observation.images.left':{'dtype':'video'}}}))
        with self.assertRaisesRegex(ValueError,'编码'):recording_options(self.cfg,self.root)


if __name__=='__main__':unittest.main()
