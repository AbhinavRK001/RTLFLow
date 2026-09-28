"""VCD reader + waveform viewer widget (SimVision-style) for the nclaunch GUI."""

from __future__ import annotations

import bisect
import re
import tkinter as tk
from tkinter import ttk

from . import theme as T


class VCD:
    def __init__(self, path: str, max_bytes: int = 200_000_000):
        self.timescale = "1ns"
        self.signals: dict[str, dict] = {}      # id -> {name, scope, width, changes[(t, val)]}
        self.order: list[str] = []              # full names in declaration order
        self.byname: dict[str, str] = {}
        self.tmax = 0
        scope: list[str] = []
        with open(path, errors="replace") as f:
            data = f.read(max_bytes)
        toks = iter(data.split())
        t = 0
        header = True
        for tok in toks:
            if header:
                if tok == "$scope":
                    next(toks)
                    scope.append(next(toks))
                elif tok == "$upscope":
                    if scope:
                        scope.pop()
                elif tok == "$var":
                    _kind, width, ident, name = next(toks), next(toks), next(toks), next(toks)
                    nxt = next(toks)
                    if nxt != "$end":
                        name += nxt
                        while next(toks) != "$end":
                            pass
                    if int(width) > 1 and "[" in name:
                        name = name.split("[")[0]
                    full = ".".join(scope + [name])
                    if ident not in self.signals:
                        self.signals[ident] = {"name": name, "scope": ".".join(scope), "width": int(width), "changes": []}
                    self.order.append(full)
                    self.byname[full] = ident
                elif tok == "$timescale":
                    ts = []
                    for x in toks:
                        if x == "$end":
                            break
                        ts.append(x)
                    self.timescale = "".join(ts)
                elif tok == "$enddefinitions":
                    header = False
                continue
            c = tok[0]
            if c == "#":
                try:
                    t = int(tok[1:])
                except ValueError:
                    pass
                self.tmax = max(self.tmax, t)
            elif c in "01xXzZ" and len(tok) > 1:
                s = self.signals.get(tok[1:])
                if s is not None:
                    s["changes"].append((t, c.lower()))
            elif c in "bBrR":
                val = tok[1:]
                ident = next(toks, "")
                s = self.signals.get(ident)
                if s is not None:
                    s["changes"].append((t, val.lower()))
            # $dumpvars / $end / comments ignored
        for s in self.signals.values():
            s["times"] = [c[0] for c in s["changes"]]

    def value_at(self, ident: str, t: int) -> str:
        s = self.signals[ident]
        i = bisect.bisect_right(s["times"], t) - 1
        return s["changes"][i][1] if i >= 0 else "x"


def fmt_bus(v: str, width: int, radix: str = "hex") -> str:
    if any(ch in v for ch in "xz"):
        return v if len(v) <= 6 else "x"
    try:
        n = int(v, 2)
    except ValueError:
        return v
    if radix == "dec":
        return str(n)
    if radix == "bin":
        return v.zfill(width)
    return f"{n:X}"


