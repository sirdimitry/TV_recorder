from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import tkinter as tk

from core.tv_archive import MOSCOW
from gui.archive_panel import ArchivePanel


class ArchiveSelectionTimeTests(unittest.TestCase):
    def make_panel(self):
        panel = object.__new__(ArchivePanel)
        interpreter = tk.Tcl()
        for field in ('day', 'position', 'start', 'end'):
            setattr(panel, field, tk.StringVar(interpreter, value='stale'))
        panel.preview = Mock()
        panel.tree = Mock()
        panel.channel_title = Mock()
        panel.timeline = Mock()
        panel.after_cancel = Mock()
        panel._selected_status = Mock()
        panel._update_versions = Mock()
        panel._validate_interval = Mock()
        panel.audio_btn = Mock()
        panel._family_by_slug = {}
        panel._seek_from_fields = Mock()
        panel._seek_id = 'old-seek'
        panel._play_request = Mock()
        panel._editing = False
        panel._active = True
        return panel

    def test_each_channel_click_resets_clip_times_to_actual_moscow_time(self):
        panel = self.make_panel()
        first = datetime(2026, 10, 23, 9, 42, 17, tzinfo=MOSCOW)
        second = first.replace(hour=13, minute=5)
        with patch('gui.archive_panel.datetime') as clock:
            clock.now.return_value = first
            panel._select(SimpleNamespace(name='Звезда', slug='zvezda'))
            self.assertEqual(panel.start.get(), '23.10.2026 09:42:17')
            self.assertEqual(panel.end.get(), '23.10.2026 09:42:17')
            panel.start.set('manual time from previous channel')
            clock.now.return_value = second
            panel._select(SimpleNamespace(name='Мир', slug='mir'))
        self.assertEqual(panel.start.get(), '23.10.2026 13:05:17')
        self.assertEqual(panel.end.get(), panel.start.get())
        self.assertEqual(panel.position.get(), '13:04:47')
        self.assertEqual(panel._seek_from_fields.call_count, 2)
        self.assertIsNone(panel._play_request)
        self.assertFalse(panel._editing)
        panel.after_cancel.assert_called_once_with('old-seek')

    def test_midnight_preview_and_fragment_keep_their_correct_dates(self):
        panel = self.make_panel()
        panel._reset_selection_time(datetime(2026, 10, 24, 0, 0, 10, tzinfo=MOSCOW))
        self.assertEqual(panel.start.get(), '24.10.2026 00:00:10')
        self.assertEqual(panel.end.get(), '24.10.2026 00:00:10')
        self.assertEqual(panel.day.get(), '23.10.2026')
        self.assertEqual(panel.position.get(), '23:59:40')


if __name__ == '__main__':
    unittest.main()
