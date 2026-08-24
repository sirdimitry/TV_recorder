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
Использование (режим поиска потока): python3 browser_capture.py <url> --sniff

Второй режим — не для просмотра/записи, а для core/link_resolver.py:
когда сайт рисует плеер через JS и обычный HTTP-скрейп HTML ничего не
находит, открываем страницу здесь (окно скрыто, hidden=True) и слушаем
её же собственные сетевые запросы — почти любой JS-плеер (hls.js и т.п.)
всё равно тянет .m3u8/.mpd/.mp4 через fetch/XHR или обычный <video src>,
просто это не видно в исходном HTML. Первая подходящая ссылка печатается
в stdout как "STREAM:<url>" (та же stdout-IPC схема, что и GEOMETRY:
ниже) — и окно сразу закрывается, никакой записи тут не происходит.

Кнопка fullscreen внутри самого плеера страницы (Fullscreen API,
element.requestFullscreen()) — та же беда, что и с нативным
toggle_fullscreen() выше: WebKit разворачивает элемент на весь ФИЗИЧЕСКИЙ
экран ноутбука, а не в пределах этого окна, и вырезаемая по старым
координатам область записи с этим никак не совпадает (замечено на
1tv.ru — играющее видео раздувается на весь монитор). Поэтому явно
запрещаем нативный fullscreen WKWebView (fullScreenEnabled=False, см.
on_gui_started) — плееру ничего не остаётся, кроме как остаться в
пределах страницы; если конкретный плеер после клика по кнопке не
перерисовался сам, добивает всё тот же _nudge_relayout ниже.

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

# Тот же UA, что и в core/link_resolver.py (_DEFAULT_UA) — держим одно и
# то же "лицо браузера" везде. Без него часть сайтов (замечено на
# 1tv.ru — баннер "ваш браузер устарел") принимает WKWebView за что-то
# нестандартное/устаревшее и рендерит урезанную/деградированную версию
# страницы.
_USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
               '(KHTML, like Gecko) Version/17.0 Safari/605.1.15')

LOADING_HTML = """
<html><body style="background:#1a1a1a;color:#999;margin:0;height:100vh;
display:flex;align-items:center;justify-content:center;
font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:15px;">
Загрузка страницы…
</body></html>
"""

