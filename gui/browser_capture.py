# gui/browser_capture.py
"""Отдельный процесс с окном встроенного браузера (pywebview) — намеренно
НЕ часть основного Tk-процесса приложения: pywebview.start() сам владеет
run loop-ом на macOS, и совмещать его с уже работающим циклом Tkinter в
одном процессе рискованно. core/recorder.py запускает этот файл через
subprocess.Popen и параллельно пишет экран через ffmpeg — сам браузер
кадры не отдаёт и с записью никак не взаимодействует.

Запись пишет НЕ весь экран, а только область под этим окном —
core/recorder.py читает координаты окна из строки "GEOMETRY:x,y,w,h",
которую этот процесс печатает в stdout один раз, сразу после появления
окна (см. on_gui_started), и обрезает по ним запись через
core/screen_capture.py. Именно поэтому окно НЕ уходит в нативный
macOS-fullscreen (Cmd+Ctrl+F/toggle_fullscreen) — тогда окно займёт весь
физический дисплей, а вырезаемая область записи так и останется старой,
маленькой: получится либо путаница ("на экране видео на весь монитор, а
в записи — крохотный обрезок"), либо запись без смысла в fullscreen
вообще. Вместо этого окну просто дают разумный размер по умолчанию —
и пишется оно целиком, каким бы оно ни было.

Использование: python3 browser_capture.py <url> [title]

Кнопка fullscreen внутри самого плеера страницы (Fullscreen API,
element.requestFullscreen()) на macOS ненадёжна: pywebview создаёт
WKWebView без приватного (не задокументированного публично) флага
fullScreenEnabled — без него WebKit по умолчанию отклоняет такие запросы
у части сайтов. Включаем этот флаг (см. on_gui_started) на случай, если
плеер при успешном fullscreen просто меняет размер/раскладку внутри той
же страницы (а не пытается захватить весь физический экран) — тогда
дальше срабатывает всё тот же _nudge_relayout ниже.

Универсальный трюк на случай, если плеер после клика по своей кнопке
fullscreen не перерисовался (по образцу OBS Studio Browser Source — там
в такой ситуации помогает подвинуть размер окна источника на 1px и
обратно): некоторые плееры меняют внутреннее состояние/CSS, но движок
браузера не перерисовывает кадр под новый размер, пока не получит
настоящее событие resize. Поэтому после каждой загрузки страницы и по
Cmd+Enter мы сами дёргаем размер ОКНА (не выходя за его прежние
границы — resize на 1px и обратно, а не в fullscreen) — см.
_nudge_relayout.

Некоторые сайты (замечено на otr-online.ru) реально грузятся по 15-20
секунд — без всякой индикации это выглядит так, будто окно просто не
открылось. Поэтому сперва показываем тёмную заглушку "Загрузка…" и
переключаемся на настоящий адрес отдельным шагом, а не грузим его сразу."""
import sys
import threading

# Побольше дефолтного 1280x800 — окно всё равно не идёт в fullscreen,
# а от размера окна напрямую зависит резкость исходного кадра для записи
# (масштабируется до 720p уже после обрезки, см. core/screen_capture.py).
DEFAULT_WIDTH = 1600
DEFAULT_HEIGHT = 900

LOADING_HTML = """
<html><body style="background:#1a1a1a;color:#999;margin:0;height:100vh;
display:flex;align-items:center;justify-content:center;
font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:15px;">
Загрузка страницы…
</body></html>
"""

# Слушатель клавиш вешаем на каждую загруженную страницу/навигацию —
# идемпотентно (флаг на window, чтобы не навешивать по второму слушателю
# при повторных вызовах evaluate_js на той же странице).
RELAYOUT_HOTKEY_JS = """
(function() {
    if (window.__tvrecorder_relayout_bound) return;
    window.__tvrecorder_relayout_bound = true;
    document.addEventListener('keydown', function(e) {
        if (e.metaKey && e.key === 'Enter') {
            e.preventDefault();
            window.pywebview.api.nudge_relayout();
        }
    }, true);
})();
"""


