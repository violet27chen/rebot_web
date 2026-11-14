import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

import backend.main as main
from backend.model_evaluation import ActionErrors, prepare_evaluation


class OfflineEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.model=self.root/'model';self.model.mkdir();self.data=self.root/'data';(self.data/'meta').mkdir(parents=True)
        self.policy={'type':'act','input_features':{'observation.state':{'shape':[14]},'observation.images.left_左':{'shape':[3,32,32]}},'output_features':{'action':{'shape':[14]}}}
        (self.model/'config.json').write_text(json.dumps(self.policy));(self.model/'model.safetensors').write_bytes(b'fixture')
        (self.data/'meta/info.json').write_text(json.dumps({'total_episodes':3,'features':{'observation.state':{'shape':[14]},'observation.images.left_左':{'shape':[32,32,3]},'action':{'shape':[14]}}}))
        self.params={'base_model':str(self.model),'dataset_dir':str(self.data),'repo_id':'qa/test','episodes':'0, 2','max_samples':10,'device':'cpu'}
        self.config=self.root/'web.json';cfg=copy.deepcopy(main.DEFAULT_CONFIG);self.config.write_text(json.dumps(cfg))
        self.patches=[patch.object(main,'CONFIG_FILE',self.config),patch.object(main,'OUTPUTS_DIR',self.root/'outputs'),patch.object(main,'_MODEL_EVAL_JOBS',{}),patch.object(main.STATE,'job',None)]
        for p in self.patches:p.start()
        self.client=TestClient(main.app)
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def test_prepare_uses_recorded_images_and_explicit_episode_subset(self):
        p=prepare_evaluation(self.params,self.root,self.root)
        self.assertEqual(p['episodes'],[0,2]);self.assertEqual(p['dataset_dir'],str(self.data))
        for episodes in ['3','-1','oops',[True]]:
            with self.subTest(episodes=episodes),self.assertRaises(ValueError):
                prepare_evaluation(dict(self.params,episodes=episodes),self.root,self.root)
    def test_start_only_spawns_offline_worker_and_no_robot_command(self):
        def spawn(kind,name,cmd,**kw):
            self.assertEqual(kind,'model-eval');self.assertIn('model_eval_worker.py',cmd[-2])
            self.assertNotIn('--robot.',str(cmd));self.assertNotIn('--teleop.',str(cmd))
            main.STATE.job=main.Job(name=name,kind=kind,command=cmd,display='fixture')
            return {'ok':True,'job_id':main.STATE.job.job_id}
        with patch.object(main,'require_env'),patch.object(main.STATE,'running',return_value=False),patch.object(main,'_spawn',side_effect=spawn) as run:
            response=self.client.post('/api/model-eval/start',json=self.params)
        self.assertEqual(response.status_code,200,response.text);run.assert_called_once()
        result=self.client.get('/api/model-eval/result',params={'evaluation_id':response.json()['evaluation_id']}).json()
        self.assertFalse(result['finished']);self.assertIsNone(result['result'])
    def test_saved_report_remains_readable_after_process_restart(self):
        ident='a'*24;out=self.root/'outputs/model_evaluations';out.mkdir(parents=True)
        report={'mae':.2,'samples':10};(out/(ident+'.json')).write_text(json.dumps(report))
        response=self.client.get('/api/model-eval/result',params={'evaluation_id':ident})
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['result'],report)
        self.assertTrue(response.json()['finished'])
        self.assertEqual(self.client.get('/api/model-eval/result',params={'evaluation_id':'../config'}).status_code,400)
    def test_busy_or_missing_weights_does_not_start_process(self):
        with patch.object(main,'require_env'),patch.object(main.STATE,'running',return_value=True),patch.object(main,'_spawn') as run:
            self.assertEqual(self.client.post('/api/model-eval/start',json=self.params).status_code,409);run.assert_not_called()
        (self.model/'model.safetensors').unlink()
        with patch.object(main,'require_env'),patch.object(main.STATE,'running',return_value=False),patch.object(main,'_spawn') as run:
            self.assertEqual(self.client.post('/api/model-eval/start',json=self.params).status_code,400);run.assert_not_called()
    def test_masked_metrics_exclude_padding_and_weight_batches_by_actions(self):
        import torch
        errors=ActionErrors()
        errors.add(torch.tensor([[[1.,3.],[99.,99.]]]),torch.zeros(1,2,2),torch.tensor([[True,False]]))
        errors.add(torch.tensor([[[5.,7.],[9.,11.]]]),torch.zeros(1,2,2),torch.tensor([[True,True]]))
        result=errors.result();self.assertEqual(result['mae'],6.)
        self.assertAlmostEqual(result['rmse'],math.sqrt(286/6));self.assertEqual(result['max_error'],11.)
        self.assertEqual(result['per_joint_mae'],[5.,7.]);self.assertEqual(result['valid_actions'],3)
        with self.assertRaises(ValueError):
            errors.add(torch.full((1,1,2),float('nan')),torch.zeros(1,1,2),torch.tensor([[True]]))


if __name__=='__main__':unittest.main()
