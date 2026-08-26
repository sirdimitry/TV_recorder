# utils/config.py
import json
import sys
from pathlib import Path


class Config:
    """Глобальные настройки приложения"""

    # Собранный PyInstaller .app — не то же самое, что "запущено из исходников":
    # sys._MEIPASS — временная read-only копия бандла, писать туда данные/логи
    # нельзя (и незачем — она пересоздаётся при каждом обновлении приложения).
    # BASE_DIR в frozen-режиме используется только для бандл-ресурсов
    # (data/default_channels.json, VERSION), а данные/логи/записи уходят в
    # обычные пользовательские папки, как и положено macOS-приложению.
    FROZEN = getattr(sys, 'frozen', False)

    if FROZEN:
        BASE_DIR = Path(getattr(sys, '_MEIPASS', Path(sys.executable).resolve().parent))
        _SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "TV Recorder"
        _MEDIA_DIR = Path.home() / "Movies" / "TV Recorder"
        DATA_DIR = _SUPPORT_DIR / "data"
        LOG_FILE = _SUPPORT_DIR / "logs" / "tv_recorder.log"
        RECORDINGS_DIR = _MEDIA_DIR / "recordings"
        DOWNLOADS_DIR = _MEDIA_DIR / "downloads"
    else:
        BASE_DIR = Path(__file__).resolve().parent.parent
        DATA_DIR = BASE_DIR / "data"
        LOG_FILE = BASE_DIR / "logs" / "tv_recorder.log"
        RECORDINGS_DIR = BASE_DIR / "recordings"
        DOWNLOADS_DIR = BASE_DIR / "downloads"

    VERSION_FILE = BASE_DIR / "VERSION"
    APP_VERSION = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else "0.0.0"

    # Пути к данным
    CHANNELS_FILE = DATA_DIR / "channels.json"
    LINKS_FILE = DATA_DIR / "links.json"
    BROWSER_LINKS_FILE = DATA_DIR / "browser_links.json"
    SCHEDULE_FILE = DATA_DIR / "schedule.json"
    DOWNLOADS_FILE = DATA_DIR / "downloads.json"
    SETTINGS_FILE = DATA_DIR / "settings.json"

    # Настройки проверки потоков
    CHECK_TIMEOUT = 5  # Таймаут проверки потока в секундах

    # Цветовая тема (Dark Mode) — фиксированная тёмная тема приложения
    COLORS = {
        'bg_primary': '#1a1a24',      # Основной фон окна
        'bg_secondary': '#20202e',    # Фон панелей/карточек
        'bg_tertiary': '#2a2a3c',     # Фон полей ввода, элевированных поверхностей
        'bg_hover': '#33334a',        # Hover-состояние строк/кнопок
        'bg_active': '#3a3a56',       # Активное/нажатое состояние
        'border': '#34344a',          # Тонкие разделители и рамки карточек
        'text_primary': '#e4e6f0',    # Основной текст
        'text_secondary': '#9a9fc0',  # Вторичный текст
        'text_muted': '#6c7086',      # Приглушённый текст (плейсхолдеры, подписи)
        'accent': '#89b4fa',          # Акцентный цвет (синий)
        'accent_hover': '#a5c9ff',    # Акцент при наведении
        'accent_text': '#11111b',     # Текст поверх акцентного фона
        'green': '#a6e3a1',           # Успех/Онлайн
        'yellow': '#f9e2af',          # Предупреждение/Буферизация
        'red': '#f38ba8',             # Ошибка/REC
        'red_hover': '#ff9db8',       # Красный при наведении
    }

    # Радиусы скругления для карточек/кнопок — единая система для всего интерфейса
    RADIUS = 10
    RADIUS_SM = 8
    
    @classmethod
    def init_dirs(cls):
        """Создает необходимые директории при первом запуске. parents=True —
        в frozen-режиме на первом запуске ещё не существует даже
        ~/Library/Application Support/TV Recorder/, не только data/ внутри неё."""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        cls.get_recordings_dir().mkdir(parents=True, exist_ok=True)
        cls.get_downloads_dir().mkdir(parents=True, exist_ok=True)

    @classmethod
    def _read_settings(cls) -> dict:
        try:
            return json.loads(cls.SETTINGS_FILE.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return {}

    @classmethod
    def _write_settings(cls, updates: dict):
        # read-modify-write, а не перезапись целиком — раньше set_recordings_dir
        # затирал весь файл одним ключом, и второй ключ (downloads_dir) сотрёт
        # первый при первом же сохранении, если писать так же в лоб.
        settings = cls._read_settings()
        settings.update(updates)
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.SETTINGS_FILE.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

    @classmethod
    def get_recordings_dir(cls) -> Path:
        """Returns the user-selected recordings directory or the project default."""
        selected = cls._read_settings().get('recordings_dir')
        return Path(selected).expanduser() if selected else cls.RECORDINGS_DIR

    @classmethod
    def set_recordings_dir(cls, directory: str | Path):
        path = Path(directory).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        cls._write_settings({'recordings_dir': str(path)})

    @classmethod
    def browser_capture_command(cls, *args: str) -> list:
        """argv для перезапуска процесса под gui/browser_capture.py
        (sniff/screen-capture/preview — pywebview не может делить run loop
        с остальным приложением, см. модульный докстринг gui/browser_capture.py).
        В dev-режиме это тот же python-интерпретатор со скриптом-путём, что
        и всегда; в frozen-сборке sys.executable — сам собранный бинарник
        (не python, и файла gui/browser_capture.py на диске у пользователя
        нет) — вместо пути к скрипту передаём флаг-сентинел, который
        main.py ловит до любых Tk-импортов."""
        if cls.FROZEN:
            return [sys.executable, '--browser-capture-worker', *args]
        browser_script = cls.BASE_DIR / 'gui' / 'browser_capture.py'
        return [sys.executable, str(browser_script), *args]

    @classmethod
    def get_downloads_dir(cls) -> Path:
        """Returns the user-selected downloads directory or the project default."""
        selected = cls._read_settings().get('downloads_dir')
        return Path(selected).expanduser() if selected else cls.DOWNLOADS_DIR

    @classmethod
    def set_downloads_dir(cls, directory: str | Path):
        path = Path(directory).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        cls._write_settings({'downloads_dir': str(path)})