def _nudge_relayout(window):
    """Резкий resize окна на 1px и обратно (внутри тех же границ, БЕЗ
    ухода в fullscreen) — форсирует у WKWebView пересчёт раскладки и
    перерисовку. Тот же приём, что спасает в OBS Studio Browser Source,
    когда плеер после клика по своему fullscreen не перерисовывается сам
    (видео технически "развернулось", но кадр так и остался маленьким)."""
    try:
        w, h = window.width, window.height
        window.resize(w, h + 1)
        threading.Timer(0.15, lambda: window.resize(w, h)).start()
    except Exception as e:
        print(f"Не удалось форсировать перерисовку окна: {e}", file=sys.stderr)


class _RelayoutApi:
    """Мост из JS страницы в pywebview.Window — сама страница попросить
    форсированный resize не может, поэтому по Cmd+Enter js-обработчик
    вызывает эту функцию через window.pywebview.api."""

    def __init__(self):
        self.window = None

    def nudge_relayout(self):
        if self.window is not None:
            _nudge_relayout(self.window)


def main():
    if len(sys.argv) < 2:
        print("Usage: browser_capture.py <url> [title]", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else "TV Recorder — Браузер"

    import webview
    api = _RelayoutApi()
    window = webview.create_window(title, html=LOADING_HTML,
                                    width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, js_api=api)
    api.window = window

    def on_page_loaded():
        try:
            window.evaluate_js(RELAYOUT_HOTKEY_JS)
        except Exception as e:
            print(f"Не удалось привязать Cmd+Enter: {e}", file=sys.stderr)
        # Даём плееру время инициализироваться, потом форсируем перерисовку
        # даже без нажатия Cmd+Enter — часто именно первый рендер после
        # загрузки и оказывается тем самым "застрявшим" кадром.
        threading.Timer(2.0, lambda: _nudge_relayout(window)).start()

    def on_splash_loaded():
        # 'loaded' сработает и для настоящей страницы тоже — отписываемся
        # от заглушки и вешаем постоянный обработчик на реальные загрузки
        # (в т.ч. переходы по ссылкам внутри сайта), иначе уйдём в
        # бесконечную перезагрузку и потеряем слушатель клавиш после
        # первой же навигации.
        window.events.loaded -= on_splash_loaded
        window.events.loaded += on_page_loaded
        window.load_url(url)

    window.events.loaded += on_splash_loaded

    def on_gui_started():
        # По умолчанию — вся рамка окна (запасной вариант для не-macOS);
        # на macOS ниже пересчитываем на область именно содержимого, без
        # строки заголовка со светофором, чтобы она не попадала в запись.
        x, y, w, h = window.x, window.y, window.width, window.height

        if sys.platform == 'darwin':
            try:
                from webview.platforms.cocoa import BrowserView
                instance = BrowserView.instances.get(window.uid)
                if instance is not None:
                    instance.webview.configuration().preferences().setValue_forKey_(True, 'fullScreenEnabled')
                    native_window = instance.window
                    content_rect = native_window.contentRectForFrameRect_(native_window.frame())
                    screen_height = instance.screen.size.height
                    x = content_rect.origin.x
                    y = screen_height - content_rect.origin.y - content_rect.size.height
                    w = content_rect.size.width
                    h = content_rect.size.height
            except Exception as e:
                print(f"Не удалось включить fullScreenEnabled / вычислить область содержимого: {e}",
                      file=sys.stderr)

        # core/recorder.py читает эту строку из stdout, чтобы обрезать
        # запись экрана точно по границам окна вместо всего дисплея —
        # координаты в points (как их отдаёт pywebview/Cocoa), пересчёт в
        # пиксели делает читающая сторона (там же, где известен retina-масштаб).
        try:
            print(f"GEOMETRY:{x},{y},{w},{h}", flush=True)
        except Exception as e:
            print(f"Не удалось сообщить геометрию окна: {e}", file=sys.stderr)

    webview.start(func=on_gui_started)


if __name__ == '__main__':
    main()
