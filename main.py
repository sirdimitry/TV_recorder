# main.py
#!/usr/bin/env python3
"""TV Recorder — приложение для записи ТВ и онлайн-трансляций"""

from gui.app_window import AppWindow
from utils.config import Config
from utils.logger import logger


def main():
    Config.init_dirs()
    logger.info("=" * 50)
    logger.info("TV Recorder запускается...")
    
    app = AppWindow()
    app.run()


if __name__ == '__main__':
    main()