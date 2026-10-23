# utils/logger.py
import logging
import os
from logging.handlers import RotatingFileHandler
from utils.config import Config

def setup_logger():
    """Настраивает детальное логирование с ротацией файлов"""
    Config.init_dirs()
    
    logger = logging.getLogger("TVRecorder")
    logger.setLevel(logging.DEBUG)
    
    # Очищаем старые хендлеры, чтобы не дублировать записи при перезапуске
    if logger.handlers:
        logger.handlers.clear()
    
    # Формат: [ВРЕМЯ] [УРОВЕНЬ] [МОДУЛЬ:ФУНКЦИЯ:СТРОКА] Сообщение
    formatter = logging.Formatter(
        fmt='%(asctime)s.%(msecs)03d | %(levelname)-8s | %(module)s:%(funcName)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 1. Файловый обработчик с ротацией (макс 10 МБ, хранит 3 старых файла)
    fh = RotatingFileHandler(
        Config.LOG_FILE, 
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=3,
        encoding='utf-8'
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    
    # 2. Консольный обработчик (только INFO и выше, чтобы не спамить в терминал)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    
    logger.addHandler(fh)
    logger.addHandler(ch)

    # APScheduler uses its own logger. In a windowed app stderr is /dev/null,
    # so missed jobs and executor errors must also reach our rotating file.
    scheduler_logger = logging.getLogger('apscheduler')
    scheduler_logger.handlers.clear()
    scheduler_logger.addHandler(fh)
    scheduler_logger.setLevel(logging.WARNING)
    scheduler_logger.propagate = False
    
    return logger

# Глобальный экземпляр логгера
logger = setup_logger()
