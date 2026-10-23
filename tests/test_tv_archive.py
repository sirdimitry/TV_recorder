# Adapted from srch-dwnld 7660027 archive parser/transport tests.
import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
import httpx
from core import tv_archive as tv
from core.archive_media import ArchiveProgress as EncodeProgress, ArchiveCanceled as _StoppedByUser
class PlaylistTests(unittest.TestCase):
    url = 'https://s103.ontvtime.ru/stream/channel/sessionplaylist.m3u8?time=123'

    def test_only_the_needed_segments_are_selected(self):
        segments = tv.parse_playlist('#EXTM3U\n#EXTINF:8,\naahls1.ts\n#EXTINF:8,\naahls2.ts\n'
                                     '#EXTINF:8,\naahls3.ts\n', self.url, 12)
        self.assertEqual(len(segments), 2)
        self.assertTrue(segments[0].url.endswith('/aahls1.ts'))

    def test_incomplete_or_gapped_recordings_are_absent(self):
        for text in ('#EXTM3U\n#EXTINF:8,\naahls1.ts\n',
                     '#EXTM3U\n#EXTINF:8,\naahls1.ts\n#EXTINF:8,\naahls3.ts\n',
                     '#EXTM3U\n#EXTINF:8,\n#EXT-X-GAP\naahls1.ts\n'):
            with self.subTest(text=text), self.assertRaises(tv.ArchiveUnavailable):
                tv.parse_playlist(text, self.url, 12)

    def test_external_or_malformed_streams_are_rejected(self):
        for text in ('<html>blocked</html>', '#EXTM3U\n#EXTINF:nan,\naahls1.ts\n',
                     '#EXTM3U\n#EXTINF:8,\nhttps://localhost/hls1.ts\n',
                     '#EXTM3U\n#EXTINF:8,\nhttps://evil.example/hls1.ts\n',
                     '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="secret"\n'):
            with self.subTest(text=text), self.assertRaises(tv.ArchiveSourceError):
                tv.parse_playlist(text, self.url, 8)


class ArchiveTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_tries_another_public_ip_and_keeps_original_tls_hostname(self):
        calls, factories = [], []
        def handler(request):
            calls.append((request.url.host, request.headers['host'], request.extensions['sni_hostname']))
            if request.url.host == '91.242.33.252':
                raise httpx.ConnectTimeout('unavailable address', request=request)
            return httpx.Response(200, text='ok')
        def factory():
            factories.append(1)
            return httpx.MockTransport(handler)
        addresses = [(2, 1, 6, '', ('91.242.33.252', 443)), (2, 1, 6, '', ('185.219.100.10', 443))]
        with patch.object(tv, 'resolve_public', return_value=addresses):
            async with httpx.AsyncClient(transport=tv._ArchiveTransport(factory)) as client:
                await client.get(tv.BASE_URL + '/records/zvezda.html')
                await client.get(tv.BASE_URL + '/records/zvezda.html')
                await client.get('https://s103.ontvtime.ru/stream/example')
        self.assertEqual(calls[:3], [('91.242.33.252', 'www.ontvtime.ru', 'www.ontvtime.ru'),
                                    ('185.219.100.10', 'www.ontvtime.ru', 'www.ontvtime.ru'),
                                    ('185.219.100.10', 'www.ontvtime.ru', 'www.ontvtime.ru')])
        self.assertEqual(len(factories), 2)  # Separate TLS pools for different original hostnames.


class ArchiveAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.day = datetime.now(tv.MOSCOW).date()-timedelta(days=3)
        self.request = tv.ArchiveRequest(tv.CHANNELS[0], datetime.combine(self.day, datetime.min.time(), tv.MOSCOW)
                                        + timedelta(hours=8), datetime.combine(self.day, datetime.min.time(), tv.MOSCOW)
                                        + timedelta(hours=8, seconds=12))
        self.calls = []
        self.missing = False
        self.network_error = False
        self.ts = (b'\x47' + b'\0'*187)*3

    def transport(self, request):
        self.calls.append(request.url.path)
        if self.network_error:
            raise httpx.ConnectError('offline', request=request)
        if request.url.path.startswith('/records/'):
            return httpx.Response(200, text=f'<a href="/index.php?option=com_content&task=select_time&id=1488&start_record={self.day}-06-00">date</a>')
        if request.url.path == '/index.php':
            return httpx.Response(200, text="var gid = '" + 'a'*32 + "';", headers=[
                ('set-cookie', 'tv=s103.ontvtime.ru; path=/'),
                ('set-cookie', 'tv2='+'b'*32+'s; path=/'),
                ('set-cookie', 'tv1='+str(int(self.request.start.timestamp()))+'; path=/')])
        if request.url.path.endswith('playlist.m3u8'):
            return httpx.Response(200, text='#EXTM3U\n#EXTINF:8,\naahls1.ts\n#EXTINF:8,\naahls2.ts\n')
        if request.url.path.endswith('hls2.ts') and self.missing:
            return httpx.Response(404)
        return httpx.Response(200, content=self.ts)

    async def test_checks_both_boundaries_and_caches_them(self):
        async with tv.ArchiveSession(transport=httpx.MockTransport(self.transport)) as session:
            plan = await session.probe(self.request)
        self.assertEqual(set(plan.cached), {0, 1})
        self.assertEqual(len(self.calls), 5)

    async def test_expired_date_is_rejected_before_opening_player(self):
        request = tv.ArchiveRequest(self.request.channel, self.request.start-timedelta(days=30),
                                    self.request.end-timedelta(days=30))
        async with tv.ArchiveSession(transport=httpx.MockTransport(self.transport)) as session:
            with self.assertRaises(tv.ArchiveUnavailable):
                await session.probe(request)
        self.assertEqual(len(self.calls), 1)

    async def test_missing_end_is_not_reported_as_available(self):
        self.missing = True
        async with tv.ArchiveSession(transport=httpx.MockTransport(self.transport)) as session:
            with self.assertRaises(tv.ArchiveUnavailable):
                await session.probe(self.request)

    async def test_network_failure_is_distinct_from_missing_recording(self):
        self.network_error = True
        async with tv.ArchiveSession(transport=httpx.MockTransport(self.transport)) as session:
            with self.assertRaises(tv.ArchiveSourceError):
                await session.probe(self.request)

    async def test_stop_prevents_network_access(self):
        progress = EncodeProgress()
        progress.stop_requested = True
        async with tv.ArchiveSession(transport=httpx.MockTransport(self.transport)) as session:
            with self.assertRaises(_StoppedByUser):
                await session.probe(self.request, progress)
        self.assertEqual(self.calls, [])

    async def test_http_200_error_page_is_not_a_recording(self):
        self.ts = b'<html>Access denied</html>'
        async with tv.ArchiveSession(transport=httpx.MockTransport(self.transport)) as session:
            with self.assertRaises(tv.ArchiveSourceError):
                await session.probe(self.request)
