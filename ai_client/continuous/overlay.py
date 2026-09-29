"""Two small draggable targets over the live emulator window."""

import tkinter as tk


class FloatingTargets:
    SIZE = (116, 42)

    def __init__(self, root, saved, on_drop):
        self.root = root
        self.on_drop = on_drop
        self.windows = {}
        self.drag = {}
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        defaults = {'start': (screen_w * .65, screen_h * .68),
                    'end': (screen_w * .65, screen_h * .40)}
        for kind, caption, color in (
            ('start', '开始对局 · 拖动', '#237447'),
            ('end', '结束对局 · 拖动', '#ad642b'),
        ):
            target = saved.get(kind) or {}
            center = (target.get('desktop_x'), target.get('desktop_y'))
            if None in center:
                center = defaults[kind]
            win = tk.Toplevel(root)
            win.withdraw()
            win.overrideredirect(True)
            win.attributes('-topmost', True)
            win.attributes('-alpha', .88)
            win.configure(bg=color)
            label = tk.Label(win, text=caption, bg=color, fg='white',
                             font=('Helvetica', 12, 'bold'), cursor='fleur')
            label.pack(fill='both', expand=True)
            win.geometry(f'{self.SIZE[0]}x{self.SIZE[1]}+'
                         f'{round(center[0]-self.SIZE[0]/2)}+{round(center[1]-self.SIZE[1]/2)}')
            for widget in (win, label):
                widget.bind('<ButtonPress-1>', lambda event, k=kind: self._press(k, event))
                widget.bind('<B1-Motion>', lambda event, k=kind: self._move(k, event))
                widget.bind('<ButtonRelease-1>', lambda event, k=kind: self._release(k))
            self.windows[kind] = win

    def _press(self, kind, event):
        win = self.windows[kind]
        self.drag[kind] = (win.winfo_pointerx() - win.winfo_x(),
                           win.winfo_pointery() - win.winfo_y())

    def _move(self, kind, event):
        if kind not in self.drag:
            return
        dx, dy = self.drag[kind]
        x, y = self.windows[kind].winfo_pointerx() - dx, self.windows[kind].winfo_pointery() - dy
        self.windows[kind].geometry(f'+{x}+{y}')

    def _release(self, kind):
        self.drag.pop(kind, None)
        win = self.windows[kind]
        center = (win.winfo_rootx() + win.winfo_width() / 2,
                  win.winfo_rooty() + win.winfo_height() / 2)
        self.on_drop(kind, center)

    def show(self):
        for win in self.windows.values():
            win.deiconify()
            win.lift()

    def hide(self):
        for win in self.windows.values():
            win.withdraw()

    def destroy(self):
        for win in self.windows.values():
            win.destroy()
        self.windows.clear()
