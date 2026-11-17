"""Remote WALL configuration and local artifact contracts; no weights/devices used."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'model_trainer/wall_oss_05'
sys.path.insert(0, str(SOURCE))
try:
    import contract
    import train_remote
    import validate_model
finally:
    sys.path.pop(0)


class WallHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'dataset'
        (self.data / 'meta').mkdir(parents=True)
        (self.data / 'data').mkdir()
        (self.data / 'data/file.parquet').write_bytes(b'fixture')
        self.mapping = contract.read_json(SOURCE / 'camera_mapping.json')
        self.features = {k: {'shape': [14], 'names': contract.NAMES, 'dtype': 'float32'}
                         for k in ('observation.state', 'action')}
        for key in self.mapping:
            self.features[key] = {'shape': [360, 640, 3], 'dtype': 'video'}
            (self.data / 'videos' / key).mkdir(parents=True)
            (self.data / 'videos' / key / 'file.mp4').write_bytes(b'fixture')
        self.info = {'total_episodes': 10, 'total_frames': 300, 'fps': 30,
                     'codebase_version': 'v3.0', 'features': self.features}
        self.save_info()
        self.stats = {key: {'q01': [-1.] * 14, 'q99': [1.] * 14}
                      for key in ('action', 'observation.state')}
        self.write(self.data / 'meta/stats.json', self.stats)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def save_info(self):
        self.write(self.data / 'meta/info.json', self.info)

    def args(self, *extra):
        return train_remote.parse(['--dataset-dir', str(self.data),
                                   '--output-dir', str(self.root / 'output'), *extra])

    def checkpoint(self):
        model = self.root / 'model'
        model.mkdir()
        inputs = {'observation.state': {'shape': [14], 'type': 'STATE'}}
        inputs.update({key: {'shape': [3, 360, 640], 'type': 'VISUAL'} for key in self.mapping})
        self.config = {'type': 'wall_oss_05', 'input_features': inputs,
                       'output_features': {'action': {'shape': [14]}},
                       'camera_key_mapping': self.mapping}
        self.write(model / 'config.json', self.config)
        self.write(model / 'dataset_reference.json', contract.dataset_contract(self.data))
        for kind, registry in (('pre', 'normalizer_processor'), ('post', 'unnormalizer_processor')):
            name = f'policy_{kind}processor'
            self.write(model / (name + '.json'),
                       {'steps': [{'registry_name': registry, 'state_file': name + '.safetensors'}]})
            (model / (name + '.safetensors')).write_bytes(b'stats-fixture')
        (model / 'model.safetensors').write_bytes(b'weights-fixture')
        return model

    def test_remote_full_training_is_sharded_not_ddp(self):
        command, ref = train_remote.prepare(self.args('--steps', '10'))
        self.assertIn('--nproc_per_node=8', command)
        self.assertIn('--parallelism.dp_shard=8', command)
        self.assertIn('--accelerator.mixed_precision=bf16', command)
        self.assertIn('--policy.path=lerobot/wall-oss-0.5', command)
        self.assertIn('--policy.input_features=null', command)
        self.assertIn('--policy.output_features=null', command)
        self.assertIn('--env_eval_freq=0', command)
        self.assertEqual(ref['effective_batch'], 32)
        self.assertFalse(any('wandb' in v or 'activation_checkpointing' in v for v in command))
        self.assertFalse((self.root / 'output').exists())

    def test_lora_uses_explicit_wall_experts_and_action_projections(self):
        import re
        command, _ = train_remote.prepare(self.args('--mode', 'lora', '--gpus', '1'))
        self.assertFalse(any('dp_shard=' in v for v in command))
        self.assertIn('--peft.method_type=LORA', command)
        for name in ['model.model.layers.0.self_attn.qkv_proj_experts.0',
                     'model.model.layers.4.self_attn.o_proj_experts.1',
                     'model.model.layers.4.moe.experts.1.gate_up_proj',
                     'model.action_preprocessor.action_proj_back']:
            self.assertIsNotNone(re.fullmatch(contract.LORA_TARGETS, name), name)
        self.assertIsNone(re.fullmatch(contract.LORA_TARGETS, 'model.visual.blocks.0.attn.qkv'))

    def test_joint_reorder_and_ambiguous_camera_mapping_rejected(self):
        self.features['action']['names'] = list(reversed(contract.NAMES))
        self.save_info()
        with self.assertRaisesRegex(ValueError, '不会猜测'):
            train_remote.prepare(self.args())
        self.features['action']['names'] = contract.NAMES
        self.save_info()
        bad = self.root / 'map.json'
        self.write(bad, {key: 'face_view' for key in self.mapping})
        with self.assertRaisesRegex(ValueError, '三个 WALL'):
            contract.dataset_contract(self.data, bad)

    def test_missing_quantiles_video_and_output_overlap_rejected(self):
        self.stats['action'].pop('q99')
        self.write(self.data / 'meta/stats.json', self.stats)
        with self.assertRaisesRegex(ValueError, 'q99'):
            contract.dataset_contract(self.data)
        self.stats['action']['q99'] = [1.] * 14
        self.write(self.data / 'meta/stats.json', self.stats)
        with self.assertRaisesRegex(ValueError, '分开'):
            train_remote.prepare(self.args('--output-dir', str(self.data / 'model')))
        next((self.data / 'videos').rglob('*.mp4')).unlink()
        with self.assertRaisesRegex(ValueError, '缺少视频'):
            contract.dataset_contract(self.data)

    def test_copy_only_delivery_dry_run_and_metadata_check_need_no_ml_packages(self):
        copied = self.root / 'delivery'
        shutil.copytree(SOURCE, copied, ignore=shutil.ignore_patterns('__pycache__'))
        result = subprocess.run([sys.executable, '-S', str(copied / 'train_remote.py'),
                                 '--dataset-dir', str(self.data), '--output-dir', str(self.root / 'output'),
                                 '--dry-run'], cwd=self.root, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--parallelism.dp_shard=8', result.stdout)
        model = self.checkpoint()
        result = subprocess.run([sys.executable, '-S', str(copied / 'validate_model.py'),
                                 '--model', str(model), '--dataset-dir', str(self.data)],
                                cwd=self.root, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['hardware_commands_sent'], 0)

    def test_base_26d_or_act_checkpoint_cannot_be_validated_as_finetuned_14d(self):
        model = self.checkpoint()
        reference = contract.dataset_contract(self.data)
        self.config['output_features']['action']['shape'] = [26]
        self.write(model / 'config.json', self.config)
        with self.assertRaisesRegex(ValueError, '不是14维'):
            contract.checkpoint_contract(model, reference)
        self.config['type'] = 'act'
        self.write(model / 'config.json', self.config)
        with self.assertRaisesRegex(ValueError, '不能直接互换'):
            contract.checkpoint_contract(model, reference)

    def test_missing_statistics_unmerged_adapter_and_swapped_views_rejected(self):
        model = self.checkpoint()
        reference = contract.dataset_contract(self.data)
        stats = model / 'policy_preprocessor.safetensors'
        stats.unlink()
        with self.assertRaisesRegex(ValueError, '统计文件缺失'):
            contract.checkpoint_contract(model, reference)
        stats.write_bytes(b'stats')
        (model / 'adapter_config.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, '合并 LoRA'):
            contract.checkpoint_contract(model, reference)
        (model / 'adapter_config.json').unlink()
        keys = list(self.mapping)
        self.config['camera_key_mapping'][keys[0]], self.config['camera_key_mapping'][keys[1]] = (
            self.config['camera_key_mapping'][keys[1]], self.config['camera_key_mapping'][keys[0]])
        self.write(model / 'config.json', self.config)
        with self.assertRaisesRegex(ValueError, '映射与数据不一致'):
            contract.checkpoint_contract(model, reference)

    def test_validation_allows_unseen_dataset_without_replacing_training_contract(self):
        model = self.checkpoint()
        self.info['total_frames'] += 1
        self.save_info()
        args = validate_model.parse(['--model', str(model), '--dataset-dir', str(self.data)])
        report = validate_model.validate(args)
        self.assertNotEqual(report['training_dataset_info_sha256'], report['validation_dataset_info_sha256'])
        self.assertEqual(report['mode'], 'metadata')


if __name__ == '__main__':
    unittest.main()
