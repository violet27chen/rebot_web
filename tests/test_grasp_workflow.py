import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import backend.main as main


class GraspTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.file=Path(self.temp.name)/'config.json'
        cfg=copy.deepcopy(main.DEFAULT_CONFIG);cfg['dual_arm']['enabled']=True
        cfg['dual_arm']['left']['follower_port']='/dev/qa-left';cfg['dual_arm']['right']['follower_port']='/dev/qa-right'
        self.file.write_text(json.dumps(cfg));self.config=patch.object(main,'CONFIG_FILE',self.file);self.config.start();self.client=TestClient(main.app)
        self.worker=patch.object(main,'_grasp_worker',return_value={'ok':True,'xyz':[.2,.1,.01],'joints':[0]*6,'error':.0001});self.mockworker=self.worker.start()
        self.feedback=patch.object(main,'_grasp_measured',return_value=[0]*6);self.feedback.start()
        main._GRASP_PLANS.clear();self.released=main.DEVICES_RELEASED;main.DEVICES_RELEASED=False
    def tearDown(self):
        main.DEVICES_RELEASED=self.released;self.feedback.stop();self.worker.stop();self.config.stop();self.temp.cleanup()
    def context(self,side='left'):return {'side':side,'camera_source':'/dev/v4l/by-path/qa-camera'}
    def calibrated(self,side='left'):
        key=main._grasp_context(self.context(side))[4]
        points=[dict(u=u,v=v,x=u/1000,y=v/1000,z=.01) for u,v in [(0,0),(100,0),(100,100),(0,100)]]
        main._grasp_save_profile(key,dict(points=points,H=[[.001,0,0],[0,.001,0],[0,0,1]],table_z=.01))
    def plan(self):return self.client.post('/api/grasp/plan',json=dict(self.context(),u=50,v=50))
    def test_calibration_is_separate_per_arm_and_camera(self):
        self.calibrated('left')
        self.assertTrue(self.client.get('/api/grasp/calibration',params=self.context()).json()['calibration']['H'])
        self.assertEqual(self.client.get('/api/grasp/calibration',params=self.context('right')).json()['calibration'],{})
        other=dict(self.context(),camera_source='/dev/video99')
        self.assertEqual(self.client.get('/api/grasp/calibration',params=other).json()['calibration'],{})
    def test_degenerate_calibration_rejected(self):
        points=[dict(u=i,v=i,x=i,y=i,z=0) for i in range(4)]
        with self.assertRaises(main.ApiError):main._homography(points)
    def test_nan_pixel_rejected_without_worker(self):
        with self.assertRaises(main.ApiError):main._grasp_values({'u':'nan','v':2},('u','v'))
        self.mockworker.assert_not_called()
    def test_outside_calibrated_area_rejected(self):
        self.calibrated();response=self.client.post('/api/grasp/plan',json=dict(self.context(),u=101,v=50))
        self.assertEqual(response.status_code,400);self.mockworker.assert_not_called()
    def test_plan_checks_both_grasp_and_approach_without_serial(self):
        self.calibrated();response=self.plan();self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.mockworker.call_count,2)
        self.assertTrue(all(call.args[0]=='ik' for call in self.mockworker.call_args_list))
        self.assertIn('plan_id',response.json());self.assertEqual(response.json()['side'],'left')
    def test_parameter_change_invalidates_old_plan(self):
        self.calibrated();plan=self.plan().json();main.save_config({'grasp':{'approach':.12}})
        with patch.object(main,'_spawn') as spawn:
            response=self.client.post('/api/grasp/execute',json={'confirm':True,'plan_id':plan['plan_id']})
            self.assertEqual(response.status_code,409);spawn.assert_not_called()
    def test_execute_cannot_use_client_coordinates_or_missing_confirmation(self):
        self.assertEqual(self.client.post('/api/grasp/execute',json={'x':.2,'y':0,'z':0,'confirm':True}).status_code,409)
        self.assertEqual(self.client.post('/api/grasp/execute',json={'confirm':'true'}).status_code,400)
    def test_recording_new_calibration_point_invalidates_old_H(self):
        self.calibrated();response=self.client.post('/api/grasp/calibrate/point',json=dict(self.context(),u=30,v=60))
        self.assertEqual(response.status_code,200,response.text)
        self.assertIsNone(main._grasp_context(self.context())[5]['H'])
    def test_offline_stable_camera_configuration_can_be_saved(self):
        camera=dict(name='offline',type='opencv',index_or_path='/dev/v4l/by-id/usb-qa-offline-video-index0',width=640,height=480,fps=30)
        response=self.client.post('/api/config',json={'cameras':[camera]})
        self.assertEqual(response.status_code,200,response.text)
