# Adapted from srch-dwnld commit 7660027.
import html
import json
import unittest
from unittest.mock import patch

from yt_dlp.extractor.odnoklassniki import OdnoklassnikiIE as UpstreamOdnoklassnikiIE
from yt_dlp.utils import ExtractorError


import yt_dlp
def SafeYoutubeDL(opts):
    ydl = yt_dlp.YoutubeDL(opts)
    from core.yt_dlp_compat import VKIE, OdnoklassnikiIE
    ydl.add_info_extractor(VKIE(ydl))
    ydl.add_info_extractor(OdnoklassnikiIE(ydl))
    return ydl

from core.yt_dlp_compat import OdnoklassnikiIE


URL = 'https://m.ok.ru/video/83023760089?utm_source=chatgpt.com'
MEDIA = 'https://vd.example.org/video.mp4?type=1'
METADATA = {
    'provider': 'UPLOADED_ODKL',
    'movie': {'title': 'Сибирский путь святителя Луки', 'duration': '1350'},
    'videos': [{'name': 'sd', 'url': MEDIA}],
}


def player_page(metadata):
    player = {'flashvars': {'videoId': '83023760089', 'metadata': metadata}}
    return '<div data-options="' + html.escape(json.dumps(player), quote=True) + '"></div>'


class OkDownloadTests(unittest.TestCase):
    def test_actual_extraction_accepts_object_and_legacy_string_metadata(self):
        for metadata in (METADATA, json.dumps(METADATA)):
            with self.subTest(metadata_type=type(metadata).__name__), \
                    SafeYoutubeDL({'quiet': True, 'skip_download': True}) as ydl, \
                    patch.object(OdnoklassnikiIE, '_download_webpage', return_value=player_page(metadata)):
                info = ydl.extract_info(URL, download=False)
            self.assertEqual(info['id'], '83023760089')
            self.assertEqual(info['title'], METADATA['movie']['title'])
            self.assertEqual(info['duration'], 1350)
            self.assertEqual(info['formats'][0]['url'], MEDIA)

    def test_replaces_upstream_extractor_without_changing_url_routing(self):
        with SafeYoutubeDL({'quiet': True}) as ydl:
            extractor = ydl.get_info_extractor('Odnoklassniki')
            self.assertIsInstance(extractor, OdnoklassnikiIE)
            self.assertEqual(extractor.ie_key(), UpstreamOdnoklassnikiIE.ie_key())
            self.assertTrue(extractor.suitable(URL))
            self.assertTrue(extractor.suitable('https://ok.ru/videoembed/83023760089'))
            self.assertFalse(extractor.suitable('https://ok.ru.example.org/video/83023760089'))

    def test_invalid_json_still_uses_upstream_error_handling(self):
        with SafeYoutubeDL({'quiet': True}) as ydl:
            extractor = ydl.get_info_extractor('Odnoklassniki')
            with self.assertRaises(ExtractorError):
                extractor._parse_json('invalid', '83023760089')


if __name__ == '__main__':
    unittest.main()
