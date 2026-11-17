"""Native Wall-X contracts/configuration. No GPU, training or physical devices."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from test_wall_oss_handoff import SOURCE, WallHandoffTests

sys.path.insert(0, str(SOURCE))
try:
    import native_common
    import native_train
    import native_serve
    import native_openloop
    import native_export
finally:
    sys.path.pop(0)


class NativeTests(unittest.TestCase):
    setUp = WallHandoffTests.setUp
    save_info = WallHandoffTests.save_info
    write = staticmethod(WallHandoffTests.write)

    def assets(self):
        base = self.root / 'base'
        base.mkdir()
        self.write(base / 'config.json', {'model_type': 'qwen2_5_vl', 'dim_inputs': [2048, 1024], 'use_x_loss': True})
        (base / 'model.safetensors').write_bytes(b'base-fixture')
        processor = self.root / 'processor'
        processor.mkdir()
        for filename in ('preprocessor_config.json', 'tokenizer.json', 'tokenizer_config.json'):
            self.write(processor / filename, {})
        (processor / 'merges.txt').write_text('merges-fixture')
        return base, processor

    def plan(self, *extra):
        base, processor = self.assets()
        args = native_train.parse(['--wallx-dir', str(self.root / 'wall-x'),
                                   '--dataset-dir', str(self.data), '--base-model', str(base),
                                   '--processor-dir', str(processor), '--output-dir', str(self.root / 'output'), *extra])
        with mock.patch.object(native_train, 'check_repo', return_value=self.root / 'wall-x'):
            return native_train.prepare(args)

    def exported_fixture(self):
        _, config, ref = self.plan('--steps', '10')
        model = self.root / 'native-model'
        model.mkdir()
        self.write(model / 'config.yml', config)
        self.write(model / 'config.json', {'model_type': 'qwen2_5_vl'})
        self.write(model / 'norm_stats.json', {'norm_stats': self.stats})
        self.write(model / 'dataset_reference.json', ref)
        for filename in ('preprocessor_config.json', 'tokenizer.json', 'tokenizer_config.json'):
            self.write(model / filename, {})
        (model / 'model.safetensors').write_bytes(b'native-fixture')
        return model

    def test_original_training_is_14_joint_degrees_plus_masked_padding(self):
        _, cfg, ref = self.plan('--steps', '10')
        self.assertEqual(cfg['task']['dof_config'], native_common.LAYOUT)
        self.assertEqual(list(cfg['task']['dof_config']), list(native_common.LAYOUT))
        self.assertEqual(cfg['task']['agent_pos_config'], native_common.LAYOUT)
        self.assertTrue(cfg['distributed']['use_fsdp'])
        self.assertTrue(cfg['distributed']['use_gradient_checkpointing'])
        self.assertEqual(ref['effective_batch'], 32)
        self.assertEqual(ref['optimizer_steps'], 10)
        self.assertEqual(ref['norm_key'], 'rebot_parcel')
        self.assertIn('--nproc_per_node=8', ref['commands'][1])
        self.assertIn('native_train_entry.py', ref['commands'][1][-3])
        self.assertFalse((self.root / 'output').exists())

    def test_cartesian_or_converted_config_is_rejected(self):
        _, cfg, _ = self.plan()
        cfg['task']['agent_pos_config'] = {'follow_left_ee_cartesian_pos': 3}
        with self.assertRaisesRegex(ValueError, 'XYZ/RPY'):
            native_common.require_joint_layout(cfg)

    def test_joint_payload_does_not_alias_angles_as_end_effector_pose(self):
        payload = native_common.joint_payload(range(14))
        self.assertEqual(payload, {'follow1_joints': list(range(7)), 'follow2_joints': list(range(7, 14))})
        for invalid in ([0.] * 13, [float('nan')] * 14):
            with self.assertRaises(ValueError):
                native_common.joint_payload(invalid)

    def test_observation_keeps_both_real_arms_and_masks_the_12_padding_dims(self):
        import numpy as np
        class Data:
            def __init__(self, config):
                self.states = {}
            def save_state_data_with_key(self, value, key):
                self.states[key] = value
        preprocessor = SimpleNamespace(config=SimpleNamespace(action_horizon=32))
        state = native_common.joint_payload(range(14))
        views = {key: np.zeros((1, 8, 8, 3), np.uint8) for key in native_common.WS_CAMERAS.values()}
        result = native_serve.joint_observation(preprocessor, state, views, numpy=np, data_class=Data)
        data = result['robot_state_action_data']
        self.assertEqual(data.states['follow_left_arm_joint_pos'].tolist(), [list(range(7))])
        self.assertEqual(data.states['follow_right_arm_joint_pos'].tolist(), [list(range(7, 14))])
        self.assertEqual(data.dof_mask.shape, (1, 32, 26))
        self.assertTrue(np.all(data.dof_mask[..., :14] == 1))
        self.assertTrue(np.all(data.dof_mask[..., 14:] == 0))
        self.assertEqual(result['face_view'].shape, (8, 8, 3))
        with self.assertRaisesRegex(ValueError, '缺少相机'):
            native_serve.joint_observation(preprocessor, state, {}, numpy=np, data_class=Data)
        with self.assertRaisesRegex(ValueError, '末端位姿'):
            native_serve.joint_observation(preprocessor, {'follow1_pos': [0.] * 7}, views, numpy=np, data_class=Data)

    def test_openloop_uses_joints_real_cameras_and_correct_joint_axis_names(self):
        calls = []
        def original(episode, index, cfg, mapping, extra_view_keys):
            calls.append(extra_view_keys)
            return {'state': module.build_state_payload(range(14), cfg)}
        module = SimpleNamespace(build_obs_payload=original)
        native_openloop.adapt_evaluator(module)
        cfg = {'data': {'key_mappings': {'camera': self.mapping}}}
        mapping = module.build_camera_ws_mapping(cfg)
        self.assertEqual(set(mapping.values()), set(native_common.WS_CAMERAS.values()))
        episode = SimpleNamespace(get_frame_images=lambda _: {key: object() for key in mapping})
        value = module.build_obs_payload(episode, 0, cfg, mapping, extra_view_keys=['fake'])
        self.assertIn('follow1_joints', value['state'])
        self.assertEqual(calls, [[]])
        self.assertIn('shoulder_pan', module._dim_label(0, 14))
        self.assertIn('command deg', module._dim_label(0, 14))
        episode.get_frame_images = lambda _: {}
        with self.assertRaisesRegex(ValueError, '不会补黑图'):
            module.build_obs_payload(episode, 0, cfg, mapping, [])

    def test_export_includes_merges_config_norms_and_reference(self):
        model = self.exported_fixture()
        output = self.root / 'delivery'
        self.assertEqual(native_export.main(['--checkpoint', str(model), '--output-dir', str(output)]), 0)
        self.assertTrue((output / 'merges.txt').is_file())
        self.assertEqual((output / 'model.safetensors').read_bytes(), b'native-fixture')
        _, _, ref = native_common.native_checkpoint(output)
        self.assertEqual(ref['runtime'], 'wall_x_native')

    def test_le_robot_converted_weight_format_and_wrong_norm_key_rejected(self):
        model = self.exported_fixture()
        self.write(model / 'config.json', {'type': 'wall_oss_05'})
        with self.assertRaisesRegex(ValueError, 'LeRobot 转换格式'):
            native_common.native_checkpoint(model)
        self.write(model / 'config.json', {'model_type': 'qwen2_5_vl'})
        ref = json.loads((model / 'dataset_reference.json').read_text())
        ref['norm_key'] = 'ex_normal'
        self.write(model / 'dataset_reference.json', ref)
        with self.assertRaisesRegex(ValueError, 'ex_normal'):
            native_common.native_checkpoint(model)


if __name__ == '__main__':
    unittest.main()
