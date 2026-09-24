"""Normalize Mail.ru links and unwrap the selected Yandex Video preview.

Kept identical in TV_recorder and srch-dwnld; no network or application imports.
"""
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit


def normalize_mail_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https'):
        return url
    if parts.hostname not in ('my.mail.ru', 'm.my.mail.ru', 'video.mail.ru'):
        return url
    path = re.sub(r'/+', '/', parts.path)
    if parts.hostname == 'video.mail.ru':
        match = re.fullmatch(r'/([^/]+/[^/]+)/(\d+/\d+\.html)', path)
        if not match:
            return url
        path = f'/{match[1]}/video/{match[2]}'
    return urlunsplit(('https', 'my.mail.ru', path, parts.query, parts.fragment))


def yandex_preview_id(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or parts.hostname not in (
        'yandex.ru', 'www.yandex.ru', 'yandex.com', 'www.yandex.com', 'ya.ru',
    ):
        return None
    match = re.fullmatch(r'/video/preview/(\d+)/?', parts.path)
    return match[1] if match else None


class _PreviewParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.states = []
        self.sources = []
        self._capture = False
        self._text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'noframes' and attrs.get('id') == 'UniAppVideo-PreloadedState':
            self._capture = True
            self._text = []
        if 'data-state' in attrs:
            self.states.append(attrs['data-state'])
        if tag == 'a' and 'VideoViewer-SourcePathLink' in attrs.get('class', '').split():
            self.sources.append(attrs.get('href', ''))

    def handle_data(self, data):
        if self._capture:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == 'noframes' and self._capture:
            self.states.append(''.join(self._text))
            self._capture = False


def _source_url(candidate: str, page_url: str) -> str | None:
    if not isinstance(candidate, str):
        return None
    candidate = normalize_mail_url(candidate)
    parts = urlsplit(candidate)
    if (parts.scheme not in ('https', 'http') or not parts.hostname
            or parts.username or parts.password or candidate == page_url
            or yandex_preview_id(candidate)):
        return None
    return candidate


def extract_yandex_source(page: str, page_url: str) -> str:
    """Only select the requested clip ID, never a recommendation/ad URL."""
    video_id = yandex_preview_id(page_url)
    if not video_id:
        raise ValueError('Это не ссылка на предпросмотр Яндекс Видео')
    parser = _PreviewParser()
    parser.feed(page)
    for raw in parser.states:
        try:
            state = json.loads(raw)
            if not isinstance(state, dict):
                continue
            state = state.get('preloadedState', state)
            for root in (state, state.get('viewer', {})):
                item = root.get('clips', {}).get('items', {}).get(video_id, {})
                source = _source_url(item.get('url'), page_url)
                if source:
                    return source
        except (ValueError, TypeError, AttributeError):
            continue
    # Older/server-rendered pages expose the active player's source link.
    sources = {_source_url(url, page_url) for url in parser.sources} - {None}
    if len(sources) == 1:
        return sources.pop()
    raise ValueError('Яндекс не отдал источник выбранного видео. Пришлите прямую ссылку с сайта видео.')
