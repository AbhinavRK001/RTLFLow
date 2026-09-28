"""modus — Modus-style DFT & ATPG shell (RTLFlow's own engine: rtlflow/backends/atpg.py).

    rtlflow> modus
    modus 1> read_netlist                 (default: genus's mapped netlist JSON)
    modus 2> build_model
    modus 3> verify_test_structures
    modus 4> connect_scan_chains -chains 1   (flops → scan flops, stitched chain; writes <top>_scan.v)
    modus 5> build_faultmodel
    modus 6> create_logic_tests            (random + PODEM, full-scan stuck-at)
    modus 7> report_fault_statistics
    modus 8> write_vectors
    modus 9> gui_show                      (coverage curve, fault browser, scan chain view)
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import CODENAME, VERSION, env
from ..backends import atpg as A
from ..backends import runner
from ..core.guishell import GuiMixin
from ..core.msg import C, console, error, info, note, ok, out, rule, table, warn
from ..core.project import Project
from ..core.shell import CmdError, Shell, command, expand_files, parse_opts


@dataclass
class ModusState:
    top: str = ""
    lib: str = ""
    netlist: str = ""
    json: str = ""
    scan: bool = False
    chains: list = field(default_factory=list)
    coverage: float = 0.0
    patterns: int = 0


class ModusShell(GuiMixin, Shell):
    tool = "modus"
    msg_prefix = "RFM"
    history_name = ".rtlflow_history_modus"
    gui_module = "modus_gui"
    gui_label = "Modus GUI"

    def __init__(self, gui: bool = False, files: list[str] | None = None) -> None:
        super().__init__()
        self._gui_init()
        self.project = Project()
        self.st = ModusState()
        self.want_gui = gui
        self.files = files or []
        self.data: dict | None = None
        self.circ: A.Circuit | None = None
        self.faults: list[A.Fault] = []
        self.result: A.AtpgResult | None = None

    def prompt(self) -> str:
        return f"{C.BOLD}{C.GRN}modus {self.counter}>{C.RESET} "

    def help_order(self) -> list[str]:
        return ["Setup", "DFT", "ATPG", "Reports", "Output", "GUI", "Shell"]

    @property
    def ddir(self) -> Path:
        return self.project.path("runs", "dft")

    def start(self) -> bool:
        console.open_logs(self.project.logs, "modus")
        out("")
        out(f"{C.BOLD}{C.WHT}           RTLFlow Modus(TM) DFT Software Solution{C.RESET}")
        out(f"{C.GRY}      Version {VERSION} ({CODENAME}) · engine: RTLFlow ATPG (full-scan stuck-at, random + PODEM){C.RESET}")
        out(rule())
        out(f"  {C.GRY}Log file  {C.RESET}{self.project.rel(console.log_path)}")
        g = self.project.get("genus") or {}
        if g.get("top"):
            note(f"  Genus netlist available for '{g['top']}' → read_netlist")
        out(rule())
        if self.want_gui:
            self.gui_open()
        return True

    def on_start(self) -> None:
        for f in self.files:
            self.c_source([f])

    def on_exit(self) -> None:
        if self.st.top:
            self.project.set(modus=asdict(self.st))
        self.gui_close()
        console.close_logs()

    def _need_model(self):
        if not self.circ:
            raise CmdError("No test model — read_netlist, then build_model", "RFM-MOD-001")

    # ─────────── setup ───────────
    @command("read_libs", cat="Setup", usage="read_libs <file.lib>", help="Liberty library (cell functions)")
    def c_read_libs(self, args):
        f = (expand_files(args) or [str(env.pdk().liberty() or "")])[0]
        if not f or not Path(f).exists():
            raise CmdError("usage: read_libs <file.lib>")
        self.st.lib = f
        n = len(A.libscan.scan(f)["cells"])
        ok(f"  Library {Path(f).name}: {n} cells")
        return True

    @command("read_netlist", cat="Setup", usage="read_netlist [netlist.v|netlist.json] [-top name]",
             help="Read the mapped netlist (default: genus output)")
    def c_read_netlist(self, args):
        opts, pos = parse_opts(args, valued={"-top": 1})
        g = self.project.get("genus") or {}
        top = opts.get("-top") or g.get("top") or self.st.top
        if not self.st.lib:
            libs = g.get("libs") or []
            self.c_read_libs(libs[:1])
        if pos:
            f = Path(expand_files(pos)[0])
        else:
            f = self.project.synth / f"{top}.mapped.json"
            if not f.exists():
                f = self.project.synth / f"{top}_mapped.v"
        if not f.exists():
            raise CmdError(f"Netlist not found: {f}  (run genus syn_map first)", "RFM-NET-001")
        if f.suffix == ".json":
            js = f
        else:
            if not top:
                raise CmdError("read_netlist <file.v> -top <name>")
            js = self.ddir / f"{top}.model.json"
            script = (f'read_liberty -lib "{self.st.lib}"\nread_verilog "{f}"\nhierarchy -top {top}\n'
                      f'write_json "{js}"\n')
            r = runner.yosys(script, self.ddir / "to_json.ys", self.ddir / "to_json.log")
            if not r.ok or not js.exists():
                raise CmdError("Yosys could not convert the netlist to JSON (see .rtlflow/runs/dft/to_json.log)")
        self.data = json.loads(js.read_text())
        mods = list(self.data.get("modules", {}))
        if not top:
            top = next((m for m, v in self.data["modules"].items() if v.get("cells") and not v.get("attributes", {}).get("blackbox")), mods[0])
        self.st.top, self.st.netlist, self.st.json = top, str(f), str(js)
        self.st.scan = False
        self.circ = None
        ok(f"  Netlist {f.name} · top '{top}' · {len(self.data['modules'][top].get('cells', {}))} instances")
        return True

    @command("build_model", cat="Setup", aliases=["build_testmode"], help="Build the gate-level test model")
    def c_build_model(self, args):
        if not self.data:
            raise CmdError("read_netlist first")
        t0 = time.time()
        self.circ = A.Circuit(self.data, self.st.top, self.st.lib)
        c = self.circ
        cons = dict(c.constraints)
        if self.st.scan:
            cons["scan_en"] = 0
        # hold asynchronous set/reset inactive when driven by a primary input
        lib = A.libscan.scan(self.st.lib)["cells"]
        pin_of = {b: n for n, b in c.pis}
        for f in c.flops:
            sq = lib[f.type]["seq"]
            for expr in (sq.get("clear", ""), sq.get("preset", "")):
                e = expr.replace(" ", "")
                if not e:
                    continue
                neg = e.startswith("!")
                pin = e.lstrip("!").strip("()")
                b = f.pins.get(pin)
                if b in pin_of:
                    cons[pin_of[b]] = 1 if neg else 0
        c.constraints = cons
        self.faults = []
        self.result = None
        out(table([("Top", self.st.top), ("Combinational cells", len(c.cells)), ("Flip-flops", len(c.flops)),
                   ("Primary inputs (excl. clocks)", len(c.pis)), ("Clock nets", len(c.clock_bits)),
                   ("Primary outputs", len(c.pos)), ("Test constraints", ", ".join(f"{k}={v}" for k, v in cons.items()) or "-"),
                   ("Scan", "inserted" if self.st.scan else "full-scan assumed (connect_scan_chains to insert)"),
                   ("Build time", f"{time.time() - t0:.2f}s")], headers=("build_model", "")))
        if c.unknown:
            warn("RFM-MOD-010", f"{len(c.unknown)} cells not modelled (no liberty function): {', '.join(c.unknown[:6])}")
        if c.loops:
            warn("RFM-MOD-011", f"Combinational loop through {len(c.loops)} cells — results may be pessimistic")
        self.gui_sync()
        return True

    # ─────────── DFT ───────────
    @command("verify_test_structures", cat="DFT", aliases=["check_dft_rules"], help="DFT rule checks")
    def c_verify(self, args):
        self._need_model()
        c = self.circ
        lib = A.libscan.scan(self.st.lib)["cells"]
        rows = []
        unsc = [f.name for f in c.flops if not A.scan_equivalent(self.st.lib, f.type) and not self.st.scan]
        rows.append(("Flops without scan equivalent", len(unsc)))
        latches = [f.name for f in c.flops if lib[f.type]["seq"]["kind"] == "latch"]
        rows.append(("Latches", len(latches)))
        inport = {b for p in c.mod["ports"].values() if p["direction"] == "input" for b in p["bits"]}
        ungated = [f.name for f in c.flops if f.pins.get(f.clock_pin) not in inport]
        rows.append(("Flops with internally generated clocks", len(ungated)))
        rows.append(("Combinational loops", len(c.loops)))
        rows.append(("Unmodelled cells", len(c.unknown)))
        out(table(rows, headers=("verify_test_structures", "count")))
        bad = unsc or ungated or c.loops
        if bad:
            warn("RFM-DFT-001", "DFT rule violations found (see counts above)")
        else:
            ok("All test structure checks passed.")
        return not bad

    @command("connect_scan_chains", cat="DFT", aliases=["insert_dft", "insert_scan"],
             usage="connect_scan_chains [-chains N] [-order placement|name]",
             help="Replace flops with scan flops and stitch scan chains")
    def c_scan(self, args):
        if not self.data:
            raise CmdError("read_netlist first")
        if self.st.scan:
            warn("RFM-DFT-010", "Scan already inserted")
            return True
        opts, _ = parse_opts(args, valued={"-chains": 1, "-order": 1})
        order = None
        if opts.get("-order") == "placement":
            order = self._placement_order()
        desc, un = A.insert_scan(self.data, self.st.top, self.st.lib, int(opts.get("-chains") or 1), order)
        self.st.scan, self.st.chains = True, desc
        v = self.project.synth / f"{self.st.top}_scan.v"
        v.write_text(A.write_verilog(self.data, self.st.top))
        js = self.ddir / f"{self.st.top}.scan.json"
        js.write_text(json.dumps(self.data))
        self.st.json = str(js)
        rows = [(d["name"], d["scan_in"], d["scan_out"], len(d["cells"])) for d in desc]
        out(table(rows, headers=("Chain", "Scan-in", "Scan-out", "Length")))
        if un:
            warn("RFM-DFT-011", f"{len(un)} flop(s) left unscanned: {', '.join(un[:5])}")
        ok(f"Scan inserted — netlist {self.project.rel(v)} (ports scan_en, scan_in, scan_out)")
        info("  Next: build_model (scan_en constrained to 0 for capture) → build_faultmodel → create_logic_tests")
        info(f"  To implement the scan design: innovus read_netlist {self.project.rel(v)}")
        return self.c_build_model([])

    def _placement_order(self):
        d = self.project.pnr / "live.def"
        if not d.exists():
            return None
        from ..gui.lefdef import read_def
        dd = read_def(str(d))
        pos = {n: (c.y, c.x if int(c.y / 2.72) % 2 == 0 else -c.x) for n, c in dd.comps.items() if c.status != "UNPLACED"}
        return sorted(pos, key=lambda n: pos[n])

    @command("report_scan_chains", cat="DFT", help="Scan chain membership")
    def c_report_chains(self, args):
        if not self.st.chains:
            info("No scan chains (connect_scan_chains)")
            return True
        for d in self.st.chains:
            out(f"  {C.BOLD}{d['name']}{C.RESET}  {d['scan_in']} → " + " → ".join(c["inst"] for c in d["cells"]) + f" → {d['scan_out']}")
        return True

    # ─────────── ATPG ───────────
    @command("build_faultmodel", cat="ATPG", help="Stuck-at-0/1 faults on every net")
    def c_faults(self, args):
        self._need_model()
        self.faults = A.build_faults(self.circ)
        self.result = None
        ok(f"  {len(self.faults)} stuck-at faults on {len(self.faults) // 2} nets")
        self.gui_sync()
        return True

    @command("create_logic_tests", cat="ATPG", aliases=["run_atpg"],
             usage="create_logic_tests [-max_random N] [-no_podem] [-backtracks N] [-time S]",
             help="Generate stuck-at patterns (random + deterministic PODEM)")
    def c_atpg(self, args):
        self._need_model()
        if not self.faults:
            self.c_faults([])
        opts, _ = parse_opts(args, flags={"-no_podem"}, valued={"-max_random": 1, "-backtracks": 1, "-time": 1, "-seed": 1})
        for f in self.faults:
            f.status, f.pattern = "untested", -1
        t0 = time.time()
        last = [0.0]

        def prog(n, det, tot):
            if time.time() - last[0] > 0.5:
                last[0] = time.time()
                self.gui_send(t="progress", patterns=n, detected=det, total=tot)
        note("Random pattern phase (bit-parallel fault simulation) …")
        self.result = A.run_atpg(self.circ, self.faults, max_random=int(opts.get("-max_random") or 4096),
                                 podem=not opts.get("-no_podem"), backtracks=int(opts.get("-backtracks") or 200),
                                 seed=int(opts.get("-seed") or 1), progress=prog, podem_budget=float(opts.get("-time") or 60))
        # scan-path faults are covered by the chain (shift) test
        if self.st.scan:
            chain_nets = {"scan_en"} | {d["scan_in"] for d in self.st.chains} | {d["scan_out"] for d in self.st.chains}
            for f in self.faults:
                if f.status != "detected" and f.net in chain_nets:
                    f.status = "chain"
        st = self._stats()
        self.st.coverage, self.st.patterns = st["coverage"], len(self.result.patterns)
        self.c_stats([])
        info(f"  ATPG time {time.time() - t0:.2f}s")
        self.gui_sync()
        return True

    def _stats(self) -> dict:
        cnt = Counter(f.status for f in self.faults)
        tot = len(self.faults) or 1
        det = cnt["detected"] + cnt["chain"]
        red = cnt["redundant"]
        return {"total": len(self.faults), "detected": cnt["detected"], "chain": cnt["chain"], "redundant": red,
                "aborted": cnt["aborted"], "undetected": cnt["undetected"] + cnt["untested"],
                "coverage": 100.0 * det / tot, "test_coverage": 100.0 * det / max(1, tot - red)}

    @command("report_fault_statistics", cat="Reports", aliases=["report_faults_summary"], help="Fault / test coverage")
    def c_stats(self, args):
        if not self.faults:
            raise CmdError("No fault model — build_faultmodel")
        s = self._stats()
        out(table([("Total faults", s["total"]), ("Detected by ATPG patterns", s["detected"]),
                   ("Detected by scan chain test", s["chain"]), ("Redundant (proven untestable)", s["redundant"]),
                   ("Aborted", s["aborted"]), ("Undetected", s["undetected"]),
                   ("Fault coverage", f"{s['coverage']:.2f} %"), ("Test coverage (excl. redundant)", f"{s['test_coverage']:.2f} %"),
                   ("Patterns", len(self.result.patterns) if self.result else 0)], headers=("Stuck-at fault statistics", "")))
        return True

    @command("report_faults", cat="Reports", usage="report_faults [-undetected|-redundant|-all]", help="List faults")
    def c_faults_list(self, args):
        if not self.faults:
            raise CmdError("No fault model")
        want = "all" if "-all" in args else ("redundant" if "-redundant" in args else "undetected")
        rows = [(f.net, f"SA{f.sv}", f.status, f.pattern if f.pattern >= 0 else "") for f in self.faults
                if want == "all" or f.status == want or (want == "undetected" and f.status in ("undetected", "aborted"))]
        out(table(rows[:500] or [("-", "-", "-", "-")], headers=("Net", "Fault", "Status", "Pattern")))
        return True

    @command("write_vectors", cat="Output", usage="write_vectors [file.pat]", help="Write test patterns (scan load / capture / unload)")
    def c_write(self, args):
        if not self.result:
            raise CmdError("create_logic_tests first")
        c = self.circ
        fn = Path(args[0]) if args else self.ddir / f"{self.st.top}.pat"
        M = 1
        lines = [f"# RTLFlow modus patterns for {self.st.top} — {time.ctime()}",
                 f"# full-scan stuck-at; {len(self.result.patterns)} patterns",
                 "# PI order: " + " ".join(n for n, _ in c.pis),
                 "# scan cells: " + " ".join(f.name for f in c.flops),
                 "# PO order: " + " ".join(n for n, _ in c.pos)]
        for i, p in enumerate(self.result.patterns):
            v = c.simulate({b: val for b, val in p.items()}, M)
            obs = c.observe(v, M)
            pi = "".join(str(p.get(b, 0)) for _, b in c.pis)
            load = "".join(str(p.get(-1000 - k, 0)) for k in range(len(c.flops)))
            po = "".join(str(x) for x in obs[:len(c.pos)])
            unload = "".join(str(x) for x in obs[len(c.pos):])
            lines.append(f"pattern {i}: pi={pi} load={load} expect_po={po} unload={unload}")
        fn.write_text("\n".join(lines) + "\n")
        ok(f"{len(self.result.patterns)} patterns written to {self.project.rel(fn)}")
        return True

    @command("status", cat="Reports", help="Model / DFT / ATPG state")
    def c_status(self, args):
        rows = [("Top", self.st.top or "-"), ("Library", Path(self.st.lib).name if self.st.lib else "-"),
                ("Netlist", Path(self.st.netlist).name if self.st.netlist else "-"),
                ("Model", "built" if self.circ else "-"), ("Scan", f"{len(self.st.chains)} chain(s)" if self.st.scan else "no"),
                ("Faults", len(self.faults)), ("Coverage", f"{self.st.coverage:.2f} %" if self.result else "-"),
                ("Patterns", self.st.patterns if self.result else "-")]
        out(table(rows, headers=("modus status", "")))
        return True

    # ─────────── GUI ───────────
    def gui_sync(self) -> None:
        c = self.circ
        self.gui_send(t="model", top=self.st.top, built=bool(c), scan=self.st.scan, chains=self.st.chains,
                      stats={"cells": len(c.cells), "flops": len(c.flops), "pis": len(c.pis), "pos": len(c.pos)} if c else {},
                      flops=[f.name for f in c.flops] if c else [])
        if self.faults:
            self.gui_send(t="faults", stats=self._stats(),
                          faults=[[f.net, f.sv, f.status, f.pattern] for f in self.faults],
                          curve=self.result.curve if self.result else [])


def run_modus(gui: bool = False, files: list[str] | None = None) -> int:
    sh = ModusShell(gui=gui, files=files)
    if not sh.start():
        console.close_logs()
        return 1
    return sh.cmdloop()
