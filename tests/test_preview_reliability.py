import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from core.live_stream import LiveThumbnailStream
from gui.app_window import AppWindow
from gui.preview_panel import PreviewPanel


class PreviewReliabilityTests(unittest.TestCase):
    def make_panel(self):
        panel = object.__new__(PreviewPanel)
        panel._running = True
        panel._generation = 3
        panel._frame_lock = threading.Lock()
        panel._pending_frame = None
        panel._stream = None
        panel._frame_count = 0
        panel.after = Mock()
        panel._display = Mock()
        return panel

    def test_frame_flood_from_worker_keeps_latest_without_tk_calls(self):
        panel = self.make_panel()

        def decode():
            for i in range(10000):
                panel._on_frame(str(i).encode(), 3)

        worker = threading.Thread(target=decode)
        worker.start()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        panel.after.assert_not_called()
        panel._poll_frame()
        panel._display.assert_called_once_with(b'9999', 3)
        self.assertIsNone(panel._pending_frame)

    def test_old_channel_and_shutdown_frames_are_discarded(self):
        panel = self.make_panel()
        panel._on_frame(b'new', 3)
        panel._on_frame(b'old', 2)
        self.assertEqual(panel._pending_frame, (b'new', 3))
        panel._running = False
        panel._on_frame(b'late', 3)
        self.assertEqual(panel._pending_frame, (b'new', 3))

    def test_lost_frames_change_status_once_and_polling_continues(self):
        panel = self.make_panel()
        panel._frame_count = 1
        panel._last_frame_at = time.monotonic() - 20
        panel._stalled = False
        panel._preview_name = 'test'
        panel.status_lbl = Mock()
        panel._poll_frame()
        panel._poll_frame()
        panel.status_lbl.configure.assert_called_once_with(text='Переподключение…')
        self.assertEqual(panel.after.call_count, 2)

    def test_reopen_restores_existing_window_without_stopping_recording(self):
        app = object.__new__(AppWindow)
        app._closing = False
        app.root = Mock()
        app.recorder = Mock()
        app._show_main_window()
        app.root.deiconify.assert_called_once()
        app.root.lift.assert_called_once()
        app.root.focus_force.assert_called_once()
        app.recorder.stop_all.assert_not_called()
        app._closing = True
        app.root.reset_mock()
        app._show_main_window()
        app.root.deiconify.assert_not_called()

    def test_stalled_decoder_ignoring_terminate_is_killed_and_reaped(self):
        # A real child reproduces a blocked stdout read and ignores SIGTERM.
        child_code = ('import signal, time; '
                      'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                      'print("ready", flush=True); time.sleep(60)')
        real_popen = subprocess.Popen
        children = []

        def launch(*args, **kwargs):
            child = real_popen([sys.executable, '-c', child_code], **kwargs)
            self.assertEqual(child.stdout.readline().strip(), b'ready')
            children.append(child)
            return child

        stream = LiveThumbnailStream('unused', None, 1, Mock())
        finished = threading.Event()

        def run():
            try:
                stream._run()
            finally:
                finished.set()

        try:
            with patch('core.live_stream.subprocess.Popen', side_effect=launch), \
                    patch('core.live_stream.FRAME_TIMEOUT', 0.1):
                stream._running = True
                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not (children and children[0].poll() is not None):
                    time.sleep(0.02)
                stream.stop()
                self.assertTrue(finished.wait(3), 'decoder thread stayed blocked')
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].returncode)
            self.assertTrue(children[0].stdout.closed)
            self.assertTrue(children[0].stderr.closed)
        finally:
            stream.stop()
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
