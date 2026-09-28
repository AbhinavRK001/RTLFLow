"""Tempus GUI — slack histogram, timing path browser and per-stage delay view,
all from the live OpenSTA session of the tempus shell."""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, ttk

from . import theme as T
from .base import Chart, ToolGUI, run


class TempusGUI(ToolGUI):
    app = "tempus"
    title = "RTLFlow Tempus Timing Signoff"
    prompt = "tempus>"

    def build(self):
        self.mode = tk.StringVar(value="setup")
        self.paths: list[dict] = []
        self.endpoints: list[dict] = []
        self.summary: dict = {}
        # toolbar
        self.tool_button("Refresh", self.refresh, "Re-query OpenSTA")
        self.tool_sep()
        for v, lab in (("setup", "Setup"), ("hold", "Hold")):
            ttk.Radiobutton(self.toolbar, text=lab, value=v, variable=self.mode, style="View.TRadiobutton",
                            command=self.refresh).pack(side="left")
        self.tool_sep()
        self.tool_button("Load from Genus", lambda: self.send_cmd("read_design -from genus"))
        self.tool_button("Load from Innovus", lambda: self.send_cmd("read_design -from innovus"))
        ttk.Label(self.toolbar, text="paths").pack(side="right")
        self.npaths = tk.StringVar(value="50")
        ttk.Spinbox(self.toolbar, from_=1, to=2000, textvariable=self.npaths, width=6).pack(side="right", padx=6)
        # body
        pw = ttk.PanedWindow(self.center, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.Frame(pw, style="Panel.TFrame", padding=10)
        pw.add(left, weight=2)
        ttk.Label(left, text="TIMING SUMMARY", style="Dim.TLabel").pack(anchor="w")
        grid = ttk.Frame(left, style="Panel.TFrame")
        grid.pack(fill="x", pady=(4, 10))
        self.chips = {}
        for i, (k, lab) in enumerate((("wns", "WNS (ns)"), ("tns", "TNS (ns)"), ("viol", "Violating"),
                                      ("whs", "WHS (ns)"), ("tns_hold", "Hold TNS"), ("clocks", "Clocks"))):
            box = tk.Frame(grid, bg=T.BG3, padx=10, pady=6)
            box.grid(row=i // 3, column=i % 3, sticky="nsew", padx=3, pady=3)
            grid.columnconfigure(i % 3, weight=1)
            tk.Label(box, text=lab, bg=T.BG3, fg=T.FG_DIM, font=self.fonts["small"]).pack(anchor="w")
            v = tk.Label(box, text="—", bg=T.BG3, fg=T.FG, font=(self.fonts["ui"][0], 15, "bold"))
            v.pack(anchor="w")
            self.chips[k] = v
        ttk.Label(left, text="ENDPOINT SLACK HISTOGRAM", style="Dim.TLabel").pack(anchor="w")
        self.hist = tk.Canvas(left, bg="#0d0f12", highlightthickness=0, height=T.px(260))
        self.hist.pack(fill="both", expand=True, pady=(4, 0))
        self.hist.bind("<Configure>", lambda e: self.draw_hist())
        right = ttk.PanedWindow(pw, orient="vertical")
        pw.add(right, weight=5)
        tf = ttk.Frame(right)
        right.add(tf, weight=3)
        cols = ("slack", "end", "start", "group", "stages")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="browse")
        for c, (lab, w) in zip(cols, (("Slack", 80), ("Endpoint", 280), ("Startpoint", 280), ("Group", 100), ("Stages", 60))):
            self.tree.heading(c, text=lab)
            self.tree.column(c, width=w, anchor="e" if c in ("slack", "stages") else "w")
        self.tree.tag_configure("viol", foreground=T.ERR)
        self.tree.tag_configure("met", foreground="#7ee2a0")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_path)
        df = ttk.Frame(right, style="Panel.TFrame")
        right.add(df, weight=2)
        ttk.Label(df, text="  PATH STAGE DELAYS  (arrival time, per-stage increment)", style="Dim.TLabel").pack(anchor="w", pady=(4, 0))
        self.wf = tk.Canvas(df, bg="#0d0f12", highlightthickness=0, height=T.px(220))
        self.wf.pack(fill="both", expand=True)
        self.wf.bind("<Configure>", lambda e: self.draw_path())
        self.rep = self.text_tab("Path report")
        self.bottom.select(0)
        self.sel = None

    def build_menus(self):
        self.menu("Design", [("Load from Genus", lambda: self.send_cmd("read_design -from genus")),
                             ("Load from Innovus", lambda: self.send_cmd("read_design -from innovus")),
                             ("Read SPEF...", self.read_spef), None,
                             ("Status", lambda: self.send_cmd("status", report=True))])
        self.menu("Analysis", [("Refresh", self.refresh),
                               ("Analysis summary", lambda: self.send_cmd("report_analysis_summary", report=True)),
                               ("Check timing", lambda: self.send_cmd("check_timing", report=True)),
                               ("Constraint violations", lambda: self.send_cmd("report_constraints -all_violators", report=True)),
                               ("Clocks", lambda: self.send_cmd("report_clocks", report=True)),
                               ("Power", lambda: self.send_cmd("report_power", report=True))])
        self.menu("Reports", [("Report timing...", self.dlg_timing),
                              ("Slack histogram (text)", lambda: self.send_cmd("report_slack_histogram", report=True))])

    def read_spef(self):
        fn = filedialog.askopenfilename(parent=self.root, filetypes=[("SPEF", "*.spef *.spef.gz"), ("All", "*")])
        if fn:
            self.send_cmd(f"read_spef {{{fn}}}")

    def dlg_timing(self):
        self.dialog("Report Timing", [("n", "Max paths", "int", None, 5), ("from", "From (blank = any)", "str", None, ""),
                                      ("to", "To (blank = any)", "str", None, ""),
                                      ("mode", "Check", "radio", ["Setup (late)", "Hold (early)"], "Setup (late)")],
                    lambda v: self.send_cmd(f"report_timing -max_paths {v['n']}" + (f" -from {v['from']}" if v["from"] else "")
                                            + (f" -to {v['to']}" if v["to"] else "") + (" -early" if v["mode"].startswith("Hold") else " -late"),
                                            report=True, title="Timing report"))

    def refresh(self):
        try:
            n = int(self.npaths.get())
        except ValueError:
            n = 50
        self.flash("Querying OpenSTA ...", T.FG_DIM)
        self.request(t="refresh", mode=self.mode.get(), n=n)

    # ─────────── data ───────────
    def on_message(self, m):
        t = m.get("t")
        if t == "hello":
            self.refresh()
        elif t == "state":
            s = m["state"]
            self.set_stage(f"{s.get('top') or '-'} · {'SPEF' if s.get('spef') else 'no SPEF'} · {s.get('source') or 'manual'}")
        elif t == "summary":
            self.show_summary(m.get("summary", {}))
        elif t == "timing":
            if m.get("error"):
                self.flash(m["error"], T.WARN)
            self.show_summary(m.get("summary", {}))
            self.endpoints = m.get("endpoints", [])
            self.paths = m.get("paths", [])
            self.tree.delete(*self.tree.get_children())
            for i, p in enumerate(self.paths):
                sl = p.get("slack")
                self.tree.insert("", "end", iid=str(i), values=(f"{sl:+.3f}" if sl is not None else "?", p.get("end"),
                                                               p.get("start"), p.get("group"), len(p.get("points", []))),
                                 tags=("viol" if sl is not None and sl < 0 else "met",))
            self.draw_hist()
            if self.paths:
                self.tree.selection_set("0")
            else:
                self.sel = None
                self.draw_path()
            self.flash(f"{len(self.endpoints)} endpoints · {len(self.paths)} paths ({m.get('mode')})", T.OK)
        elif t == "highlight":
            p = m.get("path") or {}
            self.set_text(self.rep, p.get("text", ""))

    def show_summary(self, s):
        self.summary = s
        for k, lab in self.chips.items():
            v = s.get(k)
            if v is None:
                lab.configure(text="—", fg=T.FG)
            elif isinstance(v, float):
                lab.configure(text=f"{v:+.3f}" if k != "viol" else f"{int(v)}",
                              fg=T.ERR if v < 0 or (k.startswith("viol") and v > 0) else T.OK)
            else:
                lab.configure(text=str(v), fg=T.ERR if k.startswith("viol") and v else T.FG)

    def draw_hist(self):
        cv = self.hist
        sl = [e["slack"] for e in self.endpoints if e.get("slack") is not None]
        if not sl:
            return Chart.empty(cv, "No endpoint data — load a design and Refresh")
        x0, y0, x1, y1 = Chart.frame(cv)
        lo, hi = min(sl + [0.0]), max(sl + [0.0])
        if hi - lo < 1e-9:
            hi = lo + 1
        bins = 16
        w = (hi - lo) / bins
        counts = [0] * bins
        for s in sl:
            counts[min(bins - 1, int((s - lo) / w))] += 1
        mx = max(counts) or 1
        Chart.axes(cv, x0, y0, x1, y1, 0, mx, "{:.0f}", font=self.fonts["small"])
        bw = (x1 - x0) / bins
        for i, c in enumerate(counts):
            a = lo + i * w
            col = T.ERR if a + w <= 0 else ("#e3b341" if a < 0 else "#3fb950")
            h = (y1 - y0) * c / mx
            cv.create_rectangle(x0 + i * bw + 1, y1 - h, x0 + (i + 1) * bw - 1, y1, fill=col, outline="")
        for frac in (0, 0.5, 1):
            v = lo + (hi - lo) * frac
            cv.create_text(x0 + (x1 - x0) * frac, y1 + T.px(12), text=f"{v:.2f}", fill=T.FG_DIM, font=self.fonts["small"])
        if lo < 0 < hi:
            zx = x0 + (x1 - x0) * (0 - lo) / (hi - lo)
            cv.create_line(zx, y0, zx, y1, fill="#ffffff", dash=(3, 3))
        cv.create_text(x1, y0 - T.px(6), text=f"{len(sl)} endpoints · slack (ns)", anchor="e", fill=T.FG_DIM, font=self.fonts["small"])

    def on_path(self, e=None):
        s = self.tree.selection()
        if not s:
            return
        self.sel = self.paths[int(s[0])]
        self.set_text(self.rep, self.sel.get("text", ""))
        self.draw_path()

    def draw_path(self):
        cv = self.wf
        p = self.sel
        if not p or not p.get("points"):
            return Chart.empty(cv, "Select a path")
        pts = [q for q in p["points"]]
        import tkinter.font as tkfont
        lf = tkfont.Font(font=(self.fonts["mono"][0], 9))
        labels = [f"{q['pin']} ({q['cell'].split('__')[-1]})" for q in pts]
        lw = max(lf.measure(t) for t in labels) + T.px(16)
        x0, y0, x1, y1 = Chart.frame(cv, pad=(10, 10, 60, 14))
        x0 = min(max(x0, lw), (x1 - x0) * 0.6)
        tmax = max(q["time"] for q in pts) or 1.0
        n = len(pts)
        lh = lf.metrics("linespace") + T.px(4)
        rh = max(lh, min(T.px(22), (y1 - y0) / max(n, 1)))
        for i, q in enumerate(pts):
            y = y0 + i * rh
            start = q["time"] - q["incr"]
            X1 = x0 + (x1 - x0) * max(0.0, start) / tmax
            X2 = x0 + (x1 - x0) * q["time"] / tmax
            is_net = abs(q["incr"]) < 1e-9
            col = "#d38cff" if "df" in q["cell"].lower() else ("#ffd23f" if "clk" in q["cell"].lower() else T.ACCENT)
            cv.create_text(x0 - T.px(8), y + rh / 2, text=labels[i], anchor="e",
                           fill=T.FG, font=lf)
            cv.create_rectangle(X1, y + 2, max(X2, X1 + 2), y + rh - 2, fill=col if not is_net else "#3a3f46", outline="")
            cv.create_text(max(X2, X1 + 2) + T.px(4), y + rh / 2, text=f"+{q['incr']:.3f}", anchor="w", fill=T.FG_DIM,
                           font=self.fonts["small"])
        sl = p.get("slack")
        cv.configure(scrollregion=(0, 0, cv.winfo_width(), max(cv.winfo_height(), y0 + n * rh + T.px(14))))
        cv.create_text(x1 + T.px(50), y0, anchor="ne", text=f"slack {sl:+.3f} ns" if sl is not None else "",
                       fill=T.ERR if (sl or 0) < 0 else T.OK, font=self.fonts["bold"])

    def on_report(self, m):
        return False


def main(argv=None):
    return run(TempusGUI, argv)


if __name__ == "__main__":
    raise SystemExit(main())
