"""Minimal, fast LEF/DEF readers for the layout GUI.

Only what the viewer needs: layers (order/type/width), macro size/class/pin
shapes, and from DEF: die, rows, tracks, components, IO pins, special nets
(power), regular nets with routed wiring + vias, and USE CLOCK.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_TOK = re.compile(r'"[^"]*"|\S+')


def _tokens(text: str) -> list[str]:
    # drop comments
    text = re.sub(r"#[^\n]*", "", text)
    return _TOK.findall(text)


# ─────────────────────────────────── LEF ────────────────────────────────────
@dataclass
class Layer:
    name: str
    type: str = ""
    direction: str = ""
    width: float = 0.0
    pitch: float = 0.0
    index: int = 0          # order in tech (bottom → top)
    routing_index: int = -1


@dataclass
class Macro:
    name: str
    cls: str = "CORE"
    w: float = 0.0
    h: float = 0.0
    pins: dict = field(default_factory=dict)      # pin -> [(layer, x1, y1, x2, y2)]
    pin_use: dict = field(default_factory=dict)   # pin -> USE
    pin_dir: dict = field(default_factory=dict)


@dataclass
class LefLib:
    layers: dict = field(default_factory=dict)       # name -> Layer
    layer_order: list = field(default_factory=list)
    macros: dict = field(default_factory=dict)
    sites: dict = field(default_factory=dict)        # name -> (w, h)

    def merge(self, other: "LefLib") -> None:
        for n in other.layer_order:
            if n not in self.layers:
                L = other.layers[n]
                L.index = len(self.layer_order)
                self.layers[n] = L
                self.layer_order.append(n)
        self.macros.update(other.macros)
        self.sites.update(other.sites)
        ri = 0
        for n in self.layer_order:
            if self.layers[n].type == "ROUTING":
                self.layers[n].routing_index = ri
                ri += 1


_lef_cache: dict[str, tuple[float, LefLib]] = {}


def read_lef(path: str) -> LefLib:
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return LefLib()
    c = _lef_cache.get(path)
    if c and c[0] == mt:
        return c[1]
    with open(path, errors="replace") as f:
        toks = _tokens(f.read())
    lib = LefLib()
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t in ("PROPERTYDEFINITIONS", "UNITS", "SPACING", "BEGINEXT"):
            end = {"BEGINEXT": "ENDEXT"}.get(t)
            j = i + 1
            while j < n and not ((end and toks[j] == end) or (toks[j] == "END" and j + 1 < n and toks[j + 1] == t)):
                j += 1
            i = j + (1 if end else 2)
            continue
        if t in ("VIA", "VIARULE", "NONDEFAULTRULE") and i + 1 < n:
            name = toks[i + 1]
            j = i + 2
            while j < n - 1 and not (toks[j] == "END" and toks[j + 1] == name):
                j += 1
            i = j + 2
            continue
        if t == "LAYER" and i + 2 < n and toks[i + 2] == ";":
            i += 3        # a layer reference (inside some other block), not a definition
            continue
        if t == "LAYER" and i + 1 < n:
            name = toks[i + 1]; i += 2
            L = Layer(name)
            while i < n and not (toks[i] == "END" and i + 1 < n and toks[i + 1] == name):
                k = toks[i]
                if k == "TYPE":
                    L.type = toks[i + 1]
                elif k == "DIRECTION":
                    L.direction = toks[i + 1]
                elif k == "WIDTH" and not L.width and i + 2 < n and toks[i + 2] == ";" and toks[i - 1] == ";":
                    L.width = _num(toks[i + 1])
                elif k == "PITCH" and not L.pitch:
                    L.pitch = _num(toks[i + 1])
                i += 1
            i += 2
            if name not in lib.layers:
                L.index = len(lib.layer_order)
                lib.layers[name] = L
                lib.layer_order.append(name)
            continue
        if t == "SITE" and i + 1 < n:
            name = toks[i + 1]; i += 2
            w = h = 0.0
            while i < n and not (toks[i] == "END" and i + 1 < n and toks[i + 1] == name):
                if toks[i] == "SIZE":
                    w, h = _num(toks[i + 1]), _num(toks[i + 3])
                i += 1
            lib.sites[name] = (w, h)
            i += 2
            continue
        if t == "MACRO" and i + 1 < n:
            name = toks[i + 1]; i += 2
            M = Macro(name)
            while i < n and not (toks[i] == "END" and i + 1 < n and toks[i + 1] == name):
                k = toks[i]
                if k == "CLASS":
                    j = i + 1
                    parts = []
                    while j < n and toks[j] != ";":
                        parts.append(toks[j]); j += 1
                    M.cls = " ".join(parts)
                    i = j
                elif k == "SIZE":
                    M.w, M.h = _num(toks[i + 1]), _num(toks[i + 3])
                    i += 4
                elif k == "PIN":
                    pname = toks[i + 1]; i += 2
                    shapes = []
                    layer = None
                    while i < n and not (toks[i] == "END" and i + 1 < n and toks[i + 1] == pname):
                        k2 = toks[i]
                        if k2 == "LAYER":
                            layer = toks[i + 1]; i += 2; continue
                        if k2 == "RECT":
                            j = i + 1
                            if toks[j] == "MASK":
                                j += 2
                            try:
                                x1, y1, x2, y2 = (_num(toks[j + q]) for q in range(4))
                                shapes.append((layer, min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
                            except (ValueError, IndexError):
                                pass
                            i = j + 4; continue
                        if k2 == "USE":
                            M.pin_use[pname] = toks[i + 1]
                        elif k2 == "DIRECTION":
                            M.pin_dir[pname] = toks[i + 1]
                        i += 1
                    M.pins[pname] = shapes
                    i += 2
                    continue
                elif k == "OBS":
                    while i < n and toks[i] != "END":
                        i += 1
                    i += 1
                    continue
                else:
                    i += 1
            lib.macros[name] = M
            i += 2
            continue
        i += 1
    ri = 0
    for nme in lib.layer_order:
        if lib.layers[nme].type == "ROUTING":
            lib.layers[nme].routing_index = ri
            ri += 1
    _lef_cache[path] = (mt, lib)
    return lib


def read_lefs(paths: list[str]) -> LefLib:
    lib = LefLib()
    for p in paths:
        lib.merge(read_lef(p))
    return lib


# ─────────────────────────────────── DEF ────────────────────────────────────
@dataclass
class Comp:
    name: str
    master: str
    x: float = 0.0
    y: float = 0.0
    orient: str = "N"
    status: str = "UNPLACED"      # PLACED / FIXED / COVER / UNPLACED
    source: str = ""


@dataclass
class IOPin:
    name: str
    net: str = ""
    direction: str = ""
    use: str = ""
    layer: str = ""
    rect: tuple = (0, 0, 0, 0)
    x: float = 0.0
    y: float = 0.0
    orient: str = "N"
    placed: bool = False


@dataclass
class Wire:
    layer: str
    width: float          # µm (0 → use layer default)
    pts: list             # [(x, y), ...] µm
    shape: str = ""


@dataclass
class Via:
    name: str
    x: float
    y: float


@dataclass
class Net:
    name: str
    conns: list = field(default_factory=list)      # [(comp, pin)]  comp == "PIN" for IO
    use: str = ""
    wires: list = field(default_factory=list)
    vias: list = field(default_factory=list)
    rects: list = field(default_factory=list)       # (layer, x1, y1, x2, y2)
    special: bool = False


@dataclass
class DefDesign:
    name: str = ""
    dbu: float = 1000.0
    die: tuple | None = None                  # (x1, y1, x2, y2) µm
    die_poly: list = field(default_factory=list)
    rows: list = field(default_factory=list)  # (name, site, x, y, orient, nx, ny, sx, sy)
    tracks: list = field(default_factory=list)  # (dir, start, num, step, layers)
    comps: dict = field(default_factory=dict)
    pins: dict = field(default_factory=dict)
    nets: dict = field(default_factory=dict)
    specialnets: dict = field(default_factory=dict)

    @property
    def core(self) -> tuple | None:
        if not self.rows:
            return None
        return self._core

    _core: tuple | None = None


def _num(s: str) -> float:
    return float(s)


def read_def(path: str, lib: LefLib | None = None) -> DefDesign:
    with open(path, errors="replace") as f:
        toks = _tokens(f.read())
    d = DefDesign()
    n = len(toks)
    i = 0
    u = 1000.0

    def P(v: str) -> float:
        return float(v) / u

    while i < n:
        t = toks[i]
        if t == "DESIGN" and i + 1 < n and d.name == "":
            d.name = toks[i + 1]; i += 2; continue
        if t == "UNITS" and toks[i + 1] == "DISTANCE":
            u = float(toks[i + 3]); d.dbu = u; i += 4; continue
        if t == "DIEAREA":
            pts = []
            i += 1
            while toks[i] != ";":
                if toks[i] == "(":
                    pts.append((P(toks[i + 1]), P(toks[i + 2]))); i += 4; continue
                i += 1
            if len(pts) == 2:
                (x1, y1), (x2, y2) = pts
                pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
            d.die_poly = pts
            if pts:
                xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                d.die = (min(xs), min(ys), max(xs), max(ys))
            i += 1; continue
        if t == "ROW":
            try:
                name, site = toks[i + 1], toks[i + 2]
                x, y, orient = P(toks[i + 3]), P(toks[i + 4]), toks[i + 5]
                nx = ny = 1; sx = sy = 0.0
                j = i + 6
                if toks[j] == "DO":
                    nx, ny = int(toks[j + 1]), int(toks[j + 3]); j += 4
                    if toks[j] == "STEP":
                        sx, sy = P(toks[j + 1]), P(toks[j + 2]); j += 3
                d.rows.append((name, site, x, y, orient, nx, ny, sx, sy))
            except (IndexError, ValueError):
                pass
            while toks[i] != ";":
                i += 1
            i += 1; continue
        if t == "TRACKS":
            try:
                dr, start, num, step = toks[i + 1], P(toks[i + 2]), int(toks[i + 4]), P(toks[i + 6])
                j = i + 7
                layers = []
                if toks[j] == "MASK":
                    j += 2
                    if toks[j] == "SAMEMASK":
                        j += 1
                if toks[j] == "LAYER":
                    j += 1
                    while toks[j] != ";":
                        layers.append(toks[j]); j += 1
                d.tracks.append((dr, start, num, step, layers))
            except (IndexError, ValueError):
                pass
            while toks[i] != ";":
                i += 1
            i += 1; continue
        if t == "COMPONENTS" and toks[i + 2] == ";":
            i += 3
            while not (toks[i] == "END" and toks[i + 1] == "COMPONENTS"):
                if toks[i] == "-":
                    c = Comp(toks[i + 1], toks[i + 2])
                    j = i + 3
                    while toks[j] != ";":
                        k = toks[j]
                        if k in ("PLACED", "FIXED", "COVER"):
                            c.status = k
                            c.x, c.y, c.orient = P(toks[j + 2]), P(toks[j + 3]), toks[j + 5]
                            j += 6; continue
                        if k == "UNPLACED":
                            c.status = "UNPLACED"
                        elif k == "SOURCE":
                            c.source = toks[j + 1]
                        j += 1
                    d.comps[c.name] = c
                    i = j + 1; continue
                i += 1
            i += 2; continue
        if t == "PINS" and toks[i + 2] == ";":
            i += 3
            while not (toks[i] == "END" and toks[i + 1] == "PINS"):
                if toks[i] == "-":
                    p = IOPin(toks[i + 1])
                    j = i + 2
                    while toks[j] != ";":
                        k = toks[j]
                        if k == "NET":
                            p.net = toks[j + 1]
                        elif k == "DIRECTION":
                            p.direction = toks[j + 1]
                        elif k == "USE":
                            p.use = toks[j + 1]
                        elif k == "LAYER" and not p.layer:
                            p.layer = toks[j + 1]
                            jj = j + 2
                            if toks[jj] == "MASK":
                                jj += 2
                            if toks[jj] == "SPACING" or toks[jj] == "DESIGNRULEWIDTH":
                                jj += 2
                            try:
                                p.rect = (P(toks[jj + 1]), P(toks[jj + 2]), P(toks[jj + 5]), P(toks[jj + 6]))
                            except (IndexError, ValueError):
                                pass
                            j = jj + 8; continue
                        elif k in ("PLACED", "FIXED", "COVER") and not p.placed:
                            p.x, p.y, p.orient = P(toks[j + 2]), P(toks[j + 3]), toks[j + 5]
                            p.placed = True
                            j += 6; continue
                        j += 1
                    d.pins[p.name] = p
                    i = j + 1; continue
                i += 1
            i += 2; continue
        if t in ("NETS", "SPECIALNETS") and i + 2 < n and toks[i + 2] == ";":
            special = t == "SPECIALNETS"
            section = t
            i += 3
            target = d.specialnets if special else d.nets
            while not (toks[i] == "END" and toks[i + 1] == section):
                if toks[i] == "-":
                    net = Net(toks[i + 1], special=special)
                    j = i + 2
                    # connections
                    while toks[j] == "(":
                        net.conns.append((toks[j + 1], toks[j + 2]))
                        j += 3
                        while toks[j] != ")":
                            j += 1
                        j += 1
                    # attributes / routing
                    while toks[j] != ";":
                        k = toks[j]
                        if k == "+" and toks[j + 1] == "USE":
                            net.use = toks[j + 2]; j += 3; continue
                        if k == "+" and toks[j + 1] in ("ROUTED", "FIXED", "COVER", "NOSHIELD"):
                            j = _parse_routing(toks, j + 2, net, special, P)
                            continue
                        if k == "+" and toks[j + 1] == "RECT":
                            try:
                                lay = toks[j + 2]
                                jj = j + 3
                                if toks[jj] == "+" and toks[jj + 1] == "MASK":
                                    jj += 3
                                net.rects.append((lay, P(toks[jj + 1]), P(toks[jj + 2]), P(toks[jj + 5]), P(toks[jj + 6])))
                                j = jj + 8; continue
                            except (IndexError, ValueError):
                                pass
                        j += 1
                    target[net.name] = net
                    i = j + 1; continue
                i += 1
            i += 2; continue
        i += 1
    if d.rows:
        xs, ys, xe, ye = [], [], [], []
        for (_, site, x, y, orient, nx, ny, sx, sy) in d.rows:
            sw, sh = (lib.sites.get(site, (sx or 0.46, 2.72)) if lib else (sx or 0.46, 2.72))
            xs.append(x); ys.append(y)
            xe.append(x + max(nx, 1) * (sx if sx else sw)); ye.append(y + max(ny, 1) * (sy if sy else sh))
        d._core = (min(xs), min(ys), max(xe), max(ye))
    return d


def _parse_routing(toks: list[str], j: int, net: Net, special: bool, P) -> int:
    """Parse '<layer> [width] [+ SHAPE s] ( x y [ext] ) ( * y ) via ... NEW ...' until '+' attr or ';'."""
    n = len(toks)
    while j < n and toks[j] != ";":
        if toks[j] == "+" and toks[j + 1] not in ("SHAPE", "STYLE", "MASK"):
            return j
        # layer start
        layer = toks[j]; j += 1
        width = 0.0
        shape = ""
        if special and j < n and _isnum(toks[j]):
            width = P(toks[j]); j += 1
        pts: list = []
        last = None
        while j < n and toks[j] not in (";",) and toks[j] != "NEW":
            k = toks[j]
            if k == "+":
                if toks[j + 1] == "SHAPE":
                    shape = toks[j + 2]; j += 3; continue
                if toks[j + 1] in ("STYLE", "MASK"):
                    j += 3; continue
                break
            if k in ("TAPER",):
                j += 1; continue
            if k == "TAPERRULE" or k == "STYLE" or k == "MASK":
                j += 2; continue
            if k == "(":
                xs, ys = toks[j + 1], toks[j + 2]
                x = last[0] if xs == "*" and last else (P(xs) if xs != "*" else 0.0)
                y = last[1] if ys == "*" and last else (P(ys) if ys != "*" else 0.0)
                last = (x, y)
                pts.append(last)
                j += 3
                while toks[j] != ")":
                    j += 1
                j += 1
                continue
            if k == "RECT":
                # ( dx1 dy1 dx2 dy2 ) relative to last point
                try:
                    dx1, dy1, dx2, dy2 = P(toks[j + 2]), P(toks[j + 3]), P(toks[j + 4]), P(toks[j + 5])
                    if last:
                        net.rects.append((layer, last[0] + dx1, last[1] + dy1, last[0] + dx2, last[1] + dy2))
                except (IndexError, ValueError):
                    pass
                j += 7; continue
            if k == "VIRTUAL":
                j += 5; continue
            if k in ("DO",):   # via arrays: via DO n BY m STEP x y
                j += 7; continue
            # anything else is a via name placed at the last point
            if last is not None and not _isnum(k):
                net.vias.append(Via(k, last[0], last[1]))
            j += 1
        if len(pts) >= 2:
            net.wires.append(Wire(layer, width, pts, shape))
        if j < n and toks[j] == "NEW":
            j += 1
            continue
    return j


def _isnum(s: str) -> bool:
    try:
        float(s); return True
    except ValueError:
        return False


# orientation transform of a macro-local point → placed coordinates
def xform(orient: str, x: float, y: float, w: float, h: float) -> tuple[float, float]:
    if orient == "N":
        return x, y
    if orient == "S":
        return w - x, h - y
    if orient == "FN":
        return w - x, y
    if orient == "FS":
        return x, h - y
    if orient == "W":
        return h - y, x
    if orient == "E":
        return y, w - x
    if orient == "FW":
        return y, x
    if orient == "FE":
        return h - y, w - x
    return x, y


def placed_size(orient: str, w: float, h: float) -> tuple[float, float]:
    return (h, w) if orient in ("E", "W", "FE", "FW") else (w, h)
