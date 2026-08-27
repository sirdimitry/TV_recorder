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
находит, открываем страницу здесь (окно рендерится по-настоящему, но за
пределами экрана — см. _run_sniff) и слушаем её же собственные сетевые
запросы — почти любой JS-плеер (hls.js и т.п.)
всё равно тянет .m3u8/.mpd/.mp4 через fetch/XHR или обычный <video src>,
просто это не видно в исходном HTML. Первая подходящая ссылка печатается
в stdout как "STREAM:<url>" (та же stdout-IPC схема, что и GEOMETRY:
ниже). Если вместо прямого потока страница вставляет <iframe> с чужого
домена (плеер там, а не тут — cross-origin, внутрь не залезть скриптом),
печатаем его src как "IFRAME:<url>" — resolve_link() пробует довести его
до потока отдельно (см. _resolve_via_browser_sniff). В обоих случаях окно
сразу закрывается, никакой записи тут не происходит.

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
import time

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

# Обнаружено вживую на otr-online.ru: баннер согласия на cookie перекрывает
# страницу И плеер вообще не инициализируется (не рисуется, не тянет
# поток), пока баннер не закрыт — значит из-за него молчал не только режим
# ускоренной записи (нечего искать через FIND_VIDEO_JS), но и обычный
# sniff тоже (SNIFF_JS ничего не перехватывал, потому что плеер не успевал
# даже начать грузиться). Ищем и жмём кнопку по тексту — эвристика, но
# покрывает подавляющее большинство баннеров (рус./англ. форм). Как и
# SPEED_CONTROL_JS, перепроверяем раз в секунду — баннер может появиться
# не сразу (не на первый evaluate_js после 'loaded').
COOKIE_DISMISS_JS = r"""
(function() {
    if (window.__tvrecorder_cookie_dismiss_bound) return;
    window.__tvrecorder_cookie_dismiss_bound = true;
    var PATTERN = /^(принять(\s+все)?|согласен|согласна|хорошо|ок|понятно|accept(\s+all)?|i\s+agree|agree|got it|allow all)\s*$/i;
    function tryDismiss() {
        try {
            var candidates = document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]');
            for (var i = 0; i < candidates.length; i++) {
                var el = candidates[i];
                var text = (el.value || el.textContent || '').trim();
                if (PATTERN.test(text)) {
                    try { el.click(); } catch (e) {}
                }
            }
        } catch (e) {}
    }
    tryDismiss();
    setInterval(tryDismiss, 1000);
})();
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
# Общий поиск <video> — рекурсивно внутри shadow root-ов тоже (иначе на
# части сайтов document.querySelectorAll вообще ничего не находит), и с
# тем же выбором "главного" ролика среди нескольких кандидатов (превью
# похожих роликов в сайдбаре и т.п.): либо единственный сейчас
# проигрываемый, либо (если ничего не запущено) визуально самый крупный.
# Общая функция для RELAYOUT_HOTKEY_JS (Cmd+Enter) и SPEED_CONTROL_JS
# (ускоренное воспроизведение для быстрой записи, см. ниже) — раньше была
# продублирована, теперь одна реализация на обоих потребителей.
FIND_VIDEO_JS = """
(function() {
    if (window.__tvrecorder_find_video_bound) return;
    window.__tvrecorder_find_video_bound = true;

    window.__tvrecorder_findVideo = function() {
        function collectVideos(root, out) {
            out = out || [];
            root.querySelectorAll('video').forEach(function(v) { out.push(v); });
            root.querySelectorAll('*').forEach(function(el) {
                if (el.shadowRoot) collectVideos(el.shadowRoot, out);
            });
            return out;
        }
        var videos = collectVideos(document);
        if (!videos.length) return null;
        var video = videos.find(function(v) { return !v.paused; });
        if (!video) {
            video = videos.reduce(function(best, v) {
                if (!best) return v;
                var r = v.getBoundingClientRect(), br = best.getBoundingClientRect();
                return (r.width * r.height) > (br.width * br.height) ? v : best;
            }, null);
        }
        return video;
    };
})();
"""

# Ускоренное воспроизведение для быстрой записи ("резервный" фолбэк, когда
# ни прямой поток, ни sniff не нашли ссылку — core/recorder.py:
# start_browser_recording(speed_factor=...)): реальное экранное время
# записи меньше в SPEED раз, чем длительность самого ролика — после записи
# core/screen_capture.py:build_timestretch_cmd растягивает файл обратно.
# video.playbackRate — стандартный DOM API, работает независимо от того,
# рисует ли сам плеер сайта видимый регулятор скорости в своём UI.
# Переустанавливаем раз в секунду: некоторые плееры сами сбрасывают
# playbackRate на 1 при смене качества/рекламной вставке и т.п.
SPEED_CONTROL_JS = """
(function(speed) {
    function apply() {
        try {
            var v = window.__tvrecorder_findVideo && window.__tvrecorder_findVideo();
            if (!v) return;
            if (v.playbackRate !== speed) v.playbackRate = speed;
            if (v.paused) v.play().catch(function() {});
            // Сообщаем Python-стороне, что ускорение реально применилось к
            // настоящему <video> — если плеер живёт в чужом (cross-origin)
            // iframe, window.__tvrecorder_findVideo() ничего не найдёт
            // (в главном документе видео просто нет физически), apply()
            // тихо ничего не сделает, и это сообщение не придёт вовсе —
            // core/recorder.py тогда НЕ будет растягивать запись обратно
            // по времени, раз реального ускорения не произошло.
            if (window.pywebview && window.pywebview.api && window.pywebview.api.report_speed_active) {
                window.pywebview.api.report_speed_active();
            }
        } catch (e) {}
    }
    apply();
    setInterval(apply, 1000);
})(%s);
"""


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
        var video = window.__tvrecorder_findVideo && window.__tvrecorder_findVideo();
        if (!video) return;
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
        self._speed_reported = False

    def nudge_relayout(self):
        if self.window is not None:
            _nudge_relayout(self.window)

    def report_speed_active(self):
        # SPEED_CONTROL_JS зовёт это, только когда реально нашла <video> и
        # выставила playbackRate — на сайтах, где сам плеер живёт в чужом
        # (cross-origin) iframe, найти его со страницы принципиально нельзя
        # (замечено на otr-online.ru: статья — Vue, ролик — отдельный
        # otr.webcaster.pro внутри iframe), и это сообщение просто не
        # придёт. core/recorder.py ждёт его с таймаутом именно поэтому —
        # если не подтвердилось, запись идёт обычным темпом БЕЗ обратной
        # растяжки, иначе нормальную по скорости запись растянули бы
        # заведомо неправильно.
        if self._speed_reported:
            return
        self._speed_reported = True
        try:
            print("SPEED_ACTIVE", flush=True)
        except Exception as e:
            print(f"Не удалось сообщить об активном ускорении: {e}", file=sys.stderr)


# Перехватываем fetch/XHR (там уходит подавляющее большинство запросов
# плееров вроде hls.js — сам манифест и его загрузку они всегда делают
# через один из этих двух API, даже если рендерят кадры через MediaSource)
# плюс на всякий случай следим за src/currentSrc у <video>/<source> —
# для страниц, отдающих поток нативно, без JS-плеера поверх. Дедуп по
# window.__tvrecorder_seen, чтобы не звать API на каждый повторный чанк.
#
# Отдельно ловим и <iframe> — плеер может жить в чужом origin (замечено
# на otr-online.ru: сама статья рисуется Vue-ем нормально, но конкретный
# ролик — в iframe на otr.webcaster.pro, вставленном уже ПОСЛЕ гидратации,
# так что в исходном HTML его нет). Внутрь чужого iframe скриптом не
# залезть (та же история, что и с cross-origin у 1tv.ru), но сам src
# читается снаружи без проблем — а webcaster.pro-ссылки core/link_resolver.py
# уже умеет доводить до .m3u8 своей отдельной XML-цепочкой
# (_resolve_webcaster_player). /schedule-путь у этого вендора — общий
# виджет "сейчас в эфире", не ролик конкретной страницы, его пропускаем.
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
    var seenIframes = {};
    function reportIframe(url) {
        try {
            if (!url || seenIframes[url] || url.indexOf('/schedule') !== -1) return;
            seenIframes[url] = true;
            window.pywebview.api.report_iframe(url);
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
        document.querySelectorAll('iframe').forEach(function(el) {
            reportIframe(el.src || el.getAttribute('data-src'));
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
    report_stream(url)/report_iframe(url), как только её плеер обратился
    за потоком или в разметке появился фрейм с плеером."""

    def __init__(self):
        self.window = None
        self.found = threading.Event()

    def _report(self, prefix: str, url: str):
        if self.found.is_set():
            return
        self.found.set()
        try:
            print(f"{prefix}:{url}", flush=True)
        except Exception as e:
            print(f"Не удалось сообщить найденный поток: {e}", file=sys.stderr)
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass

    def report_stream(self, url):
        self._report('STREAM', url)

    def report_iframe(self, url):
        self._report('IFRAME', url)


