import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import backend.main as main
from backend.recording_progress import RecordingProgress


class RecordingProgressTests(unittest.TestCase):
    def setUp(self):
        self.now=100.
        self.progress=RecordingProgress({'repo_id':'qa/test','num_episodes':3,'episode_time_s':30,'reset_time_s':5},clock=lambda:self.now)
    def test_duplicate_native_and_heartbeat_lines_never_restart_countdown(self):
        self.assertTrue(self.progress.feed('INFO 2026-11-15 record.py:510 Recording episode 0'))
        self.now+=7
        for line in ['INFO record.py:510 Recording episode 0','[进度] 数据采集 · 最近输出：INFO Recording episode 0']:
            self.assertFalse(self.progress.feed(line))
        self.assertEqual(self.progress.snapshot()['remaining_s'],23)
        self.assertEqual(self.progress.sequence,1)
    def test_zero_does_not_speculatively_start_reset_or_next_episode(self):
        p=self.progress;p.feed('Recording episode 0');self.now+=35
        self.assertEqual(p.snapshot()['remaining_s'],0);self.assertEqual(p.phase,'recording')
        p.feed('Reset the environment');self.now+=3
        self.assertFalse(p.feed('Reset the environment'));self.assertEqual(p.snapshot()['remaining_s'],2)
        self.now+=10;self.assertEqual(p.phase,'reset');self.assertEqual(p.snapshot()['remaining_s'],0)
        p.feed('[剧集保存] 开始保存');self.now+=10
        self.assertFalse(p.feed('[剧集保存] 编码中'));self.assertEqual(p.snapshot()['elapsed_s'],10)
        p.feed('Recording episode 1');self.assertEqual(p.snapshot()['episode'],2);self.assertEqual(p.snapshot()['remaining_s'],30)
    def test_rerecord_resets_same_index_only_when_explicitly_requested(self):
        p=self.progress;p.feed('Recording episode 12');self.now+=10
        p.feed('Reset the environment');p.feed('Re-record episode');p.feed('Recording episode 12')
        self.assertEqual(p.snapshot()['episode'],1);self.assertEqual(p.snapshot()['remaining_s'],30)
        sequence=p.sequence
        p.feed('Recording episode 12');self.assertEqual(p.sequence,sequence)
        p.feed('Recording episode 13');self.assertEqual(p.snapshot()['episode'],2)
        self.assertFalse(p.feed('Recording episode 12'))
    def test_zero_reset_and_command_snapshot_are_preserved(self):
        p=RecordingProgress.from_command(['python','record_entry.py','--dataset.reset_time_s=0','--dataset.episode_time_s=12','--dataset.num_episodes=2'])
        p.feed('\x1b[32mINFO Recording episode 0\x1b[0m');p.feed('Reset the environment')
        self.assertEqual(p.snapshot()['reset_time_s'],0);self.assertEqual(p.snapshot()['remaining_s'],0)
        p.feed('Stop recording');sequence=p.sequence
        self.assertFalse(p.feed('Recording episode 1'));self.assertEqual(p.sequence,sequence)
    def test_job_api_and_state_keep_the_same_phase_clock(self):
        job=main.Job(name='qa',kind='record',command=[],display='fixture',recording=self.progress)
        with patch.object(main.STATE,'job',job),patch.object(main.HUB,'state') as state:
            main._record_line(job,'Recording episode 0')
            self.assertEqual(state.call_args.kwargs['recording']['sequence'],1)
            self.now+=8
            response=TestClient(main.app).get('/api/status').json()
            self.assertEqual(response['job']['recording']['remaining_s'],22)
            main._record_line(job,'Recording episode 0')
            self.assertEqual(state.call_count,1)
            self.assertEqual(main._job_progress(job)['recording']['remaining_s'],22)
    def test_startup_failure_is_never_a_completed_episode(self):
        p=self.progress
        p.feed('INFO 主臂连接失败：/dev/ttyUSB0 · Servo not found for shoulder_pan (id=0).')
        p.feed('Stop recording')
        snapshot=p.snapshot();self.assertFalse(snapshot['has_started']);self.assertIsNone(snapshot['raw_episode'])
        self.assertEqual(snapshot['phase'],'closing');self.assertIn('/dev/ttyUSB0',snapshot['startup_error'])
        job=main.Job(name='fixture',kind='record',command=[],display='',recording=p)
        job.exit_code=1;job.finished.set()
        with patch.object(main.STATE,'job',job):
            status=TestClient(main.app).get('/api/status').json()
        self.assertFalse(status['running']);self.assertIsNone(status['job'])
        self.assertEqual(status['last_job']['exit_code'],1)
        self.assertFalse(status['last_job']['recording']['has_started'])


if __name__=='__main__':unittest.main()
