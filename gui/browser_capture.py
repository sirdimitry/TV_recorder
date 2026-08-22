# gui/browser_capture.py
"""Отдельный процесс с окном встроенного браузера (pywebview) — намеренно
НЕ часть основного Tk-процесса приложения: pywebview.start() сам владеет
run loop-ом на macOS, и совмещать его с уже работающим циклом Tkinter в
одном процессе рискованно. core/recorder.py запускает этот файл через
subprocess.Popen и параллельно пишет экран через ffmpeg — сам браузер
кадры не отдаёт и с записью никак не взаимодействует.

Использование: python3 browser_capture.py <url> [title] [auto_fullscreen]

auto_fullscreen ('1'/'0', по умолчанию '0') — сразу после загрузки страницы
разворачивает само ОКНО во весь экран (macOS-уровня, Cmd+Ctrl+F), а не
полагается на кнопку fullscreen внутри плеера страницы. Включаем это
только когда вызывающий код точно передал "чистую" ссылку плеера
(core/link_resolver.py: LinkInfo.player_url — прямую ссылку на сам
плеер, без сайдбаров/cookie-баннера), где содержимое и так занимает всё
окно целиком, поэтому окно=на весь экран это и есть fullscreen-видео.
Для обычной страницы (без player_url) так не делаем: у неё есть ещё
и сайдбар/реклама/баннер, разворачивать окно молча — только исказит то,
что человек ожидает увидеть.

Кнопка fullscreen внутри самого плеера страницы (Fullscreen API,
element.requestFullscreen()) на macOS ненадёжна: pywebview создаёт
WKWebView без приватного (не задокументированного публично) флага
fullScreenEnabled — без него WebKit по умолчанию отклоняет такие запросы
у части сайтов. Включаем этот флаг тоже (см. enable_page_fullscreen), но
он не гарантирует, что кнопка заработает на всех сайтах — авто-fullscreen
окна через auto_fullscreen надёжнее там, где применим.

Некоторые сайты (замечено на otr-online.ru) реально грузятся по 15-20
секунд — без всякой индикации это выглядит так, будто окно просто не
открылось. Поэтому сперва показываем тёмную заглушку "Загрузка…" и
переключаемся на настоящий адрес отдельным шагом, а не грузим его сразу."""
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
        print("Usage: browser_capture.py <url> [title] [auto_fullscreen]", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else "TV Recorder — Браузер"
    auto_fullscreen = sys.argv[3] == '1' if len(sys.argv) > 3 else False

    import webview
    window = webview.create_window(title, html=LOADING_HTML, width=1280, height=800)

    def on_real_page_loaded():
        window.events.loaded -= on_real_page_loaded
        if auto_fullscreen:
            window.toggle_fullscreen()

    def on_splash_loaded():
        # 'loaded' сработает и для настоящей страницы тоже — отписываемся
        # от заглушки и вешаем отдельный обработчик на реальную загрузку,
        # иначе уйдём в бесконечную перезагрузку.
        window.events.loaded -= on_splash_loaded
        window.events.loaded += on_real_page_loaded
        window.load_url(url)

    window.events.loaded += on_splash_loaded

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
