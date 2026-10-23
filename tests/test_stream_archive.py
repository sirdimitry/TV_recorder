import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import httpx

from core.archive_controller import build_interval_request, session_for
from core.stream_archive import (StreamArchiveChannel, StreamArchiveSession, parse_catalog,
                                 parse_timed_playlist, vod_url)
from core.tv_archive import ArchiveRequest, ArchiveSourceError, ArchiveUnavailable, MOSCOW
from gui.archive_panel import ArchivePanel
from gui.preview_panel import PreviewPanel

URL = 'https://zabava-htlive.cdn.ngenix.net/hls/CH_TNT/variant.m3u8'
START = datetime(2026, 10, 12, 20, 0, 2, tzinfo=MOSCOW)
CHANNEL = StreamArchiveChannel('stream_test', 'ТНТ', URL, {'User-Agent': 'HlsWinkPlayer'})


def playlist(start=START-timedelta(seconds=2), *, live=False):
    lines = ['#EXTM3U', '#EXT-X-TARGETDURATION:6']
    for i in range(3):
        stamp = start+timedelta(seconds=i*6)
        lines += ['#EXTINF:6,', '#EXT-X-PROGRAM-DATE-TIME:'+stamp.isoformat(),
                  f'timeline/{int(stamp.timestamp()*1000)}.ts']
    if not live:
        lines.append('#EXT-X-ENDLIST')
    return '\n'.join(lines)


class StreamArchiveTests(unittest.IsolatedAsyncioTestCase):
    def test_catalog_includes_all_regions_and_time_shifts(self):
        channels = parse_catalog((Path(__file__).resolve().parents[1]/'data/zabava-full.m3u').read_text())
        self.assertGreater(len(channels), 2500)
        self.assertGreater(len({ch.group for ch in channels}), 70)
        self.assertTrue(any(ch.name == 'ТНТ (Хабаровск)' for ch in channels))
        self.assertTrue(any('(+2)' in ch.name for ch in channels))
        self.assertTrue(any(ch.group == 'Челябинск' for ch in channels))
        self.assertEqual(len({ch.slug for ch in channels}), len(channels))
        self.assertTrue(all(ch.headers['User-Agent'] for ch in channels))

    def test_absolute_bounds_replace_sliding_or_stale_parameters(self):
        result = vod_url(URL+'?offset=-30&utcstart=1&utcend=2&useseq=1&token=test', START, START+timedelta(seconds=12))
        query = parse_qs(urlsplit(result).query)
        self.assertEqual(query, {'utcstart': [str(int(START.timestamp()))],
                                 'utcend': [str(int(START.timestamp()+12))], 'token': ['test']})

    def test_program_date_time_is_validated_and_trim_offset_preserved(self):
        request = ArchiveRequest(CHANNEL, START, START+timedelta(seconds=12))
        segments, offset, actual = parse_timed_playlist(playlist(), URL, request)
        self.assertEqual((len(segments), offset, actual), (3, 2, START-timedelta(seconds=2)))
        with self.assertRaises(ArchiveSourceError):
            parse_timed_playlist(playlist(live=True), URL, request)
        with self.assertRaises(ArchiveSourceError):
            parse_timed_playlist(playlist().replace('20:00:00', '19:00:00'), URL, request)
        with self.assertRaises(ArchiveUnavailable):
            parse_timed_playlist(playlist(START+timedelta(seconds=1)), URL, request)

    async def test_master_dropping_bounds_is_pinned_again_and_both_boundaries_checked(self):
        seen = []
        packet = b'G'+b'\0'*187
        async def respond(request):
            seen.append(request)
            if request.url.path.endswith('variant.m3u8'):
                return httpx.Response(200, text='#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=720x576\nvideo.m3u8')
            if request.url.path.endswith('video.m3u8'):
                self.assertEqual(parse_qs(request.url.query.decode())['utcstart'], [str(int(START.timestamp()))])
                self.assertIn('utcend', parse_qs(request.url.query.decode()))
                return httpx.Response(200, text=playlist())
            self.assertEqual(request.headers['user-agent'], 'HlsWinkPlayer')
            self.assertEqual(request.headers['range'], 'bytes=0-563')
            return httpx.Response(206, content=packet*3)
        async with StreamArchiveSession(CHANNEL, transport=httpx.MockTransport(respond)) as session:
            plan = await session.probe(ArchiveRequest(CHANNEL, START, START+timedelta(seconds=12)), quick=True)
        self.assertEqual(len(seen), 4)
        self.assertEqual(plan.offset, 2)
        self.assertFalse(plan.cached, 'prefix-only checks must not be cached as complete TS segments')

    async def test_missing_end_of_archive_cannot_be_saved_as_complete(self):
        async def respond(request):
            if request.url.path.endswith('.m3u8'):
                return httpx.Response(200, text=playlist())
            if request.url.path.endswith(str(int((START+timedelta(seconds=10)).timestamp()*1000))+'.ts'):
                return httpx.Response(404)
            return httpx.Response(200, content=(b'G'+b'\0'*187)*3)
        async with StreamArchiveSession(CHANNEL, transport=httpx.MockTransport(respond)) as session:
            with self.assertRaises(ArchiveUnavailable):
                await session.probe(ArchiveRequest(CHANNEL, START, START+timedelta(seconds=12)))

    def test_save_interval_has_explicit_dates_and_rejects_future_or_reversal(self):
        request = build_interval_request(CHANNEL, '12.10.2026 23:59:55', '13.10.2026 00:00:05', now=START+timedelta(days=1))
        self.assertEqual(request.duration, 10)
        with self.assertRaises(ValueError):
            build_interval_request(CHANNEL, '12.10.2026 23:59:55', '12.10.2026 00:00:05')


