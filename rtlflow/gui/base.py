"""Base window for RTLFlow tool GUIs (tempus, voltus, modus, pegasus, nclaunch).

Gives every tool the same chrome: menu bar, toolbar, central area (subclass),
bottom notebook with a live session transcript + command line, and a status
bar mirroring the shell's busy/idle state. All actions become shell commands
on the same session (send_cmd), exactly like the innovus GUI.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import socket
import sys
import tkinter as tk
from tkinter import ttk

from . import theme as T
from .innovus_gui import Dialog, ReportWindow, _Tip
from .ipc import Channel

try:
    from .. import VERSION
except Exception:  # pragma: no cover
    VERSION = "?"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class ToolGUI:
    app = "tool"
    title = "RTLFlow"
    prompt = "tool>"
    accent = T.ACCENT

    def __init__(self, root: tk.Tk, chan: Channel | None, project: str) -> None:
        self.root, self.chan, self.project = root, chan, project
        self.q: "queue.Queue[dict]" = queue.Queue()
        self.fonts = T.apply(root)
        self.busy = False
        self.cmd_id = 0
        self.hist: list[str] = []
        self.hist_i = 0
        root.title(f"{self.title} {VERSION} — {project.replace(os.path.expanduser('~'), '~')}")
        root.geometry("1400x880")
        root.minsize(900, 600)
        self.menubar = tk.Menu(root, bg=T.BG2, fg=T.FG, activebackground=T.ACCENT, relief="flat")
        root.configure(menu=self.menubar)
        self.toolbar = ttk.Frame(root, padding=(6, 4))
        self.toolbar.pack(side="top", fill="x")
        self._status()
        self.vpane = ttk.PanedWindow(root, orient="vertical")
        self.vpane.pack(side="top", fill="both", expand=True)
        self.center = ttk.Frame(self.vpane)
        self.vpane.add(self.center, weight=5)
        self.bottom = ttk.Notebook(self.vpane)
        self.vpane.add(self.bottom, weight=1)
        self._transcript()
        self.build()                      # subclass UI
        self.build_menus()
        self._std_menus()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Control-l>", lambda e: self.cmd_entry.focus_set())
        root.after(40, self._poll)
        if chan:
            chan.start_reader(self.q.put, lambda: self.q.put({"t": "quit"}), name=f"{self.app}-ipc")
            chan.send(t="ready")

    # ───────── subclass API ─────────
    def build(self) -> None: ...
    def build_menus(self) -> None: ...
    def on_message(self, m: dict) -> None: ...

    # ───────── helpers for subclasses ─────────
    def menu(self, label: str, items: list) -> tk.Menu:
        m = tk.Menu(self.menubar, tearoff=False)
        for it in items:
            if it is None:
                m.add_separator()
            else:
                m.add_command(label=it[0], command=it[1])
        self.menubar.add_cascade(label=label, menu=m)
        return m

    def tool_button(self, text: str, cmd, tip: str = "", side: str = "left", style: str = "Tool.TButton"):
        b = ttk.Button(self.toolbar, text=text, style=style, command=cmd)
        b.pack(side=side, padx=1)
        if tip:
            _Tip(b, tip)
        return b

    def tool_sep(self) -> None:
        ttk.Separator(self.toolbar, orient="vertical").pack(side="left", fill="y", padx=8, pady=2)

    def text_tab(self, name: str) -> tk.Text:
        f = ttk.Frame(self.bottom, style="Panel.TFrame")
        t = T.text_widget(f, self.fonts, height=8)
        sb = ttk.Scrollbar(f, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        self.bottom.add(f, text=name)
        return t

    def set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for line in _ANSI.sub("", text or "").splitlines():
            tg = T.classify(line)
            widget.insert("end", line + "\n", (tg,) if tg else ())

    def send_cmd(self, line: str, report: bool = False, title: str | None = None) -> None:
        if not self.chan or self.chan.closed:
            self.flash("Not connected to a session", T.ERR)
            return
        self.cmd_id += 1
        self.chan.send(t="cmd", line=line, id=self.cmd_id, report=report, title=title or line)

    def request(self, **msg) -> None:
        if self.chan and not self.chan.closed:
            self.chan.send(**msg)

    def dialog(self, title, fields, on_ok, note=""):
        return Dialog(self.root, self.fonts, title, fields, on_ok, note=note)

    def report_window(self, title, text):
        return ReportWindow(self.root, self.fonts, title, text)

    def flash(self, text: str, color: str = T.FG) -> None:
        self.st_msg.configure(text=text, foreground=color)
        self.root.after(7000, lambda: self.st_msg.configure(text="") if self.st_msg.cget("text") == text else None)

    def set_stage(self, text: str) -> None:
        self.st_stage.configure(text=text)

    # ───────── chrome ─────────
    def _status(self) -> None:
        sb = ttk.Frame(self.root, style="Panel.TFrame")
        sb.pack(side="bottom", fill="x")
        self.st_msg = ttk.Label(sb, text="", style="Status.TLabel")
        self.st_msg.pack(side="left", fill="x", expand=True)
        self.st_busy = ttk.Label(sb, text="Ready", style="Status.TLabel", foreground=T.OK)
        self.st_busy.pack(side="right")
        self.st_stage = ttk.Label(sb, text="", style="Status.TLabel", foreground="#9ecbff")
        self.st_stage.pack(side="right")

    def _transcript(self) -> None:
        f = ttk.Frame(self.bottom, style="Panel.TFrame")
        self.tx = T.text_widget(f, self.fonts, height=8)
        sb = ttk.Scrollbar(f, orient="vertical", command=self.tx.yview)
        self.tx.configure(yscrollcommand=sb.set)
        cl = ttk.Frame(f, style="Panel.TFrame", padding=(8, 4))
        cl.pack(side="bottom", fill="x")
        ttk.Label(cl, text=self.prompt, style="Panel.TLabel", font=self.fonts["mono"], foreground="#7fb6ff").pack(side="left")
        self.cmd_entry = ttk.Entry(cl, font=self.fonts["mono"])
        self.cmd_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.cmd_entry.bind("<Return>", self._entry_run)
        self.cmd_entry.bind("<Up>", lambda e: self._hist(-1))
        self.cmd_entry.bind("<Down>", lambda e: self._hist(1))
        sb.pack(side="right", fill="y")
        self.tx.pack(fill="both", expand=True)
        self.bottom.add(f, text="Transcript")

    def _std_menus(self) -> None:
        self.menu("Window", [("Clear transcript", lambda: self.tx.delete("1.0", "end")),
                             ("Command line", lambda: self.cmd_entry.focus_set()),
                             ("Close GUI (keep session)", self.on_close)])
        self.menu("Help", [("About", lambda: self.report_window("About", f"{self.title} {VERSION}\n\n"
                                                                    "This window shares one session with its terminal.\n"
                                                                    "Every action runs as a normal command there."))])

    def _entry_run(self, e=None) -> None:
        line = self.cmd_entry.get().strip()
        if line:
            self.hist.append(line)
            self.hist_i = len(self.hist)
            self.cmd_entry.delete(0, "end")
            self.send_cmd(line)

    def _hist(self, d: int) -> None:
        if not self.hist:
            return
        self.hist_i = max(0, min(len(self.hist), self.hist_i + d))
        self.cmd_entry.delete(0, "end")
        if self.hist_i < len(self.hist):
            self.cmd_entry.insert(0, self.hist[self.hist_i])

    def log(self, text: str, tag: str | None = None) -> None:
        text = _ANSI.sub("", text)
        at_end = self.tx.yview()[1] > 0.98
        for line in text.split("\n"):
            tg = tag or T.classify(line)
            self.tx.insert("end", line + "\n", (tg,) if tg else ())
        n = int(self.tx.index("end-1c").split(".")[0])
        if n > 6000:
            self.tx.delete("1.0", f"{n - 5000}.0")
        if at_end:
            self.tx.see("end")

    def _poll(self) -> None:
        try:
            for _ in range(300):
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(40, self._poll)

    def _handle(self, m: dict) -> None:
        t = m.get("t")
        if t == "log":
            self.log(m.get("text", ""))
        elif t == "busy":
            self.busy = True
            self.st_busy.configure(text=f"Busy: {m.get('cmd', '')[:48]}", foreground=T.WARN)
            self.root.configure(cursor="watch")
        elif t == "idle":
            self.busy = False
            okk = m.get("ok", True)
            self.st_busy.configure(text="Ready" if okk else f"Failed: {m.get('cmd', '')[:48]}",
                                   foreground=T.OK if okk else T.ERR)
            self.root.configure(cursor="")
        elif t == "result" and not m.get("ok"):
            self.flash(f"'{m.get('line', '')}' failed — see transcript", T.ERR)
        elif t == "report":
            if not self.on_report(m):
                self.report_window(m.get("title", "Report"), m.get("text", ""))
        elif t == "raise":
            self.root.deiconify()
            self.root.lift()
        elif t == "quit":
            self.root.after(10, self.root.destroy)
            return
        try:
            self.on_message(m)
        except Exception as e:
            self.log(f"**WARN: GUI failed to handle '{t}': {e!r}", "warn")

    def on_report(self, m: dict) -> bool:
        """Return True if the subclass displayed the report itself."""
        return False

    def on_close(self) -> None:
        try:
            if self.chan:
                self.chan.send(t="closing")
        finally:
            self.root.destroy()


def run(cls, argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fd", type=int, default=-1)
    ap.add_argument("--project", default=os.getcwd())
    ap.add_argument("--demo", default="")          # test hook: JSON file of messages to replay
    a, _ = ap.parse_known_args(argv)
    chan = Channel(socket.socket(fileno=a.fd)) if a.fd >= 0 else None
    root = tk.Tk(className=f"RTLFlow-{cls.app}")
    gui = cls(root, chan, a.project)
    if a.demo:
        import json
        msgs = json.load(open(a.demo))
        root.after(400, lambda: [gui._handle(m) for m in msgs])
    root.mainloop()
    if chan:
        chan.close()
    return 0


class Chart:
    """Tiny canvas chart helpers (bars, histogram, line) in the RTLFlow dark style."""

    @staticmethod
    def frame(cv: tk.Canvas, pad=(56, 18, 16, 34)):
        cv.delete("all")
        W, H = max(cv.winfo_width(), 50), max(cv.winfo_height(), 50)
        l, t, r, b = pad
        return l, t, W - r, H - b

    @staticmethod
    def axes(cv, x0, y0, x1, y1, ymin, ymax, yfmt="{:g}", nt=4, font=None):
        if yfmt == "{:.0f}" and ymax - ymin < nt:
            nt = max(1, int(ymax - ymin))
        cv.create_line(x0, y1, x1, y1, fill="#4a5058")
        cv.create_line(x0, y0, x0, y1, fill="#4a5058")
        for i in range(nt + 1):
            v = ymin + (ymax - ymin) * i / nt
            y = y1 - (y1 - y0) * i / nt
            cv.create_line(x0, y, x1, y, fill="#20252b")
            cv.create_text(x0 - 6, y, text=yfmt.format(v), anchor="e", fill=T.FG_DIM, font=font)

    @staticmethod
    def empty(cv, text):
        cv.delete("all")
        W, H = max(cv.winfo_width(), 50), max(cv.winfo_height(), 50)
        cv.create_text(W / 2, H / 2, text=text, fill=T.FG_DIM)


def heat(v: float) -> str:
    """0..1 → blue→cyan→green→yellow→red."""
    v = max(0.0, min(1.0, v))
    stops = [(0, (30, 60, 160)), (0.25, (30, 170, 210)), (0.5, (60, 200, 90)), (0.75, (240, 210, 50)), (1, (230, 50, 40))]
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if v <= b:
            k = (v - a) / (b - a) if b > a else 0
            c = tuple(int(ca[i] + (cb[i] - ca[i]) * k) for i in range(3))
            return "#%02x%02x%02x" % c
    return "#e63228"
