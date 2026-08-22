# core/storage.py
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
from utils.config import Config
from utils.logger import logger


class Storage:
    """Хранение каналов и расписания"""
    
    def __init__(self):
        Config.init_dirs()
        self.channels_file = Config.CHANNELS_FILE
        self.links_file = Config.LINKS_FILE
        self.browser_links_file = Config.BROWSER_LINKS_FILE
        self.schedule_file = Config.SCHEDULE_FILE
        self.default_channels_file = Config.BASE_DIR / "data" / "default_channels.json"

        # Создаем файлы если не существуют
        if not self.channels_file.exists():
            self._save_json(self.channels_file, [])
        if not self.links_file.exists():
            self._save_json(self.links_file, [])
        if not self.browser_links_file.exists():
            self._save_json(self.browser_links_file, [])
        if not self.schedule_file.exists():
            self._save_json(self.schedule_file, [])
            
        # Автозагрузка дефолтных каналов, если список пуст
        self._load_default_channels_if_empty()
    
    def _load_json(self, filepath: Path) -> list:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки {filepath}: {e}")
            return []
    
    def _save_json(self, filepath: Path, data: list):
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения {filepath}: {e}")
            
    def _load_default_channels_if_empty(self):
        """Загружает дефолтные каналы, если основной список пуст"""
        current_channels = self.get_channels()
        if len(current_channels) == 0 and self.default_channels_file.exists():
            defaults = self._load_json(self.default_channels_file)
            if defaults:
                self._save_json(self.channels_file, defaults)
                logger.info(f"Загружено {len(defaults)} каналов по умолчанию")
    
    # === Каналы ===
    def get_channels(self) -> List[Dict]:
        return self._load_json(self.channels_file)
    
    def save_channel(self, channel: Dict):
        channels = self.get_channels()
        # Обновляем или добавляем
        found = False
        for i, ch in enumerate(channels):
            if ch.get('name') == channel.get('name'):
                channels[i] = channel
                found = True
                break
        if not found:
            channels.append(channel)
        self._save_json(self.channels_file, channels)
        logger.info(f"Канал сохранен: {channel.get('name')}")
    
    def delete_channel(self, name: str):
        channels = self.get_channels()
        channels = [ch for ch in channels if ch.get('name') != name]
        self._save_json(self.channels_file, channels)
        logger.info(f"Канал удален: {name}")

    # === Вручную добавленные ссылки (YouTube/VK/RuTube/Twitch и т.п.) ===
    def get_links(self) -> List[Dict]:
        return self._load_json(self.links_file)

    def save_link(self, link: Dict):
        links = self.get_links()
        found = False
        for i, l in enumerate(links):
            if l.get('name') == link.get('name'):
                links[i] = link
                found = True
                break
        if not found:
            links.append(link)
        self._save_json(self.links_file, links)
        logger.info(f"Ссылка сохранена: {link.get('name')}")

    def delete_link(self, name: str):
        links = self.get_links()
        links = [l for l in links if l.get('name') != name]
        self._save_json(self.links_file, links)
        logger.info(f"Ссылка удалена: {name}")

    # === Ссылки в режиме браузера (захват экрана — для сайтов, чью прямую
    # ссылку на поток получить не удалось, см. core/link_resolver.py) ===
    def get_browser_links(self) -> List[Dict]:
        return self._load_json(self.browser_links_file)

    def save_browser_link(self, link: Dict):
        links = self.get_browser_links()
        found = False
        for i, l in enumerate(links):
            if l.get('name') == link.get('name'):
                links[i] = link
                found = True
                break
        if not found:
            links.append(link)
        self._save_json(self.browser_links_file, links)
        logger.info(f"Ссылка (браузер) сохранена: {link.get('name')}")

    def delete_browser_link(self, name: str):
        links = self.get_browser_links()
        links = [l for l in links if l.get('name') != name]
        self._save_json(self.browser_links_file, links)
        logger.info(f"Ссылка (браузер) удалена: {name}")

    # === Расписание ===
    def get_schedule(self) -> List[Dict]:
        return self._load_json(self.schedule_file)
    
    def add_schedule_item(self, item: Dict):
        schedule = self.get_schedule()
        schedule.append(item)
        self._save_json(self.schedule_file, schedule)
        logger.info(f"Расписание добавлено: {item.get('channel_name')} {item.get('start_time')}")
    
    def update_schedule_item(self, index: int, item: Dict):
        schedule = self.get_schedule()
        if 0 <= index < len(schedule):
            schedule[index] = item
            self._save_json(self.schedule_file, schedule)
    
    def delete_schedule_item(self, index: int):
        schedule = self.get_schedule()
        if 0 <= index < len(schedule):
            removed = schedule.pop(index)
            self._save_json(self.schedule_file, schedule)
            logger.info(f"Расписание удалено: {removed.get('channel_name')}")
    
    def toggle_schedule_item(self, index: int):
        schedule = self.get_schedule()
        if 0 <= index < len(schedule):
            schedule[index]['enabled'] = not schedule[index].get('enabled', True)
            self._save_json(self.schedule_file, schedule)