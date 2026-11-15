"""Secondary application views live in the existing root, never a Toplevel."""
import customtkinter as ctk
from utils.config import Config


class InlinePage(ctk.CTkFrame):
    def __init__(self, host, title, *, scroll=True):
        self.host = host
        self.closed = False
        self.on_close = None
        self._jobs = set()
        super().__init__(host.root, fg_color=Config.COLORS['bg_primary'], corner_radius=0)
        header = ctk.CTkFrame(self, fg_color='transparent')
        header.pack(fill='x', padx=18, pady=12)
        ctk.CTkButton(header, text='← Назад', width=100, command=self.destroy).pack(side='left')
        ctk.CTkLabel(header, text=title, font=ctk.CTkFont(size=17, weight='bold')).pack(side='left', padx=16)
        self.body = (ctk.CTkScrollableFrame(self, fg_color='transparent') if scroll
                     else ctk.CTkFrame(self, fg_color='transparent'))
        self.body._inline_page = self
        self.body.pack(fill='both', expand=True, padx=16, pady=(0, 12))
        # Existing workspace widgets stay alive underneath: selection, sashes,
        # schedule and recording callbacks do not get recreated on navigation.
        self.place(x=0, y=0, relwidth=1, relheight=1)
        self.lift()

    def after(self, ms, func=None, *args):
        if self.closed:
            return None
        if func is None:
            return super().after(ms)
        token = [None]
        def run():
            self._jobs.discard(token[0])
            if not self.closed:
                func(*args)
        token[0] = self.host.root.after(ms, run)
        self._jobs.add(token[0])
        return token[0]

    def after_cancel(self, token):
        if token is not None:
            self._jobs.discard(token)
            self.host.root.after_cancel(token)

    def destroy(self):
        if self.closed:
            return
        self.closed = True
        for token in list(self._jobs):
            self.after_cancel(token)
        if self.on_close:
            self.on_close()
        if self.host.current is self:
            self.host.current = None
        super().destroy()


class InlinePageHost:
    def __init__(self, root):
        self.root = root
        self.current = None

    def open(self, title, *, scroll=True):
        from gui.inline_messages import messagebox
        messagebox.cancel(self.root)
        self.close()
        self.current = InlinePage(self, title, scroll=scroll)
        return self.current

    def close(self):
        if self.current is not None:
            self.current.destroy()
