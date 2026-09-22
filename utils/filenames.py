# utils/filenames.py
"""Единая транслитерация+санитайзинг имени файла — раньше жила только внутри
Recorder.build_output_path, теперь общая (запись и загрузки должны давать
одинаково безопасные имена на macOS/Windows)."""
import re
import uuid
from datetime import datetime
from pathlib import Path

TRANSLITERATION = str.maketrans({
    'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'E',
    'Ж': 'Zh', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M',
    'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U',
    'Ф': 'F', 'Х': 'Kh', 'Ц': 'Ts', 'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Sch',
    'Ъ': '', 'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya',
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
})


def safe_filename(name: str) -> str:
    """Транслитерирует кириллицу и убирает всё, что не буква/цифра/-/_ —
    portable-имя, одинаково работающее на macOS и Windows."""
    latin_name = name.translate(TRANSLITERATION)
    return re.sub(r'[^A-Za-z0-9_-]+', '_', latin_name).strip('_') or 'file'


def unique_media_path(directory: Path, title: str, recorded_at: datetime | None = None,
                      extension: str = '.mp4') -> Path:
    """Создаёт практически уникальное имя даже для одновременных запусков.

    Микросекунды удобны при просмотре папки, а случайный фрагмент исключает
    совпадение между потоками и отдельными экземплярами приложения.
    """
    timestamp = (recorded_at or datetime.now()).strftime('%Y-%m-%d_%H-%M-%S-%f')
    safe_name = safe_filename(title)
    while True:
        candidate = Path(directory) / f"{safe_name}_{timestamp}_{uuid.uuid4().hex[:8]}{extension}"
        if not candidate.exists():
            return candidate
