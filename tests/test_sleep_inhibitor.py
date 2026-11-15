import os
import re
import signal
import subprocess
import sys
import threading
import tempfile
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

from core.sleep_inhibitor import SleepInhibitor
from core.recorder import Recorder, RecordingTask


class SleepInhibitorTests(unittest.TestCase):
    def helper(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        return process

    def test_parallel_leases_share_one_process_until_last_release(self):
        process = self.helper()
        with patch('core.sleep_inhibitor.sys.platform', 'darwin'), \
                patch('core.sleep_inhibitor.subprocess.Popen', return_value=process) as launch:
            guard = SleepInhibitor()
            guard.acquire('first')
            guard.acquire('second')
            guard.acquire('second')
            launch.assert_called_once()
            command = launch.call_args.args[0]
            self.assertEqual(command, ['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())])
            guard.release('first')
            guard.release('unknown')
            process.terminate.assert_not_called()
            guard.release('second')
            process.terminate.assert_called_once()
            process.wait.assert_called_once()

    def test_screen_capture_adds_display_assertion_only_when_requested(self):
        with patch('core.sleep_inhibitor.sys.platform', 'darwin'), \
                patch('core.sleep_inhibitor.subprocess.Popen', return_value=self.helper()) as launch:
            guard = SleepInhibitor(keep_display_awake=True)
            guard.acquire('screen')
            self.assertEqual(launch.call_args.args[0], ['/usr/bin/caffeinate', '-i', '-d', '-w', str(os.getpid())])
            guard.close()

    def test_concurrent_acquire_and_release_are_balanced(self):
        with patch('core.sleep_inhibitor.sys.platform', 'darwin'), \
                patch('core.sleep_inhibitor.subprocess.Popen', return_value=self.helper()) as launch:
            guard = SleepInhibitor()
            threads = [threading.Thread(target=guard.acquire, args=(str(i),)) for i in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            launch.assert_called_once()
            for i in range(12):
                guard.release(str(i))
            self.assertFalse(guard._leases)

    def test_crashed_helper_is_restarted_for_existing_lease(self):
        first, second = self.helper(), self.helper()
        with patch('core.sleep_inhibitor.sys.platform', 'darwin'), \
                patch('core.sleep_inhibitor.subprocess.Popen', side_effect=[first, second]) as launch:
            notice = Mock()
            guard = SleepInhibitor(on_error=notice)
            guard.acquire('record')
            first.poll.return_value = 1
            guard._retry_after = 0
            guard.refresh()
            self.assertEqual(launch.call_count, 2)
            notice.assert_called_once()
            guard.release('record')
            second.terminate.assert_called_once()

    def test_failed_start_warns_once_and_does_not_raise(self):
        notice = Mock(side_effect=RuntimeError('UI unavailable'))
        with patch('core.sleep_inhibitor.sys.platform', 'darwin'), \
                patch('core.sleep_inhibitor.subprocess.Popen', side_effect=OSError('not available')) as launch:
            guard = SleepInhibitor(on_error=notice)
            guard.acquire('record')
            guard.refresh()
            self.assertEqual(launch.call_count, 1)  # Backoff prevents a busy retry loop.
            self.assertFalse(guard.active)
            notice.assert_called_once()
            guard.release('record')

    def test_close_releases_all_and_blocks_new_leases(self):
        process = self.helper()
        with patch('core.sleep_inhibitor.sys.platform', 'darwin'), \
                patch('core.sleep_inhibitor.subprocess.Popen', return_value=process) as launch:
            guard = SleepInhibitor()
            guard.acquire('one')
            guard.acquire('two')
            guard.close()
            guard.acquire('new')
            guard.close()
            launch.assert_called_once()
            process.terminate.assert_called_once()
            self.assertFalse(guard._leases)

    def test_other_platform_does_not_launch_caffeinate(self):
        with patch('core.sleep_inhibitor.sys.platform', 'win32'), \
                patch('core.sleep_inhibitor.subprocess.Popen') as launch:
            guard = SleepInhibitor()
            guard.acquire('record')
            guard.refresh()
            guard.close()
            launch.assert_not_called()

    def test_unresponsive_helper_is_killed_and_reaped(self):
        process = self.helper()
        process.wait.side_effect = [subprocess.TimeoutExpired('caffeinate', 2), 0]
        with patch('core.sleep_inhibitor.sys.platform', 'darwin'), \
                patch('core.sleep_inhibitor.subprocess.Popen', return_value=process):
            guard = SleepInhibitor()
            guard.acquire('record')
            guard.release('record')
            process.kill.assert_called_once()
            self.assertEqual(process.wait.call_count, 2)

    def test_exception_in_recording_worker_still_releases_lease(self):
        recorder = Recorder()
        recorder._sleep_guard = Mock()
        task = RecordingTask('test', 'channel', 'url', '/tmp/out.mp4')
        with patch.object(recorder, '_finish_task', side_effect=RuntimeError('worker failure')):
            with self.assertRaises(RuntimeError):
                recorder._wait_for_task(task)
        recorder._sleep_guard.release.assert_called_once_with('test')

    def test_failed_ffmpeg_launch_releases_acquired_lease(self):
        recorder = Recorder()
        recorder._sleep_guard = Mock()
        with tempfile.TemporaryDirectory() as directory, \
                patch('core.recorder.resolve_variant_url', return_value='url'), \
                patch('core.recorder.prepare_recording', return_value=(Path(directory)/'raw.mp4', Path(directory)/'marker.json')), \
                patch('core.recorder.subprocess.Popen', side_effect=OSError('cannot launch')):
            self.assertEqual(recorder.start_recording('channel', 'url', str(Path(directory)/'out.mp4')), '')
        recorder._sleep_guard.acquire.assert_called_once()
        recorder._sleep_guard.release.assert_called_once_with(recorder._sleep_guard.acquire.call_args.args[0])

    def test_failed_screen_capture_launch_releases_lease(self):
        recorder = Recorder()
        recorder._sleep_guard = Mock()
        recorder._screen_guard = Mock()
        browser = Mock()
        with tempfile.TemporaryDirectory() as directory, \
                patch('core.recorder.find_screen_device_index', return_value=0), \
                patch('core.recorder.find_loopback_audio_index', return_value=1), \
                patch.object(recorder, '_read_browser_geometry', return_value=(0, 0, 100, 100)), \
                patch('core.recorder.prepare_recording', return_value=(Path(directory)/'raw.mp4', Path(directory)/'marker.json')), \
                patch('core.recorder.build_screen_capture_cmd', return_value=['ffmpeg', '-movflags', '+faststart', 'out.mp4']), \
                patch('core.recorder.subprocess.Popen', side_effect=[browser, OSError('cannot launch')]):
            self.assertEqual(recorder.start_browser_recording('channel', 'url', str(Path(directory)/'out.mp4')), '')
        recorder._sleep_guard.acquire.assert_called_once()
        recorder._sleep_guard.release.assert_called_once_with(recorder._sleep_guard.acquire.call_args.args[0])
        browser.terminate.assert_called_once()
        recorder._screen_guard.acquire.assert_called_once()
        recorder._screen_guard.release.assert_called_once()

    def test_shutdown_closes_inhibitor_even_with_no_recordings(self):
        recorder = Recorder()
        recorder._sleep_guard = Mock()
        recorder.stop_all()
        recorder._sleep_guard.close.assert_called_once()


@unittest.skipUnless(sys.platform == 'darwin', 'macOS power assertions required')
class RealPowerAssertionTests(unittest.TestCase):
    def assertion(self, pid):
        result = subprocess.run(['/usr/bin/pmset', '-g', 'assertions'],
                                capture_output=True, text=True, check=True, timeout=10)
        return any(re.search(rf'pid\s+{pid}\(caffeinate\)', line)
                   and 'PreventUserIdleSystemSleep' in line for line in result.stdout.splitlines())

    def wait_for_assertion(self, pid, expected):
        until = time.monotonic()+4
        while time.monotonic()<until:
            if self.assertion(pid) == expected:
                return
            time.sleep(.05)
        self.assertEqual(self.assertion(pid), expected)

    def test_actual_assertion_appears_and_last_release_removes_it(self):
        guard = SleepInhibitor()
        try:
            guard.acquire('first')
            guard.acquire('second')
            self.assertIsNotNone(guard._process)
            pid = guard._process.pid
            self.wait_for_assertion(pid, True)
            guard.release('first')
            self.assertTrue(self.assertion(pid))
            guard.release('second')
            self.wait_for_assertion(pid, False)
        finally:
            guard.close()

    def test_parent_crash_automatically_releases_assertion(self):
        source = ('from core.sleep_inhibitor import SleepInhibitor; import time; '
                  'g=SleepInhibitor(); g.acquire("test"); '
                  'print(g._process.pid, flush=True); time.sleep(30)')
        parent = subprocess.Popen([sys.executable, '-c', source], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True)
        pid = None
        try:
            pid = int(parent.stdout.readline())
            self.wait_for_assertion(pid, True)
            parent.kill()
            parent.wait(timeout=5)
            self.wait_for_assertion(pid, False)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            parent.stdout.close()
            if pid and self.assertion(pid):
                os.kill(pid, signal.SIGTERM)