def _hide_from_dock():
    """minimized=True (см. ниже) рендерит страницу по-настоящему, но на
    macOS каждый такой процесс всё равно ненадолго получает собственную
    иконку в Dock (обычное поведение любого GUI-процесса) — при частых
    sniff-попытках они успевают заметно накопиться визуально. Переводим
    процесс в "accessory" (как строка-меню без иконки в Dock) — окно и
    его рендеринг работают точно так же, просто в Dock ничего не видно."""
    if sys.platform != 'darwin':
        return
    try:
        import AppKit
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    except Exception as e:
        print(f"Не удалось скрыть окно поиска потока из Dock: {e}", file=sys.stderr)


def _make_invisible(window):
    """Настоящее (не hidden/minimized) окно нужно ради корректного viewport
    для сайтов с ленивой подгрузкой по видимости (IntersectionObserver —
    см. историю ниже), но пользователь не должен иметь физической
    возможности его увидеть или кликнуть по нему.

    Сначала пробовали просто отодвинуть окно координатами далеко за
    пределы экрана — оказалось, что Cocoa-бэкенд pywebview там падает:
    NSWindow.screen() возвращает nil, если точка не принадлежит ни одному
    реальному дисплею, а внутренний обработчик перемещения окна этого не
    ожидает (AttributeError на screen().frame()). Поэтому вместо геометрии
    прячем через сам Cocoa API, уже открытый в этом файле для
    fullScreenEnabled (см. main(): BrowserView.instances.get(window.uid)):
    нулевая прозрачность делает окно невидимым, а ignoresMouseEvents —
    некликабельным, оставляя саму отрисовку страницы (и её реальный
    размер) нетронутыми.

    Вызывается из func= у webview.start() — тот запускает эту функцию в
    отдельном потоке ПАРАЛЛЕЛЬНО с самим созданием окна (см. webview/
    __init__.py: сначала стартует поток с func, потом уже
    guilib.create_window(...)), а не после него — поэтому в момент вызова
    self.window нативного BrowserView может ещё не быть выставлен.
    Короткий retry-цикл вместо однократной попытки."""
    if sys.platform != 'darwin':
        return
    try:
        from webview.platforms.cocoa import BrowserView
        deadline = time.time() + 3.0
        instance = None
        while time.time() < deadline:
            instance = BrowserView.instances.get(window.uid)
            if instance is not None and getattr(instance, 'window', None) is not None:
                break
            instance = None
            time.sleep(0.02)
        if instance is not None:
            instance.window.setAlphaValue_(0.0)
            instance.window.setIgnoresMouseEvents_(True)
    except Exception as e:
        print(f"Не удалось спрятать sniff-окно: {e}", file=sys.stderr)


