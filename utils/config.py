# utils/config.py
import json
from pathlib import Path


class Config:
    """Глобальные настройки приложения"""
    
    BASE_DIR = Path(__file__).resolve().parent.parent
    VERSION_FILE = BASE_DIR / "VERSION"
    APP_VERSION = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else "0.0.0"
    
    # Пути к данным
    DATA_DIR = BASE_DIR / "data"
    CHANNELS_FILE = DATA_DIR / "channels.json"
    SCHEDULE_FILE = DATA_DIR / "schedule.json"
    SETTINGS_FILE = DATA_DIR / "settings.json"
    LOG_FILE = BASE_DIR / "logs" / "tv_recorder.log"
    RECORDINGS_DIR = BASE_DIR / "recordings"
    
    # Настройки проверки потоков
    CHECK_TIMEOUT = 5  # Таймаут проверки потока в секундах
    
    # Цветовая тема (Dark Mode)
    COLORS = {
        'bg_primary': '#1e1e2e',      # Основной фон
        'bg_secondary': '#252536',    # Фон панелей
        'bg_tertiary': '#303046',     # Фон кнопок/полей
        'text_primary': '#cdd6f4',    # Основной текст
        'text_secondary': '#a6adc8',  # Вторичный текст
        'accent': '#89b4fa',          # Акцентный цвет (синий)
        'green': '#a6e3a1',           # Успех/Онлайн
        'yellow': '#f9e2af',          # Предупреждение/Буферизация
        'red': '#f38ba8'              # Ошибка/REC
    }
    
    @classmethod
    def init_dirs(cls):
        """Создает необходимые директории при первом запуске"""
        cls.DATA_DIR.mkdir(exist_ok=True)
        cls.LOG_FILE.parent.mkdir(exist_ok=True)
        cls.get_recordings_dir().mkdir(parents=True, exist_ok=True)

    @classmethod
    def get_recordings_dir(cls) -> Path:
        """Returns the user-selected recordings directory or the project default."""
        try:
            settings = json.loads(cls.SETTINGS_FILE.read_text(encoding='utf-8'))
            selected = settings.get('recordings_dir')
            if selected:
                return Path(selected).expanduser()
        except (OSError, json.JSONDecodeError):
            pass
        return cls.RECORDINGS_DIR

    @classmethod
    def set_recordings_dir(cls, directory: str | Path):
        path = Path(directory).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        cls.DATA_DIR.mkdir(exist_ok=True)
        cls.SETTINGS_FILE.write_text(
            json.dumps({'recordings_dir': str(path)}, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
