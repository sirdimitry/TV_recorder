# gui/browser_capture.py
"""Отдельный процесс с окном встроенного браузера (pywebview) — намеренно
НЕ часть основного Tk-процесса приложения: pywebview.start() сам владеет
run loop-ом на macOS, и совмещать его с уже работающим циклом Tkinter в
одном процессе рискованно. core/recorder.py запускает этот файл через
subprocess.Popen и параллельно пишет экран через ffmpeg — сам браузер
кадры не отдаёт и с записью никак не взаимодействует.

Важно: запись пишет НЕ весь экран, а только область под этим окном —
core/recorder.py читает координаты окна из строки "GEOMETRY:x,y,w,h",
которую этот процесс печатает в stdout сразу после старта (см.
on_gui_started), и обрезает по ним запись через core/screen_capture.py.
Поэтому "раскрыть на весь экран" здесь не значит "занять весь дисплей" —
достаточно, чтобы видео заполняло само окно, каким бы оно ни было.

Использование: python3 browser_capture.py <url> [title] [auto_fullscreen]

auto_fullscreen ('1'/'0', по умолчанию '0') — сразу после загрузки страницы
разворачивает само ОКНО во весь экран (macOS-уровня, Cmd+Ctrl+F). Включаем
это только когда вызывающий код точно передал "чистую" ссылку плеера
(core/link_resolver.py: LinkInfo.player_url — прямую ссылку на сам
плеер, без сайдбаров/cookie-баннера) — там содержимое и так занимает всё
окно целиком. Для обычной страницы (без player_url) так не делаем: у неё
есть ещё и сайдбар/реклама/баннер, разворачивать окно молча — только
исказит то, что человек ожидает увидеть. И поскольку запись всё равно
обрезана по окну (см. выше), окно-fullscreen тут — просто "покрупнее",
а не обязательное условие для нормальной записи.

Кнопка fullscreen внутри самого плеера страницы (Fullscreen API,
element.requestFullscreen()) на macOS ненадёжна: pywebview создаёт
WKWebView без приватного (не задокументированного публично) флага
fullScreenEnabled — без него WebKit по умолчанию отклоняет такие запросы
у части сайтов. Включаем этот флаг тоже (см. enable_page_fullscreen), но
он не гарантирует, что кнопка заработает на всех сайтах — авто-fullscreen
окна через auto_fullscreen надёжнее там, где применим.

Универсальный запасной вариант — сочетание клавиш Cmd+Enter прямо в окне:
переключает ОКНО во весь экран независимо от того, нашёлся ли player_url
и работает ли у сайта своя кнопка fullscreen (пример, где ни то ни другое
не сработало: iz.ru — там вообще нет предсказуемо встроенного плеера).
Работает всегда, на любой странице, в т.ч. после перехода по ссылкам
внутри сайта — слушатель клавиш переустанавливается на каждую загрузку.

Некоторые сайты (замечено на otr-online.ru) реально грузятся по 15-20
секунд — без всякой индикации это выглядит так, будто окно просто не
открылось. Поэтому сперва показываем тёмную заглушку "Загрузка…" и
переключаемся на настоящий адрес отдельным шагом, а не грузим его сразу.

Ещё один универсальный трюк (по образцу OBS Studio Browser Source —
там при "не раскрывается видео на весь экран" помогает подвинуть размер
окна источника на 1px и обратно): некоторые плееры после клика по своей
кнопке fullscreen меняют внутреннее состояние/CSS, но движок браузера не
перерисовывает кадр под новый размер, пока не получит настоящее событие
resize. Поэтому после каждой загрузки страницы и по Cmd+Enter мы сами
дёргаем размер окна на 1px и возвращаем обратно — см. _nudge_relayout."""
import sys
import threading

LOADING_HTML = """
<html><body style="background:#1a1a1a;color:#999;margin:0;height:100vh;
display:flex;align-items:center;justify-content:center;
font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:15px;">
Загрузка страницы…
</body></html>
"""

# document на каждой загруженной странице/навигации — идемпотентно (флаг
# на window, чтобы не навешивать по второму слушателю при повторных вызовах
# evaluate_js на той же странице).
FULLSCREEN_HOTKEY_JS = """
(function() {
    if (window.__tvrecorder_fs_bound) return;
    window.__tvrecorder_fs_bound = true;
    document.addEventListener('keydown', function(e) {
        if (e.metaKey && e.key === 'Enter') {
            e.preventDefault();
            window.pywebview.api.toggle_fullscreen();
        }
    }, true);
})();
"""


def _nudge_relayout(window):
    """Резкий resize окна на 1px и обратно — форсирует у WKWebView пересчёт
    раскладки и перерисовку. Тот же приём, что спасает в OBS Studio Browser
    Source, когда плеер после клика по своему fullscreen не перерисовывается
    сам (видео технически "развернулось", но кадр так и остался маленьким)."""
    try:
        w, h = window.width, window.height
        window.resize(w, h + 1)
        threading.Timer(0.15, lambda: window.resize(w, h)).start()
    except Exception as e:
        print(f"Не удалось форсировать перерисовку окна: {e}", file=sys.stderr)


class _FullscreenApi:
    """Мост из JS страницы в pywebview.Window — сама страница попросить
    macOS-fullscreen окна или форсированный resize не может, поэтому по
    Cmd+Enter js-обработчик вызывает эти функции через window.pywebview.api."""

    def __init__(self):
        self.window = None

    def toggle_fullscreen(self):
        if self.window is not None:
            self.window.toggle_fullscreen()
            _nudge_relayout(self.window)


def main():
    if len(sys.argv) < 2:
        print("Usage: browser_capture.py <url> [title] [auto_fullscreen]", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else "TV Recorder — Браузер"
    auto_fullscreen = sys.argv[3] == '1' if len(sys.argv) > 3 else False

    import webview
    api = _FullscreenApi()
    window = webview.create_window(title, html=LOADING_HTML, width=1280, height=800, js_api=api)
    api.window = window

    first_real_load = {'done': False}

    def on_page_loaded():
        try:
            window.evaluate_js(FULLSCREEN_HOTKEY_JS)
        except Exception as e:
            print(f"Не удалось привязать Cmd+Enter для fullscreen: {e}", file=sys.stderr)
        if auto_fullscreen and not first_real_load['done']:
            window.toggle_fullscreen()
        first_real_load['done'] = True
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
