import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.disk_space import (DiskGuard, DiskSpaceError, FINALIZE_MARGIN,
                             GIB, MIN_FREE, ensure_space)
from core.downloader import DownloadTask, Downloader
from core.recorder import Recorder
from core.recording_recovery import (pending_recordings, prepare_recording,
                                     release_recording, finalize_recording)
from utils.config import Config


class DiskSpaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def free(self, value):
        return patch('core.disk_space.shutil.disk_usage', return_value=SimpleNamespace(free=value))

    def test_preflight_rejects_low_space_and_tests_actual_write(self):
        with self.free(MIN_FREE-1), self.assertRaises(DiskSpaceError):
            ensure_space(self.folder, writable=True)
        with patch('core.disk_space.tempfile.TemporaryFile', side_effect=PermissionError('read only')):
            with self.assertRaises(DiskSpaceError):
                ensure_space(self.folder, writable=True)

    def test_recording_and_download_do_not_launch_without_space(self):
        recorder = Recorder()
        notice = Mock()
        recorder.set_storage_callback(notice)
        downloader = Downloader()
        task = DownloadTask('test', 'https://example/video', 720, self.folder)
        with self.free(100), patch('core.recorder.subprocess.Popen') as launch, \
                patch('core.downloader.resolve_link') as resolve:
            self.assertEqual(recorder.start_recording('test', 'url', str(self.folder/'out.mp4')), '')
            downloader._run(task)
        launch.assert_not_called()
        resolve.assert_not_called()
        notice.assert_called_once()
        self.assertEqual(task.status, 'error')
        self.assertIn('Недостаточно места', task.error_message)

    def test_warning_is_once_and_critical_stops_watcher(self):
        warning, stop = Mock(), Mock()
        guard = DiskGuard(self.folder, on_warning=warning, on_stop=stop)
        with self.free(2*GIB):
            guard.check()
            guard.check()
        warning.assert_called_once()
        with self.free(GIB-1):
            guard._run()
        stop.assert_called_once()
        self.assertTrue(guard._done.is_set())

    def test_reservations_sum_concurrent_recordings_and_release(self):
        first, second = self.folder/'first.mp4', self.folder/'second.mp4'
        first.write_bytes(b'a'*200)
        second.write_bytes(b'a'*300)
        guards = [DiskGuard(self.folder, reserve_file=p) for p in (first, second)]
        try:
            with self.free(10*GIB):
                for guard in guards:
                    guard.start()
                self.assertEqual(DiskGuard.reserved_bytes(self.folder), 500)
                first.unlink()  # Publication by another task must not break checking.
                self.assertEqual(DiskGuard.reserved_bytes(self.folder), 300)
                with self.free(GIB+299):
                    with self.assertRaises(DiskSpaceError):
                        guards[1].check()
        finally:
            for guard in guards:
                guard.close()
        self.assertEqual(DiskGuard.reserved_bytes(self.folder), 0)

    def test_finalization_low_space_preserves_capture_and_recovery_metadata(self):
        with patch.object(Config, 'DATA_DIR', self.folder/'data'):
            capture, marker = prepare_recording(self.folder/'final.mp4', 'test-final')
            capture.write_bytes(b'x'*2048)
            release_recording(marker)
            probe = SimpleNamespace(readable=True, has_video=True, has_audio=False, duration=2)
            with self.free(FINALIZE_MARGIN), \
                    patch('core.recording_recovery.probe_media', return_value=probe), \
                    patch('core.recording_recovery.subprocess.run') as remux:
                with self.assertRaises(DiskSpaceError):
                    finalize_recording(marker)
            remux.assert_not_called()
            self.assertTrue(capture.exists())
            self.assertTrue(marker.exists())
            self.assertFalse((self.folder/'final.mp4').exists())

    def test_accelerated_capture_reserves_larger_final_file(self):
        source = self.folder/'accelerated.mp4'
        source.write_bytes(b'a'*100)
        guard = DiskGuard(self.folder, reserve_file=source, reserve_factor=4)
        try:
            with self.free(10*GIB):
                guard.start()
                self.assertEqual(DiskGuard.reserved_bytes(self.folder), 400)
        finally:
            guard.close()

    def test_missing_directory_or_changed_volume_is_failure(self):
        guard = DiskGuard(self.folder)
        guard._device = -1
        with self.assertRaises(DiskSpaceError):
            guard.check()
        with self.assertRaises(DiskSpaceError):
            ensure_space(self.folder/'missing')

    def test_low_disk_stop_of_download_keeps_partial_file(self):
        downloader = Downloader()
        task = DownloadTask('test', 'url', 720, self.folder)
        path = self.folder/'partial.mp4'
        path.write_bytes(b'saved data')
        task.output_path = str(path)
        task.status = 'downloading'
        task.process = Mock()
        task.process.poll.return_value = None
        with self.free(1):
            downloader._watch_task_disk(task)
            task.disk_guard._thread.join(timeout=2)
        task.disk_guard.close()
        task.process.terminate.assert_called_once()
        self.assertNotEqual(task.status, 'canceled')
        self.assertTrue(task.storage_error)
        self.assertEqual(path.read_bytes(), b'saved data')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class RealLowSpaceTests(unittest.TestCase):
    def test_download_low_disk_is_partial_and_playable(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            downloader = Downloader()
            real_popen = subprocess.Popen
            stop_at = time.monotonic()+3.5
            info = SimpleNamespace(ok=True, title='download', thumbnail='', duration=30,
                                   headers={}, video_url='local', audio_url=None)
            def usage(_):
                return SimpleNamespace(free=10*GIB if time.monotonic() < stop_at else 768*1024**2)
            def launch(cmd, *args, **kwargs):
                if cmd[0] == 'ffmpeg':
                    cmd = ['ffmpeg', '-v', 'error', '-progress', 'pipe:1', '-nostats',
                           '-re', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25',
                           '-t', '30', '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '25',
                           '-movflags', '+faststart', cmd[-1]]
                return real_popen(cmd, *args, **kwargs)
            with patch('core.disk_space.shutil.disk_usage', side_effect=usage), \
                    patch('core.downloader.resolve_link', return_value=info), \
                    patch('core.downloader.subprocess.Popen', side_effect=launch):
                try:
                    tid = downloader.start_download('url', 720, folder)
                    task = downloader.tasks[tid]
                    deadline = time.monotonic()+15
                    while task.status in ('resolving', 'downloading') and time.monotonic()<deadline:
                        time.sleep(.1)
                    self.assertEqual(task.status, 'partial')
                    self.assertTrue(task.storage_error)
                    self.assertIn('Недостаточно места', task.error_message)
                    from core.media_probe import probe_media
                    self.assertTrue(probe_media(task.output_path).has_video)
                finally:
                    downloader.shutdown()

    def test_low_disk_recording_stops_and_saves_playable_partial_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            recorder = Recorder()
            notices = []
            recorder.set_storage_callback(notices.append)
            real_popen = subprocess.Popen
            stopped_at = time.monotonic() + 3.5
            def usage(_):
                free = 10*GIB if time.monotonic() < stopped_at else 768*1024**2
                return SimpleNamespace(free=free)
            def launch(cmd, *args, **kwargs):
                if cmd[0] == 'ffmpeg' and str(cmd[-1]).endswith('.recording.mp4'):
                    cmd = ['ffmpeg', '-v', 'error', '-re', '-f', 'lavfi', '-i',
                           'testsrc2=size=320x180:rate=25', '-t', '30', '-c:v', 'libx264',
                           '-preset', 'ultrafast', '-g', '25', '-movflags',
                           '+frag_keyframe+empty_moov+default_base_moof', '-frag_duration', '1000000', cmd[-1]]
                return real_popen(cmd, *args, **kwargs)
            with patch.object(Config, 'DATA_DIR', folder/'data'), \
                    patch.object(Config, 'RECORDINGS_DIR', folder), \
                    patch.object(Config, 'DOWNLOADS_DIR', folder/'downloads'), \
                    patch.object(Config, 'SETTINGS_FILE', folder/'settings.json'), \
                    patch('core.disk_space.shutil.disk_usage', side_effect=usage), \
                    patch('core.recorder.subprocess.Popen', side_effect=launch):
                try:
                    tid = recorder.start_recording('disk test', 'local', str(folder/'result.mp4'))
                    task = recorder.tasks[tid]
                    deadline = time.monotonic()+15
                    while task.result_status is None and time.monotonic() < deadline:
                        time.sleep(.1)
                    self.assertEqual(task.result_status, 'partial')
                    self.assertTrue(task.storage_error)
                    self.assertIn('Недостаточно места', task.error_message)
                    self.assertTrue((folder/'result.mp4').exists())
                    self.assertEqual(pending_recordings(), [])
                    self.assertTrue(notices)
                finally:
                    recorder.stop_all()
