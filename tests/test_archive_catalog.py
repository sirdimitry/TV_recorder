from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import Mock
from unittest.mock import patch
import io
from PIL import Image

from core.archive_catalog import channel_identity, group_channels
from gui.archive_panel import ArchivePanel, fit_preview_size
from core.snapshot import to_ctk_image


def channel(slug, name, group='Основные'):
    return SimpleNamespace(slug=slug, name=name, group=group)


class ChannelGroupingTests(unittest.TestCase):
    def test_preview_grows_with_window_and_always_fits_at_sixteen_by_nine(self):
        sizes = [fit_preview_size(w, h) for w, h in ((300, 120), (700, 400), (1400, 700))]
        for (width, height), (available_w, available_h) in zip(sizes, ((300, 120), (700, 400), (1400, 700))):
            self.assertEqual(width * 9, height * 16)
            self.assertLessEqual(width, available_w)
            self.assertLessEqual(height, available_h)
        self.assertGreater(sizes[1][0], sizes[0][0])
        self.assertGreater(sizes[2][0], sizes[1][0])
        self.assertGreater(sizes[2][0], 352)

    def test_four_by_three_broadcast_is_fitted_without_cropping(self):
        source = Image.new('RGB', (400, 300), '#ff0000')
        data = io.BytesIO()
        source.save(data, format='JPEG')
        with patch('customtkinter.CTkImage') as image:
            to_ctk_image(data.getvalue(), (640, 360), crop=False)
        canvas = image.call_args.kwargs['light_image']
        self.assertEqual(canvas.size, (640, 360))
        self.assertEqual(canvas.getpixel((0, 180)), (13, 17, 27))
        self.assertGreater(canvas.getpixel((81, 180))[0], 240)
        self.assertGreater(canvas.getpixel((558, 180))[0], 240)
        self.assertEqual(canvas.getpixel((639, 180)), (13, 17, 27))

    def test_shift_and_regional_versions_share_one_row_without_losing_sources(self):
        channels = [channel('main', 'Звезда'), channel('2', 'Звезда (+2)'),
                    channel('7', 'Звезда (+7)'), channel('region', 'Звезда (Москва)', 'Москва'),
                    channel('backup', 'Звезда (Москва)', 'Москва')]
        families = group_channels(channels)
        self.assertEqual(len(families), 1)
        family = families[0]
        self.assertEqual(family.name, 'Звезда')
        self.assertEqual(family.versions, tuple(channels))
        labels = family.version_labels({})
        self.assertEqual(len(labels), 5)
        self.assertEqual({c.slug for c in labels.values()}, {c.slug for c in channels})
        self.assertEqual(family.choices(), tuple(channels[:3]))
        self.assertEqual(family.choices('Москва'), tuple(channels[3:]))

    def test_aliases_are_merged_but_distinct_channels_and_formats_are_not(self):
        channels = [channel('a', 'Первый канал'), channel('b', 'Первый (+3)'),
                    channel('c', 'Пятница!'), channel('d', 'Пятница (+4)'),
                    channel('e', 'Мир'), channel('f', 'МИР (+7)'), channel('g', 'МИР 24'),
                    channel('h', 'Первый Севастопольский', 'Республика Крым'),
                    channel('i', 'Кино (HD)', 'Москва')]
        names = [f.name for f in group_channels(channels)]
        self.assertEqual(names, ['Первый канал', 'Пятница', 'Мир', 'МИР 24',
                                 'Первый Севастопольский', 'Кино (HD)'])

    def test_preferred_version_respects_availability_and_summary_is_aggregate(self):
        family = group_channels([channel('main', 'Звезда'), channel('2', 'Звезда (+2)')])[0]
        results = {'main': {'state': 'unavailable'}, '2': {'state': 'available'}}
        self.assertEqual(family.preferred(results).slug, '2')
        self.assertEqual(family.state(results), 'available')
        self.assertTrue(family.matches('+2', results=results))
        self.assertTrue(family.matches('звезда', state='available', results=results))
        self.assertFalse(family.matches('мир', results=results))

    def test_choosing_other_version_preserves_time_and_clip_draft(self):
        panel = object.__new__(ArchivePanel)
        first, second = channel('main', 'Звезда'), channel('2', 'Звезда (+2)')
        panel._selected = first
        panel._version_labels = {'+2 ч': second}
        panel._select = Mock()
        panel._version_changed('+2 ч')
        panel._select.assert_called_once_with(second, reset_time=False)

    def test_version_menu_shows_main_shifts_and_selected_regional_source(self):
        panel = object.__new__(ArchivePanel)
        channels = [channel('main', 'Звезда'), channel('2', 'Звезда (+2)'),
                    channel('region', 'Звезда (Москва)', 'Москва'),
                    channel('other', 'Звезда (Омск)', 'Омск')]
        family = group_channels(channels)[0]
        panel._selected = channels[2]
        panel._family_by_slug = {c.slug: family for c in channels}
        panel._results = {}
        panel.version_menu = Mock()
        panel.channel_title = Mock()
        panel.group = Mock()
        panel.group.get.return_value = 'Все регионы'
        panel.variant = tk.StringVar(tk.Tcl())
        panel._update_versions()
        self.assertEqual({c.slug for c in panel._version_labels.values()}, {'main', '2', 'region'})
        self.assertIn('Москва', panel.variant.get())


if __name__ == '__main__':
    unittest.main()
