"""RGB-D geometry, skill compatibility and API safety, without physical devices."""
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.sorting.geometry import (SortingError,apply,unproject,rigid_fit,align_fit,locate,
                                      validate_calibration,verify_push,fingerprint)
from backend.sorting.skills import NAMES,DIRECTIONS,route,inspect_skill,action_to_raw,raw_observation
from backend.sorting.control_worker import bounded_target
from backend.sorting.service import validate_settings
from backend.sorting.api import attach


def calibration():
    K=[[80,0,40],[0,80,30],[0,0,1]]
    intrinsic={'K':K,'width':80,'height':60,'distortion':[0]*5}
    T=np.diag([1.,-1.,-1.,1.]);T[2,3]=1.
    return dict(mount='fixed',camera_source='2',depth_uri='qa-depth',depth=intrinsic,rgb=intrinsic,
                rgb_from_depth=np.eye(4).tolist(),workspace_from_depth=T.tolist(),table_plane=[0,0,1,0],
                workspace_bounds=[[-.6,.6],[-.5,.5],[0,.6]],alignment_verified=True,workspace_verified=True,
                arms={side:{'base_from_workspace':np.eye(4).tolist(),'verified':True} for side in ('left','right')})


class GeometryTests(unittest.TestCase):
    def test_depth_projection_round_trip(self):
        pixels=np.array([[0.,0],[40,30],[60,20]])
        xyz=unproject(pixels,[.7,.8,.9],calibration()['depth'])
        uv=cv2.projectPoints(xyz,np.zeros(3),np.zeros(3),np.asarray(calibration()['rgb']['K'],float),np.zeros(5))[0].reshape(-1,2)
        np.testing.assert_allclose(pixels,uv,atol=1e-8)
    def test_rigid_fit_preserves_real_scale_and_handedness(self):
        rng=np.random.default_rng(42);points=rng.uniform(-.2,.2,(15,3));R=cv2.Rodrigues(np.array([.1,.2,-.3]))[0]
        T=np.eye(4);T[:3,:3]=R;T[:3,3]=[.1,-.2,.7]
        fit=rigid_fit(points,apply(T,points));np.testing.assert_allclose(fit['matrix'],T,atol=1e-10)
        with self.assertRaises(SortingError):rigid_fit(points,apply(T,points)*1000)
        with self.assertRaises(SortingError):rigid_fit(points,points*[-1,1,1])
    def test_plane_only_points_rejected_for_3d_registration(self):
        a=np.array([[x,y,0] for x in [0,.1,.2] for y in [0,.1]])
        with self.assertRaisesRegex(SortingError,'不同高度'):rigid_fit(a,a)
    def test_rgb_depth_alignment_uses_extrinsics(self):
        rng=np.random.default_rng(9);xyz=rng.uniform([-.2,-.15,.5],[.2,.15,1.],(25,3))
        r=np.array([.02,.03,-.01]);t=np.array([.03,.005,.001]);K=np.asarray(calibration()['rgb']['K'],float)
        uv=cv2.projectPoints(xyz,r,t,K,np.zeros(5))[0].reshape(-1,2)
        fit=align_fit(xyz,uv,calibration()['rgb']);expected=np.eye(4);expected[:3,:3]=cv2.Rodrigues(r)[0];expected[:3,3]=t
        np.testing.assert_allclose(fit['matrix'],expected,atol=1e-6)
        with self.assertRaises(SortingError):align_fit([],[],calibration()['rgb'])
    def test_height_is_table_relative_and_zeros_do_not_become_objects(self):
        depth=np.full((60,80),1000,np.uint16);depth[20:40,30:50]=800;depth[25:28,35:38]=0
        targets=locate(depth,(60,80,3),[dict(name='box',conf=.9,xyxy=[27,17,53,43])],calibration())
        self.assertTrue(targets[0]['valid']);self.assertAlmostEqual(targets[0]['height_m'],.2,places=5)
        self.assertGreater(targets[0]['points'],100)
        empty=locate(np.zeros_like(depth),(60,80,3),[dict(name='bag',xyxy=[10,10,50,50])],calibration())
        self.assertFalse(empty[0]['valid'])
    def test_resolution_pseudocolor_or_unverified_calibration_cannot_localize(self):
        for depth in (np.zeros((59,80),np.uint16),np.zeros((60,80),np.uint8)):
            with self.assertRaises(SortingError):locate(depth,(60,80,3),[],calibration())
        c=calibration();c['alignment_verified']=False
        with self.assertRaises(SortingError):validate_calibration(c)
    def test_push_verification_requires_region_and_same_class(self):
        before=dict(name='box',valid=True,xyz_m=[.4,0,.1],height_m=.1)
        after=dict(before,xyz_m=[.2,0,.1]);region=[[.1,.3],[-.1,.1],[.05,.2]]
        self.assertTrue(verify_push(before,after,region)['reached'])
        self.assertFalse(verify_push(before,before,region)['reached'])
        with self.assertRaises(SortingError):verify_push(before,dict(after,name='bag'),region)
    def test_fingerprint_changes_when_intrinsics_or_arm_frame_changes(self):
        c=calibration();old=fingerprint(c);c['arms']['left']['base_from_workspace'][0][3]=.5
        self.assertNotEqual(old,fingerprint(c))


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.model=self.root/'model';self.model.mkdir();(self.model/'model.safetensors').touch()
        self.config={'type':'act','input_features':{'observation.state':{'shape':[14]},'observation.images.top':{'shape':[3,60,80]}},'output_features':{'action':{'shape':[14]}}}
        (self.model/'config.json').write_text(json.dumps(self.config))
        (self.root/'info.json').write_text(json.dumps({'features':{'observation.state':{'names':NAMES},'action':{'names':NAMES}}}))
        self.entry=dict(name='flip_box',class_name='box',checkpoint='model',dataset_info='info.json',calibration_id='qa',training_bounds=[[0,.4],[-.2,.2],[.01,.3]],start_pose_rad=[0]*14,camera_map={'observation.images.top':'2'},task='flip box')
    def tearDown(self):self.temp.cleanup()
    def test_route_never_selects_arbitrary_checkpoint(self):
        self.assertEqual(route({'name':'box'},[self.entry])['name'],'flip_box')
        for skills in ([],[self.entry,self.entry],[dict(self.entry,enabled=False)]):
            with self.assertRaises(SortingError):route({'name':'box'},skills)
    def test_14_dimension_names_and_calibration_are_checked(self):
        self.assertEqual(inspect_skill(self.entry,self.root,'qa')['action_names'],NAMES)
        with self.assertRaisesRegex(SortingError,'标定版本'):inspect_skill(self.entry,self.root,'other')
        self.config['output_features']['action']['shape']=[7];(self.model/'config.json').write_text(json.dumps(self.config))
        with self.assertRaisesRegex(SortingError,'14 维'):inspect_skill(self.entry,self.root,'qa')
    def test_camera_map_and_action_order_cannot_be_guessed(self):
        with self.assertRaises(SortingError):inspect_skill(dict(self.entry,camera_map={}),self.root,'qa')
        names=NAMES[::-1];act=np.arange(14,dtype=float);raw=action_to_raw(act,names)
        np.testing.assert_allclose(raw,np.radians(act[::-1])*DIRECTIONS)
        obs=raw_observation(np.radians(np.arange(14)),names)
        np.testing.assert_allclose(obs,np.arange(14)[::-1])
    def test_target_jump_and_joint_limits_rejected_without_clipping(self):
        limits=[[-1,1]]*14
        np.testing.assert_allclose(bounded_target(np.zeros(14),[.1]*14,limits,.15),.1)
        with self.assertRaises(SortingError):bounded_target(np.zeros(14),[.2]*14,limits,.15)
        with self.assertRaises(SortingError):bounded_target(np.zeros(14),[float('nan')]*14,limits,.15)
        with self.assertRaises(SortingError):bounded_target(np.zeros(14),[.1]*14,[[0,0]]*14,.15)
    def test_partial_setup_autosaves_but_bad_torque_or_speed_does_not(self):
        validate_settings({'calibration':{'mount':'fixed'},'skills':[]})
        for value in ({'safety':{'torque_limits_nm':[1]*7}},{'safety':{'max_velocity_rad_s':5}},{'confidence':float('nan')}):
            with self.assertRaises(SortingError):validate_settings(value)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.cfg={};self.running=False;self.spawn=Mock(return_value={'ok':True,'job_id':'qa-job'})
        def save(p):
            def merge(a,b):
                for k,v in b.items():
                    if isinstance(v,dict):a[k]=merge(a.get(k,{}) or {},v)
                    else:a[k]=v
                return a
            merge(self.cfg,copy.deepcopy(p));return self.cfg
        hooks=SimpleNamespace(root=Path(__file__).resolve().parents[1],load_config=lambda:self.cfg,save_config=save,lock=threading.RLock(),
            version=lambda:'qa',running=lambda:self.running,require_connected=Mock(),spawn=self.spawn,python=lambda:'/qa/python',local_url=lambda:'http://127.0.0.1:8000')
        app=FastAPI();self.service=attach(app,hooks);self.client=TestClient(app)
    def tearDown(self):
        import shutil
        self.service.close();shutil.rmtree(self.service.folder)
    def test_unconfigured_status_is_actionable_without_opening_cameras(self):
        response=self.client.get('/api/sorting/status');self.assertEqual(response.status_code,200)
        self.assertFalse(response.json()['ready']);self.assertTrue(response.json()['errors'])
    def test_malformed_point_request_is_structured_not_500(self):
        response=self.client.post('/api/sorting/calibration/point',json={})
        self.assertEqual(response.status_code,400);self.assertIn('error',response.json())
    def test_execution_requires_confirmation_and_immutable_fresh_plan(self):
        self.assertEqual(self.client.post('/api/sorting/execute',json={}).status_code,400)
        self.assertEqual(self.client.post('/api/sorting/execute',json={'confirm':True,'plan_id':'missing'}).status_code,400)
        self.spawn.assert_not_called()
    def test_execute_creates_private_session_and_redacts_display(self):
        self.service.plans['p']={'created':time.monotonic(),'fingerprint':fingerprint(self.service.settings()),'initial_joints':[0]*14}
        with patch.object(self.service,'prepare_detector'):
            result=self.client.post('/api/sorting/execute',json={'confirm':True,'plan_id':'p'})
        self.assertEqual(result.status_code,200,result.text);args,kw=self.spawn.call_args
        payload=json.loads(args[2][-1]);self.assertEqual(payload['root'],str(self.service.h.root))
        self.assertNotIn(payload['token'],kw['display']);self.assertNotIn(payload['token'],result.text)
        self.running=True
        self.assertEqual(self.client.post('/api/sorting/heartbeat',json={'job_id':'wrong'}).status_code,400)
        self.assertEqual(self.client.post('/api/sorting/heartbeat',json={'job_id':'qa-job'}).status_code,200)
        self.assertGreater(float(self.service.session['heartbeat'].read_text()),0)
    def test_heartbeat_cannot_resurrect_a_finished_job(self):
        self.service.session={'job_id':'old'}
        self.assertEqual(self.client.post('/api/sorting/heartbeat',json={'job_id':'old'}).status_code,400)
    def test_confirmation_cannot_create_an_unsolved_transform(self):
        for kind in ('alignment','workspace','left','right'):
            self.assertEqual(self.client.post('/api/sorting/calibration/confirm',json={'confirm':True,'kind':kind}).status_code,400)
    def test_token_and_loopback_protect_private_control_calls(self):
        self.service.session={'token':'secret'};self.running=True
        with patch('backend.sorting.api.Worker') as worker:
            result=self.client.post('/api/sorting/internal',json={'token':'wrong','method':'init'})
        self.assertEqual(result.status_code,400);worker.assert_not_called()


if __name__=='__main__':unittest.main()
