"""Site-specific compatibility fixes for the pinned yt-dlp release."""

import json
import re
from urllib.parse import urlencode

from yt_dlp.extractor.odnoklassniki import OdnoklassnikiIE as _OdnoklassnikiIE
from yt_dlp.extractor.vk import VKIE as _VKIE
from yt_dlp.utils import url_or_none


class OdnoklassnikiIE(_OdnoklassnikiIE):
    """OK now embeds flashvars.metadata as an object as well as a JSON string."""

    def _parse_json(self, json_string, video_id, *args, **kwargs):
        if isinstance(json_string, dict):
            return json_string
        return super()._parse_json(json_string, video_id, *args, **kwargs)


class VKIE(_VKIE):
    """Read the public embed's video.get prefetch before the legacy al_video API."""

    def _real_extract(self, url):
        match = self._match_valid_url(url)
        video_id = match.group('videoid')
        # Keep list-qualified links on the upstream route: the list may grant access.
        if match.group('list_id'):
            return super()._real_extract(url)
        if video_id:
            owner_id, item_id = video_id.split('_', 1)
            query = urlencode({'oid': owner_id, 'id': item_id})
        else:
            owner_id, item_id = match.group('oid'), match.group('id')
            video_id = f'{owner_id}_{item_id}'
            query = match.group('embed_query')
        embed_url = f'https://vk.com/video_ext.php?{query}'
        page = self._download_webpage(embed_url, video_id, fatal=False) or ''
        item = self._prefetched_video(page, owner_id, item_id)
        if item is None:
            return super()._real_extract(url)
        files = item.get('files') or {}
        formats = []
        for key, source in files.items():
            quality = re.fullmatch(r'mp4_(\d+)', key)
            if quality and url_or_none(source):
                formats.append({'format_id': key, 'url': source, 'ext': 'mp4',
                                'height': int(quality[1]), 'source_preference': 1})
        hls = files.get('hls')
        subtitles = {}
        if url_or_none(hls):
            hls_formats, subtitles = self._extract_m3u8_formats_and_subtitles(
                hls, video_id, 'mp4', 'm3u8_native', m3u8_id='hls', fatal=False,
            )
            formats.extend(hls_formats)
        if not formats:
            return super()._real_extract(url)
        return {'id': video_id, 'title': item.get('title') or video_id,
                'description': item.get('description'), 'duration': item.get('duration'),
                'timestamp': item.get('date'), 'formats': formats, 'subtitles': subtitles,
                'http_headers': {'Referer': embed_url}, '_format_sort_fields': ('res', 'source')}

    @staticmethod
    def _prefetched_video(page, owner_id, item_id):
        for match in re.finditer(
            r'window\.cur\s*=\s*Object\.assign\(\s*window\.cur\s*\|\|\s*\{\}\s*,\s*', page,
        ):
            try:
                state, _ = json.JSONDecoder().raw_decode(page, match.end())
            except ValueError:
                continue
            if not isinstance(state, dict):
                continue
            for entry in state.get('apiPrefetchCache') or []:
                if not isinstance(entry, dict) or entry.get('method') != 'video.get':
                    continue
                response = entry.get('response')
                if not isinstance(response, dict):
                    continue
                for item in response.get('items') or []:
                    if (isinstance(item, dict) and str(item.get('owner_id')) == owner_id
                            and str(item.get('id')) == item_id and isinstance(item.get('files'), dict)):
                        return item
        return None
