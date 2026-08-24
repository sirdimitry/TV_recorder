# core/stream_resolver.py
"""Выбирает конкретный битрейт-вариант HLS вместо того, чтобы отдавать
ffmpeg/ffplay master-плейлист и полагаться на их внутреннюю эвристику.

Запись и превью по-прежнему используют '-c copy'/без перекодирования —
это только выбор, КАКОЙ уже закодированный вариант копировать, чтобы
master-плейлист на 1080p/6+ Мбит не тянулся целиком, когда достаточно
и быстрее стартует более скромный вариант.
"""
import re
from typing import Optional

import requests

from utils.logger import logger

TARGET_HEIGHT = 720
TARGET_BITRATE_KBPS = 5000


# Без этого ffmpeg молча падает на любом временном обрыве TLS/HTTP-соединения
# посреди потока ("IO error: End of file") вместо того, чтобы просто
# переподключиться — замечено на 1tv.ru (balancer-vod.1tv.ru явно рвёт
# долгие соединения), но актуально для любого HTTP(S)-источника.
RECONNECT_OPTS = ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5']


def hls_opts(url: str) -> list:
    """-allowed_extensions — опция HLS-демуксера (снимает ограничение на
    расширения сегментов у капризных CDN); для прямого файла (.mp4 и т.п.,
    не .m3u8) ffmpeg её просто не знает и падает с "Option allowed_extensions
    not found" ещё до открытия потока — замечено на VK/okcdn.ru (прямой
    .mp4-подобный URL без .m3u8). Единая точка для этой проверки — раньше
    была продублирована и разошлась бы снова при следующей правке."""
    return ['-allowed_extensions', 'ALL'] if '.m3u8' in url.lower() else []


def resolve_variant_url(url: str, user_agent: str = 'Mozilla/5.0', referer: Optional[str] = None,
                         target_height: int = TARGET_HEIGHT,
                         target_bitrate_kbps: int = TARGET_BITRATE_KBPS) -> str:
    """Возвращает URL наиболее подходящего варианта HLS для мастер-плейлиста,
    либо исходный URL без изменений, если это не мастер-плейлист или
    что-то пошло не так (сеть, парсинг) — тогда просто ничего не меняем."""
    if not url or '.m3u8' not in url:
        return url

    try:
        headers = {'User-Agent': user_agent}
        if referer:
            headers['Referer'] = referer
        resp = requests.get(url, timeout=5, headers=headers)
        if resp.status_code != 200:
            return url
        text = resp.text
    except Exception as e:
        logger.debug(f"StreamResolver: не удалось получить плейлист для выбора качества: {e}")
        return url

    if '#EXT-X-STREAM-INF' not in text:
        return url  # не мастер-плейлист — выбирать не из чего

    lines = [l.strip() for l in text.strip().split('\n')]
    variants = []
    for i, line in enumerate(lines):
        if not line.startswith('#EXT-X-STREAM-INF'):
            continue
        bw_match = re.search(r'BANDWIDTH=(\d+)', line)
        res_match = re.search(r'RESOLUTION=\d+x(\d+)', line)
        bandwidth = int(bw_match.group(1)) if bw_match else 0
        height = int(res_match.group(1)) if res_match else None
        variant_url = next((l for l in lines[i + 1:] if l and not l.startswith('#')), None)
        if variant_url:
            variants.append((bandwidth, height, variant_url))

    if not variants:
        return url

    budget_bps = target_bitrate_kbps * 1000
    # Сначала сузим до вариантов не выше целевой высоты (если она известна).
    within_height = [v for v in variants if v[1] is not None and v[1] <= target_height]
    pool = within_height or variants

    # Внутри отобранных — лучшее качество, укладывающееся в битрейт-бюджет;
    # если совсем ничего не укладывается, берём наименее прожорливый вариант.
    within_budget = [v for v in pool if v[0] <= budget_bps]
    chosen = max(within_budget, key=lambda v: v[0]) if within_budget else min(pool, key=lambda v: v[0])

    base = url.rsplit('/', 1)[0]
    chosen_url = chosen[2] if chosen[2].startswith('http') else f"{base}/{chosen[2]}"

    if chosen_url != url:
        logger.info(f"StreamResolver: выбран вариант {chosen[1] or '?'}p / {chosen[0] // 1000} kbps")
    return chosen_url
