import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from core.media_probe import MediaProbeResult, assess_probe, probe_media, COMPLETED, PARTIAL, PROCESSING_ERROR
from core.recording_history import RecordingHistory
from core.recorder import Recorder, RecordingTask
from core.recording_recovery import prepare_recording, release_recording, finalize_recording
from gui.recording_history import format_record
from utils.config import Config


class AssessmentTests(unittest.TestCase):
    def probe(self, duration=10, audio=True, **kwargs):
        return MediaProbeResult(True, has_video=True, has_audio=audio, duration=duration, **kwargs)

    def test_clean_exit_with_short_duration_is_partial(self):
        result = assess_probe(self.probe(), True, expected_duration=30)
        self.assertEqual(result.status, PARTIAL)
        self.assertEqual(result.missing_seconds, 20)
        self.assertIn('Недобор', '; '.join(result.reasons))

    def test_tolerance_unknown_length_and_nonfinite_duration(self):
        self.assertEqual(assess_probe(self.probe(59.8), True, expected_duration=60).status, COMPLETED)
        self.assertEqual(assess_probe(self.probe(), True).status, COMPLETED)
        for value in (None, float('nan'), float('inf'), -1, 0):
            self.assertEqual(assess_probe(self.probe(value), True, expected_duration=10).status, PARTIAL)

    def test_expected_audio_missing_or_truncated_is_partial(self):
        self.assertEqual(assess_probe(self.probe(audio=False), True, expect_audio=True).status, PARTIAL)
        result = assess_probe(self.probe(video_duration=10, audio_duration=2), True, expect_audio=True)
        self.assertEqual(result.status, PARTIAL)
        self.assertIn('короче', '; '.join(result.reasons))
        self.assertEqual(assess_probe(self.probe(audio=False), True, expect_audio=False).status, COMPLETED)
        self.assertEqual(assess_probe(MediaProbeResult(False, error='broken'), True).status, PROCESSING_ERROR)

    def test_long_audio_cannot_hide_missing_video(self):
        probe = self.probe(30, video_duration=2, audio_duration=30)
        result = assess_probe(probe, True, expect_audio=True, expected_duration=30)
        self.assertEqual(result.status, PARTIAL)
        self.assertEqual(result.actual_duration, 2)
        self.assertEqual(result.missing_seconds, 28)
        self.assertEqual(assess_probe(probe, True, expect_audio=True).status, PARTIAL)

    def test_probe_reads_track_durations_and_sanitizes_nan(self):
        with tempfile.NamedTemporaryFile(suffix='.mp4') as file:
            file.write(b'x'*2048)
            file.flush()
            metadata = dict(streams=[dict(codec_type='video', duration='10'),
                                     dict(codec_type='audio', duration='2')], format={'duration': 'NaN'})
            with patch('core.media_probe.subprocess.run', return_value=Mock(
                    returncode=0, stdout=json.dumps(metadata))):
                probe = probe_media(file.name)
        self.assertIsNone(probe.duration)
        self.assertEqual((probe.video_duration, probe.audio_duration), (10, 2))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.history = RecordingHistory(self.folder)
        self.addCleanup(self.history.close)

    def snapshot(self, state='recording'):
        return dict(task_id='test', name='channel', state=state, parts=[])

    def test_completed_history_survives_reopen(self):
        self.history.save(self.snapshot(), 'started')
        self.history.save(self.snapshot('partial'), 'completed')
        self.history.close()
        reopened = RecordingHistory(self.folder)
        self.addCleanup(reopened.close)
        result = reopened.read('test')
        self.assertEqual(result['session']['state'], 'partial')
        self.assertEqual([event['type'] for event in result['events']], ['started', 'completed'])

    def test_active_owner_is_not_interrupted_but_crashed_owner_is(self):
        self.history.save(self.snapshot(), 'started')
        second = RecordingHistory(self.folder)
        self.addCleanup(second.close)
        self.assertEqual(second.read('test')['session']['state'], 'recording')
        self.history.close()
        third = RecordingHistory(self.folder)
        self.addCleanup(third.close)
        record = third.read('test')
        self.assertEqual(record['session']['state'], 'interrupted')
        self.assertEqual(record['events'][-1]['type'], 'interrupted')

    def test_corrupt_record_recovers_atomic_backup(self):
        self.history.save(self.snapshot(), 'started')
        self.history.save(self.snapshot('paused'), 'paused')
        (self.folder/'test.json').write_text('{broken')
        self.assertEqual(self.history.read('test')['session']['state'], 'recording')
        json.loads((self.folder/'test.json').read_text())

    def test_bad_schema_does_not_hide_other_records(self):
        (self.folder/'bad.json').write_text('["wrong schema"]')
        self.history.save(self.snapshot('completed'), 'completed')
        self.assertEqual(len(self.history.list_records()), 2)
        self.assertTrue(any(record['session']['state']=='history_error' for record in self.history.list_records()))

    def test_concurrent_events_are_not_lost(self):
        threads = [threading.Thread(target=self.history.save,
                                    args=(self.snapshot(), f'event-{i}')) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.history.read('test')['events']), 12)

    def test_no_source_secrets_and_ui_clear_keeps_history(self):
        recorder = Recorder(history=self.history)
        task = RecordingTask('safe', 'channel', 'https://secret.test/?token=private', '/tmp/out.mp4')
        task.headers = {'Authorization': 'very-secret-password'}
        task.error_message = 'HTTP error https://secret.test/?token=private'
        task.result_status = PARTIAL
        recorder.tasks[task.task_id] = task
        recorder._history_event(task, 'completed', state='partial')
        recorder.remove_completed_tasks()
        raw = (self.folder/'safe.json').read_text()
        self.assertNotIn('private', raw)
        self.assertNotIn('very-secret-password', raw)
        self.assertTrue(self.history.read('safe'))

    def test_history_failure_does_not_stop_recording_even_if_ui_fails(self):
        recorder = Recorder(history=Mock())
        recorder.history.save.side_effect = OSError('disk full')
        recorder.history_error_callback = Mock(side_effect=RuntimeError('UI unavailable'))
        task = RecordingTask('safe', 'channel', 'url', '/tmp/out.mp4')
        task.is_recording = True
        recorder._history_event(task, 'started')
        recorder._history_event(task, 'part_started')
        self.assertTrue(task.is_recording)
        self.assertTrue(task.history_error)
        recorder.history_error_callback.assert_called_once()

    def test_session_duration_sums_parts_and_missing_interval(self):
        recorder = Recorder()
        task = RecordingTask('safe', 'channel', 'url', '/tmp/out.mp4')
        task.is_live = True
        task.start_time, task.part_ended_wall = 100, 160
        task.parts = [{'duration': 20, 'status': COMPLETED}, {'duration': 10, 'status': PARTIAL},
                      {'duration': 100, 'status': PROCESSING_ERROR}]
        task.result_status, task.success = COMPLETED, True
        recorder._update_session_assessment(task)
        self.assertEqual((task.expected_seconds, task.actual_seconds, task.missing_seconds), (60, 30, 30))
        self.assertEqual(task.result_status, PARTIAL)

    def test_auto_join_publishes_single_callback_target_and_keeps_partial_status(self):
        recorder = Recorder()
        task = RecordingTask('auto', 'channel', 'url', '/tmp/record.mp4')
        task.parts = [dict(path='/tmp/record.mp4', status=PARTIAL),
                      dict(path='/tmp/failed.mp4', status=PROCESSING_ERROR),
                      dict(path='/tmp/record_part003.mp4', status=COMPLETED)]
        task.result_status, task.success = PARTIAL, False
        with patch('core.recording_merge.merge_mp4_parts', return_value=Path('/tmp/record_joined.mp4')) as merge:
            recorder._auto_join_parts(task)
        merge.assert_called_once_with(['/tmp/record.mp4', '/tmp/record_part003.mp4'], Path('/tmp/record_joined.mp4'))
        self.assertEqual(task.output_path, '/tmp/record_joined.mp4')
        self.assertEqual(task.result_status, PARTIAL)
        self.assertEqual(len(task.parts), 3)
        self.assertFalse(task.merging)

    def test_failed_auto_join_preserves_parts_and_diagnostic(self):
        recorder = Recorder()
        task = RecordingTask('auto', 'channel', 'url', '/tmp/record.mp4')
        task.parts = [dict(path='/tmp/a.mp4', status=PARTIAL), dict(path='/tmp/b.mp4', status=COMPLETED)]
        with patch('core.recording_merge.merge_mp4_parts', side_effect=ValueError('different codecs')):
            recorder._auto_join_parts(task)
        self.assertEqual(task.output_path, '/tmp/record.mp4')
        self.assertEqual(len(task.parts), 2)
        self.assertIn('Автоматическое объединение не удалось', task.error_message)

    def test_format_explains_duration_tracks_and_events(self):
        record = dict(session=dict(name='ТНТ', state='partial', expected_seconds=30,
            actual_seconds=10, missing_seconds=20, error='Нет звука',
            parts=[dict(path='/tmp/part.mp4', status='partial', duration=10,
                        has_video=True, has_audio=False)]),
            events=[dict(at='2026-11-14T12:00:00+03:00', type='stalled')])
        text = format_record(record)
        for value in ('ТНТ', '30.0', '10.0', '20.0', 'Нет звука', 'звук: нет', 'перестал продвигаться'):
            self.assertIn(value, text)

    def test_recovery_remains_successful_when_history_and_warning_fail(self):
        from gui.app_window import AppWindow
        marker = self.folder/'pending.json'
        marker.write_text(json.dumps({'capture': '/tmp/raw.mp4'}))
        app = object.__new__(AppWindow)
        app._closing = False
        app.recorder = Mock()
        app.recorder.get_all_tasks.return_value = []
        app.recorder.history.record_recovered.side_effect = OSError('history full')
        app.recorder.history_error_callback.side_effect = RuntimeError('window unavailable')
        app.root = Mock()
        app.root.after.side_effect = lambda delay, callback: callback()
        def run_now(*args, **kwargs):
            return Mock(start=kwargs['target'])
        with patch('core.recording_recovery.pending_recordings', return_value=[marker]), \
                patch('core.recording_recovery.finalize_recording', return_value=Path('/tmp/out.mp4')), \
                patch('gui.app_window.threading.Thread', side_effect=run_now), \
                patch('gui.app_window.messagebox.askyesno', return_value=True), \
                patch('gui.app_window.messagebox.showinfo') as show:
            app._offer_recording_recovery()
        self.assertIn('Восстановлено записей: 1', show.call_args.args[1])


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class RealHistoryTests(unittest.TestCase):
    def test_real_short_record_persists_deficit_and_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            history = RecordingHistory(folder/'history')
            recorder = Recorder(history=history)
            callback = Mock()
            real_popen = subprocess.Popen
            def launch(cmd, *args, **kwargs):
                if cmd[0] == 'ffmpeg' and str(cmd[-1]).endswith('.recording.mp4'):
                    cmd = ['ffmpeg', '-v', 'error', '-progress', 'pipe:1', '-nostats',
                           '-re', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25', '-t', '2',
                           '-c:v', 'libx264', '-preset', 'ultrafast', '-movflags',
                           '+frag_keyframe+empty_moov+default_base_moof', '-frag_duration', '1000000', cmd[-1]]
                return real_popen(cmd, *args, **kwargs)
            with patch.object(Config, 'DATA_DIR', folder/'data'), \
                    patch.object(Config, 'RECORDINGS_DIR', folder), \
                    patch.object(Config, 'DOWNLOADS_DIR', folder/'downloads'), \
                    patch.object(Config, 'SETTINGS_FILE', folder/'settings.json'), \
                    patch('core.recorder.subprocess.Popen', side_effect=launch):
                try:
                    tid = recorder.start_recording('short source', 'local', str(folder/'out.mp4'),
                                                   expected_duration_seconds=5, on_complete=callback)
                    until = time.monotonic()+10
                    while not callback.called and time.monotonic()<until:
                        time.sleep(.05)
                    callback.assert_called_once()
                    record = history.read(tid)
                    self.assertEqual(record['session']['state'], PARTIAL)
                    self.assertAlmostEqual(record['session']['missing_seconds'], 3, delta=.2)
                    self.assertTrue(record['session']['parts'][0]['has_video'])
                    recorder.remove_completed_tasks()
                    self.assertTrue(history.read(tid))
                finally:
                    recorder.stop_all()
                    history.close()

    def test_recovery_updates_interrupted_session(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            history = RecordingHistory(folder/'history')
            with patch.object(Config, 'DATA_DIR', folder/'data'):
                capture, marker = prepare_recording(folder/'out.mp4', 'crash-test')
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                                'testsrc2=size=320x180:rate=25', '-t', '1', '-c:v', 'libx264',
                                '-preset', 'ultrafast', '-movflags',
                                '+frag_keyframe+empty_moov+default_base_moof', str(capture)], check=True)
                history.save(dict(task_id='crash-test', name='channel', state='recording',
                                  capture_path=str(capture), parts=[]), 'started')
                history.close()
                history = RecordingHistory(folder/'history')
                self.assertEqual(history.read('crash-test')['session']['state'], 'interrupted')
                release_recording(marker)
                output = finalize_recording(marker)
                history.record_recovered(capture, output)
                record = history.read('crash-test')
                self.assertEqual(record['session']['state'], PARTIAL)
                self.assertEqual(record['session']['parts'][0]['path'], str(output))
                self.assertEqual(record['events'][-1]['type'], 'file_recovered')
                history.close()
