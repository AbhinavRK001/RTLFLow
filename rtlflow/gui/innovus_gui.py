"""RTLFlow Innovus layout GUI.

Launched by the innovus shell (never standalone): it receives the shell's
socket fd, renders the live OpenROAD DEF the shell writes after each
database-changing command, and sends GUI actions back as ordinary innovus
commands — so the terminal and this window drive ONE OpenROAD session.

    python -m rtlflow.gui.innovus_gui --fd N [--title top] [--project dir]
"""

from __future__ import annotations

import argparse
import math
import os
import queue
import re
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import theme as T
from .ipc import Channel
from .lefdef import DefDesign, LefLib, placed_size, read_def, read_lefs, xform

try:
    from .. import VERSION
except Exception:  # pragma: no cover
    VERSION = "?"

# ── colours ─────────────────────────────────────────────────────────────────
METAL = ["#4f8dff", "#ff4b4b", "#3ccf6e", "#ff9f1a", "#e8dd3a", "#b36bff", "#1fc8db", "#ff6ec7", "#9ad04b"]
LI_COLOR = "#9a7ad9"
CUT_COLOR = "#c8c8c8"
KIND = {  # outline, fill, label
    "comb":  ("#5aa9ff", "#0c1f33", "Std cell"),
    "seq":   ("#d38cff", "#26123a", "Sequential"),
    "clock": ("#ffd23f", "#3a2e05", "Clock cell"),
    "buf":   ("#6fe0d0", "#0b2b28", "Buffer/Inverter"),
    "tie":   ("#8be68b", "#102a10", "Tie cell"),
    "tap":   ("#6b7280", "#15171a", "Well tap"),
    "fill":  ("#3f444c", "#101214", "Filler/Decap"),
    "block": ("#ff9f6b", "#2c1a0e", "Block/Macro"),
}
DIE_COLOR = "#d0d0d0"
CORE_COLOR = "#4a6fa5"
ROW_COLOR = "#1b2635"
PIN_COLOR = "#ffcc00"
HILITE = "#ff4fd8"
SELECT = "#ffffff"
FLY = "#ffe066"
POWER_COLORS = {"POWER": "#c83b3b", "GROUND": "#3b6fc8"}

STAGES = [("init", "Init", "init_design"), ("floorplan", "Floorplan", "floorPlan"),
          ("powerplan", "Power", "__power"), ("place", "Place", "placeDesign"),
          ("cts", "CTS", "ccopt_design"), ("route", "Route", "routeDesign"),
          ("signoff", "Timing", "__timing")]
STAGE_RANK = {s: i for i, s in enumerate(["empty", "loaded", "init", "floorplan", "powerplan", "place", "cts",
                                           "route", "filled", "signoff"])}

_SEQ_RE = re.compile(r"__(?:s?e?df|dl|sdl|dlx|dlr|edf|sdf|df[rsbx]|dfxtp|dfrtp|dfstp|dfbbp|srdl|dlrtp|dlxtp|dlxbp)", re.I)


def cell_kind(master: str, cls: str, inst: str) -> str:
    m = master.lower()
    c = cls.upper()
    if c.startswith("BLOCK") or c.startswith("PAD") or c.startswith("RING"):
        return "block"
    if "WELLTAP" in c or "tap" in m:
        return "tap"
    if "SPACER" in c or "fill" in m or "decap" in m:
        return "fill"
    if "clkbuf" in m or "clkinv" in m or inst.lower().startswith(("clkbuf", "cts", "clkload")):
        return "clock"
    if _SEQ_RE.search(m) or re.search(r"(?i)(dff|latch|flop)", m):
        return "seq"
    if "conb" in m or "tie" in m:
        return "tie"
    if re.search(r"__(buf|inv|bufinv)", m) or m.startswith(("buf", "inv")):
        return "buf"
    return "comb"


