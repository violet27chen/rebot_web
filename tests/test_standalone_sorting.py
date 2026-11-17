import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'sorting_operator/run_sorting.py'
spec=importlib.util.spec_from_file_location('standalone_sorting',SOURCE)
sorting=importlib.util.module_from_spec(spec);spec.loader.exec_module(sorting)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.cfg=sorting.load_settings(ROOT/'sorting_operator/sorting.example.json')
        self.box={'bbox':[.3,.3,.6,.6],'kind':'box','score':.9}
    def choice(self,**changes):return sorting.choose_skill({'parcels':[{**self.box,**changes}]},self.cfg)
    def test_four_competition_cases_and_outside_workspace(self):
        self.assertEqual(self.choice(stacked=True)[1],'unstack')
        self.assertEqual(self.choice()[1],'flip_box')
        self.assertEqual(self.choice(kind='bag')[1],'flip_bag')
        self.assertEqual(self.choice(kind='bag',waybill='SF1234567890')[1],'route_bag')
        self.assertEqual(self.choice(waybill='123456789012')[1],'route_box')
        self.assertEqual(self.choice(bbox=[.05,.3,.25,.6])[1],'push')
    def test_visible_label_without_verified_number_cannot_route(self):
        self.assertIsNone(self.choice(label_visible=True)[1])
    def test_output_region_is_not_selected_for_push_back_into_workspace(self):
        self.assertIsNone(self.choice(bbox=[.87,.3,.98,.5],waybill='1234567890')[1])
    def test_duplicate_scene_is_not_three_independent_confirmations(self):
        workflow=sorting.Workflow(self.cfg)
        scene={'sequence':1,'parcels':[self.box]}
        for _ in range(10):self.assertIsNone(workflow.update(scene,1.))
        self.assertIsNone(workflow.skill)
    def test_occlusion_is_not_successful_output(self):
        workflow=sorting.Workflow(self.cfg)
        parcel={**self.box,'waybill':'1234567890'}
        for seq in range(3):event=workflow.update({'sequence':seq,'parcels':[parcel]},seq*.1)
        self.assertEqual(event,'route_box')
        for seq in range(3,7):workflow.update({'sequence':seq,'parcels':[]},seq*.1)
        self.assertEqual(workflow.completed,0)
    def test_output_count_needs_tracked_parcel_reaching_exit(self):
        workflow=sorting.Workflow(self.cfg);parcel={**self.box,'waybill':'1234567890'}
        for seq in range(3):workflow.update({'sequence':seq,'parcels':[parcel]},seq*.1)
        for seq,centre in enumerate([.55,.65,.75,.86,.9,.91],3):
            moved={**parcel,'bbox':[centre-.03,.4,centre+.03,.5]}
            workflow.update({'sequence':seq,'parcels':[moved]},seq*.1)
        self.assertEqual(workflow.completed,1)
    def test_example_cannot_be_used_to_enable_motors(self):
        with self.assertRaisesRegex(ValueError,'力矩阈值'):sorting.validate_physical(self.cfg)
    def test_total_torque_is_limited_including_pd_and_gravity(self):
        self.assertEqual(sorting.guarded_torque(2,0,0,5,120,8,3),3)
        self.assertEqual(sorting.guarded_torque(-2,0,0,-5,120,8,3),-3)
        with self.assertRaises(ValueError):sorting.guarded_torque(float('nan'),0,0,0,1,1,3)


