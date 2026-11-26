import ast
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gui.inline_pages import InlinePage, InlinePageHost
from gui.inline_messages import messagebox
from gui.recording_monitor import RecordingMonitorWindow


class SingleWindowTests(unittest.TestCase):
    def page(self):
        page = object.__new__(InlinePage)
        page.closed = False
        page.on_close = Mock()
        page._jobs = set()
        page.host = SimpleNamespace(root=Mock(), current=page)
        return page

    def test_navigation_cancels_pending_work_and_rejects_late_worker_results(self):
        page = self.page()
        page.host.root.after.side_effect = ['one', 'two']
        update = Mock()
        page.after(100, update)
        queued_callback = page.host.root.after.call_args.args[1]
        page.after(200, update)
        with patch('customtkinter.CTkFrame.destroy') as destroy:
            page.destroy()
            page.destroy()
        self.assertEqual(page.host.root.after_cancel.call_count, 2)
        page.on_close.assert_called_once()
        destroy.assert_called_once()
        self.assertIsNone(page.host.current)
        queued_callback()  # already dispatched before cancellation
        self.assertIsNone(page.after(0, update))  # late network result
        update.assert_not_called()
        self.assertEqual(page._jobs, set())

    def test_completed_callback_is_not_cancelled_again(self):
        page = self.page()
        page.host.root.after.return_value = 'done'
        update = Mock()
        page.after(0, update, 'result')
        page.host.root.after.call_args.args[1]()
        update.assert_called_once_with('result')
        with patch('customtkinter.CTkFrame.destroy'):
            page.destroy()
        page.host.root.after_cancel.assert_not_called()

    def test_new_page_replaces_previous_and_cancels_confirmation(self):
        root = Mock()
        old = Mock()
        host = InlinePageHost(root)
        host.current = old
        with patch('gui.inline_pages.InlinePage') as create, patch.object(messagebox, 'cancel') as cancel:
            new = host.open('Настройки')
        old.destroy.assert_called_once()
        cancel.assert_called_once_with(root)
        self.assertIs(new, host.current)
        create.assert_called_once_with(host, 'Настройки', scroll=True)

    def test_shutdown_cancels_confirmation_instead_of_waiting_forever(self):
        frame, answer = Mock(), Mock()
        root = SimpleNamespace(_inline_prompt=(frame, answer))
        messagebox.cancel(root)
        messagebox.cancel(root)
        frame.destroy.assert_called_once()
        answer.set.assert_called_once_with(False)
        self.assertIsNone(root._inline_prompt)

    def test_leaving_monitor_releases_consumers_and_callbacks_once(self):
        monitor = object.__new__(RecordingMonitorWindow)
        monitor._running = monitor._visible = monitor._owns_snapshots = True
        monitor.tile_widgets = {}
        monitor._poll_id = 'poll'
        monitor.recorder = Mock()
        monitor._stop_listening = Mock()
        monitor.after_cancel = Mock()
        monitor.destroy = Mock()
        with patch.object(RecordingMonitorWindow, '_instance', monitor):
            monitor._close()
            monitor._close()
        monitor.recorder.release_snapshot_consumer.assert_called_once()
        monitor.recorder.remove_ui_callback.assert_called_once()
        monitor.after_cancel.assert_called_once_with('poll')
        monitor.destroy.assert_called_once()
        monitor.recorder.stop_recording.assert_not_called()

    def test_isolated_profile_does_not_move_bundled_resources(self):
        import json
        import os
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            output = subprocess.check_output([sys.executable, '-c',
                'from utils.config import Config; import json; '
                'print(json.dumps([str(Config.DATA_DIR), str(Config.LOG_FILE), str(Config.BASE_DIR)]))'],
                env=dict(os.environ, TV_RECORDER_HOME=directory), text=True)
            data, log, resources = json.loads(output)
            self.assertEqual(Path(data), Path(directory).resolve()/'data')
            self.assertEqual(Path(log), Path(directory).resolve()/'logs'/'tv_recorder.log')
            self.assertNotEqual(Path(resources), Path(directory).resolve())

    def test_normal_application_routes_cannot_create_secondary_windows(self):
        # The live thumbnail grid is the only separate window in these routes.
        for name in ('app_window', 'download_dialog', 'recording_panel'):
            tree = ast.parse((Path(__file__).resolve().parents[1]/'gui'/f'{name}.py').read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, ('CTkToplevel', 'Toplevel'), name)
        from gui.app_window import AppWindow
        import inspect
        self.assertNotIn('self.root.after(0, apply)', inspect.getsource(AppWindow._add_link_dialog))
        with patch.object(AppWindow, '_on_close'):
            app = object.__new__(AppWindow)
            app.root = Mock()
            page = app.root._page_host.open.return_value
            self.assertIs(app._create_dialog('Форма', '400x300'), page.body)


if __name__ == '__main__':
    unittest.main()