# ═══════════════════════════════════════════════════════════════════════════
class InnovusGUI:
    def __init__(self, root: tk.Tk, chan: Channel | None, title: str, project: str) -> None:
        self.root = root
        self.chan = chan
        self.q: "queue.Queue[dict]" = queue.Queue()
        self.project = project
        self.title = title
        self.fonts = T.apply(root)
        self.lib = LefLib()
        self.d: DefDesign | None = None
        self.lefs: list[str] = []
        self.def_path = ""
        self.state: dict = {}
        self.stage = "empty"
        self.busy = False
        self.busy_cmd = ""
        self.backend = ""
        # view
        self.s = 10.0          # px per µm
        self.cx = self.cy = 0.0
        self.view_mode = tk.StringVar(value="physical")
        self.vis: dict[str, tk.BooleanVar] = {}
        self.layer_colors: dict[str, str] = {}
        self.hi_insts: list[str] = []
        self.hi_nets: list[str] = []
        self.hi_label = ""
        self.sel: tuple[str, str] | None = None
        self.drc_markers: list[tuple] = []
        self._redraw_job = None
        self._drag = None
        self._ruler = None
        self.ruler_mode = tk.BooleanVar(value=False)
        self.cmd_id = 0
        self.pending: dict[int, str] = {}
        self.inst_nets: dict[str, list[str]] = {}
        self.clock_nets: set[str] = set()
        self.timing_win = None
        self._first_layout = True
        self.hist: list[str] = []
        self.hist_i = 0

        root.title(self._title())
        root.geometry("1480x920")
        root.minsize(980, 640)
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_status()
        self._bind_keys()
        self._update_flow_buttons()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(40, self._poll)
        if chan:
            chan.start_reader(self.q.put, lambda: self.q.put({"t": "__closed"}), name="gui-ipc")
            chan.send(t="ready")
        steps = os.environ.get("RTLFLOW_GUI_AUTOTEST")
        if steps:   # developer self-test: "send:<cmd>|flow:<cmd>|timing|click:x,y|wait:<s>|quit"
            self._autotest(steps.split("|"), 1500)

    # ─────────────────────────────────── chrome ───────────────────────────────────
    def _title(self) -> str:
        top = self.state.get("top") or self.title or "RTLFlow"
        proj = self.project.replace(os.path.expanduser("~"), "~")
        return f"RTLFlow Innovus Implementation System {VERSION} - {top} - {proj}"

    def _menu(self, bar, label, items):
        m = tk.Menu(bar, tearoff=True, title=label)
        for it in items:
            if it is None:
                m.add_separator()
            elif isinstance(it, tuple) and it[0] == "check":
                _, text, var, cmd = it
                m.add_checkbutton(label=text, variable=var, command=cmd)
            elif isinstance(it, tuple) and it[0] == "radio":
                _, text, var, val, cmd = it
                m.add_radiobutton(label=text, variable=var, value=val, command=cmd)
            else:
                text, cmd, *acc = it
                m.add_command(label=text, command=cmd, accelerator=acc[0] if acc else "")
        bar.add_cascade(label=label, menu=m)
        return m

    def _build_menu(self) -> None:
        bar = tk.Menu(self.root, bg=T.BG2, fg=T.FG, activebackground=T.ACCENT, relief="flat", borderwidth=0)
        self._menu(bar, "File", [
            ("Save Design...", self.dlg_save), ("Restore Design...", self.dlg_restore), None,
            ("Export DEF...", lambda: self.ask_file_cmd("defOut", ".def")),
            ("Save Netlist...", lambda: self.ask_file_cmd("saveNetlist", ".v")),
            ("Write SDF...", lambda: self.ask_file_cmd("write_sdf", ".sdf")),
            ("Stream Out GDS...", self.dlg_stream), None,
            ("Save Screenshot...", self.dlg_dump), None,
            ("Close GUI (keep session)", self.on_close, "Ctrl+Q"),
        ])
        self._menu(bar, "Edit", [
            ("Deselect All", self.clear_highlight, "Esc"),
            ("Find Instance...", self.dlg_find),
        ])
        self._menu(bar, "View", [
            ("Fit", self.fit, "F"), ("Zoom In", lambda: self.zoom(1.5), "Z"), ("Zoom Out", lambda: self.zoom(1 / 1.5), "Shift+Z"),
            None,
            ("radio", "Floorplan View", self.view_mode, "floorplan", self.redraw),
            ("radio", "Amoeba View", self.view_mode, "amoeba", self.redraw),
            ("radio", "Physical View", self.view_mode, "physical", self.redraw),
            None,
            ("Redraw from database", lambda: self.send_cmd("gui_refresh"), "Ctrl+R"),
            ("Toggle Transcript", self.toggle_transcript, "F12"),
        ])
        self._menu(bar, "Floorplan", [
            ("Specify Floorplan...", self.dlg_floorplan),
            ("Quick Floorplan (0.5 util)", lambda: self.send_cmd("floorPlan -r 1.0 0.5 5 5 5 5")), None,
            ("Assign IO Pins", lambda: self.send_cmd("assignIoPins")),
            ("Add Well Taps", lambda: self.send_cmd("addWellTap")),
        ])
        self._menu(bar, "Power", [
            ("Global Net Connect", lambda: self.send_cmd("globalNetConnect")),
            ("Add Stripe...", self.dlg_stripe),
            ("Special Route (sroute)", lambda: self.send_cmd("sroute")),
        ])
        self._menu(bar, "Place", [
            ("Place Standard Cells...", self.dlg_place),
            ("Refine Placement", lambda: self.send_cmd("refinePlace")),
            ("Check Placement", lambda: self.send_cmd("checkPlace", report=True)), None,
            ("Tie Hi/Lo Cells", lambda: self.send_cmd("addTieHiLo")),
            ("Add Filler", lambda: self.send_cmd("addFiller")),
        ])
        self._menu(bar, "Clock", [
            ("CCOpt Clock Tree Synthesis", lambda: self.send_cmd("ccopt_design")),
            ("Report Clock Trees", lambda: self.send_cmd("report_ccopt_clock_trees", report=True)), None,
            ("check", "Show Clock Tree Flylines", self._v("clocktree", False), self.redraw),
        ])
        self._menu(bar, "Route", [
            ("NanoRoute...", self.dlg_route),
            ("Early Global Route", lambda: self.send_cmd("earlyGlobalRoute")),
        ])
        self._menu(bar, "Timing", [
            ("Timing Debug...", self.open_timing_debug, "Ctrl+T"), None,
            ("Report Timing...", self.dlg_report_timing),
            ("timeDesign (setup)", lambda: self.send_cmd("timeDesign", report=True)),
            ("timeDesign (hold)", lambda: self.send_cmd("timeDesign -hold", report=True)),
            ("Report QoR", lambda: self.send_cmd("report_qor", report=True)),
            ("Report Constraints", lambda: self.send_cmd("report_constraints -all_violators", report=True)),
            ("Report Power", lambda: self.send_cmd("report_power", report=True)), None,
            ("Extract RC", lambda: self.send_cmd("extractRC")),
            ("Optimize Design (setup)", lambda: self.send_cmd("optDesign -setup")),
            ("Optimize Design (hold)", lambda: self.send_cmd("optDesign -hold")),
        ])
        self._menu(bar, "Verify", [
            ("Verify DRC", lambda: self.send_cmd("verify_drc", report=True)),
            ("Verify Connectivity", lambda: self.send_cmd("verify_connectivity", report=True)),
            ("Verify Process Antenna", lambda: self.send_cmd("verify_antenna", report=True)),
            ("Check Design", lambda: self.send_cmd("checkDesign", report=True)),
        ])
        self._menu(bar, "Tools", [
            ("Design Status", lambda: self.send_cmd("status", report=True)),
            ("Technology Profile", lambda: self.send_cmd("tech_profile", report=True)),
            ("Command Line", lambda: self.cmd_entry.focus_set(), "Ctrl+L"),
            ("check", "Ruler", self.ruler_mode, lambda: None),
        ])
        self._menu(bar, "Help", [
            ("Keyboard & Mouse", self.show_keys), ("About RTLFlow Innovus", self.show_about),
        ])
        self.root.configure(menu=bar)

    def _v(self, key: str, default: bool = True) -> tk.BooleanVar:
        if key not in self.vis:
            self.vis[key] = tk.BooleanVar(value=default)
        return self.vis[key]

    def _build_toolbar(self) -> None:
        tb = ttk.Frame(self.root, padding=(6, 4))
        tb.pack(side="top", fill="x")

        def tbtn(text, cmd, tip):
            b = ttk.Button(tb, text=text, style="Tool.TButton", command=cmd)
            b.pack(side="left", padx=1)
            _Tip(b, tip)
            return b
        tbtn("Fit", self.fit, "Fit design (F)")
        tbtn("Zoom +", lambda: self.zoom(1.5), "Zoom in (Z)")
        tbtn("Zoom -", lambda: self.zoom(1 / 1.5), "Zoom out (Shift+Z)")
        tbtn("Refresh", lambda: self.send_cmd("gui_refresh"), "Re-read database (Ctrl+R)")
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=8, pady=2)
        for val, text in (("floorplan", "Floorplan"), ("amoeba", "Amoeba"), ("physical", "Physical")):
            rb = ttk.Radiobutton(tb, text=text, value=val, variable=self.view_mode, style="View.TRadiobutton",
                                 command=self.redraw)
            rb.pack(side="left", padx=0)
            _Tip(rb, f"{text} view")
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=8, pady=2)
        tbtn("Timing Debug", self.open_timing_debug, "Timing Debug (Ctrl+T)")
        tbtn("Ruler", lambda: self.ruler_mode.set(not self.ruler_mode.get()), "Ruler (K)")
        # flow chips (right side)
        self.flow_frame = ttk.Frame(tb)
        self.flow_frame.pack(side="right")
        ttk.Label(self.flow_frame, text="Flow", foreground=T.FG_DIM).pack(side="left", padx=(0, 6))
        self.flow_btns = {}
        for key, label, cmd in STAGES:
            b = ttk.Button(self.flow_frame, text=label, style="Flow.TButton",
                           command=lambda c=cmd: self.flow_action(c))
            b.pack(side="left", padx=1)
            self.flow_btns[key] = b
            _Tip(b, {"__power": "globalNetConnect + sroute", "__timing": "timeDesign + Timing Debug"}.get(cmd, cmd))

    def _build_body(self) -> None:
        self.vpane = ttk.PanedWindow(self.root, orient="vertical")
        self.vpane.pack(side="top", fill="both", expand=True)
        self.hpane = ttk.PanedWindow(self.vpane, orient="horizontal")
        self.vpane.add(self.hpane, weight=5)
        # canvas
        cf = tk.Frame(self.hpane, bg=T.CANVAS, highlightthickness=0)
        self.cv = tk.Canvas(cf, bg=T.CANVAS, highlightthickness=0, cursor="crosshair")
        self.cv.pack(fill="both", expand=True)
        self.hpane.add(cf, weight=5)
        # right panel
        rp = ttk.Frame(self.hpane, style="Panel.TFrame", width=330)
        self.hpane.add(rp, weight=0)
        self.nb = ttk.Notebook(rp)
        self.nb.pack(fill="both", expand=True)
        self._build_layers_tab()
        self._build_design_tab()
        self._build_attr_tab()
        # world view
        wv = ttk.Frame(rp, style="Panel.TFrame", padding=(8, 4, 8, 8))
        wv.pack(fill="x", side="bottom")
        ttk.Label(wv, text="WORLD VIEW", style="Dim.TLabel").pack(anchor="w")
        self.wv = tk.Canvas(wv, height=150, bg="#0a0b0d", highlightthickness=1, highlightbackground=T.LINE)
        self.wv.pack(fill="x", pady=(3, 0))
        self.wv.bind("<Button-1>", self._wv_click)
        self.wv.bind("<B1-Motion>", self._wv_click)
        # transcript
        self.tframe = ttk.Frame(self.vpane, style="Panel.TFrame")
        self.vpane.add(self.tframe, weight=1)
        hdr = ttk.Frame(self.tframe, style="Panel.TFrame", padding=(8, 3))
        hdr.pack(fill="x")
        ttk.Label(hdr, text="SESSION TRANSCRIPT  ·  same OpenROAD session as the innovus> terminal",
                  style="Dim.TLabel").pack(side="left")
        ttk.Button(hdr, text="Clear", style="Tool.TButton", command=lambda: self.tx.delete("1.0", "end")).pack(side="right")
        body = ttk.Frame(self.tframe, style="Panel.TFrame")
        body.pack(fill="both", expand=True)
        self.tx = T.text_widget(body, self.fonts, height=8)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tx.yview)
        self.tx.configure(yscrollcommand=sb.set, state="normal")
        sb.pack(side="right", fill="y")
        self.tx.pack(side="left", fill="both", expand=True)
        cl = ttk.Frame(self.tframe, style="Panel.TFrame", padding=(8, 4))
        cl.pack(fill="x")
        ttk.Label(cl, text="innovus>", style="Panel.TLabel", font=self.fonts["mono"], foreground="#7fb6ff").pack(side="left")
        self.cmd_entry = ttk.Entry(cl, font=self.fonts["mono"])
        self.cmd_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.cmd_entry.bind("<Return>", self._entry_run)
        self.cmd_entry.bind("<Up>", lambda e: self._entry_hist(-1))
        self.cmd_entry.bind("<Down>", lambda e: self._entry_hist(1))
        # canvas events
        c = self.cv
        c.bind("<Configure>", lambda e: self.schedule_redraw(10))
        c.bind("<Motion>", self._motion)
        c.bind("<Button-1>", self._b1_down)
        c.bind("<B1-Motion>", self._b1_move)
        c.bind("<ButtonRelease-1>", self._b1_up)
        c.bind("<Button-2>", self._pan_start)
        c.bind("<B2-Motion>", self._pan_move)
        c.bind("<ButtonRelease-2>", self._pan_end)
        c.bind("<Button-3>", self._zbox_start)
        c.bind("<B3-Motion>", self._zbox_move)
        c.bind("<ButtonRelease-3>", self._zbox_end)
        c.bind("<Button-4>", lambda e: self.zoom(1.25, e.x, e.y))
        c.bind("<Button-5>", lambda e: self.zoom(0.8, e.x, e.y))
        c.bind("<MouseWheel>", lambda e: self.zoom(1.25 if e.delta > 0 else 0.8, e.x, e.y))
        c.bind("<Double-Button-1>", lambda e: self.zoom_to_selection())

    def _section(self, parent, text):
        ttk.Label(parent, text=text, style="Dim.TLabel").pack(anchor="w", pady=(10, 2), padx=10)

    def _build_layers_tab(self) -> None:
        outer = ttk.Frame(self.nb, style="Panel.TFrame")
        self.nb.add(outer, text="Layers")
        cv = tk.Canvas(outer, bg=T.BG2, highlightthickness=0, width=300)
        sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
        self.layers_frame = ttk.Frame(cv, style="Panel.TFrame")
        self.layers_frame.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=self.layers_frame, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        self._layers_canvas = cv
        self._rebuild_layer_panel()

    def _rebuild_layer_panel(self) -> None:
        f = self.layers_frame
        for w in f.winfo_children():
            w.destroy()
        self._section(f, "OBJECTS")
        objs = [("die", "Die / Core boundary", DIE_COLOR), ("rows", "Rows", "#33507a"),
                ("tracks", "Routing tracks", "#2c3440"), ("cells", "Std cells", KIND["comb"][0]),
                ("cellnames", "Instance names", "#9aa4b2"), ("cellpins", "Cell pin shapes", "#8ab4ff"),
                ("iopins", "IO pins", PIN_COLOR), ("pinnames", "IO pin names", "#c9b458"),
                ("power", "Power / ground (special nets)", POWER_COLORS["POWER"]),
                ("nets", "Signal routing", METAL[1]), ("clock", "Clock nets", FLY),
                ("vias", "Vias", CUT_COLOR), ("drc", "DRC markers", T.ERR),
                ("clocktree", "Clock tree flylines", FLY), ("unplaced", "Unplaced instances", "#a0522d")]
        defaults = {"tracks": False, "cellpins": True, "clocktree": False}
        for key, text, col in objs:
            self._layer_row(f, key, text, col, defaults.get(key, True))
        if self.lib.layer_order:
            self._section(f, "ROUTING LAYERS")
            for name in self.lib.layer_order:
                L = self.lib.layers[name]
                if L.type == "ROUTING":
                    col = self.layer_color(name)
                    self._layer_row(f, "L:" + name, f"{name}   {L.direction[:1] if L.direction else ''}"
                                    f"{'  w ' + format(L.width, 'g') if L.width else ''}", col, True)
            self._section(f, "CELL CLASSES (Amoeba)")
            for k, (o, fill, label) in KIND.items():
                self._layer_row(f, "K:" + k, label, o, True)

    def _layer_row(self, parent, key, text, color, default):
        var = self._v(key, default)
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill="x", padx=10, pady=1)
        sw = tk.Canvas(row, width=14, height=14, bg=T.BG2, highlightthickness=0)
        sw.create_rectangle(1, 1, 13, 13, fill=color, outline="#000")
        sw.pack(side="left", padx=(0, 6))
        ttk.Checkbutton(row, text=text, variable=var, command=self.redraw).pack(side="left", anchor="w")

    def _build_design_tab(self) -> None:
        f = ttk.Frame(self.nb, style="Panel.TFrame", padding=(10, 8))
        self.nb.add(f, text="Design")
        self.design_labels = {}
        for key, label in [("top", "Design"), ("stage", "Stage"), ("backend", "Backend"), ("profile", "Tech profile"),
                           ("die", "Die"), ("core", "Core"), ("util", "Utilisation"), ("insts", "Instances"),
                           ("seq", "Sequential"), ("nets", "Nets"), ("terms", "IO pins"), ("clocks", "Clocks"),
                           ("wns", "WNS (setup)"), ("tns", "TNS"), ("whs", "WHS (hold)"), ("drc", "Route DRC"),
                           ("parasitics", "Parasitics")]:
            r = ttk.Frame(f, style="Panel.TFrame")
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=label, style="Dim.TLabel", width=13).pack(side="left")
            v = ttk.Label(r, text="-", style="Panel.TLabel")
            v.pack(side="left", fill="x")
            self.design_labels[key] = v
        ttk.Separator(f).pack(fill="x", pady=8)
        self.files_lbl = ttk.Label(f, text="", style="Dim.TLabel", justify="left", wraplength=290)
        self.files_lbl.pack(anchor="w")

    def _build_attr_tab(self) -> None:
        f = ttk.Frame(self.nb, style="Panel.TFrame", padding=(10, 8))
        self.nb.add(f, text="Attributes")
        self.attr_title = ttk.Label(f, text="Nothing selected", style="Head.TLabel")
        self.attr_title.pack(anchor="w")
        ttk.Label(f, text="Click an instance, IO pin or wire.", style="Dim.TLabel").pack(anchor="w", pady=(0, 6))
        self.attr_tree = ttk.Treeview(f, columns=("v",), show="tree", height=14)
        self.attr_tree.column("#0", width=110, stretch=False)
        self.attr_tree.column("v", width=180)
        self.attr_tree.pack(fill="both", expand=True)
        self.attr_tree.bind("<Double-1>", self._attr_dbl)

    def _build_status(self) -> None:
        sb = ttk.Frame(self.root, style="Panel.TFrame")
        sb.pack(side="bottom", fill="x")
        self.st_xy = ttk.Label(sb, text="x -  y -", style="Status.TLabel", width=28)
        self.st_xy.pack(side="left")
        self.st_hover = ttk.Label(sb, text="", style="Status.TLabel")
        self.st_hover.pack(side="left", fill="x", expand=True)
        self.st_zoom = ttk.Label(sb, text="", style="Status.TLabel")
        self.st_zoom.pack(side="right")
        self.st_busy = ttk.Label(sb, text="Ready", style="Status.TLabel", foreground=T.OK)
        self.st_busy.pack(side="right")
        self.st_stage = ttk.Label(sb, text="no design", style="Status.TLabel", foreground="#9ecbff")
        self.st_stage.pack(side="right")

    def _bind_keys(self) -> None:
        r = self.root

        def canvas_focus(fn):
            def h(e):
                if isinstance(r.focus_get(), (tk.Entry, ttk.Entry, tk.Text)):
                    return
                fn()
            return h
        r.bind("<KeyPress-f>", canvas_focus(self.fit))
        r.bind("<KeyPress-z>", canvas_focus(lambda: self.zoom(1.5)))
        r.bind("<KeyPress-Z>", canvas_focus(lambda: self.zoom(1 / 1.5)))
        r.bind("<KeyPress-k>", canvas_focus(lambda: self.ruler_mode.set(not self.ruler_mode.get())))
        r.bind("<Escape>", lambda e: self.clear_highlight())
        r.bind("<Control-r>", lambda e: self.send_cmd("gui_refresh"))
        r.bind("<Control-t>", lambda e: self.open_timing_debug())
        r.bind("<Control-l>", lambda e: self.cmd_entry.focus_set())
        r.bind("<Control-q>", lambda e: self.on_close())
        r.bind("<F12>", lambda e: self.toggle_transcript())
        for key, dx, dy in (("Left", -1, 0), ("Right", 1, 0), ("Up", 0, 1), ("Down", 0, -1)):
            r.bind(f"<KeyPress-{key}>", canvas_focus(lambda dx=dx, dy=dy: self.pan_step(dx, dy)))

    def _autotest(self, steps: list[str], delay: int) -> None:
        if not steps:
            return
        step, rest = steps[0], steps[1:]
        nxt = 300
        k, _, v = step.partition(":")
        if k == "send":
            self.send_cmd(v)
        elif k == "flow":
            self.flow_action(v)
        elif k == "timing":
            self.open_timing_debug()
        elif k == "view":
            self.view_mode.set(v); self.redraw()
        elif k == "click":
            x, y = (float(a) for a in v.split(","))
            hit = self._hit(x, y)
            if hit:
                self.select(hit); self.redraw()
        elif k == "wait":
            nxt = int(float(v) * 1000)
        elif k == "quit":
            self.on_close()
            return
        self.root.after(delay if delay else nxt, lambda: self._autotest(rest, 0) if rest else None)

    # ─────────────────────────────────── IPC ───────────────────────────────────
    def _poll(self) -> None:
        n = 0
        latest_layout = None
        try:
            while n < 400:
                msg = self.q.get_nowait()
                n += 1
                if msg.get("t") == "layout":
                    latest_layout = msg        # coalesce bursts of reloads
                    continue
                self._handle(msg)
        except queue.Empty:
            pass
        if latest_layout:
            self._handle(latest_layout)
        self.root.after(30 if n else 60, self._poll)

    def _handle(self, m: dict) -> None:
        t = m.get("t")
        if t == "hello":
            self.backend = m.get("backend", "")
            self.log(f"Connected to innovus session · {self.backend} · RTLFlow {m.get('version', '')}", "dim")
        elif t == "state":
            self.state = m.get("state", {})
            self.stage = self.state.get("stage", "empty")
            self._update_design_tab()
            self._update_flow_buttons()
            self.root.title(self._title())
            lefs = self.state.get("lefs") or []
            if lefs and lefs != self.lefs:
                self._load_lefs(lefs)
        elif t == "layout":
            self._load_layout(m)
        elif t == "busy":
            self.busy, self.busy_cmd = True, m.get("cmd", "")
            self.st_busy.configure(text=f"Busy: {self.busy_cmd[:40]}", foreground=T.WARN)
            self._update_flow_buttons()
            self.root.configure(cursor="watch")
        elif t == "idle":
            self.busy = False
            okk = m.get("ok", True)
            self.st_busy.configure(text="Ready" if okk else f"Failed: {m.get('cmd', '')[:40]}",
                                   foreground=T.OK if okk else T.ERR)
            self.root.configure(cursor="")
            self._update_flow_buttons()
        elif t == "log":
            self.log(m.get("text", ""))
        elif t == "result":
            if not m.get("ok"):
                self.flash(f"'{m.get('line', '')}' failed - see transcript / terminal", T.ERR)
        elif t == "report":
            ReportWindow(self.root, self.fonts, m.get("title", "Report"), m.get("text", ""))
        elif t == "highlight":
            self.hi_insts = m.get("insts", [])
            self.hi_nets = m.get("nets", [])
            lab = m.get("label", "")
            if m.get("slack") is not None:
                lab += f"  ·  slack {m['slack']:+.3f} ns"
            self.hi_label = lab
            self.redraw()
            if self.hi_insts:
                self.flash(f"Highlighted: {lab}", HILITE)
        elif t == "clear_highlight":
            self.clear_highlight()
        elif t == "select":
            names = m.get("insts", [])
            if self.d:
                pats = [re.compile("^" + re.escape(p).replace(r"\*", ".*") + "$") for p in names]
                hits = [c for c in self.d.comps if any(p.match(c) for p in pats)]
                if hits:
                    self.select(("inst", hits[0]))
                    self.hi_insts = hits
                    self.redraw()
                    if m.get("zoom"):
                        self.zoom_to_selection()
                else:
                    self.flash(f"No instance matches {' '.join(names)}", T.WARN)
        elif t == "fit":
            self.fit()
        elif t == "zoom":
            x1, y1, x2, y2 = m["box"]
            self.zoom_box(x1, y1, x2, y2)
        elif t == "dump":
            self.dump(m.get("file"))
        elif t == "timing_paths":
            if self.timing_win and self.timing_win.alive():
                self.timing_win.load(m)
        elif t == "drc":
            self._load_drc(m.get("file"))
        elif t == "clear":
            self.d = None
            self.state = {}
            self.stage = "empty"
            self.redraw()
            self._update_design_tab()
            self._update_flow_buttons()
        elif t == "raise":
            self.root.deiconify(); self.root.lift()
            try:
                self.root.focus_force()
            except tk.TclError:
                pass
        elif t in ("quit", "__closed"):
            self.root.after(10, self.root.destroy)

    def send_cmd(self, line: str, report: bool = False, title: str | None = None) -> None:
        if not self.chan or self.chan.closed:
            self.flash("Not connected to an innovus session", T.ERR)
            return
        self.cmd_id += 1
        self.pending[self.cmd_id] = line
        self.chan.send(t="cmd", line=line, id=self.cmd_id, report=report, title=title or line)

    def flow_action(self, cmd: str) -> None:
        if cmd == "__power":
            self.send_cmd("globalNetConnect")
            self.send_cmd("sroute")
        elif cmd == "__timing":
            self.send_cmd("timeDesign", report=True)
            self.open_timing_debug()
        elif cmd == "floorPlan":
            self.dlg_floorplan()
        else:
            self.send_cmd(cmd)

    # ───────────────────────────── transcript ─────────────────────────────
    _ANSI = re.compile(r"\x1b\[[0-9;]*m")

    def log(self, text: str, tag: str | None = None) -> None:
        text = self._ANSI.sub("", text)
        at_end = self.tx.yview()[1] > 0.98
        for line in text.split("\n"):
            tg = tag or T.classify(line)
            self.tx.insert("end", line + "\n", (tg,) if tg else ())
        # cap transcript
        lines = int(self.tx.index("end-1c").split(".")[0])
        if lines > 6000:
            self.tx.delete("1.0", f"{lines - 5000}.0")
        if at_end:
            self.tx.see("end")

    def toggle_transcript(self) -> None:
        panes = self.vpane.panes()
        if str(self.tframe) in [str(p) for p in panes]:
            self.vpane.forget(self.tframe)
        else:
            self.vpane.add(self.tframe, weight=1)

    def _entry_run(self, e=None) -> None:
        line = self.cmd_entry.get().strip()
        if not line:
            return
        self.hist.append(line)
        self.hist_i = len(self.hist)
        self.cmd_entry.delete(0, "end")
        self.send_cmd(line)

    def _entry_hist(self, d: int) -> None:
        if not self.hist:
            return
        self.hist_i = max(0, min(len(self.hist), self.hist_i + d))
        self.cmd_entry.delete(0, "end")
        if self.hist_i < len(self.hist):
            self.cmd_entry.insert(0, self.hist[self.hist_i])

    def flash(self, text: str, color: str) -> None:
        self.st_hover.configure(text=text, foreground=color)
        self.root.after(6000, lambda: self.st_hover.configure(text="", foreground=T.FG_DIM)
                        if self.st_hover.cget("text") == text else None)

    # ───────────────────────────── design data ─────────────────────────────
    def _load_lefs(self, lefs: list[str]) -> None:
        t0 = time.time()
        try:
            self.lib = read_lefs([l for l in lefs if os.path.exists(l)])
        except Exception as e:
            self.log(f"**WARN: LEF parse failed: {e}", "warn")
            return
        self.lefs = list(lefs)
        self.layer_colors.clear()
        self._rebuild_layer_panel()
        self.log(f"GUI: loaded {len(self.lib.macros)} macros, {len(self.lib.layers)} layers "
                 f"({time.time() - t0:.1f}s)", "dim")

    def _load_layout(self, m: dict) -> None:
        path = m.get("def")
        if m.get("lefs") and m["lefs"] != self.lefs:
            self._load_lefs(m["lefs"])
        try:
            d = read_def(path, self.lib)
        except Exception as e:
            self.log(f"**WARN: could not parse {path}: {e}", "warn")
            return
        self.d = d
        self.def_path = path
        self.stage = m.get("stage", self.stage)
        self.inst_nets = {}
        self.clock_nets = set()
        for net in d.nets.values():
            if net.use == "CLOCK":
                self.clock_nets.add(net.name)
            for comp, _pin in net.conns:
                self.inst_nets.setdefault(comp, []).append(net.name)
        # nets driven by clock cells are clock nets too (post-CTS)
        for net in d.nets.values():
            for comp, _ in net.conns:
                c = d.comps.get(comp)
                if c and "clk" in c.master.lower() and ("buf" in c.master.lower() or "inv" in c.master.lower()):
                    self.clock_nets.add(net.name)
        if self._first_layout or not d.die:
            self._first_layout = not bool(d.die)
            self.fit(redraw=False)
        self.redraw()
        self._update_design_tab()
        nplaced = sum(1 for c in d.comps.values() if c.status != "UNPLACED")
        nwires = sum(len(n.wires) for n in d.nets.values())
        self.log(f"GUI: {os.path.basename(path)} · {len(d.comps)} instances ({nplaced} placed) · "
                 f"{len(d.pins)} IO pins · {len(d.rows)} rows · {nwires} route segments", "dim")

    def _load_drc(self, path: str | None) -> None:
        self.drc_markers = []
        if not path or not os.path.exists(path):
            return
        cur_type = ""
        for line in open(path, errors="replace"):
            s = line.strip()
            if s.lower().startswith("violation type"):
                cur_type = s.split(":", 1)[-1].strip()
            m = re.search(r"bbox\s*=\s*\(\s*([-\d.]+),\s*([-\d.]+)\s*\)\s*-\s*\(\s*([-\d.]+),\s*([-\d.]+)\s*\)", s)
            if m:
                x1, y1, x2, y2 = map(float, m.groups())
                self.drc_markers.append((x1, y1, x2, y2, cur_type))
        self.redraw()
        self.flash(f"{len(self.drc_markers)} DRC markers loaded", T.ERR if self.drc_markers else T.OK)

    def layer_color(self, name: str) -> str:
        if name in self.layer_colors:
            return self.layer_colors[name]
        L = self.lib.layers.get(name)
        if name.lower().startswith("li"):
            col = LI_COLOR
        elif L is not None and L.type == "ROUTING":
            ri = L.routing_index
            has_li = any(n.lower().startswith("li") for n in self.lib.layer_order if self.lib.layers[n].type == "ROUTING")
            idx = ri - 1 if has_li else ri
            col = METAL[idx % len(METAL)] if idx >= 0 else LI_COLOR
        elif L is not None and L.type == "CUT":
            col = CUT_COLOR
        else:
            m = re.search(r"(\d+)", name)
            col = METAL[(int(m.group(1)) - 1) % len(METAL)] if m else "#aaaaaa"
        self.layer_colors[name] = col
        return col

    # ───────────────────────────── view transform ─────────────────────────────
    def W(self) -> int:
        return max(10, self.cv.winfo_width())

    def H(self) -> int:
        return max(10, self.cv.winfo_height())

    def to_px(self, x: float, y: float) -> tuple[float, float]:
        return self.W() / 2 + (x - self.cx) * self.s, self.H() / 2 - (y - self.cy) * self.s

    def to_world(self, X: float, Y: float) -> tuple[float, float]:
        return self.cx + (X - self.W() / 2) / self.s, self.cy - (Y - self.H() / 2) / self.s

    def world_bbox(self) -> tuple[float, float, float, float]:
        if self.d and self.d.die and self.d.die[2] > self.d.die[0]:
            x1, y1, x2, y2 = self.d.die
            # include unplaced parking area on the left
            if any(c.status == "UNPLACED" for c in self.d.comps.values()):
                x1 -= (x2 - x1) * 0.6 + 5
            return x1, y1, x2, y2
        if self.d and self.d.comps:
            ux = self._unplaced_positions()
            if ux:
                xs = [p[0] for p in ux.values()] + [p[0] + p[2] for p in ux.values()]
                ys = [p[1] for p in ux.values()] + [p[1] + p[3] for p in ux.values()]
                return min(xs), min(ys), max(xs), max(ys)
        return 0, 0, 100, 100

    def fit(self, redraw: bool = True) -> None:
        x1, y1, x2, y2 = self.world_bbox()
        w, h = max(x2 - x1, 1e-3), max(y2 - y1, 1e-3)
        self.cx, self.cy = (x1 + x2) / 2, (y1 + y2) / 2
        self.s = 0.88 * min(self.W() / w, self.H() / h)
        if redraw:
            self.redraw()

    def zoom(self, f: float, X: float | None = None, Y: float | None = None) -> None:
        if X is None:
            X, Y = self.W() / 2, self.H() / 2
        wx, wy = self.to_world(X, Y)
        self.s = min(max(self.s * f, 0.02), 5000)
        # keep point under cursor fixed
        self.cx = wx - (X - self.W() / 2) / self.s
        self.cy = wy + (Y - self.H() / 2) / self.s
        self.schedule_redraw(15)

    def zoom_box(self, x1, y1, x2, y2) -> None:
        w, h = max(abs(x2 - x1), 1e-3), max(abs(y2 - y1), 1e-3)
        self.cx, self.cy = (x1 + x2) / 2, (y1 + y2) / 2
        self.s = min(self.W() / w, self.H() / h) * 0.95
        self.redraw()

    def pan_step(self, dx: int, dy: int) -> None:
        self.cx += dx * self.W() * 0.15 / self.s
        self.cy += dy * self.H() * 0.15 / self.s
        self.redraw()

    def schedule_redraw(self, ms: int = 30) -> None:
        if self._redraw_job:
            self.root.after_cancel(self._redraw_job)
        self._redraw_job = self.root.after(ms, self.redraw)

    # ───────────────────────────── rendering ─────────────────────────────
    def on(self, key: str) -> bool:
        v = self.vis.get(key)
        return True if v is None else bool(v.get())

    def _unplaced_positions(self) -> dict[str, tuple]:
        """Park unplaced instances in a grid left of the die (like Innovus does)."""
        d = self.d
        if not d:
            return {}
        un = [c for c in d.comps.values() if c.status == "UNPLACED"]
        if not un:
            return {}
        if d.die and d.die[2] > d.die[0]:
            x0 = d.die[0] - (d.die[2] - d.die[0]) * 0.6 - 3
            top = d.die[3]
            width = (d.die[2] - d.die[0]) * 0.55
        else:
            x0, top = 0.0, 0.0
            width = max(20.0, math.sqrt(sum(self._msize(c.master)[0] * self._msize(c.master)[1] for c in un)) * 1.6)
        pos = {}
        x, y, row_h = x0, top, 0.0
        for c in sorted(un, key=lambda c: c.master):
            w, h = self._msize(c.master)
            if x + w > x0 + width and x > x0:
                x = x0
                y -= row_h + 0.6
                row_h = 0
            pos[c.name] = (x, y - h, w, h)
            x += w + 0.4
            row_h = max(row_h, h)
        return pos

    def _msize(self, master: str) -> tuple[float, float]:
        M = self.lib.macros.get(master)
        if M and M.w > 0:
            return M.w, M.h
        return 1.38, 2.72

    def redraw(self) -> None:
        self._redraw_job = None
        c = self.cv
        c.delete("all")
        mode = self.view_mode.get()
        d = self.d
        W, H = self.W(), self.H()
        if not d:
            self._empty_canvas()
            self._draw_world_view()
            return
        s = self.s
        vx1, vy1 = self.to_world(0, H)
        vx2, vy2 = self.to_world(W, 0)

        def vis_box(x1, y1, x2, y2):
            return not (x2 < vx1 or x1 > vx2 or y2 < vy1 or y1 > vy2)

        def rect(x1, y1, x2, y2, **kw):
            X1, Y1 = self.to_px(x1, y2)
            X2, Y2 = self.to_px(x2, y1)
            if X2 - X1 < 1:
                X2 = X1 + 1
            if Y2 - Y1 < 1:
                Y2 = Y1 + 1
            return c.create_rectangle(X1, Y1, X2, Y2, **kw)

        # rows
        if self.on("rows") and d.rows and s > 0.8:
            for (_, site, x, y, orient, nx, ny, sx, sy) in d.rows:
                sw, sh = self.lib.sites.get(site, (sx or 0.46, 2.72))
                x2 = x + nx * (sx or sw)
                if vis_box(x, y, x2, y + sh):
                    rect(x, y, x2, y + sh, outline=ROW_COLOR, fill="")
        # tracks (only when zoomed in)
        if self.on("tracks") and s > 8:
            for (dr, start, num, step, layers) in d.tracks:
                col = self.layer_color(layers[0]) if layers else "#333"
                if layers and not self.on("L:" + layers[0]):
                    continue
                if dr == "X":
                    k0 = max(0, int((vx1 - start) / step)); k1 = min(num, int((vx2 - start) / step) + 1)
                    for k in range(k0, k1):
                        X, _ = self.to_px(start + k * step, 0)
                        c.create_line(X, 0, X, H, fill=col, stipple="gray12")
                else:
                    k0 = max(0, int((vy1 - start) / step)); k1 = min(num, int((vy2 - start) / step) + 1)
                    for k in range(k0, k1):
                        _, Y = self.to_px(0, start + k * step)
                        c.create_line(0, Y, W, Y, fill=col, stipple="gray12")
        # die & core
        if self.on("die") and d.die:
            if d.die_poly and len(d.die_poly) > 4:
                pts = [v for p in d.die_poly for v in self.to_px(*p)]
                c.create_polygon(*pts, outline=DIE_COLOR, fill="", width=2)
            else:
                rect(*d.die, outline=DIE_COLOR, width=2)
            if d.core:
                rect(*d.core, outline=CORE_COLOR, dash=(5, 3))
        # power / special nets
        if self.on("power") and mode != "amoeba":
            for net in d.specialnets.values():
                base = POWER_COLORS.get(net.use, "#8a4f9e")
                for w in net.wires:
                    if not self.on("L:" + w.layer):
                        continue
                    self._draw_wire(w, self.layer_color(w.layer), special=True, tint=base)
                for (lay, x1, y1, x2, y2) in net.rects:
                    if self.on("L:" + lay):
                        rect(x1, y1, x2, y2, outline=self.layer_color(lay), fill=self.layer_color(lay), stipple="gray25")
        # cells
        unpl = self._unplaced_positions() if self.on("unplaced") else {}
        hi = set(self.hi_insts)
        path_centers = {}
        if self.on("cells"):
            show_names = self.on("cellnames")
            show_pins = self.on("cellpins") and s > 25 and mode == "physical"
            font_px = int(min(12, max(7, s * 0.9)))
            for comp in d.comps.values():
                M = self.lib.macros.get(comp.master)
                kind = cell_kind(comp.master, M.cls if M else "", comp.name)
                if not self.on("K:" + kind):
                    continue
                if comp.status == "UNPLACED":
                    if comp.name not in unpl:
                        continue
                    x, y, w, h = unpl[comp.name]
                    placed = False
                else:
                    w0, h0 = self._msize(comp.master)
                    w, h = placed_size(comp.orient, w0, h0)
                    x, y = comp.x, comp.y
                    placed = True
                if not vis_box(x, y, x + w, y + h):
                    if comp.name in hi:
                        path_centers[comp.name] = (x + w / 2, y + h / 2)
                    continue
                o, fill, _ = KIND[kind]
                if not placed:
                    rect(x, y, x + w, y + h, outline="#a0522d", fill="#1a0f08", dash=(2, 2))
                elif mode == "floorplan":
                    rect(x, y, x + w, y + h, outline="#2c3e57", fill="")
                elif mode == "amoeba":
                    rect(x, y, x + w, y + h, outline=o, fill=o, stipple="gray50")
                else:
                    rect(x, y, x + w, y + h, outline=o, fill=fill)
                    if s > 12:   # orientation notch, like Innovus' cell corner mark
                        k = min(w, h) * 0.18
                        flip_y = comp.orient in ("FS", "S")
                        flip_x = comp.orient in ("FN", "S")
                        ax = x + w if flip_x else x
                        ay = y + h if flip_y else y
                        p1 = self.to_px(ax + (-k if flip_x else k), ay)
                        p2 = self.to_px(ax, ay + (-k if flip_y else k))
                        c.create_line(*p1, *p2, fill=o)
                    if show_pins and M:
                        for pname, shapes in M.pins.items():
                            use = M.pin_use.get(pname, "")
                            if use in ("POWER", "GROUND"):
                                continue
                            for (lay, px1, py1, px2, py2) in shapes:
                                if lay and not self.on("L:" + lay):
                                    continue
                                a = xform(comp.orient, px1, py1, w0, h0)
                                b = xform(comp.orient, px2, py2, w0, h0)
                                col = self.layer_color(lay) if lay else "#8ab4ff"
                                rect(x + min(a[0], b[0]), y + min(a[1], b[1]), x + max(a[0], b[0]),
                                     y + max(a[1], b[1]), outline=col, fill="", stipple="")
                if comp.name in hi:
                    path_centers[comp.name] = (x + w / 2, y + h / 2)
                if (show_names and kind not in ("fill", "tap") and h * s > 12
                        and len(comp.name) * font_px * 0.62 < w * s - 4):
                    X, Y = self.to_px(x + w / 2, y + h / 2)
                    label = comp.name
                    two = h * s > 30 and len(comp.master.split("__")[-1]) * (font_px - 2) * 0.62 < w * s - 4
                    c.create_text(X, Y - (font_px * 0.6 if two else 0), text=label,
                                  fill="#c8d1dc", font=(self.fonts["mono"][0], font_px))
                    short = comp.master.split("__")[-1]
                    if h * s > 30 and len(short) * (font_px - 2) * 0.62 < w * s - 4:
                        c.create_text(X, Y + font_px * 0.7, text=short, fill="#7d8896",
                                      font=(self.fonts["mono"][0], max(7, font_px - 2)))
            if unpl:
                ux = min(p[0] for p in unpl.values())
                uy = max(p[1] + p[3] for p in unpl.values())
                X, Y = self.to_px(ux, uy)
                c.create_text(X, Y - 10, text=f"{len(unpl)} unplaced instances", anchor="sw", fill="#d08050",
                              font=self.fonts["small"])
        # signal routing
        if mode == "physical" and self.on("nets"):
            show_clk = self.on("clock")
            for net in d.nets.values():
                is_clk = net.name in self.clock_nets
                if is_clk and not show_clk:
                    continue
                for w in net.wires:
                    if self.on("L:" + w.layer):
                        self._draw_wire(w, self.layer_color(w.layer))
                for (lay, x1, y1, x2, y2) in net.rects:
                    if self.on("L:" + lay):
                        col = self.layer_color(lay)
                        rect(x1, y1, x2, y2, outline=col, fill=col)
                if self.on("vias") and s > 4:
                    vs = max(2, min(8, 0.17 * s))
                    for v in net.vias:
                        X, Y = self.to_px(v.x, v.y)
                        c.create_rectangle(X - vs / 2, Y - vs / 2, X + vs / 2, Y + vs / 2, outline=CUT_COLOR, fill="")
                        c.create_line(X - vs / 2, Y - vs / 2, X + vs / 2, Y + vs / 2, fill=CUT_COLOR)
        # clock-tree flylines
        if self.on("clocktree"):
            for nname in self.clock_nets:
                self._flylines(nname, FLY, dash=(3, 2))
        # IO pins
        if self.on("iopins"):
            for p in d.pins.values():
                if not p.placed:
                    continue
                w0, h0 = p.rect[2] - p.rect[0], p.rect[3] - p.rect[1]
                a = xform(p.orient, p.rect[0], p.rect[1], 0, 0)
                b = xform(p.orient, p.rect[2], p.rect[3], 0, 0)
                x1, y1 = p.x + min(a[0], b[0]), p.y + min(a[1], b[1])
                x2, y2 = p.x + max(a[0], b[0]), p.y + max(a[1], b[1])
                col = self.layer_color(p.layer) if p.layer else PIN_COLOR
                if p.use in ("POWER", "GROUND"):
                    col = POWER_COLORS[p.use]
                rect(x1, y1, x2, y2, outline=PIN_COLOR, fill=col, width=1)
                X, Y = self.to_px(p.x, p.y)
                c.create_polygon(X, Y - 5, X + 5, Y, X, Y + 5, X - 5, Y, fill=PIN_COLOR, outline="")
                if self.on("pinnames"):
                    anchor, dx, dy = self._pin_label_anchor(p)
                    c.create_text(X + dx, Y + dy, text=p.name, fill="#e6d38a", anchor=anchor,
                                  font=(self.fonts["mono"][0], 9))
        # highlighted nets
        for nname in self.hi_nets:
            net = d.nets.get(nname) or d.specialnets.get(nname)
            if not net:
                continue
            for w in net.wires:
                self._draw_wire(w, HILITE, force_width=3)
            if not net.wires:
                self._flylines(nname, HILITE, dash=(3, 3))
        # highlighted instances / timing path
        if hi:
            order = [n for n in self.hi_insts if n in path_centers]
            pts = [v for n in order for v in self.to_px(*path_centers[n])]
            if len(pts) >= 4:
                c.create_line(*pts, fill=HILITE, width=2, arrow="last", arrowshape=(10, 12, 4), smooth=False)
            for n in order:
                comp = d.comps.get(n)
                if not comp:
                    continue
                if comp.status == "UNPLACED":
                    x, y, w, h = unpl.get(n, (0, 0, 0, 0))
                else:
                    w, h = placed_size(comp.orient, *self._msize(comp.master)); x, y = comp.x, comp.y
                rect(x, y, x + w, y + h, outline=HILITE, width=3)
            if self.hi_label:
                c.create_text(12, 12, text=self.hi_label, anchor="nw", fill=HILITE, font=self.fonts["bold"])
        # DRC markers
        if self.on("drc"):
            for (x1, y1, x2, y2, typ) in self.drc_markers:
                X1, Y1 = self.to_px(x1, y2); X2, Y2 = self.to_px(x2, y1)
                c.create_rectangle(X1 - 3, Y1 - 3, X2 + 3, Y2 + 3, outline=T.ERR, width=2)
                c.create_line(X1 - 3, Y1 - 3, X2 + 3, Y2 + 3, fill=T.ERR)
                c.create_line(X1 - 3, Y2 + 3, X2 + 3, Y1 - 3, fill=T.ERR)
        # selection
        if self.sel:
            self._draw_selection()
        # ruler
        if self._ruler:
            self._draw_ruler()
        self._draw_scale_bar()
        self._draw_legend(mode)
        self.st_zoom.configure(text=f"{self.s:.2f} px/µm")
        self._draw_world_view()

    def _pin_label_anchor(self, p) -> tuple[str, int, int]:
        d = self.d
        if not d or not d.die:
            return "w", 8, 0
        x1, y1, x2, y2 = d.die
        tol = 0.5
        if abs(p.x - x1) < tol:
            return "e", -8, 0
        if abs(p.x - x2) < tol:
            return "w", 8, 0
        if abs(p.y - y1) < tol:
            return "n", 0, 8
        return "s", 0, -8

    def _draw_wire(self, w, color, special=False, tint=None, force_width=None) -> None:
        L = self.lib.layers.get(w.layer)
        width_um = w.width or (L.width if L else 0.14)
        px = force_width or max(1.0, width_um * self.s)
        pts = [v for p in w.pts for v in self.to_px(*p)]
        if special:
            self.cv.create_line(*pts, fill=color, width=px, capstyle="butt", stipple="gray50")
            if px > 6 and tint:
                self.cv.create_line(*pts, fill=tint, width=max(1, px * 0.2), capstyle="butt")
        else:
            self.cv.create_line(*pts, fill=color, width=px, capstyle="projecting")

    def _conn_points(self, nname: str) -> list[tuple[float, float]]:
        d = self.d
        net = d.nets.get(nname)
        if not net:
            return []
        pts = []
        for comp, pin in net.conns:
            if comp == "PIN":
                p = d.pins.get(pin)
                if p and p.placed:
                    pts.append((p.x, p.y))
                continue
            c = d.comps.get(comp)
            if not c or c.status == "UNPLACED":
                continue
            w0, h0 = self._msize(c.master)
            M = self.lib.macros.get(c.master)
            shapes = M.pins.get(pin) if M else None
            if shapes:
                (_, a, b, cc, dd) = shapes[0]
                lx, ly = xform(c.orient, (a + cc) / 2, (b + dd) / 2, w0, h0)
                pts.append((c.x + lx, c.y + ly))
            else:
                w, h = placed_size(c.orient, w0, h0)
                pts.append((c.x + w / 2, c.y + h / 2))
        return pts

    def _flylines(self, nname: str, color: str, dash=None) -> None:
        pts = self._conn_points(nname)
        if len(pts) < 2:
            return
        # star from the first (driver-ish) point
        X0, Y0 = self.to_px(*pts[0])
        for p in pts[1:]:
            X, Y = self.to_px(*p)
            self.cv.create_line(X0, Y0, X, Y, fill=color, dash=dash or (), width=1)

    def _draw_selection(self) -> None:
        kind, name = self.sel
        d = self.d
        if kind == "inst" and name in d.comps:
            comp = d.comps[name]
            if comp.status == "UNPLACED":
                pos = self._unplaced_positions().get(name)
                if not pos:
                    return
                x, y, w, h = pos
            else:
                w, h = placed_size(comp.orient, *self._msize(comp.master)); x, y = comp.x, comp.y
            X1, Y1 = self.to_px(x, y + h); X2, Y2 = self.to_px(x + w, y)
            self.cv.create_rectangle(X1 - 2, Y1 - 2, X2 + 2, Y2 + 2, outline=SELECT, width=2, dash=(4, 2))
            for n in self.inst_nets.get(name, []):
                self._flylines(n, "#7fdbff", dash=(2, 3))
        elif kind == "net":
            net = d.nets.get(name) or d.specialnets.get(name)
            if net:
                for w in net.wires:
                    self._draw_wire(w, SELECT, force_width=3)
                self._flylines(name, SELECT, dash=(4, 2))
        elif kind == "pin" and name in d.pins:
            p = d.pins[name]
            X, Y = self.to_px(p.x, p.y)
            self.cv.create_oval(X - 9, Y - 9, X + 9, Y + 9, outline=SELECT, width=2)

    def _draw_scale_bar(self) -> None:
        target = 120 / self.s
        mag = 10 ** math.floor(math.log10(max(target, 1e-6)))
        for m in (1, 2, 5, 10):
            if m * mag >= target * 0.6:
                val = m * mag
                break
        px = val * self.s
        H = self.H()
        x0, y0 = 16, H - 18
        self.cv.create_line(x0, y0, x0 + px, y0, fill="#d0d0d0", width=2)
        self.cv.create_line(x0, y0 - 5, x0, y0 + 1, fill="#d0d0d0")
        self.cv.create_line(x0 + px, y0 - 5, x0 + px, y0 + 1, fill="#d0d0d0")
        lab = f"{val:g} µm" if val >= 1 else f"{val * 1000:g} nm"
        self.cv.create_text(x0 + px / 2, y0 - 8, text=lab, fill="#d0d0d0", font=self.fonts["small"], anchor="s")

    def _draw_legend(self, mode: str) -> None:
        if mode != "amoeba" or not self.d:
            return
        present = {}
        for comp in self.d.comps.values():
            M = self.lib.macros.get(comp.master)
            k = cell_kind(comp.master, M.cls if M else "", comp.name)
            present[k] = present.get(k, 0) + 1
        x, y = self.W() - 190, 14
        self.cv.create_rectangle(x - 10, y - 8, self.W() - 10, y + 20 * len(present) + 4, fill="#0d0f12", outline=T.LINE)
        for k, n in sorted(present.items(), key=lambda kv: -kv[1]):
            o, _, label = KIND[k]
            self.cv.create_rectangle(x, y, x + 12, y + 12, fill=o, outline="")
            self.cv.create_text(x + 20, y + 6, text=f"{label}  {n}", fill=T.FG, anchor="w", font=self.fonts["small"])
            y += 20

    def _empty_canvas(self) -> None:
        W, H = self.W(), self.H()
        c = self.cv
        for gx in range(0, W, 40):
            c.create_line(gx, 0, gx, H, fill="#0b0d10")
        for gy in range(0, H, 40):
            c.create_line(0, gy, W, gy, fill="#0b0d10")
        stage = self.state.get("stage", "empty")
        head = "No design in the database"
        if stage == "loaded":
            head = "Libraries / netlist read - run init_design"
        c.create_text(W / 2, H / 2 - 24, text=head, fill="#8b929c", font=(self.fonts["ui"][0], 16, "bold"))
        steps = ["read_libs <liberty>", "read_physical -lef {tech.tlef cells.lef}", "read_netlist <mapped.v>",
                 "set_top <top>", "init_design"]
        for i, s in enumerate(steps):
            c.create_text(W / 2, H / 2 + 14 + i * 20, text=s, fill="#5c636d", font=self.fonts["mono"])

    def _draw_world_view(self) -> None:
        wv = self.wv
        wv.delete("all")
        if not self.d:
            return
        bw = max(wv.winfo_width(), 50)
        bh = max(wv.winfo_height(), 50)
        x1, y1, x2, y2 = self.world_bbox()
        sc = 0.9 * min(bw / max(x2 - x1, 1e-3), bh / max(y2 - y1, 1e-3))
        ox = bw / 2 - (x1 + x2) / 2 * sc
        oy = bh / 2 + (y1 + y2) / 2 * sc
        self._wv_tf = (sc, ox, oy)

        def P(x, y):
            return ox + x * sc, oy - y * sc
        if self.d.die:
            a, b = P(self.d.die[0], self.d.die[3]); cc, dd = P(self.d.die[2], self.d.die[1])
            wv.create_rectangle(a, b, cc, dd, outline="#9a9a9a")
        for comp in self.d.comps.values():
            if comp.status == "UNPLACED":
                continue
            X, Y = P(comp.x, comp.y)
            wv.create_rectangle(X, Y - 1, X + 1, Y, outline="#3d6ea8")
        vx1, vy1 = self.to_world(0, self.H())
        vx2, vy2 = self.to_world(self.W(), 0)
        a, b = P(vx1, vy2); cc, dd = P(vx2, vy1)
        wv.create_rectangle(a, b, cc, dd, outline="#ffcc00")

    def _wv_click(self, e) -> None:
        if not getattr(self, "_wv_tf", None):
            return
        sc, ox, oy = self._wv_tf
        self.cx, self.cy = (e.x - ox) / sc, (oy - e.y) / sc
        self.schedule_redraw(5)

    # ───────────────────────────── interaction ─────────────────────────────
    def _motion(self, e) -> None:
        x, y = self.to_world(e.x, e.y)
        self.st_xy.configure(text=f"x {x:9.3f}   y {y:9.3f}  µm")
        if self._ruler:
            return
        hit = self._hit(x, y, pins_first=True)
        if hit and not self.st_hover.cget("text").startswith(("Highlighted", "'")):
            kind, name = hit
            if kind == "inst":
                comp = self.d.comps[name]
                self.st_hover.configure(text=f"{name}  ({comp.master})  {comp.status} {comp.orient}", foreground=T.FG)
            elif kind == "pin":
                self.st_hover.configure(text=f"IO pin {name}  {self.d.pins[name].direction}", foreground=T.FG)
            else:
                self.st_hover.configure(text=f"net {name}", foreground=T.FG)

    def _hit(self, x: float, y: float, pins_first=False) -> tuple[str, str] | None:
        d = self.d
        if not d:
            return None
        tol = 6 / self.s
        for p in d.pins.values():
            if p.placed and abs(p.x - x) < tol and abs(p.y - y) < tol:
                return ("pin", p.name)
        unpl = self._unplaced_positions()
        for comp in d.comps.values():
            if comp.status == "UNPLACED":
                if comp.name in unpl:
                    ux, uy, w, h = unpl[comp.name]
                    if ux <= x <= ux + w and uy <= y <= uy + h:
                        return ("inst", comp.name)
                continue
            w, h = placed_size(comp.orient, *self._msize(comp.master))
            if comp.x <= x <= comp.x + w and comp.y <= y <= comp.y + h:
                # wires on top of the cell win if very close
                wn = self._hit_wire(x, y, 3 / self.s) if self.view_mode.get() == "physical" else None
                return ("net", wn) if wn else ("inst", comp.name)
        wn = self._hit_wire(x, y, tol)
        return ("net", wn) if wn else None

    def _hit_wire(self, x, y, tol) -> str | None:
        if not self.on("nets"):
            return None
        for net in self.d.nets.values():
            for w in net.wires:
                if not self.on("L:" + w.layer):
                    continue
                for (x1, y1), (x2, y2) in zip(w.pts, w.pts[1:]):
                    if min(x1, x2) - tol <= x <= max(x1, x2) + tol and min(y1, y2) - tol <= y <= max(y1, y2) + tol:
                        return net.name
        return None

    def _b1_down(self, e) -> None:
        if self.ruler_mode.get():
            x, y = self.to_world(e.x, e.y)
            self._ruler = [x, y, x, y]
            return
        self._drag = ("maybe", e.x, e.y, self.cx, self.cy)

    def _b1_move(self, e) -> None:
        if self._ruler is not None and self.ruler_mode.get():
            self._ruler[2:] = self.to_world(e.x, e.y)
            self.schedule_redraw(5)
            return
        if self._drag and (abs(e.x - self._drag[1]) > 4 or abs(e.y - self._drag[2]) > 4):
            # left-drag pans (touchpad friendly)
            _, X0, Y0, cx0, cy0 = self._drag
            self._drag = ("pan", X0, Y0, cx0, cy0)
            self.cx = cx0 - (e.x - X0) / self.s
            self.cy = cy0 + (e.y - Y0) / self.s
            self.schedule_redraw(8)

    def _b1_up(self, e) -> None:
        if self._ruler is not None and self.ruler_mode.get():
            return
        if self._drag and self._drag[0] == "maybe":
            x, y = self.to_world(e.x, e.y)
            hit = self._hit(x, y)
            if hit:
                self.select(hit)
            else:
                self.sel = None
                self._fill_attrs(None)
            self.redraw()
        self._drag = None

    def _pan_start(self, e) -> None:
        self._drag = ("pan", e.x, e.y, self.cx, self.cy)

    def _pan_move(self, e) -> None:
        if not self._drag:
            return
        _, X0, Y0, cx0, cy0 = self._drag
        self.cx = cx0 - (e.x - X0) / self.s
        self.cy = cy0 + (e.y - Y0) / self.s
        self.schedule_redraw(8)

    def _pan_end(self, e) -> None:
        self._drag = None

    def _zbox_start(self, e) -> None:
        self._zbox = [e.x, e.y, e.x, e.y]

    def _zbox_move(self, e) -> None:
        if not getattr(self, "_zbox", None):
            return
        self._zbox[2:] = [e.x, e.y]
        self.cv.delete("zbox")
        x1, y1, x2, y2 = self._zbox
        self.cv.create_rectangle(x1, y1, x2, y2, outline="#ffffff", dash=(3, 3), tags="zbox")

    def _zbox_end(self, e) -> None:
        zb = getattr(self, "_zbox", None)
        self._zbox = None
        self.cv.delete("zbox")
        if not zb:
            return
        x1, y1, x2, y2 = zb
        if abs(x2 - x1) < 8 or abs(y2 - y1) < 8:
            self.zoom(1 / 1.5, e.x, e.y)     # right-click = zoom out (Innovus-like)
            return
        a = self.to_world(x1, y1); b = self.to_world(x2, y2)
        self.zoom_box(a[0], a[1], b[0], b[1])

    def _draw_ruler(self) -> None:
        x1, y1, x2, y2 = self._ruler
        X1, Y1 = self.to_px(x1, y1); X2, Y2 = self.to_px(x2, y2)
        self.cv.create_line(X1, Y1, X2, Y2, fill="#ffffff", width=1, arrow="both")
        L = math.hypot(x2 - x1, y2 - y1)
        self.cv.create_text((X1 + X2) / 2 + 8, (Y1 + Y2) / 2 - 8, anchor="sw", fill="#ffffff",
                            text=f"{L:.3f} µm  (dx {abs(x2 - x1):.3f}, dy {abs(y2 - y1):.3f})",
                            font=self.fonts["small"])

    def select(self, hit: tuple[str, str]) -> None:
        self.sel = hit
        self._fill_attrs(hit)
        self.nb.select(2)

    def _fill_attrs(self, hit) -> None:
        tr = self.attr_tree
        tr.delete(*tr.get_children())
        if not hit or not self.d:
            self.attr_title.configure(text="Nothing selected")
            return
        kind, name = hit
        d = self.d
        if kind == "inst":
            c = d.comps[name]
            M = self.lib.macros.get(c.master)
            w, h = placed_size(c.orient, *self._msize(c.master))
            self.attr_title.configure(text=f"Instance  {name}")
            rows = [("Cell", c.master), ("Class", M.cls if M else "?"),
                    ("Kind", KIND[cell_kind(c.master, M.cls if M else '', name)][2]),
                    ("Status", c.status), ("Orient", c.orient),
                    ("Location", f"({c.x:.3f}, {c.y:.3f})"), ("Size", f"{w:.3f} × {h:.3f} µm"),
                    ("Area", f"{w * h:.3f} µm²")]
            if c.source:
                rows.append(("Source", c.source))
            for k, v in rows:
                tr.insert("", "end", text=k, values=(v,))
            nets = tr.insert("", "end", text="Nets", values=(f"{len(self.inst_nets.get(name, []))}",), open=True)
            for n in self.inst_nets.get(name, []):
                pins = [p for (cc, p) in d.nets[n].conns if cc == name] if n in d.nets else []
                tr.insert(nets, "end", text=",".join(pins) or "·", values=(n,), tags=("net",))
        elif kind == "net":
            net = d.nets.get(name) or d.specialnets.get(name)
            self.attr_title.configure(text=f"Net  {name}")
            L = sum(abs(x2 - x1) + abs(y2 - y1) for w in net.wires for (x1, y1), (x2, y2) in zip(w.pts, w.pts[1:]))
            layers = sorted({w.layer for w in net.wires})
            for k, v in [("Use", net.use or "SIGNAL"), ("Clock", "yes" if name in self.clock_nets else "no"),
                         ("Connections", len(net.conns)), ("Wire length", f"{L:.3f} µm"),
                         ("Layers", " ".join(layers) or "unrouted"), ("Vias", len(net.vias))]:
                tr.insert("", "end", text=k, values=(v,))
            conns = tr.insert("", "end", text="Terminals", values=(len(net.conns),), open=True)
            for (cc, p) in net.conns:
                tr.insert(conns, "end", text=p, values=(cc,), tags=("inst",))
        elif kind == "pin":
            p = d.pins[name]
            self.attr_title.configure(text=f"IO Pin  {name}")
            for k, v in [("Net", p.net), ("Direction", p.direction), ("Use", p.use or "SIGNAL"),
                         ("Layer", p.layer), ("Location", f"({p.x:.3f}, {p.y:.3f})"), ("Orient", p.orient)]:
                tr.insert("", "end", text=k, values=(v,))

    def _attr_dbl(self, e) -> None:
        it = self.attr_tree.focus()
        if not it:
            return
        tags = self.attr_tree.item(it, "tags")
        v = self.attr_tree.item(it, "values")
        if not v:
            return
        if "net" in tags:
            self.select(("net", v[0])); self.redraw()
        elif "inst" in tags and v[0] in (self.d.comps if self.d else {}):
            self.select(("inst", v[0])); self.zoom_to_selection()

    def zoom_to_selection(self) -> None:
        if not self.sel or not self.d:
            return
        kind, name = self.sel
        if kind == "inst" and name in self.d.comps:
            c = self.d.comps[name]
            if c.status == "UNPLACED":
                return
            w, h = placed_size(c.orient, *self._msize(c.master))
            m = max(w, h) * 3
            self.zoom_box(c.x - m, c.y - m, c.x + w + m, c.y + h + m)

    def clear_highlight(self) -> None:
        self.hi_insts, self.hi_nets, self.hi_label = [], [], ""
        self.sel = None
        self._ruler = None
        self._fill_attrs(None)
        self.redraw()

    # ───────────────────────────── panels ─────────────────────────────
    def _update_design_tab(self) -> None:
        s = self.state
        m = s.get("metrics", {}) or {}
        t = s.get("timing", {}) or {}
        L = self.design_labels

        def put(k, v, col=None):
            L[k].configure(text=str(v) if v not in (None, "") else "-", foreground=col or T.FG)
        put("top", s.get("top"))
        put("stage", s.get("stage_label", s.get("stage")), "#9ecbff")
        put("backend", s.get("backend") or self.backend)
        put("profile", (s.get("profile", "") + (f" · {s['site']}" if s.get("site") else "")))
        die = m.get("die")
        put("die", f"{die[2] - die[0]:.2f} × {die[3] - die[1]:.2f} µm" if die and die[2] > die[0] else None)
        core = m.get("core")
        put("core", f"{core[2] - core[0]:.2f} × {core[3] - core[1]:.2f} µm" if core and core[2] > core[0] else None)
        put("util", f"{m['util'] * 100:.1f} %" if m.get("util") else None)
        put("insts", f"{m.get('insts')}  ({m.get('placed')} placed)" if m.get("insts") is not None else None)
        put("seq", m.get("seq"))
        put("nets", m.get("nets"))
        put("terms", m.get("terms"))
        put("clocks", s.get("clocks"))
        for k in ("wns", "tns", "whs"):
            v = t.get(k)
            col = None
            try:
                col = T.ERR if float(v) < 0 else T.OK
                v = f"{float(v):+.3f} ns"
            except (TypeError, ValueError):
                pass
            put(k, v if v not in ("NA",) else None, col)
        put("drc", s.get("drc"), T.ERR if s.get("drc") else None)
        put("parasitics", s.get("parasitics"))
        files = []
        for key, lab in (("libs", "LIB"), ("tech_lefs", "TLEF"), ("cell_lefs", "LEF"), ("netlists", "NET"), ("sdcs", "SDC")):
            for f in s.get(key, []) or []:
                files.append(f"{lab:<5} {os.path.basename(f)}")
        self.files_lbl.configure(text="\n".join(files))
        self.st_stage.configure(text=s.get("stage_label", "no design"))

    def _update_flow_buttons(self) -> None:
        rank = STAGE_RANK.get(self.stage, 0)
        nxt = None
        for key, label, cmd in STAGES:
            b = self.flow_btns[key]
            done = rank >= STAGE_RANK[key] and key != "signoff"
            if done:
                b.configure(style="FlowDone.TButton", text=label)
            elif nxt is None and not (key == "powerplan" and rank >= STAGE_RANK["place"]):
                nxt = key
                b.configure(style="FlowNext.TButton", text=label)
            else:
                b.configure(style="Flow.TButton", text=label)
            if self.busy:
                b.state(["disabled"])
            else:
                b.state(["!disabled"])

    # ───────────────────────────── dialogs ─────────────────────────────
    def dlg_floorplan(self) -> None:
        m = self.state.get("metrics", {}) or {}
        Dialog(self.root, self.fonts, "Specify Floorplan", [
            ("mode", "Core size by", "radio", ["Aspect ratio + utilisation", "Die size (W × H)"], "Aspect ratio + utilisation"),
            ("ratio", "Ratio (H/W)", "float", None, 1.0),
            ("util", "Core utilisation", "float", None, 0.5),
            ("dw", "Die width (µm)", "float", None, round((m.get("die") or [0, 0, 60, 0])[2], 2) or 60.0),
            ("dh", "Die height (µm)", "float", None, round((m.get("die") or [0, 0, 0, 60])[3], 2) or 60.0),
            ("l", "Core -> left", "float", None, 5.0), ("b", "Core -> bottom", "float", None, 5.0),
            ("r", "Core -> right", "float", None, 5.0), ("t", "Core -> top", "float", None, 5.0),
            ("site", "Site (blank = auto)", "str", None, self.state.get("site", "")),
        ], self._do_floorplan, note="Runs initialize_floorplan + make_tracks + place_pins in the live OpenROAD DB.")

    def _do_floorplan(self, v: dict) -> None:
        site = f" -site {v['site']}" if v.get("site") else ""
        if v["mode"].startswith("Die"):
            self.send_cmd(f"floorPlan{site} -d {v['dw']} {v['dh']} {v['l']} {v['b']} {v['r']} {v['t']}")
        else:
            self.send_cmd(f"floorPlan{site} -r {v['ratio']} {v['util']} {v['l']} {v['b']} {v['r']} {v['t']}")

    def dlg_place(self) -> None:
        Dialog(self.root, self.fonts, "Place Standard Cells", [
            ("density", "Target density (blank = auto)", "str", None, ""),
            ("td", "Timing driven", "bool", None, False),
            ("rd", "Routability driven", "bool", None, False),
            ("noopt", "Skip repair_design", "bool", None, False),
        ], lambda v: self.send_cmd("placeDesign" + (f" -density {v['density']}" if v["density"] else "")
                                   + (" -timingDriven" if v["td"] else "") + (" -routabilityDriven" if v["rd"] else "")
                                   + (" -noOpt" if v["noopt"] else "")),
            note="RePlAce global placement -> repair_design -> OpenDP detailed placement.")

    def dlg_route(self) -> None:
        Dialog(self.root, self.fonts, "NanoRoute", [
            ("mode", "Route", "radio", ["Global + Detail", "Global only", "Detail only"], "Global + Detail"),
            ("iter", "Max DRC iterations (blank = default)", "str", None, ""),
        ], lambda v: self.send_cmd("routeDesign" + {"Global + Detail": "", "Global only": " -global",
                                                    "Detail only": " -detail"}[v["mode"]]
                                   + (f" -drcIter {v['iter']}" if v["iter"] else "")),
            note="FastRoute global routing -> TritonRoute detailed routing. Progress streams in the transcript.")

    def dlg_stripe(self) -> None:
        Dialog(self.root, self.fonts, "Add Stripe", [
            ("layer", "Layer", "str", None, "met4"), ("width", "Width (µm)", "float", None, 1.6),
            ("spacing", "Spacing (µm)", "float", None, 1.7), ("pitch", "Set-to-set distance (µm)", "float", None, 20.0),
            ("offset", "Start offset (µm)", "float", None, 2.0),
        ], lambda v: self.send_cmd(f"addStripe -layer {v['layer']} -width {v['width']} -spacing {v['spacing']} "
                                   f"-set_to_set_distance {v['pitch']} -start_offset {v['offset']}"),
            note="Adds to the PDN spec; run Power -> Special Route to build it.")

    def dlg_report_timing(self) -> None:
        Dialog(self.root, self.fonts, "Report Timing", [
            ("n", "Max paths", "int", None, 5),
            ("mode", "Analysis", "radio", ["Late (setup)", "Early (hold)"], "Late (setup)"),
        ], lambda v: self.send_cmd(f"report_timing -max_paths {v['n']}" + (" -early" if v["mode"].startswith("Early") else ""),
                                   report=True, title="Timing Report"))

    def dlg_save(self) -> None:
        top = self.state.get("top", "design")
        Dialog(self.root, self.fonts, "Save Design", [("name", "Checkpoint name", "str", None, f"{top}_{self.stage}")],
               lambda v: self.send_cmd(f"saveDesign {v['name']}"))

    def dlg_restore(self) -> None:
        dbs = os.path.join(self.project, ".rtlflow", "runs", "pnr", "dbs")
        fn = filedialog.askopenfilename(parent=self.root, title="Restore Design", initialdir=dbs if os.path.isdir(dbs) else self.project,
                                        filetypes=[("OpenROAD DB", "*.odb")])
        if fn:
            self.send_cmd(f"restoreDesign {{{fn}}}")

    def dlg_stream(self) -> None:
        top = self.state.get("top", "design")
        Dialog(self.root, self.fonts, "Stream Out", [
            ("file", "GDS file", "str", None, os.path.join(self.project, ".rtlflow", "runs", "pnr", f"{top}.gds")),
            ("view", "Open in KLayout afterwards", "bool", None, True)],
            lambda v: self.send_cmd(f"streamOut {{{v['file']}}}" + (" -view" if v["view"] else "")))

    def dlg_dump(self) -> None:
        fn = filedialog.asksaveasfilename(parent=self.root, title="Save Screenshot", defaultextension=".png",
                                          filetypes=[("PNG", "*.png"), ("PostScript", "*.ps")],
                                          initialfile=f"{self.state.get('top', 'layout')}_{self.stage}.png")
        if fn:
            self.dump(fn)

    def dlg_find(self) -> None:
        Dialog(self.root, self.fonts, "Find Instance", [("pat", "Name or pattern (*)", "str", None, "")],
               lambda v: self._handle({"t": "select", "insts": [v["pat"]], "zoom": True}))

    def ask_file_cmd(self, cmd: str, ext: str) -> None:
        top = self.state.get("top", "design")
        fn = filedialog.asksaveasfilename(parent=self.root, title=cmd, defaultextension=ext,
                                          initialdir=os.path.join(self.project, ".rtlflow", "runs", "pnr"),
                                          initialfile=f"{top}{ext}")
        if fn:
            self.send_cmd(f"{cmd} {{{fn}}}")

    def dump(self, fn: str | None) -> None:
        if not fn:
            return
        self.root.update_idletasks()
        base, ext = os.path.splitext(fn)
        try:
            if ext.lower() == ".png":
                try:
                    from PIL import ImageGrab  # optional
                    x, y = self.cv.winfo_rootx(), self.cv.winfo_rooty()
                    ImageGrab.grab(bbox=(x, y, x + self.W(), y + self.H())).save(fn)
                    self.flash(f"Saved {fn}", T.OK)
                    return
                except Exception:
                    ps = base + ".ps"
                    self.cv.postscript(file=ps, colormode="color")
                    import shutil
                    import subprocess
                    if shutil.which("gs"):
                        subprocess.run(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=png16m", "-r150",
                                        "-dEPSCrop", f"-sOutputFile={fn}", ps], check=False)
                        if os.path.exists(fn):
                            self.flash(f"Saved {fn}", T.OK)
                            return
                    self.flash(f"Saved {ps} (install Pillow or ghostscript for PNG)", T.WARN)
                    return
            self.cv.postscript(file=fn, colormode="color")
            self.flash(f"Saved {fn}", T.OK)
        except Exception as e:
            self.flash(f"Snapshot failed: {e}", T.ERR)

    def open_timing_debug(self) -> None:
        if self.timing_win and self.timing_win.alive():
            self.timing_win.top.lift()
        else:
            self.timing_win = TimingDebug(self)
        self.timing_win.refresh()

    def show_keys(self) -> None:
        ReportWindow(self.root, self.fonts, "Keyboard & Mouse", "\n".join([
            "Mouse",
            "  Left click          select instance / IO pin / wire",
            "  Left drag           pan",
            "  Double click        zoom to selection",
            "  Middle drag         pan",
            "  Right drag          zoom to box        right click: zoom out",
            "  Wheel               zoom at cursor",
            "",
            "Keys (canvas focus)",
            "  f                   fit",
            "  z / Z               zoom in / out",
            "  arrows              pan",
            "  k                   ruler on/off",
            "  Esc                 clear selection & highlight",
            "  Ctrl+T              Timing Debug",
            "  Ctrl+R              re-read database",
            "  Ctrl+L              command line",
            "  F12                 toggle transcript",
        ]))

    def show_about(self) -> None:
        messagebox.showinfo("About", f"RTLFlow Innovus Implementation System {VERSION}\n\n"
                                     f"Physical backend: {self.backend or 'OpenROAD'}\n"
                                     "Layout rendered from the live OpenROAD database (DEF).\n"
                                     "Terminal and GUI share one session.", parent=self.root)

    def on_close(self) -> None:
        try:
            if self.chan:
                self.chan.send(t="closing")
        finally:
            self.root.destroy()


