# Adapted from srch-dwnld commit 7660027.
import json
import unittest
from unittest.mock import AsyncMock, patch

from yt_dlp.extractor.vk import VKIE as UpstreamVKIE


import yt_dlp
def SafeYoutubeDL(opts):
    ydl = yt_dlp.YoutubeDL(opts)
    from core.yt_dlp_compat import VKIE, OdnoklassnikiIE
    ydl.add_info_extractor(VKIE(ydl))
    ydl.add_info_extractor(OdnoklassnikiIE(ydl))
    return ydl

from core.yt_dlp_compat import VKIE


URL = 'https://vkvideo.ru/video-113272656_456243947'
EMBED = 'https://vk.com/video_ext.php?oid=-113272656&id=456243947'
MEDIA = 'https://media.example.org/selected.mp4'
ITEM = {'owner_id': -113272656, 'id': 456243947, 'title': 'Selected video',
        'duration': 3, 'files': {'mp4_480': MEDIA}}


def page(items):
    state = {'apiPrefetchCache': [{'method': 'video.get', 'response': {'items': items}}]}
    return '<script>window.cur = Object.assign(window.cur || {}, ' + json.dumps(state) + ');</script>'


class VKDownloadTests(unittest.TestCase):
    def test_extracts_requested_public_video_without_legacy_api(self):
        other = {**ITEM, 'id': 123, 'files': {'mp4_1080': 'https://media.example.org/other.mp4'}}
        with SafeYoutubeDL({'quiet': True}) as ydl, \
                patch.object(VKIE, '_download_webpage', return_value=page([other, ITEM])) as fetch, \
                patch.object(VKIE, '_download_payload') as legacy:
            info = ydl.extract_info(URL, download=False)
        self.assertEqual(fetch.call_args.args[:2], (EMBED, '-113272656_456243947'))
        legacy.assert_not_called()
        self.assertEqual(info['title'], 'Selected video')
        self.assertEqual(info['duration'], 3)
        self.assertEqual([(f['height'], f['url']) for f in info['formats']], [(480, MEDIA)])
        self.assertEqual(info['http_headers']['Referer'], EMBED)

    def test_extracts_hls_formats_with_audio_and_subtitles(self):
        item = {**ITEM, 'files': {**ITEM['files'], 'hls': 'https://media.example.org/master.m3u8'}}
        with SafeYoutubeDL({'quiet': True}) as ydl, \
                patch.object(VKIE, '_download_webpage', return_value=page([item])), \
                patch.object(VKIE, '_extract_m3u8_formats_and_subtitles', return_value=(
                    [{'format_id': 'hls-720', 'url': 'https://media.example.org/720.m3u8', 'height': 720}], {},
                )):
            info = ydl.extract_info(URL, download=False)
        self.assertEqual({f['height'] for f in info['formats']}, {480, 720})

    def test_does_not_choose_recommendation_or_other_owner(self):
        for item in ({**ITEM, 'id': 123}, {**ITEM, 'owner_id': -999}, {**ITEM, 'files': None}):
            self.assertIsNone(VKIE._prefetched_video(page([item]), '-113272656', '456243947'))
        self.assertIsNone(VKIE._prefetched_video('window.cur = Object.assign(window.cur || {}, bad);', '-1', '2'))

    def test_keeps_list_access_and_legacy_player_fallback(self):
        with SafeYoutubeDL({'quiet': True}) as ydl:
            extractor = ydl.get_info_extractor('VK')
            self.assertIsInstance(extractor, VKIE)
            with patch.object(UpstreamVKIE, '_real_extract', return_value={'fallback': True}) as legacy, \
                    patch.object(VKIE, '_download_webpage', return_value='') as fetch:
                listed = URL + '?list=ln-publiclist'
                self.assertEqual(extractor._real_extract(listed), {'fallback': True})
                fetch.assert_not_called()
                legacy.assert_called_with(listed)
                self.assertEqual(extractor._real_extract(URL), {'fallback': True})
                legacy.assert_called_with(URL)

    def test_preserves_routing_and_embed_hash(self):
        with SafeYoutubeDL({'quiet': True}) as ydl:
            extractor = ydl.get_info_extractor('VK')
            for url in (URL, URL.replace('vkvideo.ru', 'm.vkvideo.ru'), EMBED):
                self.assertTrue(extractor.suitable(url))
            self.assertFalse(extractor.suitable('https://vkvideo.ru.example.org/video-1_2'))
            with patch.object(VKIE, '_download_webpage', return_value=page([ITEM])) as fetch:
                extractor._real_extract(EMBED + '&hash=public_embed_hash')
                self.assertEqual(fetch.call_args.args[0], EMBED + '&hash=public_embed_hash')
