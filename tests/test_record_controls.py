import io
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from backend.record_controls import apply_command, install, PREFIX, READY


class RecordControlsTests(unittest.TestCase):
    def test_commands_keep_native_event_semantics_and_stop_cannot_be_undone(self):
        events = dict(exit_early=False, rerecord_episode=False, stop_recording=False)
        self.assertFalse(apply_command(events, 'arbitrary shell text'))
        self.assertTrue(apply_command(events, PREFIX+'left\n'))
        self.assertTrue(events['exit_early']); self.assertTrue(events['rerecord_episode'])
        apply_command(events, PREFIX+'esc\n')
        self.assertTrue(events['stop_recording']); self.assertFalse(events['rerecord_episode'])
        apply_command(events, PREFIX+'left\n')
        self.assertFalse(events['rerecord_episode'])

    def test_headless_factory_never_imports_desktop_listener(self):
        recorder = NS(init_keyboard_listener=Mock(side_effect=AssertionError('pynput must not run')))
        with patch.dict('os.environ', {'REBOT_WEB_RECORD_CONTROL': '1'}), patch('builtins.print') as output:
            close = install(recorder, stream=io.StringIO(PREFIX+'esc\n'))
            try:
                listener, events = recorder.init_keyboard_listener()
                self.assertIsNone(listener)
                self.assertTrue(recorder.is_headless())
                output.assert_any_call(READY, flush=True)
            finally: close()

    def test_existing_direct_cli_is_not_patched_without_web_capability(self):
        original = Mock()
        recorder = NS(init_keyboard_listener=original)
        with patch.dict('os.environ', {'REBOT_WEB_RECORD_CONTROL': '0'}): install(recorder)
        self.assertIs(recorder.init_keyboard_listener, original)

    def test_web_buttons_target_child_stdin_and_never_launch_xdotool_or_stop_signal(self):
        from backend import main
        from fastapi.testclient import TestClient
        job = main.Job(name='record', kind='record', command=[], display='')
        job.proc = NS(stdin=io.BytesIO()); job.stdin_live = True
        with patch.object(main.STATE, 'job', job), patch.object(main.HUB, 'state'), patch.object(main.HUB, 'log'), \
             patch.object(main.subprocess, 'run', side_effect=AssertionError('no desktop commands')), \
             patch.object(main, 'stop_all', side_effect=AssertionError('no signal')):
            client = TestClient(main.app)
            response = client.post('/api/record/key', json={'key':'esc'})
            self.assertEqual(response.status_code,409)
            self.assertEqual(job.proc.stdin.getvalue(), b'')
            main._record_line(job, READY)
            for key in ['right', 'left', 'esc']:
                response = client.post('/api/record/key', json={'key': key})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['transport'], 'stdin')
            self.assertEqual(job.proc.stdin.getvalue(), b''.join((PREFIX+k+'\n').encode() for k in ['right','left','esc']))
            response = client.post('/api/record/key', json={'key':'bad'})
            self.assertEqual(response.status_code,400)


if __name__ == '__main__': unittest.main()