# ═════════════════════════════════════ widgets ═════════════════════════════════════
class _Tip:
    def __init__(self, w, text):
        self.w, self.text, self.tip = w, text, None
        w.bind("<Enter>", self.show)
        w.bind("<Leave>", self.hide)

    def show(self, e=None):
        if self.tip:
            return
        x = self.w.winfo_rootx() + 10
        y = self.w.winfo_rooty() + self.w.winfo_height() + 4
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#101215", fg=T.FG, relief="solid", borderwidth=1,
                 padx=6, pady=2).pack()

    def hide(self, e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Dialog:
    """Small form dialog. fields: (key, label, type, choices, default)."""

    def __init__(self, root, fonts, title, fields, on_ok, note: str = ""):
        self.top = tk.Toplevel(root)
        self.top.title(title)
        self.top.configure(bg=T.BG2)
        self.top.transient(root)
        self.top.resizable(False, False)
        self.on_ok = on_ok
        self.vars = {}
        self.types = {}
        f = ttk.Frame(self.top, style="Panel.TFrame", padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=title, style="Head.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        r = 1
        for key, label, typ, choices, default in fields:
            self.types[key] = typ
            if typ == "radio":
                v = tk.StringVar(value=default)
                ttk.Label(f, text=label, style="Panel.TLabel").grid(row=r, column=0, sticky="nw", pady=3)
                box = ttk.Frame(f, style="Panel.TFrame")
                box.grid(row=r, column=1, sticky="w")
                for ch in choices:
                    ttk.Radiobutton(box, text=ch, value=ch, variable=v).pack(anchor="w")
            elif typ == "bool":
                v = tk.BooleanVar(value=default)
                ttk.Checkbutton(f, text=label, variable=v).grid(row=r, column=0, columnspan=2, sticky="w", pady=3)
            else:
                v = tk.StringVar(value=str(default))
                ttk.Label(f, text=label, style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=3, padx=(0, 12))
                ttk.Entry(f, textvariable=v, width=34 if typ == "str" else 12).grid(row=r, column=1, sticky="w", pady=3)
            self.vars[key] = v
            r += 1
        if note:
            ttk.Label(f, text=note, style="Dim.TLabel", wraplength=380, justify="left").grid(
                row=r, column=0, columnspan=2, sticky="w", pady=(10, 0))
            r += 1
        bb = ttk.Frame(f, style="Panel.TFrame")
        bb.grid(row=r, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(bb, text="Cancel", command=self.top.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(bb, text="Apply", command=self.apply).pack(side="right", padx=(6, 0))
        ttk.Button(bb, text="OK", style="Accent.TButton", command=self.ok).pack(side="right")
        self.top.bind("<Return>", lambda e: self.ok())
        self.top.bind("<Escape>", lambda e: self.top.destroy())
        self.top.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - self.top.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - self.top.winfo_height()) // 3
        self.top.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            self.top.grab_set()
        except tk.TclError:
            pass

    def values(self) -> dict | None:
        out = {}
        for k, v in self.vars.items():
            val = v.get()
            t = self.types[k]
            try:
                if t == "float":
                    val = float(val)
                elif t == "int":
                    val = int(val)
            except ValueError:
                messagebox.showerror("Invalid value", f"{k}: '{val}' is not a number", parent=self.top)
                return None
            out[k] = val
        return out

    def apply(self) -> None:
        v = self.values()
        if v is not None:
            self.on_ok(v)

    def ok(self) -> None:
        v = self.values()
        if v is not None:
            self.top.destroy()
            self.on_ok(v)


class ReportWindow:
    def __init__(self, root, fonts, title, text):
        self.top = tk.Toplevel(root)
        self.top.title(f"{title} - RTLFlow")
        self.top.geometry("900x560")
        self.top.configure(bg=T.BG)
        bar = ttk.Frame(self.top, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text=title, font=fonts["bold"]).pack(side="left")
        ttk.Button(bar, text="Save As...", command=self.save).pack(side="right")
        ttk.Button(bar, text="Copy", command=self.copy).pack(side="right", padx=4)
        body = ttk.Frame(self.top)
        body.pack(fill="both", expand=True)
        self.tx = T.text_widget(body, fonts)
        sy = ttk.Scrollbar(body, orient="vertical", command=self.tx.yview)
        sx = ttk.Scrollbar(body, orient="horizontal", command=self.tx.xview)
        self.tx.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        self.tx.pack(fill="both", expand=True)
        ansi = re.compile(r"\x1b\[[0-9;]*m")
        for line in ansi.sub("", text or "(no output)").splitlines():
            tg = T.classify(line)
            if line.startswith(("Startpoint:", "Endpoint:")):
                tg = "bold"
            self.tx.insert("end", line + "\n", (tg,) if tg else ())
        self.tx.configure(state="disabled")
        self.text = text

    def save(self):
        fn = filedialog.asksaveasfilename(parent=self.top, defaultextension=".rpt")
        if fn:
            with open(fn, "w") as f:
                f.write(self.text)

    def copy(self):
        self.top.clipboard_clear()
        self.top.clipboard_append(self.text)


class TimingDebug:
    """Innovus-style Timing Debug: path list -> click to highlight on the layout."""

    def __init__(self, gui: InnovusGUI):
        self.gui = gui
        self.top = tk.Toplevel(gui.root)
        self.top.title("Timing Debug - Setup - RTLFlow")
        self.top.geometry("1040x620")
        self.top.configure(bg=T.BG)
        f = gui.fonts
        bar = ttk.Frame(self.top, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text="Timing Debug", font=f["bold"]).pack(side="left")
        self.chips = {}
        for k, lab in (("wns", "WNS"), ("tns", "TNS"), ("viol", "Violating"), ("clocks", "Clocks")):
            box = ttk.Frame(bar, style="Panel.TFrame", padding=(10, 3))
            box.pack(side="left", padx=(14, 0))
            ttk.Label(box, text=lab, style="Dim.TLabel").pack(side="left")
            v = ttk.Label(box, text="-", style="Panel.TLabel", font=f["bold"])
            v.pack(side="left", padx=(6, 0))
            self.chips[k] = v
        ttk.Button(bar, text="Refresh", style="Accent.TButton", command=self.refresh).pack(side="right")
        self.n = tk.StringVar(value="50")
        ttk.Spinbox(bar, from_=1, to=1000, textvariable=self.n, width=6).pack(side="right", padx=6)
        ttk.Label(bar, text="paths").pack(side="right")
        pw = ttk.PanedWindow(self.top, orient="vertical")
        pw.pack(fill="both", expand=True)
        tf = ttk.Frame(pw)
        pw.add(tf, weight=3)
        cols = ("slack", "end", "start", "group", "cells")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="browse")
        for c, (lab, w) in zip(cols, (("Slack (ns)", 90), ("Endpoint", 300), ("Startpoint", 300), ("Group", 120), ("Cells", 60))):
            self.tree.heading(c, text=lab)
            self.tree.column(c, width=w, anchor="w" if c in ("end", "start", "group") else "e")
        self.tree.tag_configure("viol", foreground=T.ERR)
        self.tree.tag_configure("met", foreground="#7ee2a0")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        df = ttk.Frame(pw)
        pw.add(df, weight=2)
        self.detail = T.text_widget(df, f, height=10)
        self.detail.pack(fill="both", expand=True)
        self.paths = []
        self.status = ttk.Label(self.top, text="", style="Status.TLabel")
        self.status.pack(fill="x", side="bottom")

    def alive(self) -> bool:
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def refresh(self):
        self.status.configure(text="Querying OpenSTA ...")
        try:
            n = int(self.n.get())
        except ValueError:
            n = 50
        if self.gui.chan:
            self.gui.chan.send(t="timing_paths", n=n)

    def load(self, m: dict):
        self.tree.delete(*self.tree.get_children())
        self.paths = m.get("paths", [])
        s = m.get("summary", {}) or {}
        for k, lab in self.chips.items():
            v = s.get(k, "-")
            col = T.FG
            try:
                if k in ("wns", "tns"):
                    col = T.ERR if float(v) < 0 else T.OK
                    v = f"{float(v):+.3f}"
            except (TypeError, ValueError):
                pass
            lab.configure(text=str(v), foreground=col)
        if m.get("error"):
            self.status.configure(text=f"Error: {m['error']}", foreground=T.ERR)
        else:
            self.status.configure(text=f"{len(self.paths)} path(s) · click a row to highlight it on the layout",
                                  foreground=T.FG_DIM)
        for i, p in enumerate(self.paths):
            sl = p.get("slack")
            self.tree.insert("", "end", iid=str(i), values=(f"{sl:+.3f}" if sl is not None else "?", p.get("end", ""),
                                                           p.get("start", ""), p.get("group", ""), len(p.get("insts", []))),
                             tags=("viol" if (sl is not None and sl < 0) else "met",))
        if self.paths:
            self.tree.selection_set("0")

    def on_select(self, e=None):
        sel = self.tree.selection()
        if not sel:
            return
        p = self.paths[int(sel[0])]
        self.gui.hi_insts = p.get("insts", [])
        self.gui.hi_nets = []
        self.gui.hi_label = f"{p.get('start')} -> {p.get('end')}  ·  slack {p.get('slack', 0):+.3f} ns"
        self.gui.redraw()
        self.detail.delete("1.0", "end")
        self.detail.insert("end", f"Startpoint : {p.get('start')}\nEndpoint   : {p.get('end')}\n"
                                  f"Group      : {p.get('group')}\nSlack      : {p.get('slack')}\n\n", ("bold",))
        for i, inst in enumerate(p.get("insts", [])):
            c = self.gui.d.comps.get(inst) if self.gui.d else None
            self.detail.insert("end", f"  {i + 1:3d}. {inst:<30} {c.master if c else ''}\n")


# ═══════════════════════════════════════ main ═══════════════════════════════════════
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fd", type=int, default=-1)
    ap.add_argument("--title", default="")
    ap.add_argument("--project", default=os.getcwd())
    ap.add_argument("--def", dest="def_file", default="")
    ap.add_argument("--lef", action="append", default=[])
    ap.add_argument("--screenshot", default="")
    a = ap.parse_args(argv)
    chan = None
    if a.fd >= 0:
        sock = socket.socket(fileno=a.fd)
        chan = Channel(sock)
    root = tk.Tk(className="RTLFlow-Innovus")
    gui = InnovusGUI(root, chan, a.title, a.project)
    if a.def_file:   # viewer/test mode (no session): python -m rtlflow.gui.innovus_gui --def x.def --lef a.lef
        gui._handle({"t": "state", "state": {"stage": "place", "top": a.title, "lefs": a.lef}})
        root.after(200, lambda: gui._handle({"t": "layout", "def": a.def_file, "lefs": a.lef, "stage": "place"}))
    if a.screenshot:
        root.after(1500, lambda: (gui.dump(a.screenshot), root.after(800, root.destroy)))
    root.mainloop()
    if chan:
        chan.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
