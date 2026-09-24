import html
import json
import unittest

from core.video_page_url import extract_yandex_source, normalize_mail_url, yandex_preview_id

PAGE = 'https://yandex.ru/video/preview/14080253930830225324'
MAIL = 'https://my.mail.ru/mail/genavertolet/video/65495/554586.html'
OLD = 'http://video.mail.ru/mail/genavertolet/65495/554586.html'
ID = '14080253930830225324'


def state(source=OLD):
    return {'clips': {'items': {'other': {'url': 'https://example.org/unrelated.mp4'},
                               ID: {'url': source}}}}


class VideoPageURLTests(unittest.TestCase):
    def test_mail_variants(self):
        for url in (OLD, MAIL, MAIL.replace('/mail/', '//mail/'), MAIL.replace('my.', 'm.my.')):
            self.assertEqual(normalize_mail_url(url), MAIL)

    def test_other_hosts_unchanged(self):
        url = 'https://my.mail.ru.example.org//mail/video'
        self.assertEqual(normalize_mail_url(url), url)
        self.assertIsNone(yandex_preview_id('https://yandex.ru.example.org/video/preview/123'))

    def test_noframes_selected_id(self):
        page = '<noframes id="UniAppVideo-PreloadedState">' + json.dumps(state()) + '</noframes>'
        self.assertEqual(extract_yandex_source(page, PAGE), MAIL)

    def test_data_state(self):
        raw = html.escape(json.dumps({'preloadedState': {'viewer': state()}}), quote=True)
        self.assertEqual(extract_yandex_source(f'<div data-state="{raw}"></div>', PAGE), MAIL)

    def test_active_source_link(self):
        page = f'<a href="{OLD}" class="Link VideoViewer-SourcePathLink">Mail</a>'
        self.assertEqual(extract_yandex_source(page * 2, PAGE), MAIL)

    def test_no_arbitrary_links(self):
        with self.assertRaises(ValueError):
            extract_yandex_source(f'<a href="{MAIL}">Recommendation</a>', PAGE)

    def test_missing_selected_id(self):
        page = '<noframes id="UniAppVideo-PreloadedState">' + json.dumps(state()).replace(ID, '42') + '</noframes>'
        with self.assertRaises(ValueError):
            extract_yandex_source(page, PAGE)

    def test_ambiguous_source_links(self):
        page = ''.join(f'<a class="VideoViewer-SourcePathLink" href="{u}">x</a>' for u in (MAIL, 'https://example.org/other'))
        with self.assertRaises(ValueError):
            extract_yandex_source(page, PAGE)

    def test_invalid_state_and_unsafe_source(self):
        for source in ('javascript:alert(1)', PAGE, None, 'https://user:secret@example.org/video'):
            page = '<noframes id="UniAppVideo-PreloadedState">' + json.dumps(state(source)) + '</noframes>'
            with self.assertRaises(ValueError):
                extract_yandex_source(page, PAGE)
        with self.assertRaises(ValueError):
            extract_yandex_source('<div data-state="[]"></div><p>Captcha</p>', PAGE)


if __name__ == '__main__':
    unittest.main()