# Раз нативный Fullscreen API у WKWebView отключён (fullScreenEnabled=False,
# см. on_gui_started — иначе видео раздувается на весь физический экран, а
# не в пределах этого окна), у страницы часто ПРОПАДАЕТ и собственная
# кнопка fullscreen в плеере: добропорядочные плееры сами проверяют
# document.fullscreenEnabled и просто не рисуют кнопку, если браузер её не
# поддерживает (замечено на vkvideo.ru — кнопки нет вовсе). Значит просить
# "нажать на кнопку в плеере" после нашего же запрета бессмысленно — надо
# самим сделать то же самое визуально, не трогая настоящий Fullscreen API.
# Поэтому Cmd+Enter теперь не полагается на requestFullscreen() вообще:
# находит проигрываемое <video>, сам растягивает его чистым CSS
# (position:fixed на весь вьюпорт) — растянуть можно только в пределах
# ЭТОГО окна (position:fixed считает от вьюпорта страницы, а не от
# физического экрана), сбежать некуда физически. Заодно включаем родные
# controls у <video> — своя оверлей-панель плеера (play/пауза/громкость)
# при таком трюке визуально перекрывается на весь экран вместе с видео и
# больше не кликабельна, нужна какая-то замена.
#
# Слушатель вешаем на каждую загруженную страницу/навигацию — идемпотентно
# (флаг на window, чтобы не навешивать по второму слушателю при повторных
# вызовах evaluate_js на той же странице). Помимо CSS-трюка дёргаем и
# старый resize-wobble (nudge_relayout через Python) — лишний толчок к
# перерисовке некоторым плеерам всё ещё не помешает.
RELAYOUT_HOTKEY_JS = """
(function() {
    if (window.__tvrecorder_relayout_bound) return;
    window.__tvrecorder_relayout_bound = true;

    window.__tvrecorder_toggle_fs = function() {
        if (window.__tvrecorder_fs_video) {
            // Второе нажатие — возвращаем как было: и стиль, и место в
            // дереве (см. ниже, почему видео вообще пришлось переносить).
            var v = window.__tvrecorder_fs_video;
            v.style.cssText = window.__tvrecorder_fs_prev_style || '';
            v.controls = window.__tvrecorder_fs_prev_controls;
            var parent = window.__tvrecorder_fs_prev_parent;
            var next = window.__tvrecorder_fs_prev_next;
            if (parent) {
                if (next && next.parentNode === parent) parent.insertBefore(v, next);
                else parent.appendChild(v);
            }
            window.__tvrecorder_fs_video = null;
            return;
        }
        // Ищем <video> не только в обычном DOM, но и рекурсивно внутри
        // shadow root-ов — иначе на таких сайтах вообще ничего не найдём
        // (document.querySelectorAll их не видит в принципе).
        function collectVideos(root, out) {
            out = out || [];
            root.querySelectorAll('video').forEach(function(v) { out.push(v); });
            root.querySelectorAll('*').forEach(function(el) {
                if (el.shadowRoot) collectVideos(el.shadowRoot, out);
            });
            return out;
        }
        var videos = collectVideos(document);
        if (!videos.length) return;
        // Среди нескольких <video> (превью похожих роликов в сайдбаре и
        // т.п.) реальный плеер почти всегда либо единственный проигрываемый
        // сейчас, либо (если ничего не запущено) визуально самый крупный.
        var video = videos.find(function(v) { return !v.paused; });
        if (!video) {
            video = videos.reduce(function(best, v) {
                if (!best) return v;
                var r = v.getBoundingClientRect(), br = best.getBoundingClientRect();
                return (r.width * r.height) > (br.width * br.height) ? v : best;
            }, null);
        }
        window.__tvrecorder_fs_video = video;
        window.__tvrecorder_fs_prev_style = video.style.cssText;
        window.__tvrecorder_fs_prev_controls = video.controls;
        window.__tvrecorder_fs_prev_parent = video.parentNode;
        window.__tvrecorder_fs_prev_next = video.nextSibling;
        // position:fixed растягивается на весь ВЬЮПОРТ, только если ни у
        // одного предка нет transform/filter/contain — у такого предка
        // fixed-потомок вместо этого растягивается только в его пределах
        // (замечено на vkvideo.ru: видео увеличивалось только в границах
        // маленького исходного плеера). Player-обёртки почти всегда именно
        // такие (transform для анимаций карусели и т.п.) — переносим сам
        // <video> прямо в <body>, минуя всех потенциальных "виновников".
        document.body.appendChild(video);
        video.style.cssText = 'position:fixed !important; inset:0 !important; ' +
            'width:100vw !important; height:100vh !important; max-width:100vw !important; ' +
            'max-height:100vh !important; z-index:2147483647 !important; background:#000 !important; ' +
            'object-fit:contain !important;';
        video.controls = true;
    };

    document.addEventListener('keydown', function(e) {
        if (e.metaKey && e.key === 'Enter') {
            e.preventDefault();
            try { window.__tvrecorder_toggle_fs(); } catch (err) {}
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


# Перехватываем fetch/XHR (там уходит подавляющее большинство запросов
# плееров вроде hls.js — сам манифест и его загрузку они всегда делают
# через один из этих двух API, даже если рендерят кадры через MediaSource)
# плюс на всякий случай следим за src/currentSrc у <video>/<source> —
# для страниц, отдающих поток нативно, без JS-плеера поверх. Дедуп по
# window.__tvrecorder_seen, чтобы не звать API на каждый повторный чанк.
SNIFF_JS = r"""
(function() {
    if (window.__tvrecorder_sniff_bound) return;
    window.__tvrecorder_sniff_bound = true;
    var seen = {};
    var STREAM_RE = /\.(m3u8|mpd|mp4)(\?|#|$)/i;
    function report(url) {
        try {
            if (!url || seen[url] || !STREAM_RE.test(url)) return;
            seen[url] = true;
            window.pywebview.api.report_stream(url);
        } catch (e) {}
    }
    var origFetch = window.fetch;
    if (origFetch) {
        window.fetch = function(input, init) {
            try { report(typeof input === 'string' ? input : (input && input.url)); } catch (e) {}
            return origFetch.apply(this, arguments);
        };
    }
    var origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        try { report(url); } catch (e) {}
        return origOpen.apply(this, arguments);
    };
    function scanMedia() {
        document.querySelectorAll('video,source').forEach(function(el) {
            report(el.currentSrc || el.src);
        });
    }
    scanMedia();
    new MutationObserver(scanMedia).observe(document.documentElement,
        {subtree: true, childList: true, attributes: true, attributeFilter: ['src']});
    setInterval(scanMedia, 1000);
})();
"""


class _SnifferApi:
    """Мост JS -> Python для режима --sniff: страница сама зовёт
    report_stream(url), как только её плеер обратился за потоком."""

    def __init__(self):
        self.window = None
        self.found = threading.Event()

    def report_stream(self, url):
        if self.found.is_set():
            return
        self.found.set()
        try:
            print(f"STREAM:{url}", flush=True)
        except Exception as e:
            print(f"Не удалось сообщить найденный поток: {e}", file=sys.stderr)
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass


def _run_sniff(url: str, timeout: float = 12.0):
    import webview
    api = _SnifferApi()
    window = webview.create_window("TV Recorder — sniff", url=url, hidden=True, js_api=api)
    api.window = window

    def on_loaded():
        try:
            window.evaluate_js(SNIFF_JS)
        except Exception as e:
            print(f"Не удалось привязать sniff: {e}", file=sys.stderr)

    window.events.loaded += on_loaded

    def watchdog():
        # Ничего не нашли за отведённое время — закрываем сами, чтобы
        # вызывающая сторона (LinkResolver) не ждала дольше таймаута.
        if not api.found.wait(timeout):
            try:
                window.destroy()
            except Exception:
                pass

    threading.Thread(target=watchdog, daemon=True).start()
    webview.start(user_agent=_USER_AGENT)


def main():
    args = [a for a in sys.argv[1:] if a != '--sniff']
    sniff = '--sniff' in sys.argv[1:]
    if not args:
        print("Usage: browser_capture.py <url> [title] | browser_capture.py <url> --sniff", file=sys.stderr)
        sys.exit(1)
    url = args[0]

    if sniff:
        _run_sniff(url)
        return

    title = args[1] if len(args) > 1 else "TV Recorder — Браузер"

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
                    instance.webview.configuration().preferences().setValue_forKey_(False, 'fullScreenEnabled')
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

    webview.start(func=on_gui_started, user_agent=_USER_AGENT)


if __name__ == '__main__':
    main()
