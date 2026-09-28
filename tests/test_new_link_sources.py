import unittest
from unittest.mock import Mock, patch

from core.link_resolver import LinkInfo, resolve_link


class NewLinkSourcesTests(unittest.TestCase):
    def test_first_channel_short_show_link_uses_its_playlist(self):
        page = Mock(url='https://www.1tv.ru/shows/chasovoy/vypuski/example', text=(
            '<div data-playlist-url="/playlist?video_id=297135"></div>'))
        playlist = Mock()
        playlist.json.return_value = [
            {'uid': 999, 'mbr': [{'name': 'hd', 'src': '//cdn.example/wrong.mp4'}]},
            {'uid': 297135, 'title': 'Часовой', 'duration': 1566,
             'mbr': [{'name': 'hd', 'src': '//cdn.example/right.mp4'}]},
        ]
        with patch('core.link_resolver.requests.get', side_effect=[page, playlist]), \
                patch('core.link_resolver._resolve_via_ytdlp') as ytdlp:
            info = resolve_link('https://www.1tv.ru/-/uacgpf')
        self.assertTrue(info.ok)
        self.assertEqual(info.video_url, 'https://cdn.example/right.mp4')
        self.assertEqual(info.duration, 1566)
        ytdlp.assert_not_called()

    def test_ren_platformcraft_url_is_direct_and_keeps_referer(self):
        url = 'https://ren.tv/project/example/123-video'
        page = Mock(url=url, text=(
            '<title>Передача</title><meta property="video:duration" content="120">'
            '<script>"vod-2.ren.cdnvideo.ru\\u002Fren2\\u002Ffilm.mp4"</script>'))
        with patch('core.link_resolver.requests.get', return_value=page), \
                patch('core.link_resolver._probe_stream', return_value=''), \
                patch('core.link_resolver._resolve_via_ytdlp') as ytdlp:
            info = resolve_link(url)
        self.assertEqual(info.video_url, 'http://vod-2.ren.cdnvideo.ru/ren2/film.mp4')
        self.assertEqual(info.headers['Referer'], url)
        self.assertEqual(info.duration, 120)
        ytdlp.assert_not_called()

    def test_passive_check_never_opens_browser_sniffer(self):
        with patch('core.link_resolver._resolve_known_site', return_value=None), \
                patch('core.link_resolver.YTDLP_AVAILABLE', False), \
                patch('core.link_resolver._resolve_via_html_scrape',
                      return_value=LinkInfo(ok=False, title='Page')), \
                patch('core.link_resolver._resolve_via_browser_sniff') as sniff:
            info = resolve_link('https://example.com/video', allow_browser_sniff=False)
        self.assertFalse(info.ok)
        sniff.assert_not_called()


if __name__ == '__main__':
    unittest.main()
