import signal
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.recorder import Recorder, RecordingTask
from core.recording_progress import RecordingProgress
from core import recording_progress as policy
from core.media_probe import probe_media
from core.scheduler import RecordingScheduler
from gui.recording_panel import RecordingPanel, STATE_ACCENT
from utils.config import Config


class ProgressTests(unittest.TestCase):
    def task(self):
        return RecordingTask('id', 'channel', 'url', '/tmp/result.mp4')

    def test_progress_requires_growing_media_values_not_heartbeats(self):
        task = self.task()
        progress = RecordingProgress(Mock(), task, Mock())
        progress.last_progress = progress.started = 0
        progress.feed('progress=continue', now=10)
        progress.feed('out_time_us=N/A', now=10)
        self.assertEqual(progress.last_progress, 0)
        progress.feed('out_time_us=1000000', now=10)
        progress.feed('total_size=5000', now=11)
        progress.feed('out_time_us=1000000', now=30)
        progress.feed('total_size=4000', now=30)
        self.assertEqual(progress.last_progress, 11)
        self.assertEqual(task.media_seconds, 1)
        self.assertEqual(task.recorded_bytes, 5000)
        with patch.object(policy, 'STALL_TIMEOUT', 20):
            self.assertFalse(progress.check(now=25))
            self.assertEqual(task.connection_state, 'reconnecting')
            self.assertTrue(progress.check(now=32))

    def test_startup_and_pause_have_separate_grace(self):
        task = self.task()
        progress = RecordingProgress(Mock(), task, Mock())
        progress.last_progress = 0
        with patch.object(policy, 'STARTUP_TIMEOUT', 90):
            self.assertFalse(progress.check(now=89))
            self.assertTrue(progress.check(now=90))
            task.is_paused = True
            self.assertFalse(progress.check(now=1000))
            task.is_paused = False
            self.assertFalse(progress.check(now=1001))

    def test_manual_stop_during_backoff_cancels_recovery(self):
        recorder, task = Recorder(), self.task()
        task.recovering = True
        task.process = Mock()
        task.process.poll.return_value = 0
        recorder.tasks[task.task_id] = task
        recorder.stop_recording(task.task_id)
        self.assertTrue(task.stop_requested)
        self.assertTrue(task.retry_cancel.is_set())
        self.assertFalse(recorder._retry_allowed(task))

    def test_no_retry_for_vod_storage_failure_or_expired_window(self):
        recorder, task = Recorder(), self.task()
        task.success = False
        self.assertFalse(recorder._retry_allowed(task))
        task.is_live = True
        self.assertTrue(recorder._retry_allowed(task))
        task.storage_error = 'full'
        self.assertFalse(recorder._retry_allowed(task))
        task.storage_error = ''
        task.deadline = time.time()-1
        self.assertFalse(recorder._retry_allowed(task))

    def test_expired_deadline_prevents_new_part_process(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder()
            task = RecordingTask('id', 'channel', 'url', str(Path(directory)/'out.mp4'))
            task.command = ['ffmpeg', '-i', 'url', 'out.mp4']
            task.source_stream_url = 'url'
            task.deadline = time.time()-1
            with patch.object(recorder, '_check_output_directory', return_value=True), \
                    patch('core.recorder.resolve_variant_url', return_value='url'), \
                    patch('core.recorder.subprocess.Popen') as launch:
                self.assertFalse(recorder._restart_part(task))
            launch.assert_not_called()

    def test_scheduler_does_not_reset_recorders_retry_budget(self):
        with patch('core.scheduler.Storage'), patch('core.scheduler.Notifier'):
            scheduler = RecordingScheduler(recorder=Mock())
        with patch.object(scheduler, '_active_window') as window:
            scheduler._on_recording_complete(False, 'channel', '/tmp/result.mp4', True,
                                             'partial', schedule_item={'enabled': True}, retry_allowed=False)
        window.assert_not_called()
        self.assertEqual(scheduler._attempts, {})

    def test_scheduled_vod_completion_does_not_restart_from_beginning(self):
        from datetime import datetime, timedelta
        from types import SimpleNamespace
        with patch('core.scheduler.Storage'), patch('core.scheduler.Notifier'):
            scheduler = RecordingScheduler(recorder=Mock())
        item = dict(channel_name='video', source_type='link', enabled=True)
        target = dict(name='video', url='https://example/video')
        scheduler._running = True
        scheduler._entries = [(target, item, 0)]
        scheduler.recorder.start_recording.return_value = 'task'
        info = SimpleNamespace(ok=True, video_url='https://example/file.mp4', audio_url=None, headers={})
        with patch('core.scheduler.resolve_link', return_value=info):
            self.assertTrue(scheduler._pre_record_check(target, item, 0,
                            deadline=datetime.now()+timedelta(minutes=1)))
        callback = scheduler.recorder.start_recording.call_args.kwargs['on_complete']
        callback(False, 'video', '/tmp/partial.mp4', True, 'partial')
        self.assertEqual(scheduler._attempts, {})

    def test_scheduler_failed_starts_are_bounded_per_occurrence(self):
        from datetime import datetime, timedelta
        with patch('core.scheduler.Storage'), patch('core.scheduler.Notifier'):
            scheduler = RecordingScheduler(recorder=Mock())
        now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        item = dict(channel_name='channel', enabled=True, start_time='12:00', end_time='13:00')
        target = dict(name='channel', url='url')
        scheduler._running = True
        scheduler._entries = [(target, item, 0)]
        scheduler._pre_record_check = Mock(return_value=False)
        for i in range(7):
            current = now+timedelta(seconds=31*i)
            with patch('core.scheduler.datetime') as clock:
                clock.now.return_value = current
                scheduler._run_scheduled_recording(target, item, 0, now=current)
        self.assertEqual(scheduler._pre_record_check.call_count, 4)
        next_day = now+timedelta(days=1)
        with patch('core.scheduler.datetime') as clock:
            clock.now.return_value = next_day
            scheduler._run_scheduled_recording(target, item, 0, now=next_day)
        self.assertEqual(scheduler._pre_record_check.call_count, 5)

    def test_ui_states_have_valid_colors_and_recovery_keeps_task_active(self):
        panel = object.__new__(RecordingPanel)
        recorder, task = Recorder(), self.task()
        recorder.tasks[task.task_id] = task
        task.is_recording = True
        self.assertEqual(panel._task_state(task), 'connecting')
        task.connection_state = 'reconnecting'
        self.assertIn(panel._task_state(task), STATE_ACCENT)
        task.is_recording = False
        task.recovering = True
        task.connection_state = 'retrying'
        self.assertEqual(panel._task_state(task), 'retrying')
        self.assertEqual(recorder.find_active_task_id('channel'), 'id')
        self.assertEqual(recorder.remove_completed_tasks(), 0)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class RecordingRecoveryIntegrationTests(unittest.TestCase):
    def run_recording(self, *, stall=False, all_fail=False, stop_in_backoff=False, deadline_seconds=9):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            recorder = Recorder()
            real_popen = subprocess.Popen
            attempts = []
            callback = Mock()
            freezer = None
            def launch(cmd, *args, **kwargs):
                nonlocal freezer
                if cmd[0] == 'ffmpeg' and str(cmd[-1]).endswith('.recording.mp4'):
                    attempt = len(attempts)
                    if all_fail:
                        replacement = ['ffmpeg', '-v', 'error', '-i', str(folder/'missing.mp4'), cmd[-1]]
                    else:
                        duration = '30' if stall or attempt else '1.5'
                        replacement = ['ffmpeg', '-v', 'error', '-progress', 'pipe:1', '-nostats',
                                       '-re', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25',
                                       '-t', duration, '-c:v', 'libx264', '-preset', 'ultrafast',
                                       '-g', '25', '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
                                       '-frag_duration', '1000000', '-flush_packets', '1', cmd[-1]]
                    child = real_popen(replacement, *args, **kwargs)
                    attempts.append(child)
                    if stall and attempt == 0:
                        def freeze():
                            until = time.monotonic()+5
                            while time.monotonic() < until:
                                path = Path(cmd[-1])
                                if path.exists() and path.stat().st_size > 10000:
                                    break
                                time.sleep(.05)
                            if child.poll() is None:
                                child.send_signal(signal.SIGSTOP)
                        freezer = threading.Thread(target=freeze)
                        freezer.start()
                    return child
                return real_popen(cmd, *args, **kwargs)
            delay = (2,) if stop_in_backoff else (.05, .05)
            with patch.object(Config, 'DATA_DIR', folder/'data'), \
                    patch.object(Config, 'RECORDINGS_DIR', folder), \
                    patch.object(Config, 'DOWNLOADS_DIR', folder/'downloads'), \
                    patch.object(Config, 'SETTINGS_FILE', folder/'settings.json'), \
                    patch('core.recorder.subprocess.Popen', side_effect=launch), \
                    patch.object(policy, 'RETRY_DELAYS', delay), \
                    patch.object(policy, 'STALL_TIMEOUT', 1.5), \
                    patch.object(policy, 'STARTUP_TIMEOUT', 4), \
                    patch.object(policy, 'STOP_GRACE', .2):
                try:
                    tid = recorder.start_recording('channel', 'local', str(folder/'result.mp4'),
                                                   is_live_channel=True,
                                                   start_deadline_timestamp=time.time()+deadline_seconds,
                                                   on_complete=callback)
                    task = recorder.tasks[tid]
                    if stop_in_backoff:
                        until = time.monotonic()+10
                        while task.connection_state != 'retrying' and time.monotonic()<until:
                            time.sleep(.03)
                        self.assertEqual(task.connection_state, 'retrying')
                        recorder.stop_recording(tid)
                    until = time.monotonic()+18
                    while not callback.called and time.monotonic()<until:
                        time.sleep(.05)
                    self.assertTrue(callback.called)
                    callback.assert_called_once()
                    self.assertLessEqual(len(attempts), len(delay)+1)
                    if all_fail:
                        self.assertEqual(len(attempts), 3)
                        self.assertTrue(task.recovery_exhausted)
                        self.assertEqual(task.result_status, 'processing_error')
                    else:
                        self.assertEqual(len(attempts), 1 if stop_in_backoff else 2)
                        self.assertEqual(task.result_status, 'partial')
                        readable = [p for p in task.parts if p['status'] in ('completed', 'partial')]
                        self.assertTrue(readable)
                        for part in readable:
                            self.assertTrue(probe_media(part['path']).has_video)
                        if stall:
                            self.assertTrue(task.parts[0]['stalled'])
                            self.assertTrue(probe_media(task.parts[0]['path']).has_video)
                    self.assertFalse(recorder._stop_timers)
                finally:
                    recorder.stop_all()
                    if freezer:
                        freezer.join(timeout=4)

    def test_stalled_process_is_killed_and_next_part_reaches_deadline(self):
        self.run_recording(stall=True)

    def test_early_eof_restarts_without_overwriting_previous_part(self):
        self.run_recording(deadline_seconds=5)

    def test_repeated_failures_exhaust_budget_even_with_no_output(self):
        self.run_recording(all_fail=True)

    def test_stop_while_waiting_prevents_any_new_process(self):
        self.run_recording(stop_in_backoff=True)