class WaveView(ttk.Frame):

    def __init__(self, parent, fonts):
        super().__init__(parent, style="Panel.TFrame")
        self.fonts = fonts
        import tkinter.font as tkfont
        self.f9 = tkfont.Font(font=(fonts["mono"][0], 9))
        self.f8 = tkfont.Font(font=(fonts["mono"][0], 8))
        lh = self.f9.metrics("linespace")
        self.ROW = max(26, lh + T.px(12))          # row height follows the real font height
        self.HEAD = lh + T.px(8)                   # time-axis band
        self.NAME_W = T.px(190)
        self.vcd: VCD | None = None
        self.shown: list[str] = []
        self.t0, self.t1 = 0, 100
        self.cursor: int | None = None
        self.radix = tk.StringVar(value="hex")
        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.Frame(pw, style="Panel.TFrame", width=T.px(230))
        pw.add(left, weight=0)
        ttk.Label(left, text="SIGNALS  (double-click to add)", style="Dim.TLabel").pack(anchor="w", padx=6, pady=(6, 2))
        self.tree = ttk.Treeview(left, show="tree")
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.tree.bind("<Double-1>", self._add)
        bb = ttk.Frame(left, style="Panel.TFrame")
        bb.pack(fill="x", padx=4, pady=(0, 6))
        ttk.Button(bb, text="Add all", style="Tool.TButton", command=self.add_all).pack(side="left")
        ttk.Button(bb, text="Clear", style="Tool.TButton", command=self.clear).pack(side="left")
        ttk.Combobox(bb, textvariable=self.radix, values=["hex", "dec", "bin"], width=5, state="readonly").pack(side="right")
        self.radix.trace_add("write", lambda *a: self.redraw())
        right = ttk.Frame(pw, style="Panel.TFrame")
        pw.add(right, weight=5)
        self.cv = tk.Canvas(right, bg="#05070a", highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.info = ttk.Label(right, text="No waveform loaded", style="Status.TLabel")
        self.info.pack(fill="x")
        self.cv.bind("<Configure>", lambda e: self.redraw())
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<Button-4>", lambda e: self.zoom(0.7, e.x))
        self.cv.bind("<Button-5>", lambda e: self.zoom(1.4, e.x))
        self.cv.bind("<MouseWheel>", lambda e: self.zoom(0.7 if e.delta > 0 else 1.4, e.x))
        self.cv.bind("<B2-Motion>", self._pan)
        self.cv.bind("<Button-2>", lambda e: setattr(self, "_px", e.x))
        self.cv.bind("<Delete>", lambda e: self.remove_selected())
        self._px = 0

    def load(self, path: str) -> bool:
        try:
            self.vcd = VCD(path)
        except Exception as e:
            self.info.configure(text=f"Cannot read {path}: {e}")
            return False
        self.tree.delete(*self.tree.get_children())
        nodes = {}
        for full in self.vcd.order:
            parts = full.split(".")
            parent = ""
            for k in range(len(parts) - 1):
                key = ".".join(parts[:k + 1])
                if key not in nodes:
                    nodes[key] = self.tree.insert(nodes.get(parent, ""), "end", text=parts[k], open=k < 2)
                parent = key
            ident = self.vcd.byname[full]
            w = self.vcd.signals[ident]["width"]
            self.tree.insert(nodes.get(parent, ""), "end", iid="sig:" + full, text=parts[-1] + (f" [{w - 1}:0]" if w > 1 else ""))
        keep = [s for s in self.shown if s in self.vcd.byname]
        self.shown = keep or self._default_signals()
        self.t0, self.t1 = 0, max(self.vcd.tmax, 1)
        self.redraw()
        return True

    def _default_signals(self):
        # signals of the top-most scope first (the testbench), capped
        if not self.vcd.order:
            return []
        depth = min(f.count(".") for f in self.vcd.order)
        return [f for f in self.vcd.order if f.count(".") in (depth, depth + 1)][:24]

    def _add(self, e=None):
        for it in self.tree.selection():
            if it.startswith("sig:") and it[4:] not in self.shown:
                self.shown.append(it[4:])
        self.redraw()

    def add_all(self):
        if self.vcd:
            self.shown = list(dict.fromkeys(self.vcd.order))[:200]
            self.redraw()

    def clear(self):
        self.shown = []
        self.redraw()

    def remove_selected(self):
        pass

    def X(self, t):
        W = max(self.cv.winfo_width(), 100)
        return self.NAME_W + (W - self.NAME_W - 10) * (t - self.t0) / max(self.t1 - self.t0, 1)

    def T(self, x):
        W = max(self.cv.winfo_width(), 100)
        return self.t0 + (x - self.NAME_W) * (self.t1 - self.t0) / max(W - self.NAME_W - 10, 1)

    def zoom(self, f, x):
        if not self.vcd:
            return
        tc = self.T(x) if x > self.NAME_W else (self.t0 + self.t1) / 2
        span = max(2, (self.t1 - self.t0) * f)
        self.t0 = max(0, tc - (tc - self.t0) * f)
        self.t1 = min(self.vcd.tmax + span * 0.05, self.t0 + span)
        self.redraw()

    def _pan(self, e):
        dt = (self._px - e.x) * (self.t1 - self.t0) / max(self.cv.winfo_width() - self.NAME_W, 1)
        self._px = e.x
        self.t0 = max(0, self.t0 + dt)
        self.t1 = self.t1 + dt
        self.redraw()

    def _click(self, e):
        self.cv.focus_set()
        if self.vcd and e.x > self.NAME_W:
            self.cursor = int(self.T(e.x))
            self.redraw()

    def redraw(self):
        cv = self.cv
        cv.delete("all")
        W, H = cv.winfo_width(), cv.winfo_height()
        if not self.vcd:
            cv.create_text(W / 2, H / 2, text="Run a simulation — the waveform appears here", fill=T.FG_DIM, font=self.fonts["ui"])
            return
        mono = self.f9
        longest = max([self.f9.measure(f.split(".")[-1] + "[31:0]  FFFFFFFF") for f in self.shown] + [T.px(190)])
        self.NAME_W = min(longest + T.px(14), max(W // 3, T.px(190)))
        hd = self.HEAD
        cv.create_rectangle(0, 0, self.NAME_W, H, fill="#0d1014", outline="")
        # time axis
        span = self.t1 - self.t0
        step = 10 ** max(0, len(str(int(span / 8))) - 1) if span > 8 else 1
        while span / step > 12:
            step *= 2
        t = (int(self.t0) // step) * step
        while t <= self.t1:
            x = self.X(t)
            if x >= self.NAME_W:
                cv.create_line(x, hd - 4, x, H, fill="#141a22")
                cv.create_text(x + 2, hd / 2, text=f"{t}", anchor="w", fill=T.FG_DIM, font=mono)
            t += step
        cv.create_text(6, hd / 2, text=f"time ({self.vcd.timescale})", anchor="w", fill=T.FG_DIM, font=mono)
        y = hd + 2
        for full in self.shown:
            ident = self.vcd.byname.get(full)
            if not ident:
                continue
            s = self.vcd.signals[ident]
            name = full.split(".")[-1]
            val_c = self.vcd.value_at(ident, self.cursor) if self.cursor is not None else ""
            lab = name if s["width"] == 1 else f"{name}[{s['width'] - 1}:0]"
            cv.create_text(8, y + self.ROW / 2, text=lab, anchor="w", fill=T.FG, font=mono)
            if self.cursor is not None:
                cv.create_text(self.NAME_W - 6, y + self.ROW / 2, text=fmt_bus(val_c, s["width"], self.radix.get()) if s["width"] > 1 else val_c,
                               anchor="e", fill="#ffcc00", font=mono)
            self._draw_sig(s, y + T.px(5), y + self.ROW - T.px(5))
            y += self.ROW
            if y > H:
                break
        if self.cursor is not None:
            x = self.X(self.cursor)
            cv.create_line(x, hd - 4, x, H, fill="#ffcc00")
            cv.create_text(x + 3, hd, text=str(self.cursor), anchor="nw", fill="#ffcc00", font=mono)
        self.info.configure(text=f"{len(self.vcd.order)} signals · end time {self.vcd.tmax} · view {int(self.t0)}–{int(self.t1)}"
                                 "   (wheel = zoom, middle-drag = pan, click = cursor)")

    def _draw_sig(self, s, ytop, ybot):
        cv = self.cv
        ch = s["changes"]
        if not ch:
            return
        i0 = max(0, bisect.bisect_right(s["times"], self.t0) - 1)
        seg = ch[i0:]
        W = cv.winfo_width()
        for k, (t, v) in enumerate(seg):
            tn = seg[k + 1][0] if k + 1 < len(seg) else max(self.t1, self.vcd.tmax)
            if t > self.t1:
                break
            x1, x2 = max(self.X(t), self.NAME_W), min(self.X(tn), W)
            if x2 < self.NAME_W:
                continue
            bad = any(c in v for c in "xz")
            if s["width"] == 1:
                col = T.ERR if bad else "#3fb950"
                yy = ybot if v == "0" else (ytop if v == "1" else (ytop + ybot) / 2)
                cv.create_line(x1, yy, x2, yy, fill=col, width=2 if v == "1" else 1)
                if k + 1 < len(seg) and x2 < W:
                    nv = seg[k + 1][1]
                    ny = ybot if nv == "0" else (ytop if nv == "1" else (ytop + ybot) / 2)
                    cv.create_line(x2, yy, x2, ny, fill=col)
            else:
                col = T.ERR if bad else "#58a6ff"
                d = min(4, (x2 - x1) / 2)
                ym = (ytop + ybot) / 2
                cv.create_polygon(x1, ym, x1 + d, ytop, x2 - d, ytop, x2, ym, x2 - d, ybot, x1 + d, ybot,
                                  outline=col, fill="#0e1f33" if not bad else "#3a0d0d")
                txt = fmt_bus(v, s["width"], self.radix.get())
                if x2 - x1 > self.f8.measure(txt) + 6:
                    cv.create_text((x1 + x2) / 2, ym, text=txt, fill="#dbe7f5", font=self.f8)
