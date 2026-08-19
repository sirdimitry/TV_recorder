# core/checker.py
import requests
import subprocess
from enum import Enum
from typing import Tuple
from utils.config import Config
from utils.vpn_manager import VPNManager
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
        vpn_required = channel.get('vpn_required')
        
        # 1. Проверяем требования VPN
        vpn_ok, vpn_msg = self._check_vpn_requirement(vpn_required)
        if not vpn_ok:
            return StreamStatus.RED, vpn_msg
        
        # 2. Проверяем интернет
        if not self._check_internet():
            return StreamStatus.RED, "Нет подключения к интернету"
        
        # 3. Проверяем поток по типу
        checkers = {
            'iptv': self._check_iptv,
            'youtube': self._check_youtube,
            'vk': self._check_vk,
            'rutube': self._check_rutube,
            'rtmp': self._check_rtmp,
        }
        
        checker = checkers.get(source_type, self._check_iptv)
        return checker(url)
    
    def _check_vpn_requirement(self, vpn_required) -> Tuple[bool, str]:
        """Проверяет соответствие VPN требованиям канала"""
        if vpn_required is None:
            return True, ""
        
        vpn_active = VPNManager.is_vpn_active()
        
        if vpn_required and not vpn_active:
            return False, "Требуется VPN, но он отключен"
        
        if not vpn_required and vpn_active:
            return False, "VPN нужно отключить для этого канала"
        
        return True, ""
    
    def _check_internet(self) -> bool:
        try:
            requests.get('https://www.google.com', timeout=3)
            return True
        except:
            return False
    
    def _check_iptv(self, url: str) -> Tuple[StreamStatus, str]:
        """Проверка HLS/DASH потока"""
        try:
            resp = requests.head(url, timeout=self.timeout, 
                                headers={'User-Agent': 'Mozilla/5.0'})
            if resp.status_code == 200:
                # Доп проверка сегментов для HLS
                if '.m3u8' in url:
                    return self._check_hls_segments(url)
                return StreamStatus.GREEN, "Поток доступен"
            elif resp.status_code in (403, 406):
                return StreamStatus.YELLOW, f"Доступ ограничен (HTTP {resp.status_code})"
            else:
                return StreamStatus.RED, f"HTTP ошибка {resp.status_code}"
        except requests.exceptions.Timeout:
            return StreamStatus.YELLOW, "Таймаут подключения"
        except requests.exceptions.ConnectionError:
            return StreamStatus.RED, "Не удалось подключиться"
        except Exception as e:
            return StreamStatus.RED, str(e)[:100]
    
    def _check_hls_segments(self, m3u8_url: str) -> Tuple[StreamStatus, str]:
        """Проверяет доступность сегментов HLS"""
        try:
            resp = requests.get(m3u8_url, timeout=self.timeout,
                               headers={'User-Agent': 'Mozilla/5.0'})
            lines = resp.text.strip().split('\n')
            segments = [l for l in lines if l.endswith('.ts') or l.endswith('.m4s')]
            
            if not segments:
                return StreamStatus.YELLOW, "Плейлист пустой"
            
            # Проверяем первый сегмент
            base = m3u8_url.rsplit('/', 1)[0]
            seg_url = f"{base}/{segments[0]}" if not segments[0].startswith('http') else segments[0]
            
            seg_resp = requests.head(seg_url, timeout=self.timeout)
            if seg_resp.status_code == 200:
                return StreamStatus.GREEN, "Поток стабилен"
            else:
                return StreamStatus.YELLOW, "Сегменты недоступны"
        except:
            return StreamStatus.YELLOW, "Ошибка проверки сегментов"
    
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