"""Voltus GUI — power breakdown, per-instance power map and IR-drop map on the real layout."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme as T
from .base import Chart, ToolGUI, heat, run
from .layoutview import LayoutCanvas

COMP = [("internal", "#4c9dff"), ("switching", "#ffb000"), ("leakage", "#d38cff")]


class VoltusGUI(ToolGUI):
    app = "voltus"
    title = "RTLFlow Voltus Power Integrity"
    prompt = "voltus>"

    def build(self):
        self.power: dict = {}
        self.inst: dict = {}
        self.volt: dict = {}
        self.ir: dict = {}
        self.tool_button("Load from Innovus", lambda: self.send_cmd("read_design -from innovus"))
        self.tool_sep()
        self.tool_button("Activity...", self.dlg_activity)
        self.tool_button("Report Power", lambda: self.send_cmd("report_power -instances"))
        self.tool_button("Analyze Rail...", self.dlg_rail)
        pw = ttk.PanedWindow(self.center, orient="horizontal")
        pw.pack(fill="both", expand=True)
        self.nb = ttk.Notebook(pw)
        pw.add(self.nb, weight=5)
        f1 = ttk.Frame(self.nb, style="Panel.TFrame")
        self.bars = tk.Canvas(f1, bg="#0d0f12", highlightthickness=0)
        self.bars.pack(fill="both", expand=True)
        self.bars.bind("<Configure>", lambda e: self.draw_bars())
        self.nb.add(f1, text="Power summary")
        self.pmap = LayoutCanvas(self.nb, self.fonts)
        self.pmap.empty_text = "Load a design, then Report Power (per-instance)"
        self.pmap.dim_cells = True
        self.pmap.show_routes = False
        self.pmap.on_hover = lambda x, y: self._hover(self.pmap, x, y, "power")
        self.nb.add(self.pmap, text="Power map")
        self.imap = LayoutCanvas(self.nb, self.fonts)
        self.imap.empty_text = "Load a design, then Analyze Rail (needs a power grid)"
        self.imap.dim_cells = True
        self.imap.show_routes = False
        self.imap.on_hover = lambda x, y: self._hover(self.imap, x, y, "ir")
        self.nb.add(self.imap, text="IR drop map")
        side = ttk.Frame(pw, style="Panel.TFrame", padding=10, width=300)
        pw.add(side, weight=0)
        ttk.Label(side, text="LEGEND", style="Dim.TLabel").pack(anchor="w")
        self.legend = tk.Canvas(side, height=46, bg=T.BG2, highlightthickness=0)
        self.legend.pack(fill="x", pady=(2, 10))
        ttk.Label(side, text="SUMMARY", style="Dim.TLabel").pack(anchor="w")
        self.summ = ttk.Label(side, text="—", style="Panel.TLabel", justify="left", font=self.fonts["mono"])
        self.summ.pack(anchor="w", pady=(2, 10))
        ttk.Label(side, text="TOP INSTANCES", style="Dim.TLabel").pack(anchor="w")
        self.top = ttk.Treeview(side, columns=("v",), show="tree headings", height=14)
        self.top.heading("#0", text="Instance")
        self.top.heading("v", text="Value")
        self.top.column("#0", width=150)
        self.top.column("v", width=100, anchor="e")
        self.top.pack(fill="both", expand=True)
        self.top.bind("<<TreeviewSelect>>", self._pick)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._side())

    def build_menus(self):
        self.menu("Design", [("Load from Innovus", lambda: self.send_cmd("read_design -from innovus")),
                             ("Status", lambda: self.send_cmd("status", report=True))])
        self.menu("Power", [("Set activity...", self.dlg_activity),
                            ("Read activity from last simulation (VCD)", lambda: self.send_cmd("read_activity_file")),
                            ("Report power", lambda: self.send_cmd("report_power", report=True)),
                            ("Report power + power map", lambda: self.send_cmd("report_power -instances"))])
        self.menu("Rail", [("Analyze VPWR...", self.dlg_rail),
                           ("Analyze VGND", lambda: self.send_cmd("analyze_rail -net VGND -voltage 0"))])

    def dlg_activity(self):
        self.dialog("Power Activity", [("a", "Global activity (toggles / clock)", "float", None, 0.1),
                                       ("duty", "Duty", "float", None, 0.5)],
                    lambda v: self.send_cmd(f"set_power_activity -global {v['a']} -duty {v['duty']}"))

    def dlg_rail(self):
        self.dialog("Analyze Rail", [("net", "Net", "str", None, "VPWR"), ("v", "Supply voltage (V)", "float", None, 1.8),
                                     ("src", "Source type", "radio", ["default", "FULL", "STRAPS", "BUMPS"], "default")],
                    lambda v: self.send_cmd(f"analyze_rail -net {v['net']} -voltage {v['v']}"
                                            + ("" if v["src"] == "default" else f" -source_type {v['src']}")),
                    note="Static IR drop with OpenROAD PDNSim. Requires a power grid (innovus: globalNetConnect + sroute).")

    def on_message(self, m):
        t = m.get("t")
        if t == "design":
            self.set_stage(f"{m.get('top') or '-'} · {m.get('source') or 'not loaded'}")
            if m.get("def_file"):
                for cv in (self.pmap, self.imap):
                    cv.load(m["def_file"], m.get("lefs", []))
        elif t == "power":
            self.power = m.get("power", {})
            self.inst = m.get("inst", {})
            self.draw_bars()
            self._colour(self.pmap, self.inst, lambda v, lo, hi: (v - lo) / (hi - lo or 1))
            self._side()
        elif t == "ir":
            self.ir = m.get("ir", {})
            self.volt = m.get("volt", {})
            sup = self.ir.get("supply", 1.8)
            drops = {k: abs(sup - v) for k, v in self.volt.items()}
            self._colour(self.imap, drops, lambda v, lo, hi: (v - lo) / (hi - lo or 1))
            self.nb.select(2)
            self._side()

    def _colour(self, cv, vals, norm):
        cv.cell_fill = {}
        if vals:
            lo, hi = min(vals.values()), max(vals.values())
            cv.cell_fill = {k: heat(norm(v, lo, hi)) for k, v in vals.items()}
        cv.schedule()

    def _side(self):
        tab = self.nb.index("current")
        self.top.delete(*self.top.get_children())
        lg = self.legend
        lg.delete("all")
        w = max(lg.winfo_width(), 200)
        for i in range(50):
            lg.create_rectangle(i * w / 50, 4, (i + 1) * w / 50, 22, fill=heat(i / 49), outline="")
        if tab == 2 and self.volt:
            sup = self.ir.get("supply", 1.8)
            drops = {k: abs(sup - v) for k, v in self.volt.items()}
            lo, hi = min(drops.values()), max(drops.values())
            lg.create_text(2, 36, text=f"{lo * 1000:.2f} mV", anchor="w", fill=T.FG_DIM, font=self.fonts["small"])
            lg.create_text(w - 2, 36, text=f"{hi * 1000:.2f} mV drop", anchor="e", fill=T.FG_DIM, font=self.fonts["small"])
            for k, v in sorted(drops.items(), key=lambda kv: -kv[1])[:40]:
                self.top.insert("", "end", iid=k, text=k, values=(f"{v * 1000:.3f} mV",))
            ir = self.ir
            self.summ.configure(text=f"net      {ir.get('net')}\nsupply   {sup:.3f} V\n"
                                     f"worst    {ir.get('worst_drop', max(drops.values())) * 1000:.3f} mV\n"
                                     f"average  {ir.get('avg_drop', sum(drops.values()) / len(drops)) * 1000:.3f} mV")
        elif self.inst:
            lo, hi = min(self.inst.values()), max(self.inst.values())
            lg.create_text(2, 36, text=f"{lo * 1e6:.3f} µW", anchor="w", fill=T.FG_DIM, font=self.fonts["small"])
            lg.create_text(w - 2, 36, text=f"{hi * 1e6:.3f} µW", anchor="e", fill=T.FG_DIM, font=self.fonts["small"])
            for k, v in sorted(self.inst.items(), key=lambda kv: -kv[1])[:40]:
                self.top.insert("", "end", iid=k, text=k, values=(f"{v * 1e6:.3f} µW",))
        if tab != 2 and self.power.get("total"):
            p = self.power
            self.summ.configure(text=f"total      {p['total'] * 1e3:.5f} mW\ninternal   {p.get('internal', 0) * 1e3:.5f} mW\n"
                                     f"switching  {p.get('switching', 0) * 1e3:.5f} mW\nleakage    {p.get('leakage', 0) * 1e3:.5f} mW")

    def _pick(self, e=None):
        s = self.top.selection()
        if not s:
            return
        cv = self.imap if self.nb.index("current") == 2 else self.pmap
        b = cv.cell_box(s[0])
        if b:
            cv.markers = [(b[0], b[1], b[2], b[3], s[0], "#ffffff")]
            cv.hi_marker = 0
            cv.zoom_to(*b, margin=6)

    def _hover(self, cv, x, y, kind):
        n = cv.inst_at(x, y)
        if not n:
            return
        if kind == "power" and n in self.inst:
            self.st_msg.configure(text=f"{n}: {self.inst[n] * 1e6:.3f} µW", foreground=T.FG)
        elif kind == "ir" and n in self.volt:
            self.st_msg.configure(text=f"{n}: {self.volt[n]:.5f} V", foreground=T.FG)

    def draw_bars(self):
        cv = self.bars
        g = self.power.get("groups", {})
        if not g:
            return Chart.empty(cv, "No power data — Report Power")
        x0, y0, x1, y1 = Chart.frame(cv, pad=(70, 40, 30, 40))
        mx = max(v["total"] for v in g.values()) or 1
        Chart.axes(cv, x0, y0, x1, y1, 0, mx * 1e6, "{:.2f}", font=self.fonts["small"])
        n = len(g)
        bw = (x1 - x0) / max(n, 1)
        for i, (name, v) in enumerate(g.items()):
            base = y1
            for comp, col in COMP:
                h = (y1 - y0) * v[comp] / mx
                cv.create_rectangle(x0 + i * bw + bw * 0.2, base - h, x0 + (i + 1) * bw - bw * 0.2, base, fill=col, outline="")
                base -= h
            cv.create_text(x0 + (i + 0.5) * bw, y1 + 14, text=name, fill=T.FG, font=self.fonts["small"])
            cv.create_text(x0 + (i + 0.5) * bw, base - 10, text=f"{v['total'] * 1e6:.2f}", fill=T.FG_DIM, font=self.fonts["small"])
        cv.create_text(x0, y0 - 24, text="Power by group (µW)", anchor="w", fill=T.FG, font=self.fonts["bold"])
        lx = x1
        for comp, col in reversed(COMP):
            lx -= 90
            cv.create_rectangle(lx, y0 - 30, lx + 12, y0 - 18, fill=col, outline="")
            cv.create_text(lx + 16, y0 - 24, text=comp, anchor="w", fill=T.FG_DIM, font=self.fonts["small"])


def main(argv=None):
    return run(VoltusGUI, argv)


if __name__ == "__main__":
    raise SystemExit(main())
