# core/checker.py
import requests
import subprocess
from enum import Enum
from typing import Tuple
from urllib.parse import urljoin
from utils.config import Config
from utils.logger import logger

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False
    logger.warning("yt-dlp не установлен, проверка YouTube недоступна")


class StreamStatus(Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class StreamChecker:
    """Проверка доступности потоков разных типов"""
    
    def __init__(self):
        self.timeout = Config.CHECK_TIMEOUT
    
    def check(self, channel: dict) -> Tuple[StreamStatus, str]:
        """
        Универсальная проверка канала
        Возвращает: (статус, сообщение)
        """
        source_type = channel.get('type', 'iptv')
        url = channel.get('url', '')
        if not url:
            return StreamStatus.RED, "URL потока не задан"

        checkers = {
            'iptv': self._check_iptv,
            'youtube': self._check_youtube,
            'vk': self._check_vk,
            'rutube': self._check_rutube,
            'rtmp': self._check_rtmp,
        }
        
        checker = checkers.get(source_type, self._check_iptv)
        if checker == self._check_iptv:
            return checker(url, self._channel_headers(channel))
        return checker(url)

    @staticmethod
    def _channel_headers(channel: dict) -> dict:
        """Те же заголовки, с которыми Recorder открывает этот канал."""
        # Локальный импорт не утяжеляет запуск checker и избегает связи модулей
        # на этапе импорта core/__init__.py.
        from core.recorder import Recorder

        return Recorder.channel_headers(channel)
    
    def _check_iptv(self, url: str, headers: dict) -> Tuple[StreamStatus, str]:
        """Проверка HLS/DASH потока"""
        try:
            if '.m3u8' in url.lower():
                return self._check_hls_segments(url, headers)

            # GET с Range работает и на серверах, которые запрещают HEAD, при
            # этом stream=True не скачивает сам эфир во время проверки.
            with requests.get(url, timeout=self.timeout, headers={**headers, 'Range': 'bytes=0-1023'},
                              allow_redirects=True, stream=True) as resp:
                if resp.status_code in (200, 206):
                    return StreamStatus.GREEN, "Поток доступен"
                if resp.status_code in (401, 403, 406):
                    return StreamStatus.YELLOW, f"Доступ ограничен (HTTP {resp.status_code})"
                return StreamStatus.RED, f"HTTP ошибка {resp.status_code}"
        except requests.exceptions.Timeout:
            return StreamStatus.YELLOW, "Таймаут подключения"
        except requests.exceptions.ConnectionError:
            return StreamStatus.RED, "Не удалось подключиться"
        except Exception as e:
            return StreamStatus.RED, str(e)[:100]
    
    def _check_hls_segments(self, m3u8_url: str, headers: dict,
                            _depth: int = 0) -> Tuple[StreamStatus, str]:
        """Проверяет доступность сегментов HLS"""
        try:
            resp = requests.get(m3u8_url, timeout=self.timeout,
                                headers=headers)
            if resp.status_code not in (200, 206):
                return StreamStatus.RED, f"HTTP ошибка плейлиста {resp.status_code}"
            lines = [l.strip() for l in resp.text.strip().split('\n')]
            resources = [line for line in lines if line and not line.startswith('#')]

            if '#EXT-X-STREAM-INF' in resp.text:
                if resources and _depth < 2:
                    return self._check_hls_segments(urljoin(m3u8_url, resources[0]), headers, _depth + 1)
                return StreamStatus.YELLOW, "Плейлист пустой"

            if not resources:
                return StreamStatus.YELLOW, "Плейлист пустой"

            segment_url = urljoin(m3u8_url, resources[0])
            with requests.get(segment_url, timeout=self.timeout,
                              headers={**headers, 'Range': 'bytes=0-1023'}, stream=True) as segment:
                if segment.status_code in (200, 206):
                    return StreamStatus.GREEN, "Поток стабилен"
                return StreamStatus.YELLOW, f"Сегменты недоступны (HTTP {segment.status_code})"
        except requests.exceptions.Timeout:
            return StreamStatus.YELLOW, "Таймаут проверки HLS"
        except requests.exceptions.ConnectionError:
            return StreamStatus.RED, "Не удалось подключиться к HLS-потоку"
        except requests.exceptions.RequestException as error:
            return StreamStatus.YELLOW, f"Ошибка проверки HLS: {str(error)[:80]}"
    
    def _check_youtube(self, url: str) -> Tuple[StreamStatus, str]:
        """Проверка YouTube без авторизации"""
        if not YTDLP_AVAILABLE:
            return StreamStatus.YELLOW, "yt-dlp не установлен"
        
        try:
            opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
                'socket_timeout': self.timeout,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info and info.get('formats'):
                    return StreamStatus.GREEN, "Видео доступно"
                return StreamStatus.RED, "Нет доступных форматов"
        except Exception as e:
            err = str(e)
            if 'Private' in err or 'Sign in' in err:
                return StreamStatus.RED, "Требуется авторизация"
            if 'Geo' in err:
                return StreamStatus.YELLOW, "Геоблокировка"
            return StreamStatus.RED, err[:100]
    
    def _check_vk(self, url: str) -> Tuple[StreamStatus, str]:
        """Проверка VK видео"""
        try:
            resp = requests.get(url, timeout=self.timeout,
                               headers={'User-Agent': 'Mozilla/5.0'},
                               allow_redirects=True)
            if 'login.vk.com' in resp.url or resp.status_code != 200:
                return StreamStatus.RED, "Требуется авторизация"
            if 'video' in resp.text.lower():
                return StreamStatus.GREEN, "Видео доступно"
            return StreamStatus.YELLOW, "Страница загружена, но видео не найдено"
        except Exception as e:
            return StreamStatus.RED, str(e)[:100]
    
    def _check_rutube(self, url: str) -> Tuple[StreamStatus, str]:
        """Проверка RuTube"""
        try:
            resp = requests.get(url, timeout=self.timeout,
                               headers={'User-Agent': 'Mozilla/5.0'})
            if resp.status_code != 200:
                return StreamStatus.RED, f"HTTP {resp.status_code}"
            if 'войдите' in resp.text.lower() or 'авториз' in resp.text.lower():
                return StreamStatus.YELLOW, "Может потребоваться авторизация"
            return StreamStatus.GREEN, "Страница доступна"
        except Exception as e:
            return StreamStatus.RED, str(e)[:100]
    
    def _check_rtmp(self, url: str) -> Tuple[StreamStatus, str]:
        """Проверка RTMP через ffprobe"""
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries',
                 'stream=codec_type', '-of', 'csv=p=0', url],
                capture_output=True, text=True, timeout=self.timeout
            )
            if result.returncode == 0 and result.stdout.strip():
                return StreamStatus.GREEN, "RTMP поток доступен"
            return StreamStatus.RED, result.stderr[:100] or "Поток недоступен"
        except subprocess.TimeoutExpired:
            return StreamStatus.YELLOW, "Таймаут ffprobe"
        except FileNotFoundError:
            return StreamStatus.RED, "ffprobe не найден"
        except Exception as e:
            return StreamStatus.RED, str(e)[:100]
