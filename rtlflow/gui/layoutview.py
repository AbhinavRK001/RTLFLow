"""Reusable layout canvas (real DEF + LEF) with overlays — used by voltus (power/IR
maps) and pegasus (DRC markers). Same renderer conventions as the innovus GUI."""

from __future__ import annotations

import math
import tkinter as tk

from .lefdef import DefDesign, LefLib, placed_size, read_def, read_lefs
from .innovus_gui import KIND, METAL, LI_COLOR, cell_kind


class LayoutCanvas(tk.Canvas):
    def __init__(self, parent, fonts, **kw):
        super().__init__(parent, bg="#000000", highlightthickness=0, cursor="crosshair", **kw)
        self.fonts = fonts
        self.lib = LefLib()
        self.d: DefDesign | None = None
        self.s, self.cx, self.cy = 10.0, 0.0, 0.0
        self.show_routes = True
        self.dim_cells = False
        self.cell_fill: dict[str, str] = {}          # inst -> fill colour (heat maps)
        self.grid: list[tuple] = []                  # (x1,y1,x2,y2,color) world rects (IR map tiles)
        self.markers: list[tuple] = []               # (x1,y1,x2,y2,label,color)
        self.hi_marker: int | None = None
        self.on_click = None
        self.on_hover = None
        self.empty_text = "No layout loaded"
        self._drag = None
        self._job = None
        self._need_fit = False
        self.bind("<Configure>", self._configure)
        self.bind("<Button-4>", lambda e: self.zoom(1.25, e.x, e.y))
        self.bind("<Button-5>", lambda e: self.zoom(0.8, e.x, e.y))
        self.bind("<MouseWheel>", lambda e: self.zoom(1.25 if e.delta > 0 else 0.8, e.x, e.y))
        self.bind("<Button-1>", self._down)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._up)
        self.bind("<Button-2>", self._down)
        self.bind("<B2-Motion>", self._move)
        self.bind("<Motion>", self._motion)

    def _configure(self, e=None):
        if self._need_fit and self.winfo_width() > 50:
            self._need_fit = False
            self.fit()
        else:
            self.schedule()

    # data
    def load(self, def_path: str, lefs: list[str]) -> bool:
        try:
            if lefs:
                self.lib = read_lefs(lefs)
            self.d = read_def(def_path, self.lib)
        except Exception:
            self.d = None
            return False
        self._need_fit = self.winfo_width() < 50 or not self.winfo_ismapped()
        self.fit()
        return True

    def cell_box(self, name: str):
        c = self.d.comps.get(name) if self.d else None
        if not c or c.status == "UNPLACED":
            return None
        M = self.lib.macros.get(c.master)
        w, h = placed_size(c.orient, *(M.w, M.h) if M and M.w else (1.38, 2.72))
        return c.x, c.y, c.x + w, c.y + h

    # view
    def P(self, x, y):
        return self.winfo_width() / 2 + (x - self.cx) * self.s, self.winfo_height() / 2 - (y - self.cy) * self.s

    def Wd(self, X, Y):
        return self.cx + (X - self.winfo_width() / 2) / self.s, self.cy - (Y - self.winfo_height() / 2) / self.s

    def fit(self):
        if not (self.d and self.d.die):
            return self.schedule()
        x1, y1, x2, y2 = self.d.die
        self.cx, self.cy = (x1 + x2) / 2, (y1 + y2) / 2
        self.s = 0.9 * min(max(self.winfo_width(), 100) / max(x2 - x1, 1e-3), max(self.winfo_height(), 100) / max(y2 - y1, 1e-3))
        self.schedule()

    def zoom_to(self, x1, y1, x2, y2, margin=4.0):
        w, h = max(x2 - x1, 1e-3) + 2 * margin, max(y2 - y1, 1e-3) + 2 * margin
        self.cx, self.cy = (x1 + x2) / 2, (y1 + y2) / 2
        self.s = min(self.winfo_width() / w, self.winfo_height() / h) * 0.9
        self.schedule()

    def zoom(self, f, X=None, Y=None):
        if X is None:
            X, Y = self.winfo_width() / 2, self.winfo_height() / 2
        wx, wy = self.Wd(X, Y)
        self.s = min(max(self.s * f, 0.02), 5000)
        self.cx = wx - (X - self.winfo_width() / 2) / self.s
        self.cy = wy + (Y - self.winfo_height() / 2) / self.s
        self.schedule()

    def schedule(self, ms=15):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(ms, self.redraw)

    def _rect(self, x1, y1, x2, y2, **kw):
        X1, Y1 = self.P(x1, y2)
        X2, Y2 = self.P(x2, y1)
        return self.create_rectangle(X1, Y1, max(X2, X1 + 1), max(Y2, Y1 + 1), **kw)

    def redraw(self):
        self._job = None
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if not self.d or not self.d.die:
            self.create_text(W / 2, H / 2, text=self.empty_text, fill="#8b929c", font=(self.fonts["ui"][0], 13, "bold"))
            return
        d = self.d
        self._rect(*d.die, outline="#d0d0d0", width=2)
        if d.core:
            self._rect(*d.core, outline="#4a6fa5", dash=(5, 3))
        for (x1, y1, x2, y2, col) in self.grid:
            self._rect(x1, y1, x2, y2, outline="", fill=col, stipple="gray50")
        for comp in d.comps.values():
            if comp.status == "UNPLACED":
                continue
            M = self.lib.macros.get(comp.master)
            w, h = placed_size(comp.orient, *(M.w, M.h) if M and M.w else (1.38, 2.72))
            kind = cell_kind(comp.master, M.cls if M else "", comp.name)
            o, f, _ = KIND[kind]
            fill = self.cell_fill.get(comp.name)
            if fill:
                self._rect(comp.x, comp.y, comp.x + w, comp.y + h, outline=fill, fill=fill)
            elif self.dim_cells:
                self._rect(comp.x, comp.y, comp.x + w, comp.y + h, outline="#2a3140", fill="")
            else:
                self._rect(comp.x, comp.y, comp.x + w, comp.y + h, outline=o, fill=f)
        if self.show_routes:
            has_li = any(n.lower().startswith("li") for n in self.lib.layer_order if self.lib.layers[n].type == "ROUTING")
            for net in list(d.nets.values()) + list(d.specialnets.values()):
                for wv in net.wires:
                    L = self.lib.layers.get(wv.layer)
                    if wv.layer.lower().startswith("li"):
                        col = LI_COLOR
                    else:
                        ri = (L.routing_index - (1 if has_li else 0)) if L else 0
                        col = METAL[max(ri, 0) % len(METAL)]
                    width = max(1.0, (wv.width or (L.width if L else 0.14)) * self.s)
                    pts = [v for p in wv.pts for v in self.P(*p)]
                    self.create_line(*pts, fill=col, width=width, stipple="gray50" if net.special else "")
        for p in d.pins.values():
            if p.placed:
                X, Y = self.P(p.x, p.y)
                self.create_polygon(X, Y - 4, X + 4, Y, X, Y + 4, X - 4, Y, fill="#ffcc00")
        for i, (x1, y1, x2, y2, label, col) in enumerate(self.markers):
            X1, Y1 = self.P(x1, y2)
            X2, Y2 = self.P(x2, y1)
            wdt = 3 if i == self.hi_marker else 1
            self.create_rectangle(X1 - 3, Y1 - 3, X2 + 3, Y2 + 3, outline=col, width=wdt)
            self.create_line(X1 - 3, Y1 - 3, X2 + 3, Y2 + 3, fill=col)
            self.create_line(X1 - 3, Y2 + 3, X2 + 3, Y1 - 3, fill=col)
            if i == self.hi_marker and label:
                self.create_text(X2 + 8, Y1, text=label, anchor="nw", fill=col, font=self.fonts["small"])
        self._scale_bar()

    def _scale_bar(self):
        target = 120 / self.s
        mag = 10 ** math.floor(math.log10(max(target, 1e-6)))
        val = next(m * mag for m in (1, 2, 5, 10) if m * mag >= target * 0.6)
        px = val * self.s
        H = self.winfo_height()
        self.create_line(16, H - 18, 16 + px, H - 18, fill="#d0d0d0", width=2)
        self.create_text(16 + px / 2, H - 26, text=f"{val:g} µm", fill="#d0d0d0", font=self.fonts["small"], anchor="s")

    # interaction
    def _down(self, e):
        self._drag = (e.x, e.y, self.cx, self.cy, False)

    def _move(self, e):
        if not self._drag:
            return
        X0, Y0, cx0, cy0, moved = self._drag
        if moved or abs(e.x - X0) + abs(e.y - Y0) > 4:
            self._drag = (X0, Y0, cx0, cy0, True)
            self.cx = cx0 - (e.x - X0) / self.s
            self.cy = cy0 + (e.y - Y0) / self.s
            self.schedule(8)

    def _up(self, e):
        if self._drag and not self._drag[4] and self.on_click:
            self.on_click(*self.Wd(e.x, e.y))
        self._drag = None

    def _motion(self, e):
        if self.on_hover:
            self.on_hover(*self.Wd(e.x, e.y))

    def inst_at(self, x, y):
        if not self.d:
            return None
        for n in self.d.comps:
            b = self.cell_box(n)
            if b and b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                return n
        return None
