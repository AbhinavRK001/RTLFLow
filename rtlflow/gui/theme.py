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


def pick_font(root: tk.Misc, families: list[str], size: int, weight: str = "normal") -> tuple:
    avail = set(tkfont.families(root))
    for f in families:
        if f in avail:
            return (f, size, weight)
    return ("TkDefaultFont", size, weight)


def apply(root: tk.Tk) -> dict:
    ui = pick_font(root, ["Inter", "Cantarell", "Noto Sans", "Ubuntu", "DejaVu Sans"], 10)
    ui_b = (ui[0], ui[1], "bold")
    small = (ui[0], 9)
    mono = pick_font(root, ["JetBrains Mono", "Ubuntu Mono", "DejaVu Sans Mono", "Monospace"], 10)
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
    st.configure("Status.TLabel", background="#17191c", foreground=FG_DIM, font=small, padding=(8, 3))
    st.configure("TButton", background=BG3, foreground=FG, borderwidth=1, padding=(10, 4), relief="flat")
    st.map("TButton", background=[("active", "#3a4048"), ("disabled", BG2)],
           foreground=[("disabled", "#5c636d")])
    st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
    st.map("Accent.TButton", background=[("active", "#6aafff"), ("disabled", "#2a4a70")])
    st.configure("Tool.TButton", background=BG, foreground=FG, borderwidth=0, padding=(8, 3), font=ui, width=0)
    st.map("Tool.TButton", background=[("active", BG3), ("pressed", "#3a4048")],
           foreground=[("disabled", "#5c636d")])
    st.configure("Flow.TButton", background=BG2, foreground=FG_DIM, borderwidth=1, padding=(10, 3), font=small, width=0)
    st.map("Flow.TButton", background=[("active", BG3), ("disabled", BG2)], foreground=[("disabled", "#5c636d")])
    st.configure("FlowDone.TButton", background="#16301f", foreground="#7ee2a0", borderwidth=1, padding=(10, 3), font=small, width=0)
    st.map("FlowDone.TButton", background=[("active", "#1d3f28")])
    st.configure("FlowNext.TButton", background="#15304f", foreground="#9ecbff", borderwidth=1, padding=(10, 3), font=ui_b, width=0)
    st.map("FlowNext.TButton", background=[("active", "#1c3d66")])
    st.configure("View.TRadiobutton", background=BG, foreground=FG_DIM, padding=(8, 3), indicatoron=False)
    st.map("View.TRadiobutton", background=[("selected", BG3), ("active", BG3)],
           foreground=[("selected", "#ffffff")])
    st.configure("TCheckbutton", background=BG2, foreground=FG, indicatorbackground=BG3,
                 indicatorforeground=ACCENT, padding=1)
    st.map("TCheckbutton", background=[("active", BG2)], indicatorbackground=[("selected", BG3)])
    st.configure("TEntry", fieldbackground=BG3, foreground=FG, insertcolor=FG, bordercolor=LINE, padding=4)
    st.configure("TSpinbox", fieldbackground=BG3, foreground=FG, arrowcolor=FG, bordercolor=LINE)
    st.configure("TCombobox", fieldbackground=BG3, foreground=FG, arrowcolor=FG, background=BG3)
    st.map("TCombobox", fieldbackground=[("readonly", BG3)], foreground=[("readonly", FG)],
           selectbackground=[("readonly", BG3)], selectforeground=[("readonly", FG)])
    root.option_add("*TCombobox*Listbox.background", BG2)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    st.configure("TNotebook", background=BG2, borderwidth=0, tabmargins=(4, 4, 4, 0))
    st.configure("TNotebook.Tab", background=BG, foreground=FG_DIM, padding=(12, 5), borderwidth=0)
    st.map("TNotebook.Tab", background=[("selected", BG2)], foreground=[("selected", "#ffffff")])
    st.configure("Treeview", background=BG2, fieldbackground=BG2, foreground=FG, rowheight=22, borderwidth=0)
    st.configure("Treeview.Heading", background=BG, foreground=FG_DIM, relief="flat", font=small, padding=(6, 4))
    st.map("Treeview", background=[("selected", "#264f78")], foreground=[("selected", "#ffffff")])
    st.map("Treeview.Heading", background=[("active", BG3)])
    st.configure("TSeparator", background=LINE)
    st.configure("TPanedwindow", background=LINE)
    st.configure("Vertical.TScrollbar", background=BG3, troughcolor=BG2, arrowcolor=FG_DIM, bordercolor=BG2)
    st.configure("Horizontal.TScrollbar", background=BG3, troughcolor=BG2, arrowcolor=FG_DIM, bordercolor=BG2)
    st.configure("TLabelframe", background=BG2, bordercolor=LINE)
    st.configure("TLabelframe.Label", background=BG2, foreground=FG_DIM)
    return {"ui": ui, "bold": ui_b, "small": small, "mono": mono}


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
