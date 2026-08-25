# gui/download_dialog.py
"""Диалог "Добавить загрузку" — отдельным файлом, а не ещё одним методом
gui/app_window.py (тот и так уже больше тысячи строк). Копирует проверенный
UX диалога добавления ссылки в "Мои ссылки" (gui/app_window.py:
_add_link_dialog): поле URL с debounce-автоопределением названия/превью/
хронометража через resolve_link(), только добавляет выбор разрешения и
папки сохранения — это разовое скачивание, а не отслеживаемая ссылка."""
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from core.link_resolver import list_available_heights, resolve_link
from utils.config import Config
from utils.tk_helpers import bind_cyrillic_layout_shortcuts

RESOLUTIONS = ["1080p", "720p", "480p", "360p"]


def show_add_download_dialog(parent, colors, storage, downloader, on_added=None):
    c = colors
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Добавить загрузку")
    dialog.geometry("480x400")
    dialog.transient(parent)
    dialog.configure(fg_color=c['bg_secondary'])
    dialog.after(100, dialog.grab_set)

    body = ctk.CTkFrame(dialog, fg_color='transparent')
    body.pack(fill='both', expand=True, padx=20, pady=20)
    body.columnconfigure(1, weight=1)

    ctk.CTkLabel(body, text="Ссылка:", text_color=c['text_secondary']).grid(
        row=0, column=0, padx=(0, 12), pady=8, sticky='w')
    url_var = tk.StringVar()
    url_entry = ctk.CTkEntry(body, textvariable=url_var, height=32, corner_radius=Config.RADIUS_SM,
                              placeholder_text="https://…", fg_color=c['bg_primary'],
                              border_color=c['border'], text_color=c['text_primary'])
    url_entry.grid(row=0, column=1, pady=8, sticky='ew')
    bind_cyrillic_layout_shortcuts(url_entry)

    hint = ctk.CTkLabel(body, text="", font=ctk.CTkFont(size=10), text_color=c['text_muted'],
                         justify='left', wraplength=430)
    hint.grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 0))

    ctk.CTkLabel(body, text="Разрешение:", text_color=c['text_secondary']).grid(
        row=2, column=0, padx=(0, 12), pady=8, sticky='w')
    resolution_var = tk.StringVar(value=RESOLUTIONS[0])
    ctk.CTkOptionMenu(body, values=RESOLUTIONS, variable=resolution_var, height=32,
                       corner_radius=Config.RADIUS_SM, fg_color=c['bg_primary'],
                       button_color=c['bg_tertiary'], button_hover_color=c['bg_hover'],
                       text_color=c['text_primary']).grid(row=2, column=1, pady=8, sticky='w')

    # Дропдаун качества реально на что-то влияет, только если у источника
    # больше одного варианта — у многих встраиваемых плееров новостных
    # сайтов доступно ровно одно (см. list_available_heights) — эта строка
    # честно говорит, во что выбор превратится, а не оставляет гадать.
    quality_hint = ctk.CTkLabel(body, text="", font=ctk.CTkFont(size=10), text_color=c['text_muted'],
                                 justify='left', wraplength=430)
    quality_hint.grid(row=3, column=0, columnspan=2, sticky='w', pady=(0, 0))

    ctk.CTkLabel(body, text="Папка:", text_color=c['text_secondary']).grid(
        row=4, column=0, padx=(0, 12), pady=8, sticky='w')
    folder_frame = ctk.CTkFrame(body, fg_color='transparent')
    folder_frame.grid(row=4, column=1, pady=8, sticky='ew')
    folder_frame.columnconfigure(0, weight=1)
    folder_var = tk.StringVar(value=str(Config.get_downloads_dir()))
    folder_entry = ctk.CTkEntry(folder_frame, textvariable=folder_var, height=32,
                                 corner_radius=Config.RADIUS_SM, fg_color=c['bg_primary'],
                                 border_color=c['border'], text_color=c['text_primary'], state='readonly')
    folder_entry.grid(row=0, column=0, sticky='ew', padx=(0, 6))

    def choose_folder():
        selected = filedialog.askdirectory(parent=dialog, initialdir=folder_var.get())
        if selected:
            folder_var.set(selected)

    ctk.CTkButton(folder_frame, text="Изменить", command=choose_folder, height=32, width=90,
                  corner_radius=Config.RADIUS_SM, fg_color=c['bg_tertiary'], hover_color=c['bg_hover'],
                  text_color=c['text_primary']).grid(row=0, column=1)

    detect_generation = {'id': 0}
    debounce = {'after_id': None}

    def on_url_change(*_):
        if debounce['after_id']:
            dialog.after_cancel(debounce['after_id'])
        debounce['after_id'] = dialog.after(600, start_detection)

    def start_detection():
        url = url_var.get().strip()
        if not url:
            hint.configure(text="")
            return

        detect_generation['id'] += 1
        my_generation = detect_generation['id']
        hint.configure(text="Ищем видео на странице…")
        quality_hint.configure(text="")

        def resolve_async():
            # Само разрешение по выбранному качеству досчитывается заново
            # непосредственно при скачивании (core/downloader.py) — здесь
            # только предпросмотр названия/превью/хронометража, дефолтного
            # качества для этого достаточно.
            info = resolve_link(url)

            def apply():
                if my_generation != detect_generation['id']:
                    return
                if not info.ok:
                    hint.configure(text=f"Не нашли видео по этой ссылке: {info.error}")
                    return
                minutes = int(info.duration // 60) if info.duration else None
                duration_part = f", ~{minutes} мин" if minutes else ""
                hint.configure(text=f"Найдено: {info.title}{duration_part}")

            dialog.after(0, apply)

            # Список доступных качеств — отдельным (более медленным) шагом
            # после основного превью, чтобы название/превью не ждали лишний
            # сетевой запрос — сама подпись обновится чуть позже, это не
            # блокирует остальной диалог.
            if info.ok:
                heights = list_available_heights(url, info)

                def apply_quality():
                    if my_generation != detect_generation['id']:
                        return
                    if not heights:
                        quality_hint.configure(text="")
                    elif len(heights) == 1:
                        quality_hint.configure(text=f"У источника доступно только {heights[0]}p "
                                                     f"— выбор качества не повлияет")
                    else:
                        available = ", ".join(f"{h}p" for h in heights)
                        quality_hint.configure(text=f"Доступные качества: {available}")

                dialog.after(0, apply_quality)

        threading.Thread(target=resolve_async, daemon=True).start()

    url_var.trace_add('write', on_url_change)

    def save():
        url = url_var.get().strip()
        if not url:
            messagebox.showwarning("Внимание", "Вставьте ссылку", parent=dialog)
            return
        target_height = int(resolution_var.get().rstrip('p'))
        output_dir = Path(folder_var.get()).expanduser()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            messagebox.showerror("Ошибка", f"Не удалось использовать папку:\n{error}", parent=dialog)
            return
        Config.set_downloads_dir(output_dir)

        downloader.start_download(url, target_height=target_height, output_dir=output_dir)
        dialog.destroy()
        if on_added:
            on_added()

    ctk.CTkButton(body, text="Скачать", command=save, height=36, corner_radius=Config.RADIUS_SM,
                  fg_color=c['accent'], hover_color=c['accent_hover'], text_color=c['accent_text']
                  ).grid(row=5, column=0, columnspan=2, pady=(16, 0), sticky='ew')
