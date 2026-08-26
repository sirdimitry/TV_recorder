# main.py
#!/usr/bin/env python3
"""TV Recorder — приложение для записи ТВ и онлайн-трансляций"""
import os
import platform
import sys
from pathlib import Path


def _inject_bundled_ffmpeg_path():
    """В собранном .app ffmpeg/ffplay/ffprobe лежат внутри бандла (у друга
    на чистом Маке их иначе не будет — Homebrew-сборка динамически
    линкована и не переносится на чужую машину), разложены по архитектуре.
    Добавляем нужную папку в начало PATH ДО первого вызова любого из них —
    сами вызовы (core/*.py, gui/mini_player.py) как звали голые
    'ffmpeg'/'ffplay'/'ffprobe' через subprocess, так и продолжают, им
    неважно, откуда бинарник взялся."""
    if not getattr(sys, 'frozen', False):
        return
    arch = platform.machine()  # 'arm64' или 'x86_64'
    bin_dir = Path(sys.executable).resolve().parent.parent / 'Resources' / 'bin' / arch
    if bin_dir.is_dir():
        os.environ['PATH'] = str(bin_dir) + os.pathsep + os.environ.get('PATH', '')


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()

    # Встроенный браузер (pywebview) не может делить run loop с остальным
    # приложением — core/link_resolver.py, core/recorder.py и
    # gui/browser_link_list.py перезапускают текущий процесс отдельным
    # воркером для sniff/screen-capture/preview. В dev-режиме это
    # `python3 gui/browser_capture.py <args>`; в собранном .app
    # sys.executable — сам этот бинарник (не интерпретатор python, и файла
    # gui/browser_capture.py на диске у пользователя нет), поэтому там
    # вместо пути к скрипту передаётся этот флаг-сентинел — ловим его
    # раньше любых Tk/customtkinter импортов.
    if len(sys.argv) > 1 and sys.argv[1] == '--browser-capture-worker':
        sys.argv = ['browser_capture.py'] + sys.argv[2:]
        from gui.browser_capture import main as browser_capture_main
        browser_capture_main()
        sys.exit(0)

    _inject_bundled_ffmpeg_path()

    from gui.app_window import AppWindow
    from utils.config import Config
    from utils.logger import logger

    Config.init_dirs()
    logger.info("=" * 50)
    logger.info("TV Recorder запускается...")

    app = AppWindow()
    app.run()
