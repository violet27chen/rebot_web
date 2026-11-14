import copy
import json
import tempfile
import unittest
from pathlib import Path
import backend.main as main


class PolicyCameraMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.cfg=copy.deepcopy(main.DEFAULT_CONFIG)
        self.cfg['dual_arm'].update(enabled=True,robot_type='bi_seeed_b601_dm_follower')
        self.cfg['cameras']=[{'name':name,'index_or_path':source} for name,source in [('左','/dev/qa-left'),('深度相机','/dev/qa-depth'),('右','/dev/qa-right')]]
    def tearDown(self):self.temp.cleanup()
    def model(self,names):
        (self.path/'config.json').write_text(json.dumps({'input_features':{f'observation.images.{name}':{'type':'VISUAL','shape':[3,480,640]} for name in names}}))
        return str(self.path)
    def test_compound_camera_prefix_is_applied_exactly_once(self):
        cameras=main.eval_policy_cameras(self.cfg,self.model(['left_左','left_深度相机','left_右']))
        self.assertEqual([c['name'] for c in cameras],['左','深度相机','右'])
        self.assertEqual([c['index_or_path'] for c in cameras],['/dev/qa-left','/dev/qa-depth','/dev/qa-right'])
    def test_single_arm_can_preserve_compound_checkpoint_key(self):
        self.cfg['dual_arm']['enabled']=False
        cameras=main.eval_policy_cameras(self.cfg,self.model(['left_左']))
        self.assertEqual(cameras[0]['name'],'left_左')
        self.assertEqual(cameras[0]['index_or_path'],'/dev/qa-left')
    def test_ambiguous_names_fail_without_guessing_a_device(self):
        self.cfg['cameras'].append({'name':'左','index_or_path':'/dev/qa-other'})
        with self.assertRaisesRegex(main.ApiError,'唯一匹配'):
            main.eval_policy_cameras(self.cfg,self.model(['left_左']))
    def test_numeric_zero_camera_can_be_prefixed(self):
        self.cfg['cameras']=[{'name':'top','index_or_path':0}]
        cameras=main.eval_policy_cameras(self.cfg,self.model(['left_cam0']))
        self.assertEqual(cameras[0]['name'],'cam0')


if __name__=='__main__':unittest.main()
