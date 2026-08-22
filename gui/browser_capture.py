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
свои кнопки плеера.

Некоторые сайты (замечено на otr-online.ru) реально грузятся по 15-20
секунд — без всякой индикации это выглядит так, будто окно просто не
открылось. Поэтому сперва показываем тёмную заглушку "Загрузка…" и
переключаемся на настоящий адрес отдельным шагом, а не грузим его сразу.

На macOS сама кнопка fullscreen внутри плеера страницы (Fullscreen API,
element.requestFullscreen()) молча не срабатывает: pywebview создаёт
WKWebView без приватного (не задокументированного публично) флага
fullScreenEnabled, без которого WebKit сам отклоняет такие запросы —
это ограничение самого WKWebView, а не конкретного сайта или его плеера.
Включаем этот флаг вручную через pyobjc сразу после старта GUI-цикла."""
import sys

LOADING_HTML = """
<html><body style="background:#1a1a1a;color:#999;margin:0;height:100vh;
display:flex;align-items:center;justify-content:center;
font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:15px;">
Загрузка страницы…
</body></html>
"""


def main():
    if len(sys.argv) < 2:
        print("Usage: browser_capture.py <url> [title]", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else "TV Recorder — Браузер"

    import webview
    window = webview.create_window(title, html=LOADING_HTML, width=1280, height=800)

    def on_loaded():
        # 'loaded' сработает и для настоящей страницы тоже — отписываемся
        # сразу, иначе уйдём в бесконечную перезагрузку.
        window.events.loaded -= on_loaded
        window.load_url(url)

    window.events.loaded += on_loaded

    def enable_page_fullscreen():
        if sys.platform != 'darwin':
            return
        try:
            from webview.platforms.cocoa import BrowserView
            instance = BrowserView.instances.get(window.uid)
            if instance is not None:
                instance.webview.configuration().preferences().setValue_forKey_(True, 'fullScreenEnabled')
        except Exception as e:
            print(f"Не удалось включить fullScreenEnabled: {e}", file=sys.stderr)

    webview.start(func=enable_page_fullscreen)


if __name__ == '__main__':
    main()
