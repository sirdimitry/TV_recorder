# gui/browser_capture.py
"""Отдельный процесс с окном встроенного браузера (pywebview) — намеренно
НЕ часть основного Tk-процесса приложения: pywebview.start() сам владеет
run loop-ом на macOS, и совмещать его с уже работающим циклом Tkinter в
одном процессе рискованно. core/recorder.py запускает этот файл через
subprocess.Popen и параллельно пишет экран через ffmpeg — сам браузер
кадры не отдаёт и с записью никак не взаимодействует.

Использование: python3 browser_capture.py <url> [title]

Пользователь сам находит в открывшейся странице плеер и включает
fullscreen вручную — окно не пытается сделать это само, у каждого сайта
свои кнопки плеера."""
import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: browser_capture.py <url> [title]", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else "TV Recorder — Браузер"

    import webview
    webview.create_window(title, url, width=1280, height=800)
    webview.start()


if __name__ == '__main__':
    main()
