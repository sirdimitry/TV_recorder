from datetime import datetime
import unittest
from unittest.mock import AsyncMock, patch

from core.archive_controller import build_request
from core.expanded_resolver import resolve_expanded
from core.tv_archive import CHANNELS, MOSCOW


class ArchiveInputTests(unittest.TestCase):
    def test_cross_midnight_is_explicitly_next_day(self):
        r = build_request(CHANNELS[0], '12.10.2026', '23:55', '00:10',
                          now=datetime(2026, 10, 16, tzinfo=MOSCOW))
        self.assertEqual(r.duration, 900)
        self.assertEqual(r.end.day, 13)

    def test_invalid_future_and_excessive_intervals_are_rejected(self):
        for day, start, end in [('31.02.2026', '08:00', '08:20'),
                                ('12.10.2026', '08:61', '09:00'),
                                ('12.10.2026', '08:00', '08:00'),
                                ('12.10.2026', '08:00', '18:00'),
                                ('17.10.2026', '08:00', '09:00')]:
            with self.subTest(day=day, start=start), self.assertRaises(ValueError):
                build_request(CHANNELS[0], day, start, end,
                              now=datetime(2026, 10, 16, tzinfo=MOSCOW))


class ExpandedResolversTests(unittest.TestCase):
    def test_new_tiktok_route_preserves_session_headers_and_duration(self):
        with patch('core.source_resolvers.tiktok_page.resolve', new=AsyncMock(return_value=(
                'https://www.tiktok.com/media.mp4', 'Clip', 720, 12.0, 'session=test'))):
            result = resolve_expanded('https://www.tiktok.com/@test/video/123', 1, 720)
        self.assertTrue(result.ok)
        self.assertEqual(result.duration, 12)
        self.assertEqual(result.headers['Cookie'], 'session=test')

    def test_new_ren_route_keeps_article_referer(self):
        url = 'https://ren.tv/project/test'
        with patch('core.source_resolvers.ren_tv.resolve', new=AsyncMock(return_value=(
                'https://cdn.example/video.mp4', 'Title', 720, 30))):
            result = resolve_expanded(url, 1, 720)
        self.assertEqual(result.headers['Referer'], url)
        self.assertEqual(result.title, 'Title')

    def test_failed_vk_does_not_scan_unrelated_embedded_players(self):
        with patch('core.source_resolvers.webcaster.resolve', new=AsyncMock()) as scanner:
            result = resolve_expanded('https://vkvideo.ru/video-1_2', 1, 720)
        self.assertIsNone(result)
        scanner.assert_not_awaited()