def _run_sniff(url: str, timeout: float = 40.0):
    import webview
    api = _SnifferApi()
    # hidden=True казалось безопаснее (никогда не мелькнёт на экране), но
    # по-настоящему скрытое окно на macOS не получает нормальный viewport: у
    # сайтов, которые подгружают плеер лениво по видимости
    # (IntersectionObserver — частый паттерн, замечено на otr-online.ru:
    # страница рендерится, а iframe с плеером — не вставляется вовсе), sniff
    # молчал все 18с впустую. minimized=True вместо этого какое-то время
    # использовался как компромисс (страница рендерится по-настоящему,
    # окно должно уходить сразу в Dock) — но на практике минимизированное
    # окно иногда всё же оказывалось видимым на экране, а его системная
    # кнопка закрытия в таком состоянии не реагирует на клик (известная
    # особенность macOS-бэкенда pywebview для окон, созданных сразу
    # свёрнутыми — у них не до конца настраивается обработчик закрытия),
    # так что пользователь видел зависшее окно, которое не закрывалось
    # руками и пропадало только по сторожевому таймеру ниже. Теперь окно
    # рендерится как обычное (полноценный viewport), а прячем его уже
    # после появления через _make_invisible — см. её docstring, почему не
    # просто офсетом координат.
    window = webview.create_window("TV Recorder — sniff", url=url,
                                    width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, js_api=api)
    api.window = window

    def on_loaded():
        try:
            window.evaluate_js(COOKIE_DISMISS_JS)
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

    def on_started():
        _hide_from_dock()
        _make_invisible(window)

    webview.start(func=on_started, user_agent=_USER_AGENT)


def main():
    raw_args = sys.argv[1:]
    sniff = '--sniff' in raw_args
    speed = None
    if '--speed' in raw_args:
        i = raw_args.index('--speed')
        try:
            speed = float(raw_args[i + 1])
        except (IndexError, ValueError):
            print("--speed требует числовой аргумент", file=sys.stderr)
        raw_args = raw_args[:i] + raw_args[i + 2:]
    args = [a for a in raw_args if a != '--sniff']
    if not args:
        print("Usage: browser_capture.py <url> [title] [--speed N] | browser_capture.py <url> --sniff",
              file=sys.stderr)
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
            window.evaluate_js(COOKIE_DISMISS_JS)
            window.evaluate_js(FIND_VIDEO_JS)
            window.evaluate_js(RELAYOUT_HOTKEY_JS)
            if speed:
                window.evaluate_js(SPEED_CONTROL_JS % speed)
        except Exception as e:
            print(f"Не удалось привязать Cmd+Enter/ускорение: {e}", file=sys.stderr)
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
                    # Запись экрана обрезает по ЭТИМ координатам с холста
                    # всего дисплея, не по конкретному окну — если поверх
                    # окажется любое чужое окно (пользователь переключился,
                    # всплыло уведомление и т.п.), запишется ОНО, а не
                    # страница, безо всякой ошибки. Живьём поймали именно
                    # так: во время фонового теста поверх оказалось совсем
                    # другое окно, и запись ушла в него. Явно поднимаем окно
                    # и активируем всё приложение перед тем, как печатать
                    # геометрию для записи — не гарантия (пользователь может
                    # переключиться позже сам), но закрывает самый частый
                    # случай ("окно открылось не поверх остального").
                    try:
                        import AppKit
                        AppKit.NSApp.activateIgnoringOtherApps_(True)
                    except Exception:
                        pass
                    native_window.makeKeyAndOrderFront_(None)
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
