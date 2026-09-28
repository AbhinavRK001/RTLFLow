"""Shared look & feel for RTLFlow GUIs (dark EDA theme, Tk/ttk)."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

BG = "#1e2024"          # window chrome
BG2 = "#26292e"         # panels
BG3 = "#2f3338"         # inputs / hover
LINE = "#3a3f46"
FG = "#d7dae0"
FG_DIM = "#8b929c"
ACCENT = "#4c9dff"
OK = "#3fb950"
WARN = "#e3b341"
ERR = "#f85149"
CANVAS = "#000000"      # Innovus-style black design canvas

MONO = ("DejaVu Sans Mono", 10)

# HiDPI: Tk scales *fonts* (points) with the display DPI, but pixel sizes (row heights,
# paddings, canvas geometry) stay fixed -> text gets clipped. apply() measures the real
# font and sets SCALE / LINE_PX so every GUI can size rows from the font, not from constants.
SCALE = 1.0
LINE_PX = 17
MLINE_PX = 17
UI_FONT = ("TkDefaultFont", 10)


def px(n: float) -> int:
    """Scale a design-time pixel size (designed at 96 dpi / 10 pt) to this display."""
    return max(1, int(round(n * SCALE)))


def pick_font(root: tk.Misc, families: list[str], size: int, weight: str = "normal") -> tuple:
    avail = {f.lower(): f for f in tkfont.families(root)}
    for f in families:
        if f.lower() in avail:
            return (avail[f.lower()], size, weight)
    return ("TkDefaultFont", size, weight)


def fit_geometry(root: tk.Tk, w: int, h: int, minw: int = 900, minh: int = 600) -> None:
    """Window size in 96-dpi pixels, scaled to the display and clamped to the screen."""
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{min(px(w), sw - 40)}x{min(px(h), sh - 80)}")
    root.minsize(min(px(minw), sw - 40), min(px(minh), sh - 80))


_orig_column = ttk.Treeview.column


def _scale_treeview_columns() -> None:
    """Treeview column widths are given in 96-dpi pixels throughout the GUIs; scale them."""
    def column(self, column, option=None, **kw):
        for k in ("width", "minwidth"):
            if k in kw and isinstance(kw[k], (int, float)):
                kw[k] = px(kw[k])
        return _orig_column(self, column, option, **kw)
    ttk.Treeview.column = column


def apply(root: tk.Tk) -> dict:
    global SCALE, LINE_PX, MLINE_PX, UI_FONT
    import os
    env = os.environ.get("RTLFLOW_GUI_SCALE")
    if env:
        try:
            root.tk.call("tk", "scaling", float(env) * 96 / 72)
        except (ValueError, tk.TclError):
            pass
    ui = pick_font(root, ["Inter", "Cantarell", "Noto Sans", "Ubuntu", "DejaVu Sans"], 10)
    ui_b = (ui[0], ui[1], "bold")
    small = (ui[0], 9)
    mono = pick_font(root, ["JetBrains Mono", "Ubuntu Mono", "DejaVu Sans Mono", "Monospace"], 10)
    UI_FONT = ui
    LINE_PX = tkfont.Font(root=root, font=ui).metrics("linespace")
    MLINE_PX = tkfont.Font(root=root, font=mono).metrics("linespace")
    SCALE = max(1.0, max(LINE_PX, MLINE_PX) / 17.0)
    P = px
    _scale_treeview_columns()
    root.configure(bg=BG)
    root.option_add("*Font", ui)
    root.option_add("*Menu.background", BG2)
    root.option_add("*Menu.foreground", FG)
    root.option_add("*Menu.activeBackground", ACCENT)
    root.option_add("*Menu.activeForeground", "#ffffff")
    root.option_add("*Menu.relief", "flat")
    root.option_add("*Menu.borderWidth", 1)
    root.option_add("*Menu.disabledForeground", "#5c636d")
    root.option_add("*Toplevel.background", BG)

    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    st.configure(".", background=BG, foreground=FG, fieldbackground=BG3, bordercolor=LINE,
                 lightcolor=BG2, darkcolor=BG2, troughcolor=BG2, focuscolor=ACCENT,
                 selectbackground=ACCENT, selectforeground="#ffffff", font=ui)
    st.configure("TFrame", background=BG)
    st.configure("Panel.TFrame", background=BG2)
    st.configure("TLabel", background=BG, foreground=FG)
    st.configure("Panel.TLabel", background=BG2, foreground=FG)
    st.configure("Dim.TLabel", background=BG2, foreground=FG_DIM, font=small)
    st.configure("Head.TLabel", background=BG2, foreground=FG, font=ui_b)
    st.configure("Status.TLabel", background="#17191c", foreground=FG_DIM, font=small, padding=(P(8), P(3)))
    st.configure("TButton", background=BG3, foreground=FG, borderwidth=1, padding=(P(10), P(4)), relief="flat")
    st.map("TButton", background=[("active", "#3a4048"), ("disabled", BG2)],
           foreground=[("disabled", "#5c636d")])
    st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
    st.map("Accent.TButton", background=[("active", "#6aafff"), ("disabled", "#2a4a70")])
    st.configure("Tool.TButton", background=BG, foreground=FG, borderwidth=0, padding=(P(8), P(3)), font=ui, width=0)
    st.map("Tool.TButton", background=[("active", BG3), ("pressed", "#3a4048")],
           foreground=[("disabled", "#5c636d")])
    st.configure("Flow.TButton", background=BG2, foreground=FG_DIM, borderwidth=1, padding=(P(10), P(3)), font=small, width=0)
    st.map("Flow.TButton", background=[("active", BG3), ("disabled", BG2)], foreground=[("disabled", "#5c636d")])
    st.configure("FlowDone.TButton", background="#16301f", foreground="#7ee2a0", borderwidth=1, padding=(P(10), P(3)), font=small, width=0)
    st.map("FlowDone.TButton", background=[("active", "#1d3f28")])
    st.configure("FlowNext.TButton", background="#15304f", foreground="#9ecbff", borderwidth=1, padding=(P(10), P(3)), font=ui_b, width=0)
    st.map("FlowNext.TButton", background=[("active", "#1c3d66")])
    st.configure("View.TRadiobutton", background=BG, foreground=FG_DIM, padding=(P(8), P(3)), indicatoron=False)
    st.map("View.TRadiobutton", background=[("selected", BG3), ("active", BG3)],
           foreground=[("selected", "#ffffff")])
    st.configure("TRadiobutton", padding=(P(2), P(2), P(4), P(2)))
    st.configure("TCheckbutton", background=BG2, foreground=FG, indicatorbackground=BG3,
                 indicatorforeground=ACCENT, padding=1)
    st.map("TCheckbutton", background=[("active", BG2)], indicatorbackground=[("selected", BG3)])
    st.configure("TEntry", fieldbackground=BG3, foreground=FG, insertcolor=FG, bordercolor=LINE, padding=P(4))
    st.configure("TSpinbox", fieldbackground=BG3, foreground=FG, arrowcolor=FG, bordercolor=LINE)
    st.configure("TCombobox", fieldbackground=BG3, foreground=FG, arrowcolor=FG, background=BG3)
    st.map("TCombobox", fieldbackground=[("readonly", BG3)], foreground=[("readonly", FG)],
           selectbackground=[("readonly", BG3)], selectforeground=[("readonly", FG)])
    root.option_add("*TCombobox*Listbox.background", BG2)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    st.configure("TNotebook", background=BG2, borderwidth=0, tabmargins=(P(4), P(4), P(4), 0))
    st.configure("TNotebook.Tab", background=BG, foreground=FG_DIM, padding=(P(12), P(5)), borderwidth=0)
    st.map("TNotebook.Tab", background=[("selected", BG2)], foreground=[("selected", "#ffffff")])
    st.configure("Treeview", background=BG2, fieldbackground=BG2, foreground=FG, rowheight=max(LINE_PX, MLINE_PX) + P(8), borderwidth=0)
    st.configure("Treeview.Heading", background=BG, foreground=FG_DIM, relief="flat", font=small, padding=(P(6), P(4)))
    st.map("Treeview", background=[("selected", "#264f78")], foreground=[("selected", "#ffffff")])
    st.map("Treeview.Heading", background=[("active", BG3)])
    st.configure("TSeparator", background=LINE)
    st.configure("TPanedwindow", background=LINE)
    st.configure("Vertical.TScrollbar", background=BG3, troughcolor=BG2, arrowcolor=FG_DIM, bordercolor=BG2)
    st.configure("Horizontal.TScrollbar", background=BG3, troughcolor=BG2, arrowcolor=FG_DIM, bordercolor=BG2)
    st.configure("TLabelframe", background=BG2, bordercolor=LINE)
    st.configure("TLabelframe.Label", background=BG2, foreground=FG_DIM)
    return {"ui": ui, "bold": ui_b, "small": small, "mono": mono,
            "line": LINE_PX, "mline": MLINE_PX, "scale": SCALE}


def text_widget(parent, fonts, **kw) -> tk.Text:
    t = tk.Text(parent, bg="#111316", fg=FG, insertbackground=FG, relief="flat", borderwidth=0,
                highlightthickness=0, font=fonts["mono"], wrap="none", padx=8, pady=6, **kw)
    t.tag_configure("err", foreground=ERR)
    t.tag_configure("warn", foreground=WARN)
    t.tag_configure("ok", foreground=OK)
    t.tag_configure("dim", foreground=FG_DIM)
    t.tag_configure("cmd", foreground="#9ecbff")
    t.tag_configure("gui", foreground="#d2a8ff")
    t.tag_configure("bold", font=(fonts["mono"][0], fonts["mono"][1], "bold"))
    return t


def classify(line: str) -> str | None:
    s = line.lstrip()
    if s.startswith(("**ERROR", "[ERROR", "ERROR", "Error")) or "(VIOLATED)" in s:
        return "err"
    if s.startswith(("**WARN", "[WARNING", "Warning", "WARNING")):
        return "warn"
    if s.startswith("[INFO"):
        return "dim"
    if s.startswith("[GUI]"):
        return "gui"
    if "(MET)" in s:
        return "ok"
    return None
