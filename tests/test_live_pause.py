import hashlib
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.recorder import Recorder, RecordingTask
from core.recording_merge import merge_mp4_parts
from core.media_probe import probe_media
from core import recording_progress as policy
from core.disk_space import DiskSpaceError
from utils.config import Config


class LivePauseUnitTests(unittest.TestCase):
    def test_live_pause_and_fast_resume_never_freeze_process(self):
        recorder = Recorder()
        task = RecordingTask('id', 'live', 'url', '/tmp/file.mp4')
        task.is_live = task.is_recording = task.recovering = True
        task.process = Mock()
        task.process.poll.return_value = None
        task.start_time = time.time()-5
        recorder.tasks['id'] = task
        recorder.pause_recording('id')
        self.assertTrue(task.is_paused)
        self.assertTrue(task.pause_pending)
        self.assertEqual(task.pause_count, 1)
        task.process.terminate.assert_called_once()
        task.process.send_signal.assert_not_called()
        recorder.pause_recording('id')
        self.assertFalse(task.is_paused)
        self.assertTrue(task.pause_pending)  # Supervisor must still start a new part.
        task.process.send_signal.assert_not_called()
        self.assertEqual(task.retry_count, 0)

    def test_resume_after_part_exit_is_allowed(self):
        recorder = Recorder()
        task = RecordingTask('id', 'live', 'url', '/tmp/file.mp4')
        task.is_live = task.recovering = task.is_paused = task.pause_pending = True
        task.pause_time = time.time()-5
        task.process = Mock()
        task.process.poll.return_value = 255
        recorder.tasks['id'] = task
        recorder.pause_recording('id')
        self.assertFalse(task.is_paused)
        self.assertGreaterEqual(task.total_paused_duration, 5)
        task.process.send_signal.assert_not_called()


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class LivePauseIntegrationTests(unittest.TestCase):
    def wait_for(self, condition, timeout=8):
        until = time.monotonic()+timeout
        while not condition() and time.monotonic()<until:
            time.sleep(.03)
        self.assertTrue(condition())

    def session(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            recorder = Recorder()
            callback = Mock()
            processes, signals, commands = [], [], []
            real_popen = subprocess.Popen
            def launch(cmd, *args, **kwargs):
                if cmd[0] == 'ffmpeg' and str(cmd[-1]).endswith('.recording.mp4'):
                    commands.append(list(cmd))
                    cmd = ['ffmpeg', '-v', 'error', '-progress', 'pipe:1', '-nostats',
                           '-re', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25',
                           '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000',
                           '-t', '60', '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '25',
                           '-c:a', 'aac', '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
                           '-frag_duration', '1000000', '-flush_packets', '1', cmd[-1]]
                    proc = real_popen(cmd, *args, **kwargs)
                    original_signal = proc.send_signal
                    def send(sig):
                        signals.append(sig)
                        original_signal(sig)
                    proc.send_signal = send
                    processes.append(proc)
                    return proc
                return real_popen(cmd, *args, **kwargs)
            with patch.object(Config, 'DATA_DIR', folder/'data'), \
                    patch.object(Config, 'RECORDINGS_DIR', folder), \
                    patch.object(Config, 'DOWNLOADS_DIR', folder/'downloads'), \
                    patch.object(Config, 'SETTINGS_FILE', folder/'settings.json'), \
                    patch('core.recorder.resolve_variant_url', side_effect=lambda url, **kwargs: url), \
                    patch('core.recorder.subprocess.Popen', side_effect=launch), \
                    patch.object(policy, 'STOP_GRACE', 1):
                try:
                    deadline = time.time()+(4 if mode == 'deadline' else 25)
                    tid = recorder.start_recording('live', 'https://example/live.m3u8', str(folder/'result.mp4'),
                                                   is_live_channel=True, start_deadline_timestamp=deadline,
                                                   on_complete=callback)
                    task = recorder.tasks[tid]
                    def captured():
                        return task.capture_path.exists() and task.capture_path.stat().st_size > 10000
                    self.wait_for(captured)
                    recorder.pause_recording(tid)
                    if mode == 'fast':
                        recorder.pause_recording(tid)
                    else:
                        self.wait_for(lambda: len(task.parts) == 1 and not task.is_recording)
                        self.assertTrue(task.is_paused)
                        self.assertFalse(callback.called)
                        self.assertTrue(probe_media(task.parts[0]['path']).has_audio)
                        count = len(processes)
                        time.sleep(.3)
                        self.assertEqual(len(processes), count)
                    if mode == 'stop':
                        recorder.stop_recording(tid)
                    elif mode == 'deadline':
                        pass
                    elif mode == 'shutdown':
                        recorder.stop_all()
                    else:
                        if mode != 'fast':
                            recorder.pause_recording(tid)
                        self.wait_for(lambda: len(processes) == 2)
                        self.wait_for(captured)
                        if mode == 'repeat':
                            recorder.pause_recording(tid)
                            self.wait_for(lambda: len(task.parts) == 2 and not task.is_recording)
                            self.assertTrue(task.is_paused)
                            recorder.pause_recording(tid)
                            self.wait_for(lambda: len(processes) == 3)
                            self.wait_for(captured)
                        recorder.stop_recording(tid)
                    self.wait_for(lambda: callback.called, timeout=10)
                    self.wait_for(lambda: not task.recovering)
                    callback.assert_called_once()
                    self.assertEqual(task.retry_count, 0)
                    self.assertEqual(task.pause_count, 2 if mode == 'repeat' else 1)
                    self.assertEqual(len(processes), 1 if mode in ('stop', 'deadline', 'shutdown') else (3 if mode == 'repeat' else 2))
                    self.assertEqual(task.result_status, 'partial')
                    self.assertNotIn(signal.SIGSTOP, signals)
                    self.assertNotIn(signal.SIGCONT, signals)
                    self.assertEqual(len({part['path'] for part in task.parts}), len(task.parts))
                    for part in task.parts:
                        self.assertTrue(probe_media(part['path']).has_video)
                    if len(commands) > 1:
                        self.assertEqual(commands[1][commands[1].index('-live_start_index')+1], '-1')
                    if mode == 'repeat':
                        paths = [Path(part['path']) for part in task.parts]
                        original_hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
                        self.assertTrue(task.merged_path)  # No manual action required.
                        self.assertTrue(task.output_path.endswith('_joined.mp4'))
                        merged = recorder.merge_recording_parts(tid)
                        self.assertTrue(probe_media(merged).has_audio)
                        expected = sum(probe_media(path).duration for path in paths)
                        self.assertAlmostEqual(probe_media(merged).duration, expected, delta=.3)
                        self.assertEqual(original_hashes, [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])
                        self.assertEqual(task.output_path, str(merged))
                finally:
                    recorder.stop_all()

    def test_two_pauses_create_three_parts_and_lossless_join(self):
        self.session('repeat')

    def test_fast_resume_waits_for_old_part_to_finish(self):
        self.session('fast')

    def test_stop_during_pause_does_not_restart(self):
        self.session('stop')

    def test_deadline_during_pause_does_not_restart(self):
        self.session('deadline')

    def test_shutdown_during_pause_does_not_restart(self):
        self.session('shutdown')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class MergePartsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="parts with 'quote ")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.paths = []
        for i, size in enumerate(('320x180', '320x180', '640x360')):
            path = self.folder/f'part {i}.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=25',
                            '-t', '1', '-c:v', 'libx264', '-preset', 'ultrafast', str(path)],
                           check=True, timeout=10)
            self.paths.append(path)

    def test_join_preserves_encoded_video_packets_and_originals(self):
        def packets(path):
            return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path),
                                             '-map', '0:v:0', '-c', 'copy', '-f', 'data', '-'])
        before = b''.join(packets(path) for path in self.paths[:2])
        hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in self.paths]
        output = merge_mp4_parts(self.paths[:2], self.folder/'joined.mp4')
        self.assertEqual(hashlib.sha256(before).hexdigest(), hashlib.sha256(packets(output)).hexdigest())
        self.assertEqual(hashes, [hashlib.sha256(path.read_bytes()).hexdigest() for path in self.paths])

    def test_incompatible_parts_and_existing_output_are_not_overwritten(self):
        output = self.folder/'joined.mp4'
        with self.assertRaises(ValueError):
            merge_mp4_parts((self.paths[0], self.paths[2]), output)
        self.assertFalse(output.exists())
        output.write_bytes(b'existing')
        with self.assertRaises(FileExistsError):
            merge_mp4_parts(self.paths[:2], output)
        self.assertEqual(output.read_bytes(), b'existing')

    def test_no_space_keeps_parts_and_does_not_publish(self):
        output = self.folder/'joined.mp4'
        with patch('core.recording_merge.ensure_space', side_effect=DiskSpaceError('full')):
            with self.assertRaises(DiskSpaceError):
                merge_mp4_parts(self.paths[:2], output)
        self.assertFalse(output.exists())
        self.assertTrue(all(path.exists() for path in self.paths))
