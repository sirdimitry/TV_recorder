import unittest
from unittest.mock import Mock, patch

from core.link_resolver import LinkInfo, resolve_link, list_available_heights

PAGE = 'https://yandex.ru/video/preview/123'
MAIL = 'https://my.mail.ru/mail/user/video/1/2.html'
HTML = '<noframes id="UniAppVideo-PreloadedState">{"clips":{"items":{"123":{"url":"http://video.mail.ru/mail/user/1/2.html"}}}}</noframes>'


class YandexResolverTests(unittest.TestCase):
    @patch('core.link_resolver._resolve_via_ytdlp')
    @patch('core.link_resolver.requests.get')
    def test_resolves_source_before_ytdlp(self, get, extract):
        get.return_value = Mock(text=HTML)
        extract.return_value = LinkInfo(ok=True, title='video', video_url='https://cdn.example/video.mp4')
        self.assertTrue(resolve_link(PAGE, target_height=480).ok)
        extract.assert_called_once_with(MAIL, 15, 480)

    @patch('core.link_resolver._resolve_via_ytdlp')
    @patch('core.link_resolver.requests.get')
    def test_no_browser_on_missing_source(self, get, extract):
        get.return_value = Mock(text='<p>Captcha</p>')
        info = resolve_link(PAGE)
        self.assertFalse(info.ok)
        self.assertTrue(info.skip_browser_fallback)
        extract.assert_not_called()

    @patch('core.link_resolver.yt_dlp.YoutubeDL')
    @patch('core.link_resolver.requests.get')
    def test_heights_without_codec(self, get, ydl):
        get.return_value = Mock(text=HTML)
        extractor = ydl.return_value.__enter__.return_value
        extractor.extract_info.return_value = {'formats': [{'height': 720}, {'height': 360}, {'height': 1080, 'vcodec': 'none'}]}
        self.assertEqual(list_available_heights(PAGE, LinkInfo(ok=True)), [720, 360])
        extractor.extract_info.assert_called_once_with(MAIL, download=False)
