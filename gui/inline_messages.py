"""Acknowledgements and confirmations inside the root, without extra windows."""
import tkinter as tk
import customtkinter as ctk
from utils.config import Config


class InlineMessages:
    def _show(self, title, message, *, parent=None, question=False, **kwargs):
        root = parent.winfo_toplevel() if parent is not None else tk._default_root
        if root is None or not root.winfo_exists():
            return False
        self.cancel(root)
        answer = tk.BooleanVar(master=root, value=False)
        frame = ctk.CTkFrame(root, fg_color=Config.COLORS['bg_secondary'], border_width=2,
                             border_color=Config.COLORS['accent'], corner_radius=12)
        root._inline_prompt = (frame, answer)
        frame.place(relx=.5, rely=.5, anchor='center', relwidth=.85)
        frame.lift()
        ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(size=16, weight='bold')).pack(padx=20, pady=(16, 8))
        label = ctk.CTkLabel(frame, text=str(message), wraplength=700, justify='left')
        label.pack(fill='x', padx=20, pady=8)
        frame.bind('<Configure>', lambda event: label.configure(wraplength=max(100, event.width-50)))
        actions = ctk.CTkFrame(frame, fg_color='transparent')
        actions.pack(pady=(8, 16))
        def finish(value):
            answer.set(value)
        if question:
            ctk.CTkButton(actions, text='Отмена', command=lambda: finish(False)).pack(side='left', padx=6)
        ctk.CTkButton(actions, text='Да' if question else 'Понятно', command=lambda: finish(True)).pack(side='left', padx=6)
        try:
            # Tk keeps dispatching events: recording callbacks and shutdown run.
            root.wait_variable(answer)
            return answer.get()
        finally:
            if getattr(root, '_inline_prompt', None) == (frame, answer):
                root._inline_prompt = None
            if frame.winfo_exists():
                frame.destroy()

    @staticmethod
    def cancel(root):
        prompt = getattr(root, '_inline_prompt', None)
        if prompt:
            root._inline_prompt = None
            frame, answer = prompt
            frame.destroy()
            answer.set(False)

    def askyesno(self, title, message, **kwargs):
        return self._show(title, message, question=True, **kwargs)

    def showerror(self, title, message, **kwargs):
        return self._show(title, message, **kwargs)

    showwarning = showerror
    showinfo = showerror


messagebox = InlineMessages()
