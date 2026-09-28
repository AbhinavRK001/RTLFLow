"""RTLFlow ATPG engine (behind `modus`): full-scan stuck-at test generation.

* Gate-level model from a Yosys JSON netlist + liberty cell functions
  (combinational `function`, sequential `ff { next_state }`).
* Full-scan view: every flop output is a pseudo-primary input (PPI), every flop
  next-state is a pseudo-primary output (PPO).
* Random bit-parallel pattern generation + event-driven fault simulation,
  then PODEM (3-valued, backtrack-limited) for the faults random patterns miss,
  which also proves redundant faults untestable.
* Scan insertion (flop → scan-equivalent flop, chain stitching) and structural
  Verilog writer.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from itertools import product

from . import liberty as libscan

# ─────────────────────────── liberty boolean functions ───────────────────────────
_TOK = re.compile(r"\s*([A-Za-z_][\w\[\]]*|[01]|[()!&|^*+']|\s)")


class _Parser:
    """Liberty function grammar: ! ' (not), & * space (and), ^ (xor), | + (or)."""

    def __init__(self, s: str):
        s = s.strip().strip('"')
        self.toks = [t for t in re.findall(r"[A-Za-z_][\w\[\]]*|[01]|[()!&|^*+']", s)]
        # implicit AND: insert '&' between adjacent operands
        out = []
        for t in self.toks:
            if out and (out[-1] not in "(!&|^*+" or out[-1] in ("'",)) and (t not in ")&|^*+'" ):
                out.append("&")
            out.append(t)
        self.toks = out
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def eat(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def parse(self):
        e = self.p_or()
        return e

    def p_or(self):
        e = self.p_xor()
        while self.peek() in ("|", "+"):
            self.eat()
            e = ("or", e, self.p_xor())
        return e

    def p_xor(self):
        e = self.p_and()
        while self.peek() == "^":
            self.eat()
            e = ("xor", e, self.p_and())
        return e

    def p_and(self):
        e = self.p_not()
        while self.peek() in ("&", "*"):
            self.eat()
            e = ("and", e, self.p_not())
        return e

    def p_not(self):
        if self.peek() == "!":
            self.eat()
            e = ("not", self.p_not())
        else:
            e = self.p_atom()
        while self.peek() == "'":
            self.eat()
            e = ("not", e)
        return e

    def p_atom(self):
        t = self.eat()
        if t == "(":
            e = self.p_or()
            if self.peek() == ")":
                self.eat()
            return e
        if t in ("0", "1"):
            return ("const", int(t))
        return ("var", t)


def compile_fn(expr: str):
    """→ (python_callable(vals: dict, mask) -> int, input_names)."""
    ast = _Parser(expr).parse()
    names: list[str] = []

    def emit(n):
        k = n[0]
        if k == "var":
            if n[1] not in names:
                names.append(n[1])
            return f"v[{n[1]!r}]"
        if k == "const":
            return "M" if n[1] else "0"
        if k == "not":
            return f"(~{emit(n[1])} & M)"
        op = {"and": "&", "or": "|", "xor": "^"}[k]
        return f"({emit(n[1])} {op} {emit(n[2])})"
    code = emit(ast)
    fn = eval(f"lambda v, M: {code}")  # noqa: S307 — expression built from our own AST
    return fn, names


# ─────────────────────────────── circuit model ───────────────────────────────
CONST0, CONST1 = -1, -2


@dataclass
class Cell:
    name: str
    type: str
    ins: dict            # pin -> bit
    outs: dict           # pin -> bit
    fns: dict            # out pin -> (fn, names)


@dataclass
class Flop:
    name: str
    type: str
    pins: dict           # all pins -> bit (inputs)
    q: int | None
    qn: int | None
    next_fn: object
    next_names: list
    clock_pin: str


@dataclass
class Fault:
    bit: int
    sv: int              # stuck value
    net: str
    status: str = "untested"     # detected | redundant | aborted | untested
    pattern: int = -1


class Circuit:
    def __init__(self, data: dict, top: str, lib_path: str):
        self.lib = libscan.scan(lib_path)
        mod = data["modules"].get(top)
        if mod is None:
            raise ValueError(f"top module '{top}' not in netlist JSON")
        self.top = top
        self.mod = mod
        self.cells: list[Cell] = []
        self.flops: list[Flop] = []
        self.names: dict[int, str] = {}
        self.driver: dict[int, tuple] = {}
        self.clock_bits: set[int] = set()
        self.unknown: list[str] = []
        self._name_bits()
        cells = self.lib["cells"]
        for cname, c in mod.get("cells", {}).items():
            t = c["type"]
            info = cells.get(t)
            conns = {p: (b[0] if b else None) for p, b in c.get("connections", {}).items()}
            conns = {p: (CONST0 if b == "0" else CONST1 if b == "1" else b) for p, b in conns.items() if b is not None and b != "x"}
            if info is None:
                self.unknown.append(f"{cname} ({t})")
                continue
            pins = info["pins"]
            if info.get("seq"):
                sq = info["seq"]
                q = qn = None
                for p, (d, f) in pins.items():
                    if d == "output" and p in conns:
                        if f.strip() == sq["q"]:
                            q = conns[p]
                        elif f.strip() == sq["qn"]:
                            qn = conns[p]
                fn, nm = compile_fn(sq["next"] or "D")
                ins = {p: b for p, b in conns.items() if pins.get(p, ("",))[0] == "input"}
                ck = sq.get("clock", "").strip("!() ")
                if ck in ins:
                    self.clock_bits.add(ins[ck])
                self.flops.append(Flop(cname, t, ins, q, qn, fn, nm, ck))
                for b in (q, qn):
                    if isinstance(b, int) and b >= 0:
                        self.driver[b] = ("flop", len(self.flops) - 1)
            else:
                ins = {p: b for p, b in conns.items() if pins.get(p, ("",))[0] == "input"}
                outs = {p: b for p, b in conns.items() if pins.get(p, ("",))[0] == "output"}
                fns = {}
                for p in outs:
                    f = pins[p][1]
                    if not f:
                        self.unknown.append(f"{cname}.{p} (no function)")
                        continue
                    fns[p] = compile_fn(f)
                self.cells.append(Cell(cname, t, ins, outs, fns))
                for p, b in outs.items():
                    if isinstance(b, int) and b >= 0:
                        self.driver[b] = ("cell", len(self.cells) - 1)
        self.pis, self.pos = [], []
        for pname, p in mod.get("ports", {}).items():
            for i, b in enumerate(p["bits"]):
                if not isinstance(b, int):
                    continue
                nm = pname if len(p["bits"]) == 1 else f"{pname}[{i}]"
                if p["direction"] == "input":
                    if b not in self.clock_bits:
                        self.pis.append((nm, b))
                else:
                    self.pos.append((nm, b))
        self.constraints: dict[str, int] = {}
        self._levelize()

    def _name_bits(self):
        for pname, p in self.mod.get("ports", {}).items():
            for i, b in enumerate(p["bits"]):
                if isinstance(b, int):
                    self.names[b] = pname if len(p["bits"]) == 1 else f"{pname}[{i}]"
        for nn, n in self.mod.get("netnames", {}).items():
            for i, b in enumerate(n["bits"]):
                if isinstance(b, int) and (b not in self.names or (not n.get("hide_name") and self.names[b].startswith("$"))):
                    self.names[b] = nn if len(n["bits"]) == 1 else f"{nn}[{i}]"

    def _levelize(self):
        known = {CONST0, CONST1} | {b for _, b in self.pis} | self.clock_bits
        for f in self.flops:
            known |= {b for b in (f.q, f.qn) if isinstance(b, int)}
        order, pending = [], list(range(len(self.cells)))
        while pending:
            nxt = []
            progress = False
            for ci in pending:
                c = self.cells[ci]
                if all((b in known) or (b not in self.driver) for b in c.ins.values()):
                    order.append(ci)
                    known |= set(c.outs.values())
                    progress = True
                else:
                    nxt.append(ci)
            if not progress:          # combinational loop: break it
                self.loops = [self.cells[ci].name for ci in nxt]
                order += nxt
                break
            pending = nxt
        else:
            self.loops = []
        self.order = order
        self.fanout: dict[int, list[int]] = {}
        pos = {ci: k for k, ci in enumerate(order)}
        for ci in order:
            for b in self.cells[ci].ins.values():
                self.fanout.setdefault(b, []).append(ci)
        self.pos_in_order = pos
        self.flop_readers: dict[int, list[int]] = {}
        for fi, f in enumerate(self.flops):
            for b in f.pins.values():
                self.flop_readers.setdefault(b, []).append(fi)
        self.po_bits = {b for _, b in self.pos}

    # inputs of the test model: PIs + PPIs (flop states)
    def inputs(self) -> list[tuple[str, int, str]]:
        res = [(n, b, "pi") for n, b in self.pis]
        for i, f in enumerate(self.flops):
            res.append((f.name, -1000 - i, "ppi"))
        return res

    def all_nets(self) -> list[int]:
        bits = set()
        for c in self.cells:
            bits |= {b for b in list(c.ins.values()) + list(c.outs.values()) if isinstance(b, int) and b >= 0}
        for f in self.flops:
            bits |= {b for b in list(f.pins.values()) + [f.q, f.qn] if isinstance(b, int) and b >= 0}
        bits |= {b for _, b in self.pis} | {b for _, b in self.pos}
        return sorted(bits - self.clock_bits)

    # ── bit-parallel simulation ──
    def simulate(self, inputs: dict[int, int], M: int, force: dict[int, int] | None = None) -> dict[int, int]:
        v = {CONST0: 0, CONST1: M}
        for b in self.clock_bits:
            v[b] = 0
        for n, b in self.pis:
            v[b] = inputs.get(b, 0)
        for i, f in enumerate(self.flops):
            s = inputs.get(-1000 - i, 0)
            if f.q is not None:
                v[f.q] = s
            if f.qn is not None:
                v[f.qn] = (~s) & M
        if force:
            v.update({b: val for b, val in force.items()})
        for ci in self.order:
            c = self.cells[ci]
            vals = {p: v.get(b, 0) for p, b in c.ins.items()}
            for p, (fn, _) in c.fns.items():
                b = c.outs[p]
                if force and b in force:
                    continue
                v[b] = fn(vals, M)
        return v

    def observe(self, v: dict[int, int], M: int) -> list[int]:
        obs = [v.get(b, 0) for _, b in self.pos]
        for f in self.flops:
            obs.append(f.next_fn({p: v.get(b, 0) for p, b in f.pins.items()}, M))
        return obs

    def fault_detect(self, good: dict[int, int], fault: Fault, M: int) -> int:
        """Event-driven single-fault propagation; returns detection mask."""
        fv = M if fault.sv else 0
        if good.get(fault.bit, 0) == fv:
            return 0
        ov = {fault.bit: fv}
        changed = {fault.bit}
        todo = sorted(set(self.fanout.get(fault.bit, [])), key=lambda ci: self.pos_in_order[ci])
        import heapq
        heap = [self.pos_in_order[ci] for ci in todo]
        heapq.heapify(heap)
        seen = set(heap)
        while heap:
            k = heapq.heappop(heap)
            c = self.cells[self.order[k]]
            vals = {p: ov.get(b, good.get(b, 0)) for p, b in c.ins.items()}
            for p, (fn, _) in c.fns.items():
                b = c.outs[p]
                if b == fault.bit:
                    continue
                nv = fn(vals, M)
                if nv != good.get(b, 0):
                    ov[b] = nv
                    changed.add(b)
                    for ci2 in self.fanout.get(b, []):
                        kk = self.pos_in_order[ci2]
                        if kk not in seen:
                            seen.add(kk)
                            heapq.heappush(heap, kk)
                elif b in ov:
                    del ov[b]
        det = 0
        for b in changed:
            if b in self.po_bits and b in ov:
                det |= ov[b] ^ good.get(b, 0)
        flops = set()
        for b in changed:
            flops.update(self.flop_readers.get(b, []))
        for fi in flops:
            f = self.flops[fi]
            gv = f.next_fn({p: good.get(b, 0) for p, b in f.pins.items()}, M)
            fvv = f.next_fn({p: ov.get(b, good.get(b, 0)) for p, b in f.pins.items()}, M)
            det |= gv ^ fvv
        return det

    # ── 3-valued single-pattern sim (for PODEM) ──
    def sim3(self, assign: dict[int, int | None], fault: Fault | None = None):
        """Returns (good, faulty) value dicts with 0/1/None."""
        def run(forced):
            v: dict[int, int | None] = {CONST0: 0, CONST1: 1}
            for b in self.clock_bits:
                v[b] = 0
            for n, b in self.pis:
                v[b] = assign.get(b)
            for i, f in enumerate(self.flops):
                s = assign.get(-1000 - i)
                if f.q is not None:
                    v[f.q] = s
                if f.qn is not None:
                    v[f.qn] = None if s is None else 1 - s
            if forced:
                v.update(forced)
            for ci in self.order:
                c = self.cells[ci]
                for p, (fn, names) in c.fns.items():
                    b = c.outs[p]
                    if forced and b in forced:
                        continue
                    v[b] = _eval3(fn, names, {q: v.get(bb) for q, bb in c.ins.items()})
            return v
        g = run(None)
        fv = run({fault.bit: fault.sv}) if fault else g
        return g, fv

    def obs3(self, v):
        res = [v.get(b) for _, b in self.pos]
        for f in self.flops:
            res.append(_eval3(f.next_fn, f.next_names, {p: v.get(b) for p, b in f.pins.items()}))
        return res


def _eval3(fn, names, vals):
    xs = [n for n in names if vals.get(n) is None]
    if not xs:
        return fn({n: vals.get(n, 0) for n in names}, 1)
    if len(xs) > 8:
        return None
    seen = set()
    for combo in product((0, 1), repeat=len(xs)):
        vv = {n: vals.get(n, 0) for n in names}
        vv.update(dict(zip(xs, combo)))
        seen.add(fn(vv, 1))
        if len(seen) > 1:
            return None
    return seen.pop()


# ─────────────────────────────── ATPG driver ───────────────────────────────
@dataclass
class AtpgResult:
    patterns: list = field(default_factory=list)      # list of dict input_bit -> 0/1
    curve: list = field(default_factory=list)         # (n_patterns, coverage%)
    faults: list = field(default_factory=list)


def build_faults(c: Circuit) -> list[Fault]:
    fl = []
    for b in c.all_nets():
        n = c.names.get(b, f"n{b}")
        fl.append(Fault(b, 0, n))
        fl.append(Fault(b, 1, n))
    return fl


def run_atpg(c: Circuit, faults: list[Fault], max_random: int = 2048, podem: bool = True,
             backtracks: int = 200, seed: int = 1, progress=None, podem_budget: float = 60.0) -> AtpgResult:
    rng = random.Random(seed)
    ins = c.inputs()
    W = 64
    M = (1 << W) - 1
    res = AtpgResult(faults=faults)
    total = len(faults)
    live = [f for f in faults if f.status != "detected"]
    npat = 0
    stale = 0
    cons = c.constraints
    # ── random phase ──
    while npat < max_random and live and stale < 6:
        block = {}
        for n, b, kind in ins:
            if n in cons:
                block[b] = M if cons[n] else 0
            else:
                block[b] = rng.getrandbits(W)
        good = c.simulate(block, M)
        used = 0
        new_live = []
        before = len(live)
        hits = []
        for f in live:
            d = c.fault_detect(good, f, M)
            if d:
                lsb = (d & -d).bit_length() - 1
                f.status = "detected"
                hits.append((f, lsb))
                used |= 1 << lsb
            else:
                new_live.append(f)
        # keep only the useful patterns of this block (static compaction)
        kept = {}
        for k in range(W):
            if used >> k & 1:
                kept[k] = len(res.patterns)
                res.patterns.append({b: (block[b] >> k) & 1 for _, b, _ in ins})
        for f, lsb in hits:
            f.pattern = kept[lsb]
        npat += W
        live = new_live
        stale = stale + 1 if len(live) == before else 0
        res.curve.append((len(res.patterns), 100.0 * (total - len(live)) / total if total else 100.0))
        if progress:
            progress(npat, total - len(live), total)
    # ── deterministic phase (PODEM) ──
    if podem and live:
        import time as _t
        budget_end = _t.time() + podem_budget
        for f in list(live):
            if f.status == "detected":
                continue
            if _t.time() > budget_end:
                f.status = "aborted"
                continue
            pat = _podem(c, f, backtracks, cons, time_limit=min(2.0, max(0.05, budget_end - _t.time())))
            if pat is None:
                continue
            if pat == "redundant":
                f.status = "redundant"
                continue
            if pat == "aborted":
                f.status = "aborted"
                continue
            filled = {b: (pat.get(b) if pat.get(b) is not None else rng.getrandbits(1)) for _, b, _ in ins}
            for n, b, _ in ins:
                if n in cons:
                    filled[b] = cons[n]
            good = c.simulate({b: (M if v else 0) for b, v in filled.items()}, M)
            res.patterns.append(filled)
            pid = len(res.patterns) - 1
            for g in live:
                if g.status in ("detected", "redundant"):
                    continue
                if c.fault_detect(good, g, M) & 1:
                    g.status, g.pattern = "detected", pid
            res.curve.append((len(res.patterns), 100.0 * sum(1 for x in faults if x.status == "detected") / total))
    for f in faults:
        if f.status == "untested":
            f.status = "undetected"
    # coverage curve per kept pattern (first-detection index)
    first = [0] * (len(res.patterns) + 1)
    for f in faults:
        if f.status == "detected" and 0 <= f.pattern < len(res.patterns):
            first[f.pattern] += 1
    run, curve = 0, []
    step = max(1, len(res.patterns) // 200)
    for i in range(len(res.patterns)):
        run += first[i]
        if i % step == 0 or i == len(res.patterns) - 1:
            curve.append((i + 1, 100.0 * run / total if total else 100.0))
    res.curve = curve
    return res


def _podem(c: Circuit, fault: Fault, limit: int, cons: dict, time_limit: float = 2.0):
    ins = c.inputs()
    pi_bits = {b for _, b, _ in ins}
    assign: dict[int, int | None] = {b: None for b in pi_bits}
    for n, b, _ in ins:
        if n in cons:
            assign[b] = cons[n]
    free = {b for n, b, _ in ins if n not in cons}
    back = [0]
    driver = c.driver

    def status():
        g, fv = c.sim3(assign, fault)
        og, of = c.obs3(g), c.obs3(fv)
        for a, b in zip(og, of):
            if a is not None and b is not None and a != b:
                return "detected", g, fv
        if g.get(fault.bit) == fault.sv:
            return "fail", g, fv
        return "open", g, fv

    def objective(g, fv):
        if g.get(fault.bit) is None:
            return fault.bit, 1 - fault.sv
        # D-frontier: cell with an input carrying D and an X output
        for ci in c.order:
            cell = c.cells[ci]
            has_d = any(g.get(b) is not None and fv.get(b) is not None and g.get(b) != fv.get(b) for b in cell.ins.values())
            if not has_d:
                continue
            if any(g.get(b) is None or fv.get(b) is None for b in cell.outs.values()):
                for b in cell.ins.values():
                    if g.get(b) is None:
                        return b, 1
        for f in c.flops:   # D at a flop input with X capture
            if any(g.get(b) is not None and fv.get(b) is not None and g.get(b) != fv.get(b) for b in f.pins.values()):
                for b in f.pins.values():
                    if g.get(b) is None:
                        return b, 1
        return None

    def backtrace(b, v, g):
        for _ in range(10000):
            if b in free and assign.get(b) is None:
                return b, v
            # flop outputs map to PPIs
            d = driver.get(b)
            if d is None:
                return None
            kind, idx = d
            if kind == "flop":
                fl = c.flops[idx]
                ppi = -1000 - idx
                if ppi in free and assign.get(ppi) is None:
                    return ppi, (v if b == fl.q else 1 - v)
                return None
            cell = c.cells[idx]
            xin = [bb for bb in cell.ins.values() if g.get(bb) is None]
            if not xin:
                return None
            b = xin[0]
            v = v  # heuristic: keep value; PODEM backtracking guarantees completeness
        return None

    import time as _t
    t_end = _t.time() + time_limit

    def rec() -> str:
        if _t.time() > t_end:
            return "abort"
        st, g, fv = status()
        if st == "detected":
            return "ok"
        if st == "fail":
            return "fail"
        obj = objective(g, fv)
        if obj is None:
            return "fail"
        bt = backtrace(obj[0], obj[1], g)
        if bt is None:
            return "fail"
        pb, pv = bt
        for val in (pv, 1 - pv):
            assign[pb] = val
            r = rec()
            if r == "ok":
                return "ok"
            if r == "abort":
                return "abort"
            back[0] += 1
            if back[0] > limit:
                return "abort"
        assign[pb] = None
        return "fail"

    r = rec()
    if r == "ok":
        return dict(assign)
    if r == "abort":
        return "aborted"
    return "redundant"


# ─────────────────────────────── scan insertion ───────────────────────────────
def scan_equivalent(lib_path: str, flop_type: str) -> tuple[str, dict] | None:
    """Find a scan flop for `flop_type`: same next-state (with SCE=0) and extra SD/SE pins."""
    cells = libscan.scan(lib_path)["cells"]
    info = cells.get(flop_type)
    if not info or not info.get("seq"):
        return None
    base_pins = {p for p, (d, _) in info["pins"].items() if d == "input"}
    m = re.match(r"(.*__)(\w+?)(_\d+)$", flop_type)
    cands = []
    if m:
        cands.append(f"{m.group(1)}s{m.group(2)}{m.group(3)}")
    cands += [n for n, ci in cells.items() if ci.get("seq")]
    for n in cands:
        ci = cells.get(n)
        if not ci or not ci.get("seq"):
            continue
        ins = {p for p, (d, _) in ci["pins"].items() if d == "input"}
        extra = ins - base_pins
        if not base_pins <= ins or len(extra) != 2:
            continue
        nxt = ci["seq"]["next"].replace(" ", "")
        se = next((p for p in extra if re.search(rf"[&*]{p}\)|\({p}[&*]|&{p}$|!{p}", nxt)), None)
        sd = next((p for p in extra if p != se), None)
        if se and sd and ci["seq"].get("clear", "") == info["seq"].get("clear", "") \
                and ci["seq"].get("preset", "") == info["seq"].get("preset", ""):
            return n, {"SD": sd, "SE": se}
    return None


def insert_scan(data: dict, top: str, lib_path: str, chains: int = 1, order: list[str] | None = None):
    """Mutates the Yosys JSON; returns chain description list."""
    mod = data["modules"][top]
    cells = libscan.scan(lib_path)["cells"]
    maxbit = 1
    for p in mod["ports"].values():
        maxbit = max([maxbit] + [b for b in p["bits"] if isinstance(b, int)])
    for n in mod["netnames"].values():
        maxbit = max([maxbit] + [b for b in n["bits"] if isinstance(b, int)])
    for c in mod["cells"].values():
        for bits in c["connections"].values():
            maxbit = max([maxbit] + [b for b in bits if isinstance(b, int)])

    def newbit():
        nonlocal maxbit
        maxbit += 1
        return maxbit
    flops = [n for n, c in mod["cells"].items() if cells.get(c["type"], {}).get("seq")]
    if order:
        flops = [f for f in order if f in flops] + [f for f in flops if f not in order]
    else:
        flops.sort()
    if not flops:
        raise ValueError("no flip-flops to scan")
    se_bit = newbit()
    mod["ports"]["scan_en"] = {"direction": "input", "bits": [se_bit]}
    mod["netnames"]["scan_en"] = {"bits": [se_bit], "hide_name": 0}
    k = max(1, min(chains, len(flops)))
    per = (len(flops) + k - 1) // k
    desc = []
    unscannable = []
    for ch in range(k):
        members = flops[ch * per:(ch + 1) * per]
        if not members:
            continue
        si = newbit()
        pname_in = f"scan_in{ch}" if k > 1 else "scan_in"
        pname_out = f"scan_out{ch}" if k > 1 else "scan_out"
        mod["ports"][pname_in] = {"direction": "input", "bits": [si]}
        mod["netnames"][pname_in] = {"bits": [si], "hide_name": 0}
        prev = si
        chain = []
        for fname in members:
            c = mod["cells"][fname]
            eq = scan_equivalent(lib_path, c["type"])
            if not eq:
                unscannable.append(f"{fname} ({c['type']})")
                continue
            stype, pmap = eq
            c["type"] = stype
            c.setdefault("port_directions", {})
            c["connections"][pmap["SD"]] = [prev]
            c["connections"][pmap["SE"]] = [se_bit]
            c["port_directions"][pmap["SD"]] = "input"
            c["port_directions"][pmap["SE"]] = "input"
            qpin = next((p for p, (d, f) in cells[stype]["pins"].items()
                         if d == "output" and f.strip() == cells[stype]["seq"]["q"]), "Q")
            q = c["connections"].get(qpin)
            if not q:
                q = [newbit()]
                c["connections"][qpin] = q
            prev = q[0]
            chain.append({"inst": fname, "type": stype})
        mod["ports"][pname_out] = {"direction": "output", "bits": [prev]}
        desc.append({"name": f"chain{ch}", "scan_in": pname_in, "scan_out": pname_out, "cells": chain})
    return desc, unscannable


def write_verilog(data: dict, top: str) -> str:
    mod = data["modules"][top]
    name_of: dict[int, str] = {}
    decl: list[str] = []
    ports = list(mod["ports"].items())
    for pn, p in ports:
        for i, b in enumerate(p["bits"]):
            if isinstance(b, int) and b not in name_of:
                name_of[b] = _vid(pn) if len(p["bits"]) == 1 else f"{_vid(pn)}[{i}]"
    wires = []
    for nn, n in sorted(mod.get("netnames", {}).items(), key=lambda kv: kv[1].get("hide_name", 0)):
        if nn in mod["ports"]:
            continue
        fresh = [b for b in n["bits"] if isinstance(b, int) and b not in name_of]
        if not fresh:
            continue
        w = len(n["bits"])
        wires.append(f"  wire {'[' + str(w - 1) + ':0] ' if w > 1 else ''}{_vid(nn)};")
        for i, b in enumerate(n["bits"]):
            if isinstance(b, int) and b not in name_of:
                name_of[b] = _vid(nn) if w == 1 else f"{_vid(nn)}[{i}]"
    extra = []

    def ref(bits):
        parts = []
        for b in bits:
            if b in ("0", "1"):
                parts.append(f"1'b{b}")
            elif b == "x" or b is None:
                parts.append("1'bx")
            else:
                if b not in name_of:
                    name_of[b] = f"_rf_n{b}_"
                    extra.append(f"  wire _rf_n{b}_;")
                parts.append(name_of[b])
        return parts[0] if len(parts) == 1 else "{" + ", ".join(reversed(parts)) + "}"
    body = []
    for cn, c in mod["cells"].items():
        conns = ", ".join(f".{p}({ref(b)})" for p, b in c["connections"].items())
        body.append(f"  {c['type']} {_vid(cn)} ({conns});")
    # outputs that alias another port's bit need an assign
    assigns = []
    for pn, p in ports:
        if p["direction"] == "output":
            for i, b in enumerate(p["bits"]):
                me = _vid(pn) if len(p["bits"]) == 1 else f"{_vid(pn)}[{i}]"
                if isinstance(b, int) and name_of.get(b) != me:
                    assigns.append(f"  assign {me} = {name_of[b]};")
                elif b in ("0", "1"):
                    assigns.append(f"  assign {me} = 1'b{b};")
    hdr = [f"// RTLFlow modus — scan-inserted netlist", f"module {_vid(top)} ({', '.join(_vid(pn) for pn, _ in ports)});"]
    for pn, p in ports:
        w = len(p["bits"])
        decl.append(f"  {p['direction']} {'[' + str(w - 1) + ':0] ' if w > 1 else ''}{_vid(pn)};")
    return "\n".join(hdr + decl + wires + extra + body + assigns + ["endmodule", ""])


def _vid(n: str) -> str:
    n = n.lstrip("\\")
    return n if re.match(r"^[A-Za-z_][\w$]*$", n) else "\\" + n + " "
