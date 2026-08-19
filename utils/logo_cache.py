# utils/logo_cache.py
import os
import hashlib
import requests
from pathlib import Path
from typing import Optional
from utils.config import Config
from utils.logger import logger


class LogoCache:
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'image/*,*/*;q=0.8'
    }
    
    def __init__(self):
        self.cache_dir = Config.DATA_DIR / "logos"
        self.cache_dir.mkdir(exist_ok=True)
    
    def get_logo_path(self, channel_name: str, logo_url: str) -> Optional[Path]:
        if not logo_url:
            return None
        
        safe_name = "".join(c if c.isalnum() or c in '_-' else '_' for c in channel_name)
        url_hash = hashlib.md5(logo_url.encode()).hexdigest()[:8]
        
        # Ищем в кэше
        existing = list(self.cache_dir.glob(f"{safe_name}_{url_hash}.*"))
        if existing:
            return existing[0]
        
        # Скачиваем
        try:
            resp = requests.get(logo_url, timeout=5, headers=self.HEADERS)
            if resp.status_code == 200 and len(resp.content) > 100:
                # Определяем расширение из Content-Type или URL
                content_type = resp.headers.get('Content-Type', '')
                ext = 'png'
                if 'svg' in content_type: ext = 'svg'
                elif 'jpeg' in content_type: ext = 'jpg'
                
                filename = f"{safe_name}_{url_hash}.{ext}"
                filepath = self.cache_dir / filename
                
                with open(filepath, 'wb') as f:
                    f.write(resp.content)
                    
                logger.info(f"Логотип сохранен: {filename}")
                return filepath
            else:
                logger.warning(f"HTTP {resp.status_code} для логотипа {channel_name}")
                return None
        except Exception as e:
            logger.error(f"Ошибка загрузки логотипа {channel_name}: {e}")
            return None