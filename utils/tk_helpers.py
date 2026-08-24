# utils/tk_helpers.py
"""Мелкие переиспользуемые Tk-хелперы, общие для нескольких gui/*.py —
раньше bind_cyrillic_layout_shortcuts жила только в gui/app_window.py;
вынесена сюда, чтобы gui/download_dialog.py могла её использовать без
циклического импорта (app_window.py <-> download_dialog.py)."""


def _make_clipboard_action(virtual_event: str):
    def handler(event):
        event.widget.event_generate(virtual_event)
        return 'break'
    return handler


# Tk на macOS матчит Cmd+C/V/X/A по СИМВОЛУ, который даёт нажатая клавиша,
# а не по её физическому положению. При активной русской раскладке (ЙЦУКЕН)
# те же клавиши дают кириллицу (V→М, C→С, X→Ч, A→Ф), и родные биндинги
# просто не срабатывают — при этом обычный ввод текста не страдает, отсюда
# и путаница "вставить не могу, а руками написать могу". Дублируем те же
# действия на кириллические варианты, ничего не убирая: на английской
# раскладке эти биндинги на кириллические клавиши никогда не сработают,
# так что двойной вставки не будет.
_CYRILLIC_CLIPBOARD_KEYSYMS = {
    '<<Paste>>': ('Cyrillic_em', 'Cyrillic_EM'),
    '<<Copy>>': ('Cyrillic_es', 'Cyrillic_ES'),
    '<<Cut>>': ('Cyrillic_che', 'Cyrillic_CHE'),
    '<<SelectAll>>': ('Cyrillic_ef', 'Cyrillic_EF'),
}


def bind_cyrillic_layout_shortcuts(widget):
    for virtual_event, keysyms in _CYRILLIC_CLIPBOARD_KEYSYMS.items():
        action = _make_clipboard_action(virtual_event)
        for keysym in keysyms:
            widget.bind(f'<Command-{keysym}>', action)