class DistributionTests(unittest.TestCase):
    def test_copied_folder_runs_plan_and_scenarios_with_stdlib_only(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'sorting'
            shutil.copytree(SOURCE.parent,target,ignore=shutil.ignore_patterns('__pycache__'))
            for option in ['--dry-run','--simulate']:
                result=subprocess.run([sys.executable,'-S',str(target/'run_sorting.py'),option],cwd=directory,capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                if option=='--dry-run':self.assertFalse(json.loads(result.stdout)['motion_enabled'])
                else:self.assertIn('route_box',result.stdout)
    def test_checkpoint_camera_keys_and_dimensions_are_checked_without_torch(self):
        cfg=sorting.load_settings(ROOT/'sorting_operator/sorting.example.json')
        cfg['execution_mode']='skills'
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory);(p/'model.safetensors').write_bytes(b'config-only-test')
            inputs={'observation.state':{'shape':[14]}}
            inputs.update({'observation.images.'+c['name']:{'shape':[3,32,32]} for c in cfg['cameras']})
            info={'type':'act','input_features':inputs,'output_features':{'action':{'shape':[14]}}}
            (p/'config.json').write_text(json.dumps(info));cfg['models']={k:str(p) for k in sorting.SKILLS}
            paths,shape=sorting.model_plan(cfg);self.assertEqual(shape,(3,32,32));self.assertEqual(len(paths),7)
            inputs['observation.images.wrong']=inputs.pop('observation.images.left_左');(p/'config.json').write_text(json.dumps(info))
            with self.assertRaisesRegex(ValueError,'相机键不一致'):sorting.model_plan(cfg)

    def test_complete_mode_needs_only_the_one_existing_model(self):
        cfg=sorting.load_settings(ROOT/'sorting_operator/sorting.example.json')
        paths,shape=sorting.model_plan(cfg)
        self.assertEqual(set(paths),{'complete'});self.assertEqual(shape,(3,360,640))

    def test_complete_policy_is_started_once_and_barcode_only_does_not_fake_counts(self):
        cfg=sorting.load_settings(ROOT/'sorting_operator/sorting.example.json');workflow=sorting.CompleteWorkflow(cfg)
        self.assertEqual(workflow.update({'sequence':1,'parcels':[],'codes':['1234567890']},1.),'complete')
        self.assertIsNone(workflow.update({'sequence':2,'parcels':[],'codes':[]},2.))
        self.assertEqual(workflow.completed,0)
    def test_official_urdf_mesh_dependencies_are_all_included(self):
        import xml.etree.ElementTree as ET
        urdf=SOURCE.parent/'assets/DM/urdf/ReBot_Arm_DM.urdf'
        for mesh in ET.parse(urdf).getroot().iter('mesh'):
            self.assertTrue((urdf.parent/mesh.attrib['filename']).is_file(),mesh.attrib['filename'])


class ActorIntegrationTests(unittest.TestCase):
    def test_fresh_interpreter_registers_act_before_reading_bundled_config(self):
        code="import sys;sys.path.insert(0,sys.argv[1]);from run_sorting import load_act_config;cfg=load_act_config(sys.argv[2],'cpu');assert cfg.type=='act';assert cfg.device=='cpu';print('ACT_CONFIG_READY')"
        result=subprocess.run([sys.executable,'-c',code,str(SOURCE.parent),str(SOURCE.parent/'models/current_act')],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('ACT_CONFIG_READY',result.stdout)

    def test_bundled_actor_loads_real_14d_three_camera_act_on_cpu(self):
        import numpy as np
        import torch
        from lerobot.policies.act.configuration_act import ACTConfig
        from lerobot.policies.act.modeling_act import ACTPolicy
        from lerobot.policies.factory import make_pre_post_processors
        from lerobot.configs.types import PolicyFeature,FeatureType,NormalizationMode
        from rebot_sort_support.async_act_runtime import AsyncACTPolicy
        contract=json.loads((SOURCE.parent/'assets/robot_contract.json').read_text())
        features={'action':{'names':sorting.ACTION_NAMES,'shape':(14,)},'observation.state':{'dtype':'float32','shape':(14,)}}
        inputs={'observation.state':PolicyFeature(FeatureType.STATE,(14,))}
        for name in ['left_左','left_深度相机','left_右']:
            key='observation.images.'+name;features[key]={'dtype':'video','shape':(32,32,3)};inputs[key]=PolicyFeature(FeatureType.VISUAL,(3,32,32))
        with tempfile.TemporaryDirectory() as directory:
            config=ACTConfig(input_features=inputs,output_features={'action':PolicyFeature(FeatureType.ACTION,(14,))},device='cpu',pretrained_backbone_weights=None,dim_model=32,n_heads=4,dim_feedforward=64,n_encoder_layers=1,n_decoder_layers=1,chunk_size=20,n_action_steps=20,use_vae=False,normalization_mapping={k:NormalizationMode.IDENTITY for k in ['VISUAL','STATE','ACTION']})
            policy=ACTPolicy(config);policy.save_pretrained(directory)
            pre,post=make_pre_post_processors(config);pre.save_pretrained(directory);post.save_pretrained(directory)
            config.pretrained_path=Path(directory)
            client=AsyncACTPolicy(config,features,{'n_action_steps':16,'max_age_s':3,'wait_timeout_s':3})
            robot=NS(cameras={})
            for side in ['left','right']:
                setattr(robot,side+'_arm',NS(config=NS(joint_directions=contract['joint_directions'],joint_limits=contract['joint_limits_deg']),motors={j:NS(get_state=lambda:NS(pos=0.)) for j in sorting.JOINTS}))
            try:
                client.start({},startup_timeout=45)
                self.assertEqual(client.chunk_size,16);self.assertEqual(client.model_chunk_size,20)
                client.attach(robot,{},10);client.reset()
                obs={k:np.zeros(f['shape'],np.uint8 if f['dtype']=='video' else np.float32) for k,f in features.items() if k.startswith('observation.')}
                predicted=False
                for _ in range(30):
                    value=client.predict(obs);self.assertEqual(tuple(value.shape),(1,14));self.assertTrue(torch.isfinite(value).all())
                    if not client.waiting_for_prediction:predicted=True;break
                    if client.failed:break
                    time.sleep(.1)
                self.assertFalse(client.failed);self.assertTrue(predicted)
            finally:client.close()


if __name__=='__main__':unittest.main()
