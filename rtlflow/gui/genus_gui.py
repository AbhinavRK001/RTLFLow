"""RTLFlow Genus GUI — gate-level schematic + design browser + reports.

Opened by `gui_show` in the genus shell (or `genus -gui`). Talks to the same
genus session over the shell's socket: schematic comes from the Yosys JSON of
the *actual* mapped netlist; menu actions run as genus commands.
"""

from __future__ import annotations

import argparse
import json
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

G_STAGES = [("read", "Read HDL"), ("elaborated", "Elaborate"), ("generic", "Generic"),
            ("mapped", "Map"), ("optimized", "Opt")]
G_RANK = {s: i for i, s in enumerate(["empty", "read", "elaborated", "generic", "mapped", "optimized"])}
CELL_W, PIN_H, COL_W = 96.0, 16.0, 190.0
COL = {"comb": ("#5aa9ff", "#0f2238"), "seq": ("#d38cff", "#26123a"), "buf": ("#6fe0d0", "#0b2b28"),
       "tie": ("#8be68b", "#102a10"), "port_in": ("#ffcc00", ""), "port_out": ("#ffcc00", "")}
HILITE = "#ff4fd8"


class Schematic:
    def __init__(self, data: dict, top: str, seq: set[str]):
        mod = data["modules"].get(top) or next(iter(data["modules"].values()))
        self.nodes: dict[str, dict] = {}
        self.drv: dict = {}
        self.sinks: dict = {}
        for pname, p in mod.get("ports", {}).items():
            kind = "port_in" if p["direction"] == "input" else "port_out"
            self.nodes["port:" + pname] = {"name": pname, "type": kind, "kind": kind, "ins": [], "outs": [],
                                          "bits": p["bits"]}
            for b in p["bits"]:
                if isinstance(b, int):
                    if kind == "port_in":
                        self.drv[b] = ("port:" + pname, pname)
                    else:
                        self.sinks.setdefault(b, []).append(("port:" + pname, pname))
        for cname, c in mod.get("cells", {}).items():
            t = c["type"]
            dirs = c.get("port_directions", {})
            ins, outs = [], []
            for pin, bits in c.get("connections", {}).items():
                d = dirs.get(pin) or ("output" if pin in ("X", "Y", "Q", "Q_N", "Z", "ZN", "HI", "LO", "COUT", "SUM") else "input")
                (outs if d == "output" else ins).append(pin)
                for b in bits:
                    if not isinstance(b, int):
                        continue
                    if d == "output":
                        self.drv[b] = (cname, pin)
                    else:
                        self.sinks.setdefault(b, []).append((cname, pin))
            kind = "seq" if t in seq or re.search(r"(?i)__s?df|__dl|dff|latch", t) else \
                ("tie" if re.search(r"(?i)conb|tie", t) else ("buf" if re.search(r"__(buf|inv|clkbuf)", t) else "comb"))
            self.nodes[cname] = {"name": cname, "type": t, "kind": kind, "ins": sorted(ins), "outs": sorted(outs),
                                 "conn": c.get("connections", {})}
        self.netnames = {}
        for nn, n in mod.get("netnames", {}).items():
            for b in n.get("bits", []):
                if isinstance(b, int) and (b not in self.netnames or not n.get("hide_name")):
                    self.netnames[b] = nn
        self._layout()

    def _layout(self) -> None:
        level: dict[str, int] = {}
        visiting = set()

        def lv(n: str) -> int:
            if n in level:
                return level[n]
            node = self.nodes[n]
            if node["kind"] in ("port_in", "seq", "tie"):
                level[n] = 0
                return 0
            if n in visiting:
                return 0
            visiting.add(n)
            m = 0
            for pin in node.get("ins", []):
                for b in node["conn"].get(pin, []):
                    d = self.drv.get(b) if isinstance(b, int) else None
                    if d:
                        m = max(m, lv(d[0]) + 1)
            visiting.discard(n)
            level[n] = max(m, 1)
            return level[n]
        for n in self.nodes:
            if self.nodes[n]["kind"] not in ("port_out",):
                lv(n)
        # flops: one column after their D-cone
        for n, node in self.nodes.items():
            if node["kind"] == "seq":
                m = 0
                for pin in node["ins"]:
                    for b in node["conn"].get(pin, []):
                        d = self.drv.get(b) if isinstance(b, int) else None
                        if d and self.nodes[d[0]]["kind"] not in ("seq",):
                            m = max(m, level.get(d[0], 0) + 1)
                level[n] = max(1, m)
        maxl = max([v for v in level.values()] + [1])
        for n, node in self.nodes.items():
            if node["kind"] == "port_out":
                level[n] = maxl + 1
            elif node["kind"] == "port_in":
                level[n] = 0
        cols: dict[int, list[str]] = {}
        for n, l in level.items():
            cols.setdefault(l, []).append(n)
        # order: barycentre of fan-in, 2 sweeps
        ypos = {}
        for l in sorted(cols):
            cols[l].sort(key=lambda n: (self.nodes[n]["kind"] != "port_in", n))
        for _ in range(2):
            for l in sorted(cols):
                def bary(n):
                    ys = []
                    node = self.nodes[n]
                    for pin in node.get("ins", []) or []:
                        for b in node.get("conn", {}).get(pin, []):
                            d = self.drv.get(b) if isinstance(b, int) else None
                            if d and d[0] in ypos:
                                ys.append(ypos[d[0]])
                    if node["kind"] == "port_out":
                        for b in node["bits"]:
                            d = self.drv.get(b) if isinstance(b, int) else None
                            if d and d[0] in ypos:
                                ys.append(ypos[d[0]])
                    return sum(ys) / len(ys) if ys else ypos.get(n, 0)
                cols[l].sort(key=bary)
                y = 0.0
                for n in cols[l]:
                    ypos[n] = y
                    y += self._h(n) + 28
        self.pos = {}
        heights = {l: sum(self._h(n) + 28 for n in cols[l]) for l in cols}
        H = max(heights.values()) if heights else 100
        for l, ns in cols.items():
            y = (H - heights[l]) / 2
            for n in ns:
                self.pos[n] = (l * COL_W, y)
                y += self._h(n) + 28
        self.W = (maxl + 2) * COL_W
        self.H = H

    def _h(self, n: str) -> float:
        node = self.nodes[n]
        if node["kind"].startswith("port"):
            return 18.0
        return max(len(node["ins"]), len(node["outs"]), 1) * PIN_H + 14

    def pin_xy(self, n: str, pin: str) -> tuple[float, float]:
        x, y = self.pos[n]
        node = self.nodes[n]
        if node["kind"] == "port_in":
            return x + 70, y + 9
        if node["kind"] == "port_out":
            return x, y + 9
        if pin in node["outs"]:
            i = node["outs"].index(pin)
            h = self._h(n)
            k = len(node["outs"])
            return x + CELL_W, y + h / (k + 1) * (i + 1)
        i = node["ins"].index(pin) if pin in node["ins"] else 0
        return x, y + 10 + i * PIN_H + PIN_H / 2


