# utils/config.py
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
        cls.RECORDINGS_DIR.mkdir(exist_ok=True)
