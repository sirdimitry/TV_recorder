"""Real FFmpeg checks for crash recovery and safe publication."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from core.media_probe import probe_media
from core.recording_recovery import (FRAGMENT_FLAGS, finalize_recording,
                                     pending_recordings, prepare_recording, release_recording)
from utils.config import Config


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.config = patch.object(Config, 'DATA_DIR', self.directory / 'data')
        self.config.start()
        self.addCleanup(self.config.stop)
        self.addCleanup(self.temp.cleanup)

    def capture(self, *, crash=False, speed=None):
        output = self.directory / 'result.mp4'
        source, marker = prepare_recording(output, 'test', speed=speed, has_audio=False)
        self.addCleanup(release_recording, marker)
        cmd = ['ffmpeg', '-v', 'error', '-re', '-f', 'lavfi', '-i',
               'testsrc2=size=320x180:rate=25', '-t', '10' if crash else '2',
               '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '25',
               '-movflags', FRAGMENT_FLAGS, '-frag_duration', '1000000', str(source)]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if crash:
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    if source.exists() and source.stat().st_size > 15000:
                        break
                    time.sleep(.1)
                proc.kill()
            proc.wait(timeout=12)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return output, source, marker

    def test_normal_capture_publishes_regular_mp4_without_reencoding(self):
        output, source, marker = self.capture()
        release_recording(marker)
        before = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(source), '-map', '0:v:0', '-c', 'copy', '-f', 'hash', '-'])
        result = finalize_recording(marker)
        after = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(output), '-map', '0:v:0', '-c', 'copy', '-f', 'hash', '-'])
        self.assertEqual(before, after)
        self.assertEqual(result, output.resolve())
        self.assertTrue(probe_media(output).has_video)
        self.assertNotIn(b'moof', output.read_bytes())
        self.assertFalse(source.exists())
        self.assertFalse(marker.exists())

    def test_audio_and_video_packets_survive_remux(self):
        output = self.directory / 'av.mp4'
        source, marker = prepare_recording(output, 'audio-video')
        self.addCleanup(release_recording, marker)
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=320x180:rate=25', '-f', 'lavfi', '-i',
                        'sine=frequency=1000:sample_rate=48000', '-t', '2',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac',
                        '-movflags', FRAGMENT_FLAGS, '-frag_duration', '1000000', str(source)],
                       check=True, timeout=15)
        def hashes(path):
            return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path),
                                            '-map', '0', '-c', 'copy', '-f', 'streamhash', '-'])
        before = hashes(source)
        release_recording(marker)
        finalize_recording(marker)
        self.assertTrue(probe_media(output).has_audio)
        self.assertEqual(before, hashes(output))

    def test_sigkill_leaves_recoverable_fragments(self):
        output, source, marker = self.capture(crash=True)
        release_recording(marker)
        self.assertIn(marker, pending_recordings())
        self.assertTrue(probe_media(source).has_video)
        finalize_recording(marker)
        self.assertGreater(probe_media(output).duration, 0)

    def test_failure_keeps_source_and_existing_destination(self):
        output, source, marker = self.capture()
        release_recording(marker)
        output.write_bytes(b'Existing user file')
        with self.assertRaises(FileExistsError):
            finalize_recording(marker)
        self.assertEqual(output.read_bytes(), b'Existing user file')
        self.assertTrue(source.exists())
        self.assertTrue(marker.exists())

    def test_active_recordings_cannot_be_recovered(self):
        output, source, marker = self.capture()
        self.assertNotIn(marker, pending_recordings())
        with self.assertRaises(BlockingIOError):
            finalize_recording(marker)

    def test_failed_remux_keeps_original_and_marker(self):
        output, source, marker = self.capture()
        release_recording(marker)
        real_run = subprocess.run
        def fail_remux(cmd, **kwargs):
            if cmd[0] == 'ffmpeg':
                raise OSError('cannot launch')
            return real_run(cmd, **kwargs)
        with patch('core.recording_recovery.subprocess.run', side_effect=fail_remux):
            with self.assertRaises(OSError):
                finalize_recording(marker)
        self.assertTrue(source.exists())
        self.assertTrue(marker.exists())
        self.assertFalse(output.exists())

    def test_accelerated_screen_recording_restores_timing(self):
        output, source, marker = self.capture(speed=2)
        release_recording(marker)
        duration = probe_media(source).duration
        finalize_recording(marker)
        self.assertAlmostEqual(probe_media(output).duration, duration*2, delta=.2)
