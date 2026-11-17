"""Copy-only distribution and small real CPU training, never use user data."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE=Path(__file__).resolve().parents[1]/'model_trainer/train.py'
spec=importlib.util.spec_from_file_location('standalone_trainer_test',SOURCE)
trainer=importlib.util.module_from_spec(spec);spec.loader.exec_module(trainer)


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.data=self.root/'data';(self.data/'meta').mkdir(parents=True);(self.data/'data').mkdir()
        (self.data/'data/fixture.parquet').touch();video=self.data/'videos/observation.images.left_左';video.mkdir(parents=True);(video/'fixture.mp4').touch()
        self.features={'observation.state':{'shape':[14]},'action':{'shape':[14]},'observation.images.left_左':{'shape':[360,640,3],'dtype':'video'}}
        (self.data/'meta/info.json').write_text(json.dumps({'total_episodes':2,'total_frames':60,'features':self.features}))
    def test_copied_file_dry_run_references_original_data_and_creates_no_output(self):
        copied=self.root/'train.py';shutil.copy2(SOURCE,copied);output=self.root/'model'
        result=subprocess.run([sys.executable,str(copied),'--dataset-dir',str(self.data),'--output-dir',str(output),'--dry-run'],cwd=self.root,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn(str(self.data),result.stdout);self.assertIn('left_左',result.stdout)
        self.assertFalse(output.exists());self.assertIn('--policy.type=act',result.stdout)
    def test_finetune_requires_exact_camera_keys_and_preserves_model_architecture(self):
        model=self.root/'model';model.mkdir()
        policy={'type':'act','input_features':{'observation.state':{'shape':[14]},'observation.images.left_左':{'shape':[3,360,640]}},'output_features':{'action':{'shape':[14]}}}
        (model/'config.json').write_text(json.dumps(policy))
        for name in ['model.safetensors','policy_preprocessor.json','policy_postprocessor.json']:(model/name).touch()
        args=trainer.parse(['--dataset-dir',str(self.data),'--base-model',str(model),'--output-dir',str(self.root/'new-model')])
        command,ref=trainer.prepare(args);self.assertEqual(ref['mode'],'finetune');self.assertIn('--policy.path='+str(model),command)
        self.assertFalse(any(x.startswith('--policy.type=') or x.startswith('--policy.chunk_size=') for x in command))
        policy['input_features']['observation.images.left_wrong']=policy['input_features'].pop('observation.images.left_左')
        (model/'config.json').write_text(json.dumps(policy))
        with self.assertRaisesRegex(ValueError,'相机键'):trainer.prepare(args)


class CpuTrainingTests(unittest.TestCase):
    def test_scratch_finetune_and_resume_single_file_with_real_saved_checkpoints(self):
        import numpy as np
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);copied=root/'train.py';shutil.copy2(SOURCE,copied)
            def dataset(name,offset):
                path=root/name
                features={'observation.state':{'dtype':'float32','shape':(2,),'names':['a','b']},'action':{'dtype':'float32','shape':(2,),'names':['a','b']},'observation.images.top':{'dtype':'video','shape':(64,64,3),'names':['height','width','channels']}}
                ds=LeRobotDataset.create('qa/'+name,fps=10,root=path,features=features,vcodec='h264',image_writer_threads=2)
                try:
                    for step in range(12):
                        image=np.full((64,64,3),offset+step*5,np.uint8)
                        ds.add_frame({'observation.state':np.array([step,step*.1],np.float32),'action':np.array([step*.1,step*.2],np.float32),'observation.images.top':image,'task':'push parcel'})
                    ds.save_episode(parallel_encoding=False)
                finally:ds.finalize()
                return path
            first,second=dataset('first',20),dataset('second',50)
            env={**os.environ,'CUDA_VISIBLE_DEVICES':'','OMP_NUM_THREADS':'2','MKL_NUM_THREADS':'2','OPENBLAS_NUM_THREADS':'2','HF_HUB_OFFLINE':'1','WANDB_MODE':'disabled'}
            def run(parameters,label):
                result=subprocess.run([sys.executable,str(copied),*parameters],cwd=root,env=env,capture_output=True,text=True,timeout=120)
                log=Path('/tmp')/('rebot-trainer-'+label+'.log');log.write_text(result.stdout+'\n'+result.stderr)
                self.assertEqual(result.returncode,0,'Log: '+str(log)+'\n'+result.stderr[-3500:])
            output=root/'scratch'
            run(['--dataset-dir',str(first),'--output-dir',str(output),'--device','cpu','--steps','1','--batch-size','1','--workers','0','--save-every','1','--log-every','1','--chunk-size','4','--dim-model','32','--dim-feedforward','64','--encoder-layers','1','--decoder-layers','1','--n-heads','4','--random-backbone'],'scratch')
            model=(output/'checkpoints/last/pretrained_model').resolve();self.assertTrue((model/'model.safetensors').is_file())
            before=hashlib.sha256((model/'model.safetensors').read_bytes()).hexdigest()
            tuned=root/'finetuned'
            run(['--dataset-dir',str(second),'--output-dir',str(tuned),'--base-model',str(model),'--device','cpu','--steps','1','--batch-size','1','--workers','0','--log-every','1'],'finetune')
            self.assertEqual(hashlib.sha256((model/'model.safetensors').read_bytes()).hexdigest(),before)
            reference=json.loads((tuned/'dataset_reference.json').read_text());self.assertEqual(reference['dataset_dir'],str(second));self.assertEqual(reference['mode'],'finetune')
            run(['--resume',str(model/'train_config.json'),'--steps','2','--device','cpu'],'resume')
            step=json.loads((output/'checkpoints/last/training_state/training_step.json').read_text())['step']
            self.assertEqual(step,2)


if __name__=='__main__':unittest.main()
