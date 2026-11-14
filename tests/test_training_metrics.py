import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import backend.main as main
from backend.training_metrics import numeric_step, parse_metric
from backend.trainers.lerobot_train import exact_step_formatter


class TrainingMetricTests(unittest.TestCase):
    def setUp(self):self.counter=main.STATE.step_counter
    def tearDown(self):main.STATE.step_counter=self.counter
    def test_compact_comma_scientific_steps_are_not_truncated(self):
        for value, expected in [('9800', 9800), ('10K', 10000), ('10.2k', 10200),
                                ('1M', 1000000), ('20,000', 20000), ('1e5', 100000)]:
            with self.subTest(value=value):
                self.assertEqual(numeric_step(value), expected)
                self.assertEqual(parse_metric(f'step:{value} smpl:2M loss:0.175')['step'], expected)

    def test_numeric_zero_and_missing_step_are_distinct(self):
        with patch.object(main.HUB, 'loss') as loss:
            main._emit_line(json.dumps({'type': 'metric', 'step': 0, 'loss': .1}), 'train')
            self.assertEqual(loss.call_args.args[0], 0)
        self.assertIsNone(parse_metric('loss=0.1')['step'])
        self.assertEqual(parse_metric('global_step=20000 loss=1.5e-3')['value'], .0015)

    def test_invalid_metrics_are_not_published(self):
        for line in ['loss=nan', 'loss=inf', 'loss=1e999', '{"type":"metric","step":12,"loss":"nan"}']:
            with self.subTest(line=line), patch.object(main.HUB, 'loss') as loss:
                main._emit_line(line, 'train')
                loss.assert_not_called()

    def test_job_identity_and_epoch_units_follow_metric(self):
        job = SimpleNamespace(job_id='qa-train', kind='train', command=['python', '--epochs', '10'])
        with patch.object(main.HUB, 'loss') as loss:
            main._emit_line('{"type":"metric","step":2,"loss":0.3}', 'train', job)
            self.assertEqual(loss.call_args.kwargs, {'step_unit': 'epoch', 'kind': 'train', 'job_id': 'qa-train'})

    def test_exact_official_step_log_keeps_all_other_metrics(self):
        render = exact_step_formatter(lambda self: 'step:10K smpl:20K epch:1.0 loss:0.15')
        self.assertEqual(render(SimpleNamespace(steps=9800)), 'step:9800 smpl:20K epch:1.0 loss:0.15')


if __name__ == '__main__':
    unittest.main()
