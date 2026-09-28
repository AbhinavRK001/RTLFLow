"""Test fixture: a realistic sky130 `count` design at every PnR stage, as DEF.

Used ONLY by tests/mock_openroad.py so the shell ⇄ GUI pipeline can be exercised
on machines without OpenROAD. Geometry uses real sky130_fd_sc_hd LEF sizes.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtlflow.gui.lefdef import read_lefs, xform  # noqa: E402

P = "sky130_fd_sc_hd__"
CELLS = [
    ("_10_", "dfxtp_1"), ("_11_", "dfxtp_1"), ("_12_", "dfxtp_1"), ("_13_", "dfxtp_1"),
    ("_14_", "mux2_1"), ("_15_", "mux2_1"), ("_16_", "mux2_1"),
    ("_17_", "xor2_1"), ("_18_", "xor2_1"), ("_19_", "xor2_1"),
    ("_20_", "and2_0"), ("_21_", "and3_1"), ("_22_", "nand2_1"), ("_23_", "a21oi_1"),
    ("_24_", "o21ai_0"), ("_25_", "inv_1"), ("_26_", "nor2_1"), ("_27_", "xnor2_1"),
    ("_28_", "mux2i_1"), ("_29_", "a22o_1"), ("_30_", "buf_1"), ("_31_", "nand3_1"), ("_32_", "o21a_1"),
]
PORTS = [("clk", "INPUT"), ("rst", "INPUT"), ("en", "INPUT"), ("load", "INPUT")] + \
        [(f"d[{i}]", "INPUT") for i in range(4)] + [(f"q[{i}]", "OUTPUT") for i in range(4)]
DBU = 1000


def netlist_verilog() -> str:
    lines = ["module count(clk, rst, en, load, d, q);", "  input clk; input rst; input en; input load;",
             "  input [3:0] d; output [3:0] q;"]
    for n, c in CELLS:
        lines.append(f"  {P}{c} {n} ();")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


class Fixture:
    def __init__(self, lefs: list[str]) -> None:
        self.lib = read_lefs(lefs)
        rnd = random.Random(7)
        self.cells = list(CELLS)
        self.nets = self._make_nets(rnd)
        self.util = 0.5
        self.taps = False
        self.pdn = False
        self.cts = False
        self.fill = False
        self.pos = {}

    def _outputs(self, master):
        M = self.lib.macros[master]
        return [p for p, d in M.pin_dir.items() if d == "OUTPUT" and M.pin_use.get(p) not in ("POWER", "GROUND")]

    def _inputs(self, master):
        M = self.lib.macros[master]
        return [p for p, d in M.pin_dir.items() if d == "INPUT" and M.pin_use.get(p) not in ("POWER", "GROUND")
                and p != "CLK"]

    def _make_nets(self, rnd):
        nets = {"clk": {"use": "CLOCK", "conns": [("PIN", "clk")] + [(n, "CLK") for n, c in CELLS if c.startswith("df")]}}
        drivers = []
        for n, c in CELLS:
            for o in self._outputs(P + c):
                net = f"_{n.strip('_')}_{o.lower()}"
                nets[net] = {"use": "", "conns": [(n, o)]}
                drivers.append(net)
        for pn, d in PORTS:
            if d == "INPUT" and pn != "clk":
                nets[pn] = {"use": "", "conns": [("PIN", pn)]}
                drivers.append(pn)
        for i, (n, c) in enumerate(CELLS):
            for inp in self._inputs(P + c):
                cand = [d for d in drivers if not any(cc == n for cc, _ in nets[d]["conns"])]
                net = rnd.choice(cand)
                nets[net]["conns"].append((n, inp))
        for i in range(4):
            reg = f"_{10 + i}_"
            src = f"_{10 + i}_q"
            nets[src]["conns"].append(("PIN", f"q[{i}]"))
        return nets

    # ── geometry ────────────────────────────────────────────────────────────
    def area(self):
        return sum(self.lib.macros[P + c].w * self.lib.macros[P + c].h for _, c in self.cells)

    def plan(self):
        core_area = self.area() / self.util
        side = math.sqrt(core_area)
        rows = max(2, round(side / 2.72))
        h = rows * 2.72
        w = math.ceil(core_area / h / 0.46) * 0.46
        m = 5.0
        self.core = (m, m, m + w, m + h)
        self.die = (0.0, 0.0, 2 * m + w, 2 * m + h)
        self.rows = rows
        self.sites = int(round(w / 0.46))

    def place(self):
        rnd = random.Random(3)
        order = sorted(self.cells, key=lambda x: (x[1].startswith("df"), rnd.random()))
        x0, y0, x1, y1 = self.core
        self.pos = {}
        slots = []
        for r in range(self.rows):
            slots.append([x0, r])
        if self.taps:
            for r in range(self.rows):
                for k, tx in enumerate([x0, x0 + 7 * 0.46 * 4]):
                    if tx < x1:
                        self.pos[f"TAP_{r}_{k}"] = (P + "tapvpwrvgnd_1", tx, y0 + r * 2.72, "N" if r % 2 == 0 else "FS")
        r = 0
        cursors = [x0 + (0.46 if self.taps else 0.0) + 0.46 * 2 for _ in range(self.rows)]
        for n, c in order:
            M = self.lib.macros[P + c]
            for _ in range(self.rows):
                if cursors[r] + M.w <= x1 - 0.46:
                    break
                r = (r + 1) % self.rows
            x = round(cursors[r] / 0.46) * 0.46
            cursors[r] = x + M.w + 0.46 * rnd.randint(0, 2)
            self.pos[n] = (P + c, x, y0 + r * 2.72, "N" if r % 2 == 0 else "FS")
            r = (r + 1) % self.rows
        if self.cts:
            x = (x0 + x1) / 2
            self.pos["clkbuf_0_clk"] = (P + "clkbuf_4", round(x / 0.46) * 0.46, y0 + (self.rows // 2) * 2.72,
                                        "N" if (self.rows // 2) % 2 == 0 else "FS")
            self.nets["clk"] = {"use": "CLOCK", "conns": [("PIN", "clk"), ("clkbuf_0_clk", "A")]}
            self.nets["clknet_0_clk"] = {"use": "CLOCK", "conns": [("clkbuf_0_clk", "X")] +
                                         [(n, "CLK") for n, c in CELLS if c.startswith("df")]}

    def pin_xy(self, inst, pin):
        master, x, y, o = self.pos[inst]
        M = self.lib.macros[master]
        sh = M.pins.get(pin) or [(None, 0, 0, M.w, M.h)]
        _, a, b, cc, dd = sh[0]
        lx, ly = xform(o, (a + cc) / 2, (b + dd) / 2, M.w, M.h)
        return x + lx, y + ly

    def io_xy(self):
        x0, y0, x1, y1 = self.die
        ins = [p for p, d in PORTS if d == "INPUT"]
        outs = [p for p, d in PORTS if d == "OUTPUT"]
        res = {}
        for i, p in enumerate(ins):
            y = y0 + (y1 - y0) * (i + 1) / (len(ins) + 1)
            res[p] = ("met3", 0.0, round(y / 0.68) * 0.68, "E" if False else "N", "left")
        for i, p in enumerate(outs):
            y = y0 + (y1 - y0) * (i + 1) / (len(outs) + 1)
            res[p] = ("met3", x1, round(y / 0.68) * 0.68, "N", "right")
        return res

    # ── DEF writer ───────────────────────────────────────────────────────────
    def def_text(self, stage: str) -> str:
        u = lambda v: int(round(v * DBU))  # noqa: E731
        L = ["VERSION 5.8 ;", 'DIVIDERCHAR "/" ;', 'BUSBITCHARS "[]" ;', "DESIGN count ;", f"UNITS DISTANCE MICRONS {DBU} ;"]
        fp = stage not in ("init",)
        if fp:
            x0, y0, x1, y1 = self.die
            L.append(f"DIEAREA ( {u(x0)} {u(y0)} ) ( {u(x1)} {u(y1)} ) ;")
            cx0, cy0, cx1, cy1 = self.core
            for r in range(self.rows):
                L.append(f"ROW ROW_{r} unithd {u(cx0)} {u(cy0 + r * 2.72)} {'N' if r % 2 == 0 else 'FS'} "
                         f"DO {self.sites} BY 1 STEP 460 0 ;")
            for lay, xo, xp, yo, yp in [("li1", .23, .46, .17, .34), ("met1", .17, .34, .17, .34), ("met2", .23, .46, .23, .46),
                                        ("met3", .34, .68, .34, .68), ("met4", .46, .92, .46, .92), ("met5", 1.7, 3.4, 1.7, 3.4)]:
                L.append(f"TRACKS X {u(xo)} DO {int((x1 - xo) / xp)} STEP {u(xp)} LAYER {lay} ;")
                L.append(f"TRACKS Y {u(yo)} DO {int((y1 - yo) / yp)} STEP {u(yp)} LAYER {lay} ;")
        placed = stage in ("place", "cts", "route", "filled")
        comps = []
        if placed:
            for n, (m, x, y, o) in self.pos.items():
                comps.append(f"- {n} {m} + {'SOURCE TIMING ' if n.startswith('clkbuf') else ''}PLACED ( {u(x)} {u(y)} ) {o} ;")
            if self.fill and stage in ("route", "filled"):
                comps += self._fillers(u)
        else:
            for n, c in self.cells:
                comps.append(f"- {n} {P}{c} ;")
        L.append(f"COMPONENTS {len(comps)} ;")
        L += comps
        L.append("END COMPONENTS")
        io = self.io_xy() if fp else {}
        L.append(f"PINS {len(PORTS)} ;")
        for p, d in PORTS:
            ln = f"- {p} + NET {p} + DIRECTION {d} + USE {'CLOCK' if p == 'clk' else 'SIGNAL'}"
            if p in io:
                lay, x, y, o, side = io[p]
                ln += f"\n  + PORT\n    + LAYER {lay} ( -400 -150 ) ( 400 150 )\n    + PLACED ( {u(x)} {u(y)} ) {o}"
            L.append(ln + " ;")
        L.append("END PINS")
        if fp and self.pdn:
            L.append("SPECIALNETS 2 ;")
            cx0, cy0, cx1, cy1 = self.core
            for net, use, par in (("VPWR", "POWER", 1), ("VGND", "GROUND", 0)):
                segs = []
                for r in range(self.rows + 1):
                    if r % 2 == par:
                        continue
                    y = cy0 + r * 2.72
                    segs.append(f"met1 480 + SHAPE FOLLOWPIN ( {u(cx0)} {u(y)} ) ( {u(cx1)} {u(y)} )")
                sx = cx0 + (3.0 if par else 8.0)
                while sx < cx1 - 1:
                    segs.append(f"met4 1600 + SHAPE STRIPE ( {u(sx)} {u(cy0)} ) ( {u(sx)} {u(cy1)} )")
                    sx += 20
                L.append(f"- {net} ( * {net} ) + USE {use}\n  + ROUTED " + "\n    NEW ".join(segs) + " ;")
            L.append("END SPECIALNETS")
        L.append(f"NETS {len(self.nets)} ;")
        for net, info in self.nets.items():
            conns = " ".join(f"( {c} {p} )" for c, p in info["conns"] if c == "PIN" or c in self.pos or not placed)
            ln = f"- {net} {conns}"
            if info["use"]:
                ln += f" + USE {info['use']}"
            if stage in ("route", "filled"):
                ln += self._route(net, info, u, io)
            L.append(ln + " ;")
        L.append("END NETS")
        L.append("END DESIGN")
        return "\n".join(L) + "\n"

    def _fillers(self, u):
        out = []
        occ = {}
        for n, (m, x, y, o) in self.pos.items():
            occ.setdefault(round(y, 2), []).append((x, x + self.lib.macros[m].w))
        cx0, cy0, cx1, cy1 = self.core
        k = 0
        for r in range(self.rows):
            y = round(cy0 + r * 2.72, 2)
            spans = sorted(occ.get(y, []))
            x = cx0
            for a, b in spans + [(cx1, cx1)]:
                while a - x >= 0.459:
                    wdt = 0.46 * max(1, min(8, int((a - x + 1e-6) / 0.46)))
                    size = max(s for s in (1, 2, 4, 8) if 0.46 * s <= wdt + 1e-6)
                    out.append(f"- FILLER_{r}_{k} {P}fill_{size} + SOURCE DIST + PLACED ( {u(x)} {u(cy0 + r * 2.72)} ) "
                               f"{'N' if r % 2 == 0 else 'FS'} ;")
                    x += 0.46 * size
                    k += 1
                x = max(x, b)
        return out

    def _route(self, net, info, u, io):
        pts = []
        for c, p in info["conns"]:
            if c == "PIN":
                if p in io:
                    _, x, y, _, _ = io[p]
                    pts.append((x, y, "met3"))
            elif c in self.pos:
                x, y = self.pin_xy(c, p)
                pts.append((x, y, "li1"))
        if len(pts) < 2:
            return ""
        segs = []
        sx, sy, sl = pts[0]
        trunk_y = round(sy / 0.34) * 0.34 + 0.17
        for (x, y, l) in pts[1:]:
            segs.append(f"met1 ( {u(sx)} {u(trunk_y)} ) ( {u(x)} * ) M1M2_PR")
            segs.append(f"met2 ( {u(x)} {u(trunk_y)} ) ( * {u(y)} ) M1M2_PR")
            segs.append(f"met1 ( {u(x)} {u(y)} ) L1M1_PR_MR")
        segs.append(f"met1 ( {u(sx)} {u(sy)} ) ( * {u(trunk_y)} )")
        return "\n  + ROUTED " + "\n    NEW ".join(segs)


if __name__ == "__main__":
    lefs = sys.argv[1:3]
    f = Fixture(lefs)
    f.plan(); f.place()
    print(f.def_text("route"))
