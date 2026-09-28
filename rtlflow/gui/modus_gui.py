"""Modus GUI — DFT/ATPG cockpit: coverage curve, fault browser, scan chain view."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme as T
from .base import Chart, ToolGUI, run

STATUS_COL = {"detected": "#7ee2a0", "chain": "#9ecbff", "redundant": "#8b929c", "aborted": "#e3b341",
              "undetected": "#f85149", "untested": "#f85149"}


class ModusGUI(ToolGUI):
    app = "modus"
    title = "RTLFlow Modus DFT & ATPG"
    prompt = "modus>"

    def build(self):
        self.curve, self.faults, self.chains, self.flops = [], [], [], []
        for text, cmd in (("Read Netlist", "read_netlist"), ("Build Model", "build_model"),
                          ("Verify DFT", "verify_test_structures")):
            self.tool_button(text, lambda c=cmd: self.send_cmd(c))
        self.tool_sep()
        self.tool_button("Insert Scan...", self.dlg_scan)
        self.tool_button("Fault Model", lambda: self.send_cmd("build_faultmodel"))
        self.tool_button("Run ATPG...", self.dlg_atpg, style="Accent.TButton")
        self.tool_sep()
        self.tool_button("Write Vectors", lambda: self.send_cmd("write_vectors"))
        pw = ttk.PanedWindow(self.center, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.Frame(pw, style="Panel.TFrame", padding=10)
        pw.add(left, weight=2)
        ttk.Label(left, text="COVERAGE", style="Dim.TLabel").pack(anchor="w")
        grid = ttk.Frame(left, style="Panel.TFrame")
        grid.pack(fill="x", pady=(4, 10))
        self.chips = {}
        for i, (k, lab) in enumerate((("coverage", "Fault cov."), ("test_coverage", "Test cov."), ("patterns", "Patterns"),
                                      ("total", "Faults"), ("redundant", "Redundant"), ("undetected", "Undetected"))):
            box = tk.Frame(grid, bg=T.BG3, padx=10, pady=6)
            box.grid(row=i // 3, column=i % 3, sticky="nsew", padx=3, pady=3)
            grid.columnconfigure(i % 3, weight=1)
            tk.Label(box, text=lab, bg=T.BG3, fg=T.FG_DIM, font=self.fonts["small"]).pack(anchor="w")
            v = tk.Label(box, text="—", bg=T.BG3, fg=T.FG, font=(self.fonts["ui"][0], 15, "bold"))
            v.pack(anchor="w")
            self.chips[k] = v
        ttk.Label(left, text="COVERAGE vs PATTERNS", style="Dim.TLabel").pack(anchor="w")
        self.cv = tk.Canvas(left, bg="#0d0f12", highlightthickness=0, height=T.px(260))
        self.cv.pack(fill="both", expand=True, pady=(4, 0))
        self.cv.bind("<Configure>", lambda e: self.draw_curve())
        self.model_lbl = ttk.Label(left, text="", style="Dim.TLabel", justify="left")
        self.model_lbl.pack(anchor="w", pady=(8, 0))
        right = ttk.Notebook(pw)
        pw.add(right, weight=4)
        ff = ttk.Frame(right, style="Panel.TFrame")
        right.add(ff, text="Fault browser")
        bar = ttk.Frame(ff, style="Panel.TFrame", padding=(6, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="Show", style="Panel.TLabel").pack(side="left")
        self.filt = tk.StringVar(value="not detected")
        cb = ttk.Combobox(bar, textvariable=self.filt, state="readonly", width=16,
                          values=["not detected", "all", "detected", "chain", "redundant", "aborted"])
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.fill_faults())
        self.count_lbl = ttk.Label(bar, text="", style="Dim.TLabel")
        self.count_lbl.pack(side="left", padx=10)
        cols = ("net", "fault", "status", "pattern")
        self.tree = ttk.Treeview(ff, columns=cols, show="headings")
        for c, (lab, w) in zip(cols, (("Net", 260), ("Fault", 70), ("Status", 110), ("Pattern", 80))):
            self.tree.heading(c, text=lab)
            self.tree.column(c, width=w, anchor="w" if c == "net" else "center")
        for st, col in STATUS_COL.items():
            self.tree.tag_configure(st, foreground=col)
        sb = ttk.Scrollbar(ff, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        sf = ttk.Frame(right, style="Panel.TFrame")
        right.add(sf, text="Scan chains")
        self.sc = tk.Canvas(sf, bg="#0d0f12", highlightthickness=0)
        ssb = ttk.Scrollbar(sf, orient="vertical", command=self.sc.yview)
        self.sc.configure(yscrollcommand=ssb.set)
        ssb.pack(side="right", fill="y")
        self.sc.pack(fill="both", expand=True)
        self.sc.bind("<Button-4>", lambda e: self.sc.yview_scroll(-3, "units"))
        self.sc.bind("<Button-5>", lambda e: self.sc.yview_scroll(3, "units"))
        self.sc.bind("<MouseWheel>", lambda e: self.sc.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.sc.bind("<Configure>", lambda e: self.draw_chains())

    def build_menus(self):
        self.menu("Model", [("Read netlist (genus)", lambda: self.send_cmd("read_netlist")),
                            ("Build model", lambda: self.send_cmd("build_model")),
                            ("Verify test structures", lambda: self.send_cmd("verify_test_structures", report=True)),
                            ("Status", lambda: self.send_cmd("status", report=True))])
        self.menu("DFT", [("Connect scan chains...", self.dlg_scan),
                          ("Report scan chains", lambda: self.send_cmd("report_scan_chains", report=True))])
        self.menu("ATPG", [("Build fault model", lambda: self.send_cmd("build_faultmodel")),
                           ("Create logic tests...", self.dlg_atpg),
                           ("Fault statistics", lambda: self.send_cmd("report_fault_statistics", report=True)),
                           ("Write vectors", lambda: self.send_cmd("write_vectors"))])

    def dlg_scan(self):
        self.dialog("Connect Scan Chains", [("n", "Number of chains", "int", None, 1),
                                            ("order", "Chain order", "radio", ["name", "placement"], "name")],
                    lambda v: self.send_cmd(f"connect_scan_chains -chains {v['n']} -order {v['order']}"),
                    note="Swaps each flip-flop for its scan equivalent from the liberty (e.g. dfxtp → sdfxtp) and "
                         "stitches scan_in → … → scan_out. 'placement' orders by the innovus layout.")

    def dlg_atpg(self):
        self.dialog("Create Logic Tests", [("r", "Max random patterns", "int", None, 4096),
                                           ("bt", "PODEM backtrack limit / fault", "int", None, 200),
                                           ("t", "Deterministic time budget (s)", "int", None, 60),
                                           ("podem", "Deterministic top-up (PODEM)", "bool", None, True)],
                    lambda v: self.send_cmd(f"create_logic_tests -max_random {v['r']} -backtracks {v['bt']} -time {v['t']}"
                                            + ("" if v["podem"] else " -no_podem")))

    def on_message(self, m):
        t = m.get("t")
        if t == "model":
            self.chains = m.get("chains") or []
            self.flops = m.get("flops") or []
            s = m.get("stats") or {}
            self.set_stage(f"{m.get('top') or '-'} · {'scan inserted' if m.get('scan') else 'no scan'}")
            self.model_lbl.configure(text=(f"Model: {s.get('cells', 0)} gates · {s.get('flops', 0)} flops · "
                                           f"{s.get('pis', 0)} PIs · {s.get('pos', 0)} POs") if m.get("built") else "No model yet")
            self.draw_chains()
        elif t == "faults":
            self.faults = m.get("faults", [])
            self.curve = m.get("curve", [])
            s = m.get("stats", {})
            s["patterns"] = self.curve[-1][0] if self.curve else 0
            for k, lab in self.chips.items():
                v = s.get(k)
                if v is None:
                    lab.configure(text="—")
                elif k in ("coverage", "test_coverage"):
                    lab.configure(text=f"{v:.2f}%", fg=T.OK if v >= 95 else (T.WARN if v >= 80 else T.ERR))
                else:
                    lab.configure(text=str(v), fg=T.ERR if k == "undetected" and v else T.FG)
            self.fill_faults()
            self.draw_curve()
        elif t == "progress":
            tot = m.get("total") or 1
            self.flash(f"ATPG: {m['patterns']} random patterns simulated · {100 * m['detected'] / tot:.1f}% detected", T.FG_DIM)

    def fill_faults(self):
        self.tree.delete(*self.tree.get_children())
        f = self.filt.get()
        rows = [x for x in self.faults if f == "all" or (f == "not detected" and x[2] not in ("detected", "chain")) or x[2] == f]
        for i, (net, sv, st, pat) in enumerate(rows[:5000]):
            self.tree.insert("", "end", values=(net, f"SA{sv}", st, pat if pat >= 0 else ""), tags=(st,))
        self.count_lbl.configure(text=f"{len(rows)} fault(s)")

    def draw_curve(self):
        cv = self.cv
        if not self.curve:
            return Chart.empty(cv, "Run ATPG to see coverage growth")
        x0, y0, x1, y1 = Chart.frame(cv, pad=(46, 14, 16, 30))
        Chart.axes(cv, x0, y0, x1, y1, 0, 100, "{:.0f}%", font=self.fonts["small"])
        nmax = max(p for p, _ in self.curve) or 1
        pts = [(x0, y1)] + [(x0 + (x1 - x0) * p / nmax, y1 - (y1 - y0) * c / 100) for p, c in self.curve]
        cv.create_line(*[v for p in pts for v in p], fill="#4c9dff", width=2)
        for (x, y) in pts[1:]:
            cv.create_oval(x - 2, y - 2, x + 2, y + 2, fill="#4c9dff", outline="")
        P = T.px
        cv.create_text(x1, y1 + P(14), text=f"{nmax} patterns", anchor="e", fill=T.FG_DIM, font=self.fonts["small"])
        cv.create_text(x0, y1 + P(14), text="0", anchor="w", fill=T.FG_DIM, font=self.fonts["small"])

    def draw_chains(self):
        cv = self.sc
        cv.delete("all")
        P = T.px
        W = max(cv.winfo_width(), 200)
        if not self.chains:
            txt = "No scan chains yet — DFT → Connect scan chains"
            if self.flops:
                txt += f"\n({len(self.flops)} flip-flops in the model)"
            cv.create_text(W / 2, P(60), text=txt, fill=T.FG_DIM, justify="center", font=self.fonts["ui"])
            return
        import tkinter.font as tkfont
        sm = tkfont.Font(font=self.fonts["small"])
        mono = tkfont.Font(font=self.fonts["mono"])
        cells = [c for ch in self.chains for c in ch["cells"]]
        # box size from the real text size, so nothing is clipped at any DPI
        bw = max([P(106)] + [sm.measure(c["inst"]) + P(12) for c in cells] +
                 [sm.measure(c["type"].split("__")[-1]) + P(12) for c in cells])
        lh = sm.metrics("linespace")
        bh = 2 * lh + P(8)
        gap = P(24)
        y = P(30)
        for ch in self.chains:
            cv.create_text(P(16), y, text=ch["name"], anchor="w", fill=T.FG, font=self.fonts["bold"])
            y += T.LINE_PX + P(10)
            x = P(16)
            cv.create_text(x, y + bh / 2, text=ch["scan_in"], anchor="w", fill="#ffcc00", font=self.fonts["mono"])
            x0 = x + mono.measure(ch["scan_in"]) + gap + P(8)
            x = x0
            for c in ch["cells"]:
                if x + bw + gap > W:
                    x = x0
                    y += bh + P(26)
                cv.create_line(x - gap, y + bh / 2, x, y + bh / 2, fill="#6fa8ff", arrow="last")
                cv.create_rectangle(x, y, x + bw, y + bh, outline="#d38cff", fill="#26123a")
                cv.create_text(x + bw / 2, y + P(4) + lh / 2, text=c["inst"], fill=T.FG, font=self.fonts["small"])
                cv.create_text(x + bw / 2, y + P(4) + lh * 1.5, text=c["type"].split("__")[-1], fill=T.FG_DIM, font=self.fonts["small"])
                x += bw + gap
            cv.create_line(x - gap, y + bh / 2, x, y + bh / 2, fill="#6fa8ff", arrow="last")
            cv.create_text(x + P(4), y + bh / 2, text=ch["scan_out"], anchor="w", fill="#ffcc00", font=self.fonts["mono"])
            y += bh + P(40)
        cv.configure(scrollregion=(0, 0, W, y))


def main(argv=None):
    return run(ModusGUI, argv)


if __name__ == "__main__":
    raise SystemExit(main())