class GenusGUI:
    def __init__(self, root: tk.Tk, chan: Channel | None, project: str):
        self.root, self.chan, self.project = root, chan, project
        self.q: "queue.Queue[dict]" = queue.Queue()
        self.fonts = T.apply(root)
        self.sch: Schematic | None = None
        self.top = ""
        self.stage = "empty"
        self.s, self.cx, self.cy = 1.0, 0.0, 0.0
        self.hi: list[str] = []
        self.hi_label = ""
        self.sel: str | None = None
        self.busy = False
        self.cmd_id = 0
        self._drag = None
        root.title(f"RTLFlow Genus Synthesis Solution {VERSION} — {project}")
        root.geometry("1440x900")
        self._menu()
        self._toolbar()
        self._body()
        self._status()
        root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        root.after(40, self._poll)
        for k, f in (("<KeyPress-f>", self.fit), ("<KeyPress-z>", lambda: self.zoom(1.4)),
                     ("<KeyPress-Z>", lambda: self.zoom(1 / 1.4))):
            root.bind(k, lambda e, f=f: None if isinstance(root.focus_get(), (tk.Entry, ttk.Entry)) else f())
        root.bind("<Escape>", lambda e: self.clear())
        if chan:
            chan.start_reader(self.q.put, lambda: self.q.put({"t": "quit"}))
            chan.send(t="ready")

    # ───────────── chrome ─────────────
    def _menu(self):
        bar = tk.Menu(self.root, bg=T.BG2, fg=T.FG, activebackground=T.ACCENT, relief="flat")

        def mk(label, items):
            m = tk.Menu(bar, tearoff=False)
            for it in items:
                if it is None:
                    m.add_separator()
                else:
                    m.add_command(label=it[0], command=it[1])
            bar.add_cascade(label=label, menu=m)
        mk("File", [("Read Libraries…", self.dlg_libs), ("Read HDL…", self.dlg_hdl), ("Elaborate…", self.dlg_elab), None,
                    ("Write HDL…", lambda: self.send("write_hdl", report=True)),
                    ("Write SDC…", lambda: self.send("write_sdc", report=True)),
                    ("Write Design for Innovus", lambda: self.send("write_design -innovus", report=True)), None,
                    ("Close GUI", self.root.destroy)])
        mk("Synthesis", [("Elaborate", lambda: self.send("elaborate")), ("Check Design", lambda: self.send("check_design", report=True)),
                         None, ("syn_generic", lambda: self.send("syn_generic")), ("syn_map", lambda: self.send("syn_map")),
                         ("syn_opt", lambda: self.send("syn_opt"))])
        mk("Constraints", [("Create Clock…", self.dlg_clock), ("Report Clocks", lambda: self.send("report_clocks", report=True))])
        mk("Report", [("Area", lambda: self.rep("report_area")), ("Gates", lambda: self.rep("report_gates")),
                      ("Timing", lambda: self.rep("report_timing")), ("QoR", lambda: self.rep("report_qor")),
                      ("Power", lambda: self.rep("report_power")), ("Hierarchy", lambda: self.rep("report_hierarchy"))])
        mk("View", [("Fit", self.fit), ("Zoom In", lambda: self.zoom(1.4)), ("Zoom Out", lambda: self.zoom(1 / 1.4)),
                    ("Clear Highlight", self.clear)])
        mk("Help", [("About", lambda: ReportWindow(self.root, self.fonts, "About",
                                                   f"RTLFlow Genus {VERSION}\nSchematic from the live Yosys netlist (write_json)."))])
        self.root.configure(menu=bar)

    def _toolbar(self):
        tb = ttk.Frame(self.root, padding=(6, 4))
        tb.pack(fill="x")
        for text, cmd, tip in (("Fit", self.fit, "Fit (F)"), ("Zoom +", lambda: self.zoom(1.4), "Z"),
                               ("Zoom -", lambda: self.zoom(1 / 1.4), "Shift+Z")):
            b = ttk.Button(tb, text=text, style="Tool.TButton", command=cmd)
            b.pack(side="left", padx=1)
            _Tip(b, tip)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=8)
        for text, cmd in (("Read Libs", self.dlg_libs), ("Read HDL", self.dlg_hdl), ("Clock", self.dlg_clock)):
            ttk.Button(tb, text=text, style="Tool.TButton", command=cmd).pack(side="left", padx=1)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(tb, text="Find").pack(side="left")
        self.find = ttk.Entry(tb, width=22)
        self.find.pack(side="left", padx=6)
        self.find.bind("<Return>", lambda e: self.do_find())
        self.flow = {}
        fr = ttk.Frame(tb)
        fr.pack(side="right")
        cmds = {"read": None, "elaborated": "elaborate", "generic": "syn_generic", "mapped": "syn_map", "optimized": "syn_opt"}
        for key, label in G_STAGES:
            b = ttk.Button(fr, text=label, style="Flow.TButton",
                           command=(self.dlg_hdl if key == "read" else (self.dlg_elab if key == "elaborated" else
                                    (lambda c=cmds[key]: self.send(c)))))
            b.pack(side="left", padx=1)
            self.flow[key] = b

    def _body(self):
        pw = ttk.PanedWindow(self.root, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.Frame(pw, style="Panel.TFrame", width=270)
        pw.add(left, weight=0)
        ttk.Label(left, text="DESIGN BROWSER", style="Dim.TLabel").pack(anchor="w", padx=10, pady=(8, 2))
        self.tree = ttk.Treeview(left, show="tree")
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.tree.bind("<<TreeviewSelect>>", self._tree_sel)
        mid = ttk.PanedWindow(pw, orient="vertical")
        pw.add(mid, weight=5)
        cf = tk.Frame(mid, bg="#050607")
        self.cv = tk.Canvas(cf, bg="#050607", highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        mid.add(cf, weight=4)
        self.nb = ttk.Notebook(mid)
        mid.add(self.nb, weight=2)
        self.tabs = {}
        for name in ("Summary", "Area", "Timing", "QoR", "Constraints", "Transcript"):
            f = ttk.Frame(self.nb, style="Panel.TFrame")
            t = T.text_widget(f, self.fonts, height=10)
            sb = ttk.Scrollbar(f, orient="vertical", command=t.yview)
            t.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            t.pack(fill="both", expand=True)
            self.nb.add(f, text=name)
            self.tabs[name] = t
        c = self.cv
        c.bind("<Configure>", lambda e: self.redraw())
        c.bind("<Button-1>", self._click)
        c.bind("<B1-Motion>", self._drag_move)
        c.bind("<ButtonRelease-1>", self._drag_end)
        c.bind("<Button-2>", self._pan0)
        c.bind("<B2-Motion>", self._drag_move)
        c.bind("<Button-4>", lambda e: self.zoom(1.2, e.x, e.y))
        c.bind("<Button-5>", lambda e: self.zoom(1 / 1.2, e.x, e.y))
        c.bind("<MouseWheel>", lambda e: self.zoom(1.2 if e.delta > 0 else 1 / 1.2, e.x, e.y))
        c.bind("<Motion>", self._motion)

    def _status(self):
        sb = ttk.Frame(self.root, style="Panel.TFrame")
        sb.pack(side="bottom", fill="x")
        self.st_l = ttk.Label(sb, text="", style="Status.TLabel")
        self.st_l.pack(side="left", fill="x", expand=True)
        self.st_busy = ttk.Label(sb, text="Ready", style="Status.TLabel", foreground=T.OK)
        self.st_busy.pack(side="right")
        self.st_stage = ttk.Label(sb, text="no design", style="Status.TLabel", foreground="#d2a8ff")
        self.st_stage.pack(side="right")

    # ───────────── IPC ─────────────
    def send(self, line, report=False, title=None):
        if self.chan:
            self.cmd_id += 1
            self.chan.send(t="cmd", line=line, id=self.cmd_id, report=report, title=title or line)

    def rep(self, cmd):
        self.send(cmd, report=True, title=cmd)

    def _poll(self):
        try:
            for _ in range(200):
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _handle(self, m):
        t = m.get("t")
        if t == "hello":
            self.top = m.get("top", "")
            self._stage(m.get("stage", "empty"))
        elif t == "stage":
            self.top = m.get("top", self.top)
            self._stage(m.get("stage"))
        elif t == "design":
            self.top = m.get("top", "")
            self._stage(m.get("stage", self.stage))
            if m.get("json") and os.path.exists(m["json"]):
                try:
                    data = json.load(open(m["json"]))
                    self.sch = Schematic(data, self.top, set(m.get("seq", [])))
                    self._fill_tree()
                    self.fit()
                except Exception as e:
                    self._log(f"**WARN: schematic build failed: {e}")
            self._summary(m)
            sdc = m.get("sdc", "")
            self._set_tab("Constraints", sdc or "(no constraints)")
        elif t == "report":
            title = m.get("title", "")
            tab = {"report_area": "Area", "report_gates": "Area", "report_timing": "Timing",
                   "report_qor": "QoR", "report_clocks": "Constraints"}.get(title.split()[0] if title else "")
            if tab:
                self._set_tab(tab, m.get("text", ""))
                self.nb.select(list(self.tabs).index(tab))
            else:
                ReportWindow(self.root, self.fonts, title, m.get("text", ""))
        elif t == "highlight":
            self.hi = m.get("insts", [])
            self.hi_label = m.get("label", "")
            self.redraw()
        elif t == "busy":
            self.busy = True
            self.st_busy.configure(text=f"Busy: {m.get('cmd', '')[:40]}", foreground=T.WARN)
            self._log(f"> {m.get('cmd', '')}", "cmd")
        elif t == "idle":
            self.busy = False
            self.st_busy.configure(text="Ready" if m.get("ok", True) else f"Failed: {m.get('cmd', '')[:40]}",
                                   foreground=T.OK if m.get("ok", True) else T.ERR)
        elif t == "result" and not m.get("ok"):
            self.st_l.configure(text=f"'{m.get('line')}' failed - see terminal", foreground=T.ERR)
        elif t == "raise":
            self.root.deiconify(); self.root.lift()
        elif t == "quit":
            self.root.after(10, self.root.destroy)

    def _stage(self, s):
        if not s:
            return
        self.stage = s
        self.st_stage.configure(text=f"{self.top or '-'} · {s}")
        r = G_RANK.get(s, 0)
        nxt = None
        for key, label in G_STAGES:
            b = self.flow[key]
            if r >= G_RANK[key]:
                b.configure(style="FlowDone.TButton")
            elif nxt is None:
                nxt = key
                b.configure(style="FlowNext.TButton")
            else:
                b.configure(style="Flow.TButton")

    def _log(self, text, tag=None):
        t = self.tabs["Transcript"]
        t.insert("end", text + "\n", (tag,) if tag else ())
        t.see("end")

    def _set_tab(self, name, text):
        t = self.tabs[name]
        t.configure(state="normal")
        t.delete("1.0", "end")
        for line in re.sub(r"\x1b\[[0-9;]*m", "", text).splitlines():
            tg = T.classify(line)
            t.insert("end", line + "\n", (tg,) if tg else ())

    def _summary(self, m):
        a = m.get("area") or {}
        tm = m.get("timing") or {}
        lines = [f"Design        {self.top}", f"Stage         {self.stage}",
                 f"Cells         {a.get('cells', '-')}   (sequential {a.get('seq', '-')})",
                 f"Area          {a.get('area', 0):.4f}" if a.get("area") else "Area          -",
                 f"WNS           {tm.get('wns'):.3f} ns" if tm.get("wns") is not None else "WNS           - (report_qor)"]
        if a.get("types"):
            lines.append("")
            lines.append("Cell usage")
            for tname, n in sorted(a["types"].items(), key=lambda kv: -kv[1]):
                lines.append(f"  {n:4d}  {tname}")
        self._set_tab("Summary", "\n".join(lines))

    def _fill_tree(self):
        tr = self.tree
        tr.delete(*tr.get_children())
        if not self.sch:
            return
        root = tr.insert("", "end", text=self.top, open=True)
        pi = tr.insert(root, "end", text="Ports", open=False)
        for n, nd in self.sch.nodes.items():
            if nd["kind"].startswith("port"):
                tr.insert(pi, "end", iid=n, text=f"{'>' if nd['kind'] == 'port_in' else '<'} {nd['name']}")
        by: dict = {}
        for n, nd in self.sch.nodes.items():
            if not nd["kind"].startswith("port"):
                by.setdefault(nd["type"], []).append(n)
        ci = tr.insert(root, "end", text=f"Instances ({sum(len(v) for v in by.values())})", open=True)
        for t, ns in sorted(by.items(), key=lambda kv: -len(kv[1])):
            ti = tr.insert(ci, "end", text=f"{t.split('__')[-1]}  ({len(ns)})")
            for n in sorted(ns):
                tr.insert(ti, "end", iid=n, text=n)

    def _tree_sel(self, e=None):
        s = self.tree.selection()
        if s and self.sch and s[0] in self.sch.nodes:
            self.sel = s[0]
            x, y = self.sch.pos[s[0]]
            self.cx, self.cy = x + CELL_W / 2, y + 20
            self.s = max(self.s, 1.2)
            self.redraw()

    def do_find(self):
        pat = self.find.get().strip()
        if not pat or not self.sch:
            return
        rx = re.compile(re.escape(pat).replace(r"\*", ".*"), re.I)
        hits = [n for n, nd in self.sch.nodes.items() if rx.search(nd["name"]) or rx.search(nd["type"])]
        self.hi = hits
        self.hi_label = f"find '{pat}': {len(hits)}"
        self.redraw()

    def dlg_libs(self):
        from tkinter import filedialog
        fns = filedialog.askopenfilenames(parent=self.root, title="Read timing libraries",
                                          initialdir=os.path.expanduser("~/.volare/sky130A/libs.ref"),
                                          filetypes=[("Liberty", "*.lib *.lib.gz"), ("All", "*")])
        if fns:
            self.send("read_libs " + " ".join("{" + f + "}" for f in fns))
        else:
            self.send("read_libs")          # PDK default

    def dlg_hdl(self):
        from tkinter import filedialog
        fns = filedialog.askopenfilenames(parent=self.root, title="Read HDL", initialdir=self.project,
                                          filetypes=[("SystemVerilog / Verilog", "*.sv *.v"), ("All", "*")])
        if fns:
            self.send("read_hdl -sv {" + " ".join(fns) + "}")

    def dlg_elab(self):
        Dialog(self.root, self.fonts, "Elaborate", [("top", "Top module (blank = auto)", "str", None, self.top or "")],
               lambda v: self.send("elaborate" + (f" {v['top']}" if v["top"] else "")))

    def dlg_clock(self):
        Dialog(self.root, self.fonts, "Create Clock", [("name", "Name", "str", None, "clk"),
                                                        ("period", "Period (ns)", "float", None, 10.0),
                                                        ("port", "Port", "str", None, "clk")],
               lambda v: self.send(f"create_clock -name {v['name']} -period {v['period']} [get_ports {v['port']}]"))

    # ───────────── view ─────────────
    def P(self, x, y):
        return (x - self.cx) * self.s + self.cv.winfo_width() / 2, (y - self.cy) * self.s + self.cv.winfo_height() / 2

    def Wd(self, X, Y):
        return (X - self.cv.winfo_width() / 2) / self.s + self.cx, (Y - self.cv.winfo_height() / 2) / self.s + self.cy

    def fit(self):
        if not self.sch:
            return self.redraw()
        W, H = max(self.cv.winfo_width(), 10), max(self.cv.winfo_height(), 10)
        self.s = 0.92 * min(W / max(self.sch.W, 1), H / max(self.sch.H, 1))
        self.cx, self.cy = self.sch.W / 2 - COL_W / 2 + CELL_W / 2, self.sch.H / 2
        self.redraw()

    def zoom(self, f, X=None, Y=None):
        if X is None:
            X, Y = self.cv.winfo_width() / 2, self.cv.winfo_height() / 2
        wx, wy = self.Wd(X, Y)
        self.s = min(max(self.s * f, 0.05), 20)
        self.cx = wx - (X - self.cv.winfo_width() / 2) / self.s
        self.cy = wy - (Y - self.cv.winfo_height() / 2) / self.s
        self.redraw()

    def clear(self):
        self.hi, self.hi_label, self.sel = [], "", None
        self.redraw()

    def redraw(self):
        c = self.cv
        c.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if not self.sch:
            c.create_text(W / 2, H / 2 - 10, text="No mapped design yet", fill="#8b929c",
                          font=(self.fonts["ui"][0], 15, "bold"))
            c.create_text(W / 2, H / 2 + 18, text="read_hdl  ->  elaborate  ->  syn_generic  ->  syn_map",
                          fill="#5c636d", font=self.fonts["mono"])
            return
        sch = self.sch
        s = self.s
        hi = set(self.hi)
        fs = max(6, min(12, int(9 * s)))
        # wires
        for b, (dn, dp) in sch.drv.items():
            sinks = sch.sinks.get(b, [])
            if not sinks or dn not in sch.pos:
                continue
            x1, y1 = sch.pin_xy(dn, dp)
            hot = dn in hi and any(sn in hi for sn, _ in sinks)
            col = HILITE if hot else "#3d6b99"
            for sn, sp in sinks:
                if sn not in sch.pos:
                    continue
                x2, y2 = sch.pin_xy(sn, sp)
                if x2 > x1:
                    xm = x1 + 18 + (b % 7) * 6
                    pts = [(x1, y1), (xm, y1), (xm, y2), (x2, y2)]
                else:   # feedback (flop Q back to logic): go around above
                    ytop = min(y1, y2) - 30 - (b % 5) * 6
                    pts = [(x1, y1), (x1 + 14, y1), (x1 + 14, ytop), (x2 - 14, ytop), (x2 - 14, y2), (x2, y2)]
                flat = [v for p in pts for v in self.P(*p)]
                c.create_line(*flat, fill=HILITE if (hot and sn in hi) else col, width=2 if hot else 1)
        # cells & ports
        for n, (x, y) in sch.pos.items():
            nd = sch.nodes[n]
            h = sch._h(n)
            X1, Y1 = self.P(x, y)
            if nd["kind"] in ("port_in", "port_out"):
                X2, Y2 = self.P(x + 70, y + 18)
                pts = ([X1, Y1, X2 - 8 * s, Y1, X2, (Y1 + Y2) / 2, X2 - 8 * s, Y2, X1, Y2] if nd["kind"] == "port_in"
                       else [X1, (Y1 + Y2) / 2, X1 + 8 * s, Y1, X2, Y1, X2, Y2, X1 + 8 * s, Y2])
                c.create_polygon(*pts, outline="#ffcc00", fill="#2a2208" if n not in hi else "#4a1440")
                c.create_text((X1 + X2) / 2, (Y1 + Y2) / 2, text=nd["name"], fill="#ffe28a",
                              font=(self.fonts["mono"][0], fs))
                continue
            o, f = COL.get(nd["kind"], COL["comb"])
            X2, Y2 = self.P(x + CELL_W, y + h)
            width = 3 if (n in hi or n == self.sel) else 1
            outline = HILITE if n in hi else ("#ffffff" if n == self.sel else o)
            if nd["kind"] == "seq":
                c.create_rectangle(X1, Y1, X2, Y2, outline=outline, fill=f, width=width)
                # clock triangle
                cy = Y2 - 8 * s
                c.create_line(X1, cy - 5 * s, X1 + 7 * s, cy, X1, cy + 5 * s, fill=o)
            else:
                c.create_rectangle(X1, Y1, X2, Y2, outline=outline, fill=f, width=width)
            if s > 0.35:
                c.create_text((X1 + X2) / 2, Y1 + 8 * s, text=nd["type"].split("__")[-1], fill="#e6edf3",
                              font=(self.fonts["mono"][0], fs, "bold"))
                c.create_text((X1 + X2) / 2, Y2 - 7 * s, text=nd["name"], fill="#8b949e",
                              font=(self.fonts["mono"][0], max(6, fs - 1)))
            if s > 0.8:
                for pin in nd["ins"]:
                    px, py = self.P(*sch.pin_xy(n, pin))
                    c.create_text(px + 3, py, text=pin, anchor="w", fill="#6e7681", font=(self.fonts["mono"][0], max(6, fs - 2)))
                for pin in nd["outs"]:
                    px, py = self.P(*sch.pin_xy(n, pin))
                    c.create_text(px - 3, py, text=pin, anchor="e", fill="#6e7681", font=(self.fonts["mono"][0], max(6, fs - 2)))
        if self.hi_label:
            c.create_text(12, 12, anchor="nw", text=self.hi_label, fill=HILITE, font=self.fonts["bold"])

    def _hit(self, X, Y):
        if not self.sch:
            return None
        x, y = self.Wd(X, Y)
        for n, (px, py) in self.sch.pos.items():
            w = 70 if self.sch.nodes[n]["kind"].startswith("port") else CELL_W
            if px <= x <= px + w and py <= y <= py + self.sch._h(n):
                return n
        return None

    def _click(self, e):
        self._drag = (e.x, e.y, self.cx, self.cy, False)

    def _pan0(self, e):
        self._drag = (e.x, e.y, self.cx, self.cy, True)

    def _drag_move(self, e):
        if not self._drag:
            return
        X0, Y0, cx0, cy0, moved = self._drag
        if abs(e.x - X0) + abs(e.y - Y0) > 4 or moved:
            self._drag = (X0, Y0, cx0, cy0, True)
            self.cx = cx0 - (e.x - X0) / self.s
            self.cy = cy0 - (e.y - Y0) / self.s
            self.redraw()

    def _drag_end(self, e):
        if self._drag and not self._drag[4]:
            n = self._hit(e.x, e.y)
            self.sel = n
            if n:
                nd = self.sch.nodes[n]
                if nd["kind"].startswith("port"):
                    self.st_l.configure(text=f"port {nd['name']}", foreground=T.FG)
                else:
                    conns = ", ".join(f"{p}={self.sch.netnames.get(b[0], b[0]) if b else '-'}"
                                      for p, b in nd["conn"].items())
                    self.st_l.configure(text=f"{n}  ({nd['type']})   {conns}", foreground=T.FG)
                try:
                    self.tree.see(n); self.tree.selection_set(n)
                except tk.TclError:
                    pass
            self.redraw()
        self._drag = None

    def _motion(self, e):
        n = self._hit(e.x, e.y)
        self.cv.configure(cursor="hand2" if n else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fd", type=int, default=-1)
    ap.add_argument("--project", default=os.getcwd())
    ap.add_argument("--json", default="")
    ap.add_argument("--top", default="")
    a = ap.parse_args(argv)
    chan = Channel(socket.socket(fileno=a.fd)) if a.fd >= 0 else None
    root = tk.Tk(className="RTLFlow-Genus")
    g = GenusGUI(root, chan, a.project)
    if a.json:
        root.after(300, lambda: g._handle({"t": "design", "json": a.json, "top": a.top, "stage": "mapped"}))
    root.mainloop()
    if chan:
        chan.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
