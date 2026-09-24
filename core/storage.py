# core/storage.py
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import List, Dict
from utils.config import Config
from utils.logger import logger


class StorageError(RuntimeError):
    """Данные приложения не удалось прочитать или надёжно сохранить."""


class Storage:
    """Хранение каналов и расписания"""

    # Storage создаётся отдельно в AppWindow и RecordingScheduler. Одна
    # блокировка на класс защищает read-modify-write между всеми экземплярами.
    _io_lock = threading.RLock()
    
    def __init__(self):
        Config.init_dirs()
        self.channels_file = Config.CHANNELS_FILE
        self.links_file = Config.LINKS_FILE
        self.browser_links_file = Config.BROWSER_LINKS_FILE
        self.schedule_file = Config.SCHEDULE_FILE
        self.downloads_file = Config.DOWNLOADS_FILE
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
        if not self.downloads_file.exists():
            self._save_json(self.downloads_file, [])
            
        # Автозагрузка дефолтных каналов, если список пуст
        self._load_default_channels_if_empty()
        # "Мои ссылки" и "Браузер" были объединены в одну вкладку — старые
        # ссылки режима браузера переносим в общий список один раз при
        # первом запуске после обновления (см. _migrate_browser_links).
        self._migrate_browser_links()
        self._ensure_source_ids()

    @staticmethod
    def _new_source_id() -> str:
        return uuid.uuid4().hex

    def _ensure_source_ids(self):
        """Добавляет постоянные ID старым источникам и связывает с ними расписание."""
        with self._io_lock:
            source_maps = {}
            for source_type, filepath in (('channel', self.channels_file), ('link', self.links_file)):
                sources = self._load_json(filepath)
                changed = False
                used_ids = set()
                for source in sources:
                    source_id = source.get('id')
                    if not source_id or source_id in used_ids:
                        source_id = self._new_source_id()
                        source['id'] = source_id
                        changed = True
                    used_ids.add(source_id)
                if changed:
                    self._save_json(filepath, sources)
                source_maps[source_type] = {
                    source.get('name'): source.get('id') for source in sources if source.get('name')
                }

            schedule = self.get_schedule()
            changed = False
            for item in schedule:
                if not item.get('source_id'):
                    source_type = item.get('source_type', 'channel')
                    source_id = source_maps.get(source_type, {}).get(item.get('channel_name'))
                    if source_id:
                        item['source_id'] = source_id
                        changed = True
            if changed:
                self._save_json(self.schedule_file, schedule)

    def _migrate_browser_links(self):
        """Раньше это были две вкладки с двумя разными хранилищами —
        "Мои ссылки" (прямой поток) и "Браузер" (захват экрана для сайтов,
        чью прямую ссылку получить не удалось). Теперь это одна вкладка:
        запись сама пробует прямой поток и, если не вышло, автоматически
        переключается на захват экрана (см. AppWindow._record_link_now) —
        отдельное хранилище для этого больше не нужно. Переносим то, что
        накопилось в старом browser_links.json, в links.json под тем же
        именем (если такого имени там ещё нет — иначе ссылка уже была
        добавлена вручную и трогать её не нужно), и заодно обновляем
        source_type в уже сохранённом расписании: 'browser' -> 'link', это
        то же самое хранилище. Идемпотентно — второй и последующие запуски
        просто не находят новых имён для переноса."""
        browser_links = self._load_json(self.browser_links_file)
        if browser_links:
            links = self.get_links()
            existing_names = {l.get('name') for l in links}
            changed = False
            for bl in browser_links:
                name = bl.get('name')
                if name and name not in existing_names:
                    links.append({
                        'name': name, 'url': bl.get('url', ''), 'type': 'other',
                        'player_url': bl.get('player_url', ''),
                    })
                    existing_names.add(name)
                    changed = True
            if changed:
                self._save_json(self.links_file, links)
                logger.info(f"Перенесено {sum(1 for bl in browser_links if bl.get('name') in existing_names)} "
                            f"ссылок режима «Браузер» в общий список")

        schedule = self.get_schedule()
        schedule_changed = False
        for item in schedule:
            if item.get('source_type') == 'browser':
                item['source_type'] = 'link'
                schedule_changed = True
        if schedule_changed:
            self._save_json(self.schedule_file, schedule)
    
    def _load_json(self, filepath: Path) -> list:
        with self._io_lock:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    raise ValueError("ожидался JSON-массив")
                return data
            except FileNotFoundError:
                return []
            except (OSError, json.JSONDecodeError, ValueError) as error:
                backup = filepath.with_suffix(filepath.suffix + '.bak')
                try:
                    with open(backup, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if not isinstance(data, list):
                        raise ValueError("ожидался JSON-массив")
                    logger.warning(f"{filepath} повреждён, данные загружены из {backup}: {error}")
                    self._save_json(filepath, data, create_backup=False)
                    return data
                except (OSError, json.JSONDecodeError, ValueError) as backup_error:
                    message = (f"Не удалось загрузить {filepath}; резервная копия тоже недоступна: "
                               f"{backup_error}")
                    logger.error(message)
                    raise StorageError(message) from error
    
    def _save_json(self, filepath: Path, data: list, create_backup: bool = True):
        """Атомарно сохраняет JSON и оставляет последний корректный файл в .bak."""
        if not isinstance(data, list):
            raise StorageError(f"Нельзя сохранить {filepath}: ожидался список")

        with self._io_lock:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            temp_path = None
            backup_temp_path = None
            try:
                fd, temp_name = tempfile.mkstemp(prefix=f'.{filepath.name}.', suffix='.tmp',
                                                 dir=filepath.parent)
                temp_path = Path(temp_name)
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.write('\n')
                    f.flush()
                    os.fsync(f.fileno())

                if create_backup and filepath.exists():
                    # В backup попадает только JSON, который действительно читается.
                    old_bytes = filepath.read_bytes()
                    old_data = json.loads(old_bytes.decode('utf-8'))
                    if isinstance(old_data, list):
                        backup = filepath.with_suffix(filepath.suffix + '.bak')
                        backup_fd, backup_temp_name = tempfile.mkstemp(
                            prefix=f'.{backup.name}.', suffix='.tmp', dir=filepath.parent)
                        backup_temp_path = Path(backup_temp_name)
                        with os.fdopen(backup_fd, 'wb') as backup_file:
                            backup_file.write(old_bytes)
                            backup_file.flush()
                            os.fsync(backup_file.fileno())
                        os.replace(backup_temp_path, backup)
                        backup_temp_path = None

                os.replace(temp_path, filepath)
                temp_path = None
                # Гарантируем фиксацию самого rename, а не только содержимого файла.
                dir_fd = os.open(filepath.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception as error:
                message = f"Ошибка сохранения {filepath}: {error}"
                logger.error(message)
                raise StorageError(message) from error
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
                if backup_temp_path is not None:
                    backup_temp_path.unlink(missing_ok=True)
            
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
        saved = self._save_source(self.channels_file, channel, 'channel')
        logger.info(f"Канал сохранен: {saved.get('name')}")
        return saved
    
    def delete_channel(self, name: str):
        with self._io_lock:
            channels = [ch for ch in self.get_channels()
                        if ch.get('name') != name and ch.get('id') != name]
            self._save_json(self.channels_file, channels)
        logger.info(f"Канал удален: {name}")

    # === Вручную добавленные ссылки (YouTube/VK/RuTube/Twitch и т.п.) ===
    def get_links(self) -> List[Dict]:
        return self._load_json(self.links_file)

    def save_link(self, link: Dict):
        saved = self._save_source(self.links_file, link, 'link')
        logger.info(f"Ссылка сохранена: {saved.get('name')}")
        return saved

    def delete_link(self, name: str):
        with self._io_lock:
            links = [link for link in self.get_links()
                     if link.get('name') != name and link.get('id') != name]
            self._save_json(self.links_file, links)
        logger.info(f"Ссылка удалена: {name}")

    def delete_all_links(self):
        with self._io_lock:
            self._save_json(self.links_file, [])
        logger.info("Все ссылки удалены")

    # === Расписание ===
    def get_schedule(self) -> List[Dict]:
        return self._load_json(self.schedule_file)
    
    def add_schedule_item(self, item: Dict):
        with self._io_lock:
            schedule = self.get_schedule()
            item = self._with_source_id(item)
            schedule.append(item)
            self._save_json(self.schedule_file, schedule)
        logger.info(f"Расписание добавлено: {item.get('channel_name')} {item.get('start_time')}")
    
    def update_schedule_item(self, index: int, item: Dict):
        with self._io_lock:
            schedule = self.get_schedule()
            if 0 <= index < len(schedule):
                schedule[index] = self._with_source_id(item)
                self._save_json(self.schedule_file, schedule)

    def _with_source_id(self, item: Dict) -> Dict:
        item = dict(item)
        if item.get('source_id'):
            return item
        sources = self.get_links() if item.get('source_type') == 'link' else self.get_channels()
        source = next((source for source in sources
                       if source.get('name') == item.get('channel_name')), None)
        if source:
            item['source_id'] = source.get('id')
        return item

    def _save_source(self, filepath: Path, source: Dict, source_type: str) -> Dict:
        """Обновляет источник по ID, сохраняя поля, которых нет в форме."""
        with self._io_lock:
            sources = self._load_json(filepath)
            incoming = dict(source)
            source_id = incoming.get('id')
            match_index = next((i for i, existing in enumerate(sources)
                                if source_id and existing.get('id') == source_id), None)
            if match_index is None:
                match_index = next((i for i, existing in enumerate(sources)
                                    if existing.get('name') == incoming.get('name')), None)

            old_name = None
            if match_index is not None:
                existing = sources[match_index]
                old_name = existing.get('name')
                saved = {**existing, **incoming}
                saved['id'] = existing.get('id') or source_id or self._new_source_id()
                sources[match_index] = saved
            else:
                saved = incoming
                saved['id'] = source_id or self._new_source_id()
                sources.append(saved)
            self._save_json(filepath, sources)

            if old_name and old_name != saved.get('name'):
                schedule = self.get_schedule()
                changed = False
                for item in schedule:
                    same_source = item.get('source_type', 'channel') == source_type
                    matches_id = item.get('source_id') == saved['id']
                    legacy_match = not item.get('source_id') and item.get('channel_name') == old_name
                    if same_source and (matches_id or legacy_match):
                        item['source_id'] = saved['id']
                        item['channel_name'] = saved.get('name')
                        changed = True
                if changed:
                    self._save_json(self.schedule_file, schedule)
            return saved
    
    def delete_schedule_item(self, index: int):
        with self._io_lock:
            schedule = self.get_schedule()
            if 0 <= index < len(schedule):
                removed = schedule.pop(index)
                self._save_json(self.schedule_file, schedule)
                logger.info(f"Расписание удалено: {removed.get('channel_name')}")

    def delete_all_schedule_items(self):
        with self._io_lock:
            self._save_json(self.schedule_file, [])
        logger.info("Расписание очищено")
    
    def toggle_schedule_item(self, index: int):
        with self._io_lock:
            schedule = self.get_schedule()
            if 0 <= index < len(schedule):
                schedule[index]['enabled'] = not schedule[index].get('enabled', True)
                self._save_json(self.schedule_file, schedule)

    # === Загрузки (вкладка "Загрузки" — разовое скачивание в файл,
    # core/downloader.py) — упорядочены по "id", а не по "name": в отличие
    # от каналов/ссылок несколько загрузок вполне могут называться одинаково. ===
    def get_downloads(self) -> List[Dict]:
        return self._load_json(self.downloads_file)

    def save_download(self, item: Dict):
        self.save_downloads([item])

    def save_downloads(self, items: List[Dict]):
        """Одним чтением и одной записью обновляет несколько загрузок."""
        with self._io_lock:
            downloads = self.get_downloads()
            indexes = {item.get('id'): index for index, item in enumerate(downloads)}
            for item in items:
                download_id = item.get('id')
                if download_id in indexes:
                    downloads[indexes[download_id]] = item
                else:
                    indexes[download_id] = len(downloads)
                    downloads.append(item)
            self._save_json(self.downloads_file, downloads)

    def delete_download(self, download_id: str):
        with self._io_lock:
            downloads = [item for item in self.get_downloads() if item.get('id') != download_id]
            self._save_json(self.downloads_file, downloads)
        logger.info(f"Загрузка удалена: {download_id}")

    def delete_all_downloads(self):
        with self._io_lock:
            self._save_json(self.downloads_file, [])
        logger.info("История загрузок очищена")
