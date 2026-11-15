from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.live_stream import LiveThumbnailStream
from core.monitor_profile import monitor_profile
from core.recorder import Recorder, RecordingTask
from core.stream_resolver import RECONNECT_OPTS
from gui.recording_monitor import RecordingMonitorWindow
from utils.config import Config


class MonitorPerformanceTests(unittest.TestCase):
    def recorder(self, count):
        recorder = Recorder()
        for i in range(count):
            task = RecordingTask(str(i), f'channel-{i}', 'url', '/tmp/out.mp4')
            task.is_recording = True
            task.process = Mock()
            recorder.tasks[task.task_id] = task
        return recorder

    def test_adaptive_profiles_and_off(self):
        self.assertIsNone(monitor_profile('off', 16))
        self.assertIsNone(monitor_profile('economy', 0))
        self.assertEqual(monitor_profile('economy', 4).fps, 1)
        self.assertEqual(monitor_profile('economy', 5).fps, .5)
        self.assertTrue(monitor_profile('economy', 1).keyframes_only)
        self.assertEqual([monitor_profile('normal', n).fps for n in (1, 4, 9)], [4, 2, 1])
        self.assertFalse(monitor_profile('normal', 16).keyframes_only)

    def test_modes_stop_decoders_and_reject_stale_frames_without_touching_recording(self):
        recorder = self.recorder(1)
        task = recorder.tasks['0']
        streams, callbacks = [], []
        def create(*args, **kwargs):
            stream = Mock()
            streams.append(stream)
            callbacks.append(kwargs['on_frame'])
            return stream
        with patch('core.recorder.LiveThumbnailStream', side_effect=create):
            recorder.acquire_snapshot_consumer()
            callbacks[0](b'first')
            self.assertEqual(task.last_snapshot, b'first')
            recorder.set_snapshot_mode('off', persist=False)
            streams[0].stop.assert_called_once()
            callbacks[0](b'stale')
            self.assertIsNone(task.last_snapshot)
            recorder.set_snapshot_mode('normal', persist=False)
            self.assertEqual(len(streams), 2)
            callbacks[0](b'still stale')
            self.assertIsNone(task.last_snapshot)
            callbacks[1](b'new')
            self.assertEqual(task.last_snapshot, b'new')
            recorder.release_snapshot_consumer()
        self.assertTrue(task.is_recording)
        task.process.terminate.assert_not_called()
        task.process.kill.assert_not_called()

    def test_consumer_balance_and_same_profile_do_not_duplicate_processes(self):
        recorder = self.recorder(2)
        with patch('core.recorder.LiveThumbnailStream', side_effect=lambda *a, **k: Mock()) as create:
            recorder.acquire_snapshot_consumer()
            streams = [task.snapshot_stream for task in recorder.tasks.values()]
            recorder.acquire_snapshot_consumer()
            recorder._sync_snapshot_streams()
            self.assertEqual(create.call_count, 2)
            recorder.release_snapshot_consumer()
            for stream in streams:
                stream.stop.assert_not_called()
            recorder.release_snapshot_consumer()
            recorder.release_snapshot_consumer()
            for stream in streams:
                stream.stop.assert_called_once()
            self.assertEqual(recorder._snapshot_consumers, 0)

    def test_task_count_changes_budget_but_paused_and_screen_tasks_do_not_decode(self):
        recorder = self.recorder(5)
        with patch('core.recorder.LiveThumbnailStream', side_effect=lambda *a, **k: Mock()) as create:
            recorder.acquire_snapshot_consumer()
            self.assertEqual(create.call_count, 5)
            self.assertEqual(recorder.tasks['0'].snapshot_profile.fps, .5)
            recorder.tasks['4'].is_paused = True
            recorder.tasks['3'].is_screen_capture = True
            recorder._sync_snapshot_streams()
            self.assertIsNone(recorder.tasks['4'].snapshot_stream)
            self.assertIsNone(recorder.tasks['3'].snapshot_stream)
            self.assertEqual(recorder.tasks['0'].snapshot_profile.fps, 1)
            recorder.release_snapshot_consumer()

    def test_mode_persists_without_overwriting_other_settings(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(Config, 'SETTINGS_FILE', Path(directory)/'settings.json'), \
                patch.object(Config, 'DATA_DIR', Path(directory)):
            Config._write_settings({'recordings_dir': directory})
            recorder = Recorder()
            recorder.set_snapshot_mode('normal')
            self.assertEqual(Config._read_settings()['recordings_dir'], directory)
            self.assertEqual(Recorder().snapshot_mode, 'normal')
        with patch.object(Config, '_read_settings', return_value={'snapshot_mode': 'unknown'}):
            self.assertEqual(Recorder().snapshot_mode, 'economy')

    def test_unmap_map_are_balanced_and_completed_tasks_are_excluded(self):
        window = object.__new__(RecordingMonitorWindow)
        window._running, window._visible, window._owns_snapshots = True, True, True
        window.recorder = Mock(snapshot_mode='economy')
        window._stop_listening = Mock()
        window._maybe_rebuild = Mock()
        window._clear_images = Mock()
        window._set_visible(False)
        window._set_visible(False)
        window.recorder.release_snapshot_consumer.assert_called_once()
        window._set_visible(True)
        window._set_visible(True)
        window.recorder.acquire_snapshot_consumer.assert_called_once()
        window._unmapped(SimpleNamespace(widget=object()))
        window.recorder.release_snapshot_consumer.assert_called_once()
        active, paused, finished = (RecordingTask(str(i), 'name', 'url', 'out') for i in range(3))
        active.is_recording = True
        paused.recovering = paused.is_paused = True
        window.recorder.get_all_tasks.return_value = [active, paused, finished]
        self.assertEqual(window._monitor_tasks(), [active, paused])

    def test_keyframe_mode_cannot_change_archive_timing(self):
        with self.assertRaises(ValueError):
            LiveThumbnailStream('url', None, 1, Mock(), keyframes_only=True, on_timed_frame=Mock())


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
class RealThumbnailPerformanceTests(unittest.TestCase):
    def test_economy_decodes_first_keyframe_and_stop_reaps_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'source.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=size=320x180:rate=25', '-t', '8', '-c:v', 'libx264',
                            '-preset', 'ultrafast', '-g', '25', str(path)], check=True)
            real_popen = subprocess.Popen
            commands, children, frames = [], [], []
            def launch(cmd, **kwargs):
                commands.append(list(cmd))
                excluded = set(RECONNECT_OPTS[::2]) | {'-headers'}
                filtered, index = [], 0
                while index < len(cmd):
                    if cmd[index] in excluded:
                        index += 2
                    else:
                        filtered.append(cmd[index])
                        index += 1
                process = real_popen(filtered, **kwargs)
                children.append(process)
                return process
            stream = LiveThumbnailStream(str(path), None, 1, frames.append, width=220, keyframes_only=True)
            try:
                with patch('core.live_stream.subprocess.Popen', side_effect=launch):
                    stream.start()
                    until = time.monotonic()+6
                    while not frames and time.monotonic()<until:
                        time.sleep(.03)
                    self.assertTrue(frames)
                    self.assertTrue(frames[0].startswith(b'\xff\xd8'))
                    self.assertLess(commands[0].index('-skip_frame'), commands[0].index('-i'))
                    self.assertIn('nokey', commands[0])
                    stream.stop()
                    self.assertTrue(stream.wait_stopped(5))
                    self.assertEqual(len(children), 1)
                    self.assertIsNotNone(children[0].poll())
                    self.assertTrue(children[0].stdout.closed)
            finally:
                stream.stop()
                stream.wait_stopped(5)
