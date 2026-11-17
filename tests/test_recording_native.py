"""Use the installed LeRobot for CPU inference and video saving, no hardware."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
import numpy as np


class NativeIntegrationTests(unittest.TestCase):
    def test_three_camera_two_episode_h264_recording(self):
        import av
        from lerobot.datasets import lerobot_dataset
        from backend.record_progress import install
        install(lerobot_dataset)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'data'
            features={'observation.state':{'dtype':'float32','shape':(2,),'names':['a','b']},
                      'action':{'dtype':'float32','shape':(2,),'names':['a','b']}}
            for name in ('left','top','right'):
                features['observation.images.'+name]={'dtype':'video','shape':(360,640,3),'names':['height','width','channels']}
            data=lerobot_dataset.LeRobotDataset.create('qa/video',fps=30,root=root,features=features,
                    image_writer_threads=6,vcodec='h264',batch_encoding_size=1)
            try:
                for episode in range(2):
                    for step in range(12):
                        image=np.full((360,640,3),120,np.uint8);image[100:170,20+step:100+step]=230
                        frame={key:image.copy() for key in features if '.images.' in key}
                        frame.update({'observation.state':np.zeros(2,np.float32),'action':np.ones(2,np.float32),'task':'sort parcel'})
                        data.add_frame(frame)
                    data.save_episode()
                self.assertEqual(data.num_episodes,2)
            finally:data.finalize()
            videos=list((root/'videos').rglob('*.mp4'))
            self.assertEqual(len(videos),3)
            for video in videos:
                with av.open(str(video)) as source:
                    frames=list(source.decode(video=0))
                    self.assertEqual(len(frames),24)
                    self.assertEqual((frames[0].width,frames[0].height),(640,360))
                    self.assertEqual(source.streams.video[0].codec_context.name,'h264')
            self.assertFalse(list((root/'images').rglob('*.png')))

    def test_saved_three_camera_act_loads_and_predicts_in_child(self):
        import torch
        from lerobot.policies.act.configuration_act import ACTConfig
        from lerobot.policies.act.modeling_act import ACTPolicy
        from lerobot.policies.factory import make_pre_post_processors
        from lerobot.configs.types import PolicyFeature, FeatureType, NormalizationMode
        from backend.async_act_runtime import AsyncACTPolicy
        with tempfile.TemporaryDirectory() as directory:
            inputs={'observation.state':PolicyFeature(FeatureType.STATE,(2,))}
            features={'observation.state':{'dtype':'float32','shape':(2,)},
                      'action':{'names':['shoulder_pan.pos','gripper.pos'],'shape':(2,)}}
            for name in ('left','top','right'):
                key='observation.images.'+name
                inputs[key]=PolicyFeature(FeatureType.VISUAL,(3,32,32))
                features[key]={'dtype':'video','shape':(32,32,3)}
            cfg=ACTConfig(input_features=inputs,output_features={'action':PolicyFeature(FeatureType.ACTION,(2,))},
                    device='cpu',pretrained_backbone_weights=None,dim_model=32,n_heads=4,dim_feedforward=64,
                    n_encoder_layers=1,n_decoder_layers=1,chunk_size=20,n_action_steps=20,use_vae=False,
                    normalization_mapping={key:NormalizationMode.IDENTITY for key in ('VISUAL','STATE','ACTION')})
            policy=ACTPolicy(cfg);policy.save_pretrained(directory)
            pre,post=make_pre_post_processors(cfg);pre.save_pretrained(directory);post.save_pretrained(directory)
            cfg.pretrained_path=Path(directory)
            client=AsyncACTPolicy(cfg,features,{'chunk_size':20,'max_age_s':3,'wait_timeout_s':3})
            try:
                client.start({},startup_timeout=45)
                robot=NS(config=NS(joint_directions={'shoulder_pan':1,'gripper':-6}),cameras={},
                        motors={name:NS(get_state=lambda:NS(pos=0.)) for name in ('shoulder_pan','gripper')})
                client.attach(robot,{'exit_early':False,'stop_recording':False},10);client.reset()
                observation={key:np.zeros(feature['shape'],np.uint8 if feature['dtype']=='video' else np.float32)
                             for key,feature in features.items() if key.startswith('observation.')}
                predicted=False
                for _ in range(30):
                    output=client.predict(observation)
                    self.assertEqual(tuple(output.shape),(1,2));self.assertTrue(torch.isfinite(output).all())
                    if not client.waiting_for_prediction:
                        predicted=True;break
                    if client.failed:break
                    time.sleep(.1)
                self.assertFalse(client.failed)
                self.assertTrue(predicted,'real ACT child must deliver a usable action chunk')
            finally:client.close()


if __name__=='__main__':unittest.main()