class ArchiveInteractionTests(unittest.TestCase):
    def test_channel_selection_autoplays_and_resets_sound(self):
        panel = object.__new__(ArchivePanel)
        panel.preview = Mock()
        panel.tree = Mock()
        panel.tree.exists.return_value = True
        panel.channel_title = Mock()
        panel._selected_status = Mock()
        panel._update_versions = Mock()
        panel._validate_interval = Mock()
        panel.audio_btn = Mock()
        panel._family_by_slug = {}
        panel._reset_selection_time = Mock()
        panel._active = True
        panel._seek_from_fields = Mock()
        panel._select(CHANNEL)
        panel.preview.set_audio_enabled.assert_called_once_with(False)
        panel._seek_from_fields.assert_called_once()
        panel._reset_selection_time.assert_called_once()
        self.assertFalse(panel._audio_after_seek)

    def test_sound_clicks_toggle_and_unmute_starts_at_current_archive_position(self):
        panel = object.__new__(PreviewPanel)
        panel._url, panel._audio_url, panel._headers = URL, None, CHANNEL.headers
        panel._duration_limit, panel._start_offset, panel.playback_seconds = 300, 2, 45
        panel._preview_name = 'ТНТ'
        panel._listening, panel._listener = False, None
        panel.listen_icon, panel.status_lbl, panel.audio_status_lbl = Mock(), Mock(), Mock()
        with patch('gui.preview_panel.AudioListener') as listener:
            panel._toggle_listen()
            self.assertTrue(panel._want_audio)
            self.assertEqual(listener.call_args.kwargs['start_offset'], 47)
            self.assertEqual(listener.call_args.kwargs['duration_limit'], 255)
            panel._toggle_listen()
            self.assertFalse(panel._want_audio)
            listener.return_value.stop.assert_called_once()

    def test_broadcast_clock_does_not_advance_without_decoded_frames(self):
        panel = object.__new__(ArchivePanel)
        panel.preview = Mock(playback_seconds=None)
        panel._play_request = ArchiveRequest(CHANNEL, START, START+timedelta(minutes=5))
        self.assertIsNone(panel.current_position())
        panel.preview.playback_seconds = 23.4
        self.assertEqual(panel.current_position(), START+timedelta(seconds=23.4))
        self.assertEqual(panel.current_position(), START+timedelta(seconds=23.4))

    def test_export_always_uses_global_recording_directory(self):
        panel = object.__new__(ArchivePanel)
        panel._selected = CHANNEL
        panel.start, panel.end = Mock(), Mock()
        panel.start.get.return_value = '12.10.2026 20:00:00'
        panel.end.get.return_value = '12.10.2026 20:00:12'
        panel.controller = Mock()
        panel.controller.download.return_value = False
        with patch('gui.archive_panel.Config.get_recordings_dir', return_value=Path('/tmp/global-recordings')):
            panel._download()
        self.assertEqual(panel.controller.download.call_args.args[1], Path('/tmp/global-recordings'))
