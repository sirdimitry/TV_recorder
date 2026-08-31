# core/m3u_parser.py
import requests
import re
from typing import List, Dict, Optional, Set
from utils.logger import logger


class M3UParser:
    """Парсер с ручными фиксами и правильными логотипами"""
    
    MAIN_PLAYLIST = "https://raw.githubusercontent.com/smolnp/IPTVru/gh-pages/IPTVru.m3u"
    
    # РУЧНЫЕ ФИКСЫ: Ссылки и логотипы, которые точно работают
    MANUAL_FIXES = {
        # vgtrkregion-reg.cdnvideo.ru отдаёт TLS-обрыв (Error in the pull
        # function / End of file) на все каналы ВГТРК независимо от VPN —
        # CDN мёртв, а не заблокирован локально (проверено curl+ffmpeg
        # с VPN включённым и выключенным — результат одинаковый). Заменено
        # на официальный источник ВГТРК stream.smotrim.ru — проверено вживую,
        # ffmpeg реально получает кадр за ~1с.
        "Россия 1": {
            "url": "https://stream.smotrim.ru/hls2/russia_hd/playlist_6.m3u8",
            "logo": "https://iptvx.one/picons/rossia1.png"
        },
        # У 'Россия 24' и 'Россия К' на stream.smotrim.ru видео и звук —
        # РАЗНЫЕ HLS-рендиции (playlist_1..5 — видео разных битрейтов без
        # звука вообще, playlist_6 — отдельно звук без видео), не как у
        # 'Россия 1' (russia_hd), где playlist_6 — уже готовый микс обоих.
        # Проверено напрямую ffprobe по сегментам .ts: без audio_url запись
        # получается полностью без звука — сама дорожка отсутствует в PMT,
        # это не баг ffmpeg/наших флагов, а структура именно этого источника.
        "Россия 24": {
            "url": "https://stream.smotrim.ru/hls2/russia24nl_smotrim/playlist_5.m3u8",
            "audio_url": "https://stream.smotrim.ru/hls2/russia24nl_smotrim/playlist_6.m3u8",
            "logo": "https://iptvx.one/picons/rossia-24.png"
        },
        "Россия К": {
            "url": "https://stream.smotrim.ru/hls2/russia_k/playlist_5.m3u8",
            "audio_url": "https://stream.smotrim.ru/hls2/russia_k/playlist_6.m3u8",
            "logo": "https://iptvx.one/picons/kultura.png"
        },
        "Известия": {
            "url": "http://hls-igi.cdnvideo.ru/igi/igi_sq/playlist.m3u8",
            "logo": "https://iptvx.one/picons/izvestia.png"
        },
        # Логотипы: старые Wikimedia thumb-ссылки перестали отдаваться (HTTP 400),
        # заменены на действующие picons из того же плейлиста IPTVru.
        "Матч ТВ": {
            "logo": "https://iptvx.one/picons/match-tv.png"
        },
        "ТВ-3": {
            "logo": "https://iptvx.one/picons/tv3-ru.png"
        },
        "Муз-ТВ": {
            "logo": "https://iptvx.one/picons/muztv.png"
        },
        # РЕН ТВ: ни старая Wikimedia-ссылка, ни собственный tvg-logo плейлиста
        # (iptvx.one/picons/18.png) сейчас не отдают файл — рабочей замены нет,
        # оставлено как было.
        "РЕН ТВ": {
            "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/5/5a/Ren_tv_logo.svg/200px-Ren_tv_logo.svg.png"
        }
    }
    
    # Спец. заголовки для НТВ
    SPECIAL_HEADERS = {
        "НТВ": {
            "Referer": "https://www.ntv.ru/",
            "Origin": "https://www.ntv.ru"
        }
    }
    
    FALLBACK_LOGOS = {
        "Первый канал": "https://upload.wikimedia.org/wikipedia/commons/e/e5/1TV_Logo.svg",
        "Россия 1": "https://upload.wikimedia.org/wikipedia/commons/9/9e/Russia-1_logo.svg",
        "НТВ": "https://upload.wikimedia.org/wikipedia/commons/3/3c/NTV_logo.svg",
        "Пятый канал": "https://upload.wikimedia.org/wikipedia/commons/4/4b/5_kanal_logo.svg",
        "Россия К": "https://upload.wikimedia.org/wikipedia/commons/7/7a/Russia-K_logo.svg",
        "Карусель": "https://upload.wikimedia.org/wikipedia/commons/5/5a/Karusel_logo.svg",
        "ОТР": "https://upload.wikimedia.org/wikipedia/commons/2/2f/OTR_logo.svg",
        "ТВ Центр": "https://upload.wikimedia.org/wikipedia/commons/3/3c/TVC_logo.svg",
        "СТС": "https://upload.wikimedia.org/wikipedia/commons/4/4b/STS_logo.svg",
        "Домашний": "https://upload.wikimedia.org/wikipedia/commons/6/6c/Domashniy_logo.svg",
        "Пятница!": "https://upload.wikimedia.org/wikipedia/commons/8/8f/Pyatnitsa_logo.svg",
        "Звезда": "https://upload.wikimedia.org/wikipedia/commons/9/9e/Zvezda_logo.svg",
        "Мир": "https://upload.wikimedia.org/wikipedia/commons/1/1a/Mir_logo.svg",
        "ТНТ": "https://upload.wikimedia.org/wikipedia/commons/2/2f/TNT_logo.svg",
        "Спас": "https://upload.wikimedia.org/wikipedia/commons/4/4b/Spas_logo.svg"
    }
    
    CHANNEL_SYNONYMS = {
        "Первый канал": {"первый", "1tv", "1 тв", "perviy", "channel one", "орт"},
        "Россия 1": {"россия 1", "russia 1", "rtr", "vgtrk", "rossiya 1"},
        "Матч ТВ": {"матч тв", "match tv", "matchtv", "спорт"},
        "НТВ": {"нтв", "ntv"},
        "Пятый канал": {"пятый", "5 канал", "5tv", "fifth channel"},
        "Россия К": {"россия к", "russia k", "kultura", "культура"},
        "Россия 24": {"россия 24", "russia 24", "vesti", "вести", "news", "russia24"},
        "Карусель": {"карусель", "karusel", "carousel"},
        "ОТР": {"отр", "otr", "общественное"},
        "ТВ Центр": {"тв центр", "tvc", "tv center", "твц"},
        "РЕН ТВ": {"рен тв", "ren tv", "rentv"},
        "СТС": {"стс", "sts", "ctc"},
        "Домашний": {"домашний", "domashniy", "home"},
        "ТВ-3": {"тв-3", "tv3"},
        "Пятница!": {"пятница", "pyatnica", "friday"},
        "Звезда": {"звезда", "zvezda", "star"},
        "Мир": {"мир", "mir", "world"},
        "ТНТ": {"тнт", "tnt"},
        "Муз-ТВ": {"муз-тв", "muz-tv", "music tv"},
        "Спас": {"спас", "spas", "save"}
    }
    
    def fetch_and_parse(self) -> List[Dict]:
        content = self._fetch_playlist(self.MAIN_PLAYLIST)
        if not content:
            logger.error("M3UParser: Не удалось загрузить плейлист")
            return []
            
        all_channels = self._parse_m3u(content)
        federal_channels = self._identify_federal(all_channels)
        
        # Применяем ручные фиксы (логотипы и ссылки)
        for ch in federal_channels:
            name = ch['name']
            if name in self.MANUAL_FIXES:
                fix = self.MANUAL_FIXES[name]
                if 'url' in fix:
                    ch['url'] = fix['url']
                if 'audio_url' in fix:
                    ch['audio_url'] = fix['audio_url']
                if 'logo' in fix:
                    ch['logo_url'] = fix['logo']
                logger.info(f"M3UParser: Применен фикс для '{name}'")
        
        # Добавляем "Известия" вручную
        if "Известия" not in [ch['name'] for ch in federal_channels]:
            fix = self.MANUAL_FIXES["Известия"]
            federal_channels.append({
                'name': 'Известия',
                'url': fix['url'],
                'logo_url': fix['logo'],
                'type': 'iptv',
                'vpn_required': None,
                'alt_urls': []
            })
            logger.info("M3UParser: Добавлен канал 'Известия'")
        
        if federal_channels:
            logger.info(f"M3UParser: Итого {len(federal_channels)} федеральных каналов")
            return federal_channels
        
        logger.warning("M3UParser: Федеральные каналы не найдены")
        return []
    
    def _fetch_playlist(self, url: str) -> Optional[str]:
        for attempt in range(1, 3):
            try:
                logger.info(f"M3UParser: Загрузка плейлиста (попытка {attempt})...")
                resp = requests.get(url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
                if resp.status_code == 200 and '#EXTINF' in resp.text:
                    return resp.text
                logger.warning(f"M3UParser: Неожиданный ответ (HTTP {resp.status_code})")
            except Exception as e:
                logger.warning(f"M3UParser: Ошибка загрузки (попытка {attempt}): {e}")
        return None
    
    def _normalize_name(self, name: str) -> str:
        name = name.lower().strip()
        name = re.sub(r'\s*(hd|fhd|uhd|4k|\+1|\+2)\s*', '', name)
        name = re.sub(r'\s+', ' ', name).strip()
        return name

    @staticmethod
    def _loose_key(name: str) -> str:
        """Strips spaces/hyphens/"!" for fuzzy matching. The source occasionally
        tweaks punctuation in channel names (e.g. "Матч ТВ" -> "Матч!",
        "ТВ-3" -> "ТВ3", "Муз-ТВ" -> "Муз ТВ"); comparing on this loose key
        keeps such drift from breaking channel identification."""
        return re.sub(r'[\s\-!]+', '', name)

    def _identify_federal(self, channels: List[Dict]) -> List[Dict]:
        result = []
        found_standards: Set[str] = set()

        for ch in channels:
            raw_name = ch['name']
            normalized = self._normalize_name(raw_name)
            loose = self._loose_key(normalized)

            matched_standard = None
            for standard_name, synonyms in self.CHANNEL_SYNONYMS.items():
                loose_synonyms = {self._loose_key(syn) for syn in synonyms}
                if loose in loose_synonyms or any(
                    syn in loose or loose in syn for syn in loose_synonyms
                ):
                    matched_standard = standard_name
                    break
            
            if matched_standard and matched_standard not in found_standards:
                ch['name'] = matched_standard
                
                # Логотипы: fallback, если в плейлисте пусто
                if not ch.get('logo_url') or 'placeholder' in ch['logo_url'].lower():
                    ch['logo_url'] = self.FALLBACK_LOGOS.get(matched_standard, "")
                    
                result.append(ch)
                found_standards.add(matched_standard)
                if len(result) >= len(self.CHANNEL_SYNONYMS): 
                    break
                
        return result
    
    def _parse_m3u(self, content: str) -> List[Dict]:
        channels = []
        lines = content.strip().split('\n')
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            if line.startswith('#EXTINF'):
                name = ""
                logo = ""
                
                if ',' in line:
                    parts = line.rsplit(',', 1)
                    attrs = parts[0]
                    name = parts[1].strip()
                    
                    logo_match = re.search(r'tvg-logo="([^"]+)"', attrs)
                    if logo_match:
                        logo = logo_match.group(1)
                
                url = ""
                j = i + 1
                while j < len(lines) and j < i + 5:
                    next_line = lines[j].strip()
                    if next_line and not next_line.startswith('#'):
                        url = next_line
                        break
                    j += 1
                
                if name and url:
                    channels.append({
                        'name': name,
                        'url': url,
                        'logo_url': logo,
                        'type': 'iptv',
                        'vpn_required': None,
                        'alt_urls': []
                    })
                    
            i += 1
            
        return channels