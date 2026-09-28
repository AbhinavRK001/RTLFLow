"""genus — Genus-style synthesis shell on Yosys (+ OpenSTA for timing).

    rtlflow> genus            → @genus:root: N>        (terminal only, like Cadence)
    @genus:root: 5> gui_show  → Genus GUI: schematic, hierarchy, reports (same session)
    rtlflow> genus -gui       → start with the GUI open

Stages are Yosys runs chained through RTLIL checkpoints in .rtlflow/runs/synth:
    elaborate    read + hierarchy + proc              → <top>.elab.il
    syn_generic  synth -run :fine (coarse, generic)    → <top>.generic.il
    syn_map      techmap + dfflibmap + abc (area)      → <top>.mapped.il / <top>_mapped.v
    syn_opt      re-map with delay-driven ABC script, keep if better → <top>_mapped.v
Timing (report_timing / report_qor) runs OpenSTA on the mapped netlist + SDC.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import CODENAME, VERSION, env
from ..backends import liberty as libscan
from ..backends import runner
from ..core.msg import C, colorize_backend_line, console, error, info, note, ok, out, rule, table, warn
from ..core.project import Project
from ..core.shell import CmdError, Shell, command, expand_files, parse_opts
from ..gui.host import GuiHost, display_available
from ..tech import match_profile

GSTAGES = ["empty", "read", "elaborated", "generic", "mapped", "optimized"]


@dataclass
class GenusState:
    hdl: list[str] = field(default_factory=list)
    hdl_lang: str = "sv"
    defines: list[str] = field(default_factory=list)
    incdirs: list[str] = field(default_factory=list)
    libs: list[str] = field(default_factory=list)
    lefs: list[str] = field(default_factory=list)
    top: str = ""
    stage: str = "empty"
    sdc_lines: list[str] = field(default_factory=list)
    sdc_files: list[str] = field(default_factory=list)
    netlist: str = ""
    area: dict = field(default_factory=dict)
    timing: dict = field(default_factory=dict)
    cells: dict = field(default_factory=dict)
    frontend: str = "yosys"
    effort: dict = field(default_factory=lambda: {"generic": "medium", "map": "medium", "opt": "medium"})

    def at_least(self, s: str) -> bool:
        return GSTAGES.index(self.stage) >= GSTAGES.index(s)


class GenusShell(Shell):
    tool = "genus"
    msg_prefix = "RFG"
    history_name = ".rtlflow_history_genus"

    def __init__(self, gui: bool = False, files: list[str] | None = None) -> None:
        super().__init__()
        self.project = Project()
        self.st = GenusState()
        self.want_gui = gui
        self.init_files = files or []
        self.gui: GuiHost | None = None
        self._gui_q: "queue.Queue[dict]" = queue.Queue()
        self._gui_worker: threading.Thread | None = None
        prev = self.project.get("genus")
        if prev and isinstance(prev, dict):
            # remember libs/RTL from the last run for convenience (not auto-loaded)
            self._prev = prev
        else:
            self._prev = {}

    def prompt(self) -> str:
        return f"{C.BOLD}{C.MAG}@genus:root: {self.counter}>{C.RESET} "

    def help_order(self) -> list[str]:
        return ["Setup", "HDL", "Constraints", "Synthesis", "Reports", "Output", "GUI", "Session", "Shell"]

    # ─────────────────────────────── lifecycle ───────────────────────────────
    def start(self) -> bool:
        console.open_logs(self.project.logs, "genus")
        y = env.tool("yosys")
        out("")
        out(f"{C.BOLD}{C.WHT}           RTLFlow Genus(R) Synthesis Solution{C.RESET}")
        out(f"{C.GRY}      Version {VERSION} ({CODENAME}) · logic synthesis: Yosys {y.version} · timing: OpenSTA{C.RESET}")
        out(rule())
        if not y.ok:
            error("RFG-ENV-001", "Yosys not found (tools -rescan, or RTLFLOW_YOSYS=/path/to/yosys)")
            return False
        for k, v in (("Yosys", env.shorten(y.path, 60)),
                     ("STA", env.shorten(env.tool("sta").path or env.tool("openroad").path, 60) or "-"),
                     ("Project", str(self.project.root).replace(str(Path.home()), "~")),
                     ("Log file", self.project.rel(console.log_path)),
                     ("Cmd file", self.project.rel(console.cmd_path))):
            out(f"  {C.GRY}{k:<10}{C.RESET}{v}")
        out(rule())
        if self._prev.get("hdl"):
            note(f"  Last session: top '{self._prev.get('top')}', {len(self._prev.get('hdl', []))} HDL file(s) — "
                 f"'restore_session' reloads them")
        out(f"  {C.GRY}Type 'help' · 'gui_show' opens the Genus GUI · 'man <cmd>' for usage{C.RESET}")
        out("")
        if self.want_gui:
            self.c_gui_show([])
        return True

    def on_start(self) -> None:
        for f in self.init_files:
            self.c_source([f])

    def on_exit(self) -> None:
        self._persist()
        if self.gui:
            self.gui.close()
        out(f"{C.GRY}genus: exiting · log {self.project.rel(console.log_path) if console.log_path else ''}{C.RESET}")
        console.close_logs()

    def _persist(self) -> None:
        if not (self.st.hdl or self.st.libs or self.st.top):
            return          # never overwrite a previous session with an empty one
        try:
            self.project.set(genus=asdict(self.st), top=self.st.top or self.project.get("top", ""))
        except Exception:
            pass

    def run_line(self, line: str, source: str = "terminal") -> bool:
        busy = line.strip() and not line.strip().startswith("#")
        if busy:
            self.gui_send(t="busy", cmd=line.strip()[:100])
        okk = False
        try:
            okk = super().run_line(line, source)
            return okk
        finally:
            if busy:
                self.gui_send(t="idle", cmd=line.strip()[:100], ok=okk)

    # ─────────────────────────────── helpers ───────────────────────────────
    @property
    def sdir(self) -> Path:
        return self.project.synth

    def _ck(self, stage: str) -> Path:
        return self.sdir / f"{self.st.top}.{stage}.il"

    def _yosys(self, script: str, name: str, show_filter=None) -> runner.RunResult:
        sp = self.sdir / f"{self.st.top or 'design'}.{name}.ys"
        log = self.sdir / f"{self.st.top or 'design'}.{name}.log"
        t0 = time.time()
        r = runner.yosys(script, sp, log, show=show_filter or runner.important)
        # yosys -q suppresses most output; the full log is in the -l file
        try:
            r.lines = log.read_text(errors="replace").splitlines()
        except OSError:
            pass
        if not r.ok:
            errs = list(dict.fromkeys(l for l in r.lines if l.startswith("ERROR") or "error" in l.lower()[:20]))
            for l in errs[-5:]:
                console.write(colorize_backend_line(l))
            info(f"  full log: {self.project.rel(log)}")
        console.log_only(f"[yosys {name}] {time.time() - t0:.2f}s rc={r.rc}")
        return r

    def _need(self, stage: str, cmd: str) -> None:
        if not self.st.at_least(stage):
            prev = {"read": "read_hdl", "elaborated": "elaborate", "generic": "syn_generic", "mapped": "syn_map"}[stage]
            raise CmdError(f"requires '{prev}' first (current stage: {self.st.stage})", "RFG-FLW-001")

    def _need_lib(self, cmd: str) -> str:
        if not self.st.libs:
            p = env.pdk()
            lib = p.liberty()
            if lib:
                note(f"No library set — using PDK default {lib.name}  (set_db library …)")
                self.st.libs = [str(lib)]
            else:
                raise CmdError(f"{cmd}: no liberty library (read_libs / set_db library)", "RFG-LIB-001")
        return self.st.libs[0]

    def _liberty_args(self) -> str:
        return " ".join(f"-liberty {_yq(l)}" for l in self.st.libs)

    def _dont_use(self) -> list[str]:
        prof = match_profile(" ".join(self.st.libs))
        return prof.dont_use

    def _clock_period_ps(self) -> int | None:
        for l in self.st.sdc_lines + [x for f in self.st.sdc_files for x in _safe_read(f).splitlines()]:
            m = re.search(r"create_clock\b.*-period\s+([0-9.]+)", l)
            if m:
                unit = libscan.scan(self.st.libs[0])["time_unit"] if self.st.libs else "1ns"
                mult = 1000.0 if unit.endswith("ns") else (1.0 if unit.endswith("ps") else 1000.0)
                return int(float(m.group(1)) * mult)
        return None

    def _stat(self, il: Path) -> dict:
        """Area & cell stats via `stat -liberty` (Yosys)."""
        script = f"read_rtlil {_yq(il)}\n" + "".join(f"read_liberty -lib {_yq(l)}\n" for l in self.st.libs) + \
                 f"tee -o {_yq(il.with_suffix('.stat.txt'))} stat {self._liberty_args()}\n"
        r = self._yosys(script, "stat", show_filter=lambda l: False)
        txt = _safe_read(il.with_suffix(".stat.txt")) or r.text
        return _parse_stat(txt)

    # ════════════════════════════════ Setup ════════════════════════════════
    @command("set_db", cat="Setup", usage="set_db <attribute> <value>",
             help="Set a root attribute (library, init_hdl_search_path, lef_library, syn_*_effort …)")
    def c_set_db(self, args):
        if len(args) < 2:
            raise CmdError("usage: set_db <attr> <value>")
        attr = args[0].lstrip("/").replace("root:", "").lstrip("/")
        val = " ".join(args[1:])
        if attr in ("library", "lib_search_path_library"):
            return self.c_read_libs(val.split())
        if attr in ("lef_library",):
            self.st.lefs = expand_files([val])
            ok(f"  lef_library = {', '.join(Path(x).name for x in self.st.lefs)}")
            return True
        if attr in ("init_hdl_search_path", "hdl_search_path"):
            self.st.incdirs = expand_files([val])
        elif attr in ("init_lib_search_path",):
            self.vars["init_lib_search_path"] = val
        elif attr.startswith("syn_") and attr.endswith("_effort"):
            self.st.effort[attr[4:-7]] = val
        elif attr in ("hdl_frontend",):
            self.st.frontend = val
        self.vars[attr] = val
        info(f"  Setting attribute of root '/': '{attr}' = {val}")
        return True

    @command("get_db", cat="Setup", usage="get_db <attr|designs|insts|lib_cells>", help="Query attributes / objects")
    def c_get_db(self, args):
        if not args:
            raise CmdError("usage: get_db <attr>")
        a = args[0]
        if a in ("designs", "design"):
            out(f"design:{self.st.top}" if self.st.top else "")
        elif a in ("insts", "inst"):
            for n in sorted(self.st.cells.get("_inst_names", [])):
                out(n)
        elif a == "library":
            out(" ".join(self.st.libs))
        elif a == "hdl_files":
            out(" ".join(self.st.hdl))
        else:
            out(self.vars.get(a, ""))
        return True

    @command("read_libs", cat="Setup", aliases=["read_lib"], usage="read_libs <file.lib ...>", help="Read timing libraries")
    def c_read_libs(self, args):
        search = [Path(p) for p in self.vars.get("init_lib_search_path", "").split()]
        files = []
        for a in expand_files(args):
            p = Path(a)
            if not p.exists():
                for s in search:
                    if (s / a).exists():
                        p = s / a
                        break
            if not p.exists():
                raise CmdError(f"Library not found: {a}", "RFG-LIB-002")
            files.append(str(p))
        if not files:
            lib = env.pdk().liberty()
            if not lib:
                raise CmdError("usage: read_libs <file.lib>")
            files = [str(lib)]
        for f in files:
            if f not in self.st.libs:
                self.st.libs.append(f)
            t0 = time.time()
            n = len(libscan.scan(f)["cells"])
            ok(f"  Library {Path(f).name}: {n} cells ({time.time() - t0:.1f}s)")
        return True

    @command("read_physical", cat="Setup", usage="read_physical -lef {files}", help="Record LEF for the innovus handoff")
    def c_read_physical(self, args):
        opts, pos = parse_opts(args, valued={"-lef": "+"})
        self.st.lefs = expand_files(opts.get("-lef", []) + pos)
        ok(f"  LEF recorded for handoff: {', '.join(Path(x).name for x in self.st.lefs)}")
        return True

    # ════════════════════════════════ HDL ════════════════════════════════
    @command("read_hdl", cat="HDL", usage="read_hdl [-sv|-v2001|-v1995] [-define X=Y] {files}",
             help="Read RTL (SystemVerilog by default)")
    def c_read_hdl(self, args):
        opts, pos = parse_opts(args, flags={"-sv", "-v2001", "-v1995", "-language"}, valued={"-define": 1, "-incdir": "+"})
        files = expand_files(pos, None)
        if not files:
            raise CmdError("usage: read_hdl -sv {rtl/*.sv}")
        for f in files:
            if not Path(f).exists():
                raise CmdError(f"File not found: {f}", "RFG-HDL-001")
        self.st.hdl_lang = "v2001" if opts.get("-v2001") else ("v1995" if opts.get("-v1995") else "sv")
        for f in files:
            f = str(Path(f).resolve())
            if f not in self.st.hdl:
                self.st.hdl.append(f)
            info(f"  Reading {self.st.hdl_lang.upper()} file '{self.project.rel(f)}'")
        if opts.get("-define"):
            self.st.defines.append(opts["-define"])
        self.st.incdirs += expand_files(opts.get("-incdir", []))
        # quick syntax check (like Genus parse errors at read time)
        script = self._read_script() + "hierarchy\n"
        r = self._yosys(script, "read", show_filter=runner.important)
        if not r.ok:
            error("RFG-HDL-002", "HDL parse failed")
            return False
        mods = sorted({m.group(1) for l in r.lines for m in [re.match(r"Generating RTLIL representation for module `\\?(\S+)'", l)] if m})
        if mods:
            info(f"  Modules: {', '.join(mods)}")
        if self.st.stage == "empty":
            self.st.stage = "read"
        self._persist()
        return True

    def _read_script(self) -> str:
        defs = " ".join(f"-D{d}" for d in self.st.defines)
        incs = " ".join(f"-I{_yq(d)}" for d in self.st.incdirs)
        if self.st.frontend == "slang":
            return "plugin -i slang\nread_slang " + " ".join(_yq(f) for f in self.st.hdl) + "\n"
        flag = "-sv" if self.st.hdl_lang == "sv" else ""
        return "".join(f"read_verilog {flag} {defs} {incs} {_yq(f)}\n" for f in self.st.hdl)

    @command("elaborate", cat="HDL", usage="elaborate [top]", help="Build the design hierarchy")
    def c_elaborate(self, args):
        self._need("read", "elaborate")
        top = args[0] if args else (self.st.top or self._guess_top())
        if not top:
            raise CmdError("Cannot determine top module — elaborate <top>")
        mods = []
        for f in self.st.hdl:
            t = re.sub(r"//[^\n]*|/\*.*?\*/", "", _safe_read(f), flags=re.S)
            mods += re.findall(r"^\s*module\s+(\w+)", t, re.M)
        if mods and top not in mods:
            raise CmdError(f"Module '{top}' is not in the HDL read so far. Modules: {', '.join(sorted(set(mods)))}"
                           + (f"  → elaborate {self._guess_top()}" if self._guess_top() else ""), "RFG-ELAB-003")
        self.st.top = top
        note(f"Info    : Elaborating Design. [ELAB-1]\n        : Elaborating top-level block '{top}'.")
        script = (self._read_script() + f"hierarchy -check -top {top}\nproc\nopt_clean\ncheck\n"
                  f"write_rtlil {_yq(self._ck('elab'))}\ntee -o {_yq(self.sdir / f'{top}.elab.stat.txt')} stat\n")
        r = self._yosys(script, "elab")
        if not r.ok:
            error("RFG-ELAB-002", f"Elaboration of '{top}' failed")
            return False
        st = _parse_stat(_safe_read(self.sdir / f"{top}.elab.stat.txt"))
        mods = sorted({m.group(1) for l in r.lines for m in [re.match(r"\s+Used module:\s+\\?(\S+)", l)] if m})
        warns = [l for l in r.lines if l.startswith("Warning")]
        self.st.stage = "elaborated"
        self._persist()
        out(table([("Top", top), ("Hierarchy", " → ".join([top] + [m for m in mods if m != top]) or top),
                   ("Processes", "converted (proc)"), ("RTL cells", st.get("cells", "?")),
                   ("Wires / bits", f"{st.get('wires', '?')} / {st.get('wire_bits', '?')}"),
                   ("Ports / bits", f"{st.get('ports', '?')} / {st.get('port_bits', '?')}"),
                   ("Warnings", len(warns))], headers=("elaborate", "")))
        ok(f"Done elaborating '{top}'.")
        self.gui_send(t="stage", stage=self.st.stage, top=top)
        return True

    def _guess_top(self) -> str | None:
        mods, insts = [], set()
        for f in self.st.hdl:
            t = _safe_read(f)
            t = re.sub(r"//[^\n]*|/\*.*?\*/", "", t, flags=re.S)
            ms = re.findall(r"^\s*module\s+(\w+)", t, re.M)
            mods += ms
            for m in re.finditer(r"^\s*(\w+)\s*(?:#\s*\(.*?\)\s*)?\s+\w+\s*\(", t, re.M | re.S):
                insts.add(m.group(1))
        cands = [m for m in mods if m not in insts]
        # testbenches have no ports and usually start with tb
        cands = [m for m in cands if not re.match(r"(?i)^(tb|test)", m)] or cands
        return cands[0] if cands else (mods[0] if mods else None)

    @command("check_design", cat="HDL", usage="check_design [-all|-unresolved|-undriven]",
             help="Lint the elaborated design (undriven, multi-driven, unresolved)")
    def c_check_design(self, args):
        self._need("elaborated", "check_design")
        ck = self._ck("mapped") if self.st.at_least("mapped") else self._ck("elab")
        script = (f"read_rtlil {_yq(ck)}\n" + "".join(f"read_liberty -lib {_yq(l)}\n" for l in self.st.libs) +
                  f"hierarchy -top {self.st.top}\ncheck\n")
        r = self._yosys(script, "check", show_filter=lambda l: False)
        issues = [l for l in r.lines if l.startswith("Warning:") or "is used but has no driver" in l
                  or "multiple conflicting drivers" in l]
        undriven = [l for l in issues if "no driver" in l]
        multi = [l for l in issues if "conflicting drivers" in l]
        unres = [l for l in r.lines if "is not part of the design" in l or "Module" in l and "not found" in l]
        rows = [("Unresolved references", len(unres)), ("Undriven signals", len(undriven)),
                ("Multi-driven nets", len(multi)), ("Other warnings", len(issues) - len(undriven) - len(multi))]
        out(f"\n  Check Design Report  ({'mapped' if 'mapped' in ck.name else 'generic'})")
        out(table(rows, headers=("Check", "Count")))
        for l in issues[:20]:
            console.write(colorize_backend_line("    " + l))
        if not issues and not unres:
            ok("check_design: no issues found.")
        return not (unres or multi)

    # ════════════════════════════════ Constraints ════════════════════════════════
    @command("read_sdc", cat="Constraints", usage="read_sdc <file.sdc>", help="Read SDC constraints")
    def c_read_sdc(self, args):
        for f in expand_files(args):
            if not Path(f).exists():
                raise CmdError(f"File not found: {f}")
            self.st.sdc_files.append(str(Path(f).resolve()))
            ok(f"  Read SDC {Path(f).name}")
        return True

    def _sdc_cmd(self, name, args):
        self._need("elaborated", name)
        line = name + " " + " ".join(a if (a.startswith("[") or " " not in a) else "{" + a + "}" for a in args)
        self.st.sdc_lines.append(line.strip())
        return True

    @command("create_clock", cat="Constraints", usage="create_clock -name clk -period 10 [get_ports clk]",
             help="Define a clock")
    def c_create_clock(self, args):
        okk = self._sdc_cmd("create_clock", args)
        m = re.search(r"-period\s+([0-9.]+)", " ".join(args))
        if okk:
            ok(f"  Clock defined{f' (period {m.group(1)})' if m else ''}")
        return okk

    @command("set_input_delay", cat="Constraints", usage="set_input_delay <d> -clock clk [all_inputs]", help="SDC input delay")
    def c_sid(self, args):
        return self._sdc_cmd("set_input_delay", args)

    @command("set_output_delay", cat="Constraints", usage="set_output_delay <d> -clock clk [all_outputs]", help="SDC output delay")
    def c_sod(self, args):
        return self._sdc_cmd("set_output_delay", args)

    @command("set_load", cat="Constraints", usage="set_load <pF> [all_outputs]", help="SDC output load")
    def c_sl(self, args):
        return self._sdc_cmd("set_load", args)

    @command("set_driving_cell", cat="Constraints", usage="set_driving_cell -lib_cell X [all_inputs]", help="SDC driving cell")
    def c_sdc_drv(self, args):
        return self._sdc_cmd("set_driving_cell", args)

    @command("set_clock_uncertainty", cat="Constraints", usage="set_clock_uncertainty <d> [get_clocks clk]", help="SDC uncertainty")
    def c_scu(self, args):
        return self._sdc_cmd("set_clock_uncertainty", args)

    @command("set_false_path", cat="Constraints", usage="set_false_path -from … -to …", help="SDC false path")
    def c_sfp(self, args):
        return self._sdc_cmd("set_false_path", args)

    @command("set_clock_transition", cat="Constraints", usage="set_clock_transition <d> [get_clocks clk]", help="SDC clock transition", hidden=True)
    def c_sct(self, args):
        return self._sdc_cmd("set_clock_transition", args)

    @command("set_max_fanout", cat="Constraints", usage="set_max_fanout <n> [current_design]", help="SDC max fanout", hidden=True)
    def c_smf(self, args):
        return self._sdc_cmd("set_max_fanout", args)

    @command("report_clocks", cat="Constraints", aliases=["report_clock"], help="List defined clocks")
    def c_report_clocks(self, args):
        rows = []
        for l in self._all_sdc().splitlines():
            m = re.search(r"create_clock\b(.*)", l)
            if m:
                nm = re.search(r"-name\s+(\S+)", l)
                per = re.search(r"-period\s+([0-9.]+)", l)
                src = re.search(r"\[get_ports\s+\{?([^\]}]+)\}?\]", l)
                p = float(per.group(1)) if per else 0
                rows.append((nm.group(1) if nm else "?", f"{p:g}", f"{1000 / p:.1f} MHz" if p else "-",
                             src.group(1).strip() if src else "-"))
        if not rows:
            warn("RFG-SDC-001", "No clocks defined")
            return True
        out(table(rows, headers=("Clock", "Period", "Frequency", "Source")))
        return True

    def _all_sdc(self) -> str:
        txt = ""
        for f in self.st.sdc_files:
            txt += _safe_read(f) + "\n"
        txt += "\n".join(self.st.sdc_lines) + "\n"
        return txt

    def _write_sdc_file(self) -> Path:
        p = self.sdir / f"{self.st.top}.sdc"
        hdr = f"# RTLFlow genus {VERSION} — constraints for {self.st.top}\n# {time.ctime()}\n"
        p.write_text(hdr + self._all_sdc())
        return p

    # ════════════════════════════════ Synthesis ════════════════════════════════
    @command("syn_generic", cat="Synthesis", usage="syn_generic", help="RTL optimisation → generic gates (technology independent)")
    def c_syn_generic(self, args):
        self._need("elaborated", "syn_generic")
        top = self.st.top
        note(f"Info    : Synthesizing '{top}' to generic gates. [SYNTH-1]")
        eff = self.st.effort.get("generic", "medium")
        extra = "opt -full\n" if eff == "high" else ""
        script = (f"read_rtlil {_yq(self._ck('elab'))}\nhierarchy -top {top}\n"
                  f"synth -top {top} -flatten -run :fine\n{extra}opt_clean -purge\n"
                  f"write_rtlil {_yq(self._ck('generic'))}\ntee -o {_yq(self.sdir / f'{top}.generic.stat.txt')} stat\n")
        t0 = time.time()
        r = self._yosys(script, "generic")
        if not r.ok:
            error("RFG-SYN-101", "syn_generic failed")
            return False
        st = _parse_stat(_safe_read(self.sdir / f"{top}.generic.stat.txt"))
        self.st.stage = "generic"
        self._persist()
        out(table([("Generic cells", st.get("cells", "?")), ("Wires / bits", f"{st.get('wires', '?')} / {st.get('wire_bits', '?')}"),
                   ("Effort", eff), ("Runtime", f"{time.time() - t0:.1f}s")], headers=("syn_generic", "")))
        top_types = sorted(st.get("types", {}).items(), key=lambda kv: -kv[1])[:8]
        if top_types:
            info("  " + "  ".join(f"{t.lstrip('$_')}:{n}" for t, n in top_types))
        ok("Done synthesizing to generic gates.")
        self.gui_send(t="stage", stage=self.st.stage, top=top)
        return True

    def _map_script(self, strategy: str) -> str:
        lib = self._need_lib("syn_map")
        top = self.st.top
        du = " ".join(f"-dont_use {_yq(p)}" for p in self._dont_use())
        period = self._clock_period_ps()
        tie_hi, tie_lo = libscan.tie_cells(lib)
        buf = libscan.buffer_cell(lib)
        libs_lib = "".join(f"read_liberty -lib {_yq(l)}\n" for l in self.st.libs)
        if strategy == "delay":
            d = period or 10000
            abc = (f"abc {self._liberty_args()} -D {d} {du} "
                   f"-script +strash;ifraig;scorr;dc2;dretime;strash;&get,-n;&dch,-f;&nf,{{D}};&put;buffer;upsize,{{D}};dnsize,{{D}};stime,-p")
        else:
            dflag = f"-D {period}" if period else ""
            abc = f"abc {self._liberty_args()} {dflag} {du}"
        s = (f"read_rtlil {_yq(self._ck('generic'))}\n{libs_lib}hierarchy -top {top}\n"
             f"synth -top {top} -flatten -run fine:check\n"
             f"dfflibmap {self._liberty_args()} {du}\nopt\n{abc}\n"
             "setundef -zero\nopt_clean -purge\n")
        if tie_hi and tie_lo:
            s += f"hilomap -singleton -hicell {tie_hi[0]} {tie_hi[1]} -locell {tie_lo[0]} {tie_lo[1]}\n"
        if buf:
            s += f"insbuf -buf {buf[0]} {buf[1]} {buf[2]}\n"
        s += f"opt_clean -purge\ncheck\n"
        return s

    @command("syn_map", cat="Synthesis", usage="syn_map", help="Technology mapping to the target library")
    def c_syn_map(self, args):
        self._need("generic", "syn_map")
        top = self.st.top
        lib = self._need_lib("syn_map")
        note(f"Info    : Mapping '{top}' to library {Path(lib).name}. [SYNTH-2]")
        period = self._clock_period_ps()
        if not period:
            warn("RFG-MAP-010", "No clock defined — mapping for area only (create_clock before syn_map for timing-driven ABC)")
        mapped = self.sdir / f"{top}_mapped.v"
        script = self._map_script("area") + (f"write_rtlil {_yq(self._ck('mapped'))}\n"
                                             f"write_verilog -noattr -noexpr -nohex -nodec -defparam {_yq(mapped)}\n")
        t0 = time.time()
        r = self._yosys(script, "map")
        if not r.ok:
            error("RFG-MAP-101", "syn_map failed")
            return False
        self.st.netlist = str(mapped)
        self.st.stage = "mapped"
        st = self._stat(self._ck("mapped"))
        self.st.area = st
        self._dump_json()
        self._persist()
        out(table([("Mapped cells", st.get("cells", "?")), ("Sequential", st.get("seq", "?")),
                   ("Chip area", f"{st.get('area', 0):.4f}"), ("Library", Path(lib).name),
                   ("Netlist", self.project.rel(mapped)), ("Runtime", f"{time.time() - t0:.1f}s")],
                  headers=("syn_map", "")))
        ok("Done mapping.")
        self.gui_send(t="stage", stage=self.st.stage, top=top)
        self._gui_design()
        return True

    @command("syn_opt", cat="Synthesis", usage="syn_opt [-effort low|medium|high]",
             help="Timing-driven re-map (ABC delay script, upsizing/buffering); kept only if QoR improves")
    def c_syn_opt(self, args):
        self._need("mapped", "syn_opt")
        top = self.st.top
        opts, _ = parse_opts(args, valued={"-effort": 1}, flags={"-incremental"})
        note(f"Info    : Incrementally optimizing '{top}'. [SYNTH-7]")
        period = self._clock_period_ps()
        before_area = self.st.area.get("area", 0.0)
        before = self._sta_summary(quiet=True) if period else {}
        cand = self.sdir / f"{top}.opt.il"
        cand_v = self.sdir / f"{top}_opt_candidate.v"
        script = self._map_script("delay" if period else "area") + (
            f"opt -purge\nwrite_rtlil {_yq(cand)}\nwrite_verilog -noattr -noexpr -nohex -nodec -defparam {_yq(cand_v)}\n")
        r = self._yosys(script, "opt")
        if not r.ok:
            warn("RFG-OPT-101", "Delay-driven re-map failed; keeping syn_map result")
            self.st.stage = "optimized"
            return True
        st_c = self._stat(cand)
        after = self._sta_summary(quiet=True, netlist=cand_v) if period else {}
        keep = False
        if period and before.get("wns") is not None and after.get("wns") is not None:
            keep = after["wns"] > before["wns"] + 1e-4 or (abs(after["wns"] - before["wns"]) < 1e-4 and st_c.get("area", 1e9) < before_area)
        elif not period:
            keep = st_c.get("area", 1e9) < before_area
        rows = [("Area", f"{before_area:.3f}", f"{st_c.get('area', 0):.3f}")]
        if period:
            rows.append(("WNS (ns)", _fmt(before.get("wns")), _fmt(after.get("wns"))))
        out(table(rows, headers=("syn_opt", "syn_map", "delay re-map")))
        if keep:
            shutil.copy(cand, self._ck("mapped"))
            shutil.copy(cand_v, self.sdir / f"{top}_mapped.v")
            self.st.area = st_c
            ok("  Kept optimised netlist (better QoR).")
        else:
            info("  syn_map result already better — kept it (no degradation).")
        self.st.stage = "optimized"
        self._dump_json()
        self._persist()
        ok("Done incrementally optimizing.")
        self.gui_send(t="stage", stage=self.st.stage, top=top)
        self._gui_design()
        return True

    @command("synthesize", cat="Synthesis", usage="synthesize -to_mapped", help="Legacy: generic + map + opt")
    def c_synthesize(self, args):
        return self.c_syn_generic([]) and self.c_syn_map([]) and self.c_syn_opt([])

    # ════════════════════════════════ Reports ════════════════════════════════
    @command("report_area", cat="Reports", usage="report_area [-detail]", help="Area report (Yosys stat -liberty)")
    def c_report_area(self, args):
        self._need("mapped", "report_area")
        st = self.st.area or self._stat(self._ck("mapped"))
        top = self.st.top
        out(_genus_header("report_area", self.st.libs))
        rows = [(top, "", st.get("cells", 0), f"{st.get('area', 0):.3f}", "0.000", f"{st.get('area', 0):.3f}")]
        out(table(rows, headers=("Instance", "Module", "Cell Count", "Cell Area", "Net Area", "Total Area")))
        seq_a = st.get("seq_area", 0.0)
        if st.get("area"):
            info(f"  sequential area {seq_a:.4f} ({100 * seq_a / st['area']:.2f}%)   "
                 f"wires {st.get('wires', '?')} · wire bits {st.get('wire_bits', '?')} · ports {st.get('ports', '?')} "
                 f"· port bits {st.get('port_bits', '?')} · memories {st.get('memories', 0)}")
        return True

    @command("report_gates", cat="Reports", aliases=["report_cells"], usage="report_gates", help="Cell usage by library cell")
    def c_report_gates(self, args):
        self._need("mapped", "report_gates")
        st = self.st.area or self._stat(self._ck("mapped"))
        areas = {n: c["area"] for n, c in libscan.scan(self.st.libs[0])["cells"].items()} if self.st.libs else {}
        seqs = libscan.seq_cells(self.st.libs[0]) if self.st.libs else set()
        rows = []
        for t, n in sorted(st.get("types", {}).items(), key=lambda kv: (-kv[1], kv[0])):
            a = areas.get(t, 0.0)
            rows.append((t, n, f"{a * n:.3f}", "seq" if t in seqs else ""))
        out(_genus_header("report_gates", self.st.libs))
        out(table(rows, headers=("Gate", "Instances", "Area", "")))
        info(f"  total {st.get('cells', 0)} instances · area {st.get('area', 0):.3f}")
        return True

    def _sta_script(self, body: str, netlist: str | None = None) -> str:
        nl = netlist or str(self.sdir / f"{self.st.top}_mapped.v")
        sdc = self._write_sdc_file()
        return ("".join(f"read_liberty {{{l}}}\n" for l in self.st.libs) +
                f"read_verilog {{{nl}}}\nlink_design {self.st.top}\nread_sdc {{{sdc}}}\n" + body)

    def _sta_summary(self, quiet=False, netlist=None) -> dict:
        body = ("proc _v {cmd} { if {[catch {set v [uplevel #0 $cmd]}]} { return NA }; return $v }\n"
                "puts \"RF_WNS [_v {sta::worst_slack -max}]\"\n"
                "puts \"RF_WHS [_v {sta::worst_slack -min}]\"\n"
                "puts \"RF_TNS [_v {sta::total_negative_slack -max}]\"\n"
                "puts \"RF_VIOL [_v {sta::endpoint_violation_count max}]\"\n"
                "report_worst_slack -max -digits 4\n")
        r = runner.sta(self._sta_script(body, netlist), self.sdir / "rf_qor.tcl", show=False)
        d = {}
        for l in r.lines:
            m = re.match(r"RF_(WNS|WHS|TNS|VIOL)\s+(\S+)", l)
            if m and m.group(2) not in ("NA", "INF", "-INF"):
                try:
                    d[m.group(1).lower()] = float(m.group(2))
                except ValueError:
                    pass
            m2 = re.match(r"worst slack\s+(-?[\d.]+)", l.strip())
            if m2 and "wns" not in d:
                d["wns"] = float(m2.group(1))
        if not r.ok and not quiet:
            for l in r.lines[-8:]:
                console.write(colorize_backend_line(l))
        self.st.timing = d
        return d

    @command("report_timing", cat="Reports", usage="report_timing [-max_paths N] [-early]",
             help="Worst path(s) through OpenSTA on the mapped netlist")
    def c_report_timing(self, args):
        self._need("mapped", "report_timing")
        opts, _ = parse_opts(args, flags={"-early", "-late", "-unconstrained"}, valued={"-max_paths": 1, "-nworst": 1,
                                                                                      "-from": 1, "-to": 1})
        if not self._clock_period_ps():
            warn("RFG-TIM-001", "No clocks defined — paths will be unconstrained (create_clock first)")
        pd = "min" if opts.get("-early") else "max"
        n = int(opts.get("-max_paths") or 1)
        extra = "".join(f" {k} {opts[k]}" for k in ("-from", "-to") if opts.get(k))
        if opts.get("-unconstrained") or not self._clock_period_ps():
            extra += " -unconstrained"
        body = (f"if {{[catch {{report_checks -path_delay {pd} -group_path_count {n} -format full_clock_expanded "
                f"-fields {{slew cap input_pins fanout}} -digits 3{extra}}}]}} {{\n"
                f"  report_checks -path_delay {pd} -group_count {n} -format full_clock_expanded "
                f"-fields {{slew cap input_pins fanout}} -digits 3{extra}\n}}\n")
        r = runner.sta(self._sta_script(body), self.sdir / "rf_timing.tcl", log=self.sdir / f"{self.st.top}.timing.rpt")
        out(_genus_header("report_timing", self.st.libs))
        for l in r.lines:
            if l.startswith(("OpenSTA", "Copyright", "License", "This is free", "There is NO", "Features", "Type ")):
                continue
            console.write(_color_timing(l))
        if not r.ok:
            error("RFG-TIM-002", "OpenSTA failed (see above)")
            return False
        from .innovus import _parse_paths
        paths = _parse_paths(r.text)
        if paths:
            self.gui_send(t="highlight", insts=paths[0]["insts"], label=f"worst path · slack {paths[0].get('slack')}")
        return True

    @command("report_qor", cat="Reports", usage="report_qor", help="QoR summary — WNS/TNS via OpenSTA worst_slack queries")
    def c_report_qor(self, args):
        self._need("mapped", "report_qor")
        st = self.st.area or self._stat(self._ck("mapped"))
        t = self._sta_summary() if self._clock_period_ps() else {}
        per = self._clock_period_ps()
        out(_genus_header("report_qor", self.st.libs))
        rows = [("Design", self.st.top), ("Stage", self.st.stage),
                ("Clock period", f"{per / 1000:g} ns" if per else "none"),
                ("Critical path slack (WNS)", _fmt(t.get("wns")) + " ns" if per else "unconstrained"),
                ("Total negative slack (TNS)", _fmt(t.get("tns")) + " ns" if per else "-"),
                ("Hold slack (WHS)", _fmt(t.get("whs")) + " ns" if per else "-"),
                ("Violating endpoints", _fmt(t.get("viol"), 0) if per else "-"),
                ("Leaf instances", st.get("cells", "?")), ("Sequential instances", st.get("seq", "?")),
                ("Cell area", f"{st.get('area', 0):.4f}"),
                ("Sequential area", f"{st.get('seq_area', 0):.4f}")]
        out(table(rows, headers=("Metric", "Value")))
        if per:
            info("  WNS/TNS are dedicated OpenSTA queries (worst_slack / total_negative_slack), identical to report_timing.")
        return True

    @command("report_power", cat="Reports", usage="report_power", help="Power estimate (OpenSTA, default activity)")
    def c_report_power(self, args):
        self._need("mapped", "report_power")
        r = runner.sta(self._sta_script("report_power -digits 4\n"), self.sdir / "rf_power.tcl")
        out(_genus_header("report_power", self.st.libs))
        for l in r.lines:
            if not l.startswith(("OpenSTA", "Copyright", "License", "This is free", "There is NO", "Features", "Type ")):
                console.write(l)
        return r.ok

    @command("report_hierarchy", cat="Reports", help="Module hierarchy of the elaborated design")
    def c_report_hier(self, args):
        self._need("elaborated", "report_hierarchy")
        r = self._yosys(f"read_rtlil {_yq(self._ck('elab'))}\nhierarchy -top {self.st.top}\nls\n", "hier",
                        show_filter=lambda l: False)
        mods = [l.strip() for l in r.lines if l.startswith("  ") and not l.strip().startswith(("$", "-"))]
        out(table([(m,) for m in mods] or [(self.st.top,)], headers=("Modules",)))
        return True

    # ════════════════════════════════ Output ════════════════════════════════
    @command("write_hdl", cat="Output", aliases=["write_netlist"], usage="write_hdl [> file.v]",
             help="Print the mapped netlist (redirect with > file)")
    def c_write_hdl(self, args):
        self._need("mapped", "write_hdl")
        nl = self.sdir / f"{self.st.top}_mapped.v"
        if args:
            dst = Path(args[0]).expanduser()
            shutil.copy(nl, dst)
            ok(f"Netlist written to {dst}")
            return True
        console.write(_safe_read(nl).rstrip())
        return True

    @command("write_sdc", cat="Output", usage="write_sdc [> file.sdc]", help="Print current constraints")
    def c_write_sdc(self, args):
        p = self._write_sdc_file() if self.st.top else None
        if args:
            shutil.copy(p, Path(args[0]).expanduser())
            ok(f"SDC written to {args[0]}")
            return True
        console.write(_safe_read(p).rstrip() if p else "")
        return True

    @command("write_design", cat="Output", usage="write_design -innovus [-base_name dir/name]",
             help="Handoff package for innovus (netlist, SDC, setup script)")
    def c_write_design(self, args):
        """Like Genus 'write_design -innovus': writes <base>.v, <base>.sdc and <base>.invs_setup.tcl.
        In innovus:   source .rtlflow/handoff/<top>.invs_setup.tcl"""
        self._need("mapped", "write_design")
        opts, pos = parse_opts(args, flags={"-innovus", "-gzip_files"}, valued={"-base_name": 1})
        top = self.st.top
        base = Path(opts.get("-base_name") or (self.project.handoff / top))
        base.parent.mkdir(parents=True, exist_ok=True)
        nl = base.with_suffix(".v")
        shutil.copy(self.sdir / f"{top}_mapped.v", nl)
        sdc = base.with_suffix(".sdc")
        sdc.write_text(self._all_sdc())
        lefs = self.st.lefs
        if not lefs:
            p = env.pdk()
            lefs = [str(x) for x in (p.tech_lef(), p.cell_lef()) if x]
        setup = base.parent / f"{base.name}.invs_setup.tcl"
        setup.write_text(
            f"# RTLFlow genus {VERSION} → innovus handoff for '{top}'  ({time.ctime()})\n"
            f"# usage:  innovus 1> source {self.project.rel(setup)}\n"
            f"set init_lib_file   {{{' '.join(self.st.libs)}}}\n"
            f"set init_lef_file   {{{' '.join(lefs)}}}\n"
            f"set init_verilog    {{{nl}}}\n"
            f"set init_top_cell   {top}\n"
            f"set init_sdc_file   {{{sdc}}}\n"
            f"init_design\n")
        flow = base.parent / f"{base.name}.invs_flow.tcl"
        flow.write_text(
            f"# RTLFlow reference implementation flow for '{top}'\n"
            f"source {{{setup}}}\n"
            "floorPlan -r 1.0 0.5 5 5 5 5\naddWellTap\nglobalNetConnect\nsroute\n"
            "placeDesign\ntimeDesign -preCTS\nccopt_design\ntimeDesign -postCTS\n"
            "routeDesign\naddFiller\nextractRC\ntimeDesign -postRoute\nreport_qor\n"
            f"saveDesign {top}_final\n")
        self.project.set(handoff=str(setup), top=top)
        out(table([("Netlist", self.project.rel(nl)), ("SDC", self.project.rel(sdc)),
                   ("Innovus setup", self.project.rel(setup)), ("Reference flow", self.project.rel(flow))],
                  headers=("write_design -innovus", "")))
        ok(f"Handoff ready. In innovus:  source {self.project.rel(setup)}")
        return True

    # ════════════════════════════════ Session ════════════════════════════════
    @command("status", cat="Session", help="Synthesis state")
    def c_status(self, args):
        s = self.st
        tick, cross = f"{C.GRN}✓{C.RESET}", f"{C.RED}✗{C.RESET}"
        per = self._clock_period_ps() if s.libs else None
        rows = [("HDL", f"{tick if s.hdl else cross} " + (", ".join(self.project.rel(f) for f in s.hdl) or "-")),
                ("Library", f"{tick if s.libs else cross} " + (", ".join(Path(l).name for l in s.libs) or "-")),
                ("Top", f"{tick if s.top else cross} {s.top or '-'}"),
                ("Stage", f"{C.BOLD}{s.stage}{C.RESET}"),
                ("Clocks", f"{per / 1000:g} ns period" if per else "none"),
                ("Netlist", self.project.rel(s.netlist) if s.netlist else "-")]
        if s.area:
            rows.append(("Area / cells", f"{s.area.get('area', 0):.3f} / {s.area.get('cells', '?')}"))
        if s.timing.get("wns") is not None:
            rows.append(("WNS", f"{s.timing['wns']:.3f} ns"))
        rows.append(("GUI", "open" if self.gui and self.gui.is_open else "closed ('gui_show')"))
        out(table(rows, headers=("genus status", "")))
        nxt = {"empty": "read_hdl -sv {…}", "read": "elaborate", "elaborated": "syn_generic", "generic": "syn_map",
               "mapped": "syn_opt / report_timing / write_design -innovus", "optimized": "write_design -innovus"}[s.stage]
        info(f"  Next step: {nxt}")
        return True

    @command("restore_session", cat="Session", help="Reload libs/RTL/constraints from the last genus run")
    def c_restore_session(self, args):
        p = self._prev
        if not p:
            raise CmdError("No previous genus session recorded in this project")
        self.st.libs = [l for l in p.get("libs", []) if Path(l).exists()]
        self.st.hdl = [h for h in p.get("hdl", []) if Path(h).exists()]
        self.st.sdc_lines = p.get("sdc_lines", [])
        self.st.sdc_files = p.get("sdc_files", [])
        self.st.top = p.get("top", "")
        self.st.lefs = p.get("lefs", [])
        self.st.stage = "read" if self.st.hdl else "empty"
        ok(f"Restored: {len(self.st.hdl)} HDL, {len(self.st.libs)} lib(s), top '{self.st.top}' — run elaborate")
        return True

    @command("version", cat="Session", aliases=["get_version"], help="Version info")
    def c_version(self, args):
        out(f"RTLFlow genus {VERSION} ({CODENAME}) · Yosys {env.tool('yosys').version}")
        return True

    # ════════════════════════════════ GUI ════════════════════════════════
    @command("gui_show", cat="GUI", aliases=["gui"], help="Open the Genus GUI (schematic / reports) on this session")
    def c_gui_show(self, args):
        if self.gui and self.gui.is_open:
            self.gui_send(t="raise")
            return True
        if not display_available():
            warn("RFG-GUI-001", "No display available")
            return False
        self.gui = GuiHost("genus_gui", self.project.logs / "genus_gui.log", self._gui_message,
                           lambda code: console.write(f"{C.GRY}genus: GUI closed — gui_show reopens it{C.RESET}"))
        okk, why = self.gui.launch(["--project", str(self.project.root)])
        if not okk:
            if "no module named" in why.lower() and "tkinter" in why.lower():
                error("RFG-GUI-002", "Python Tk is missing:  sudo apt install python3-tk")
            else:
                error("RFG-GUI-003", f"GUI failed to start: {why}")
            return False
        if not self._gui_worker or not self._gui_worker.is_alive():
            self._gui_worker = threading.Thread(target=self._gui_loop, daemon=True, name="genus-gui-exec")
            self._gui_worker.start()
        ok(f"  Genus GUI open (pid {self.gui.proc.pid})")
        return True

    @command("gui_hide", cat="GUI", help="Close the Genus GUI")
    def c_gui_hide(self, args):
        if self.gui:
            self.gui.close()
        return True

    def gui_send(self, **msg) -> None:
        if self.gui and self.gui.is_open:
            self.gui.send(**msg)

    def _gui_message(self, msg: dict) -> None:
        if msg.get("t") in ("ready", "cmd", "request_state"):
            self._gui_q.put(msg)

    def _gui_loop(self) -> None:
        while True:
            msg = self._gui_q.get()
            t = msg.get("t")
            try:
                if t in ("ready", "request_state"):
                    with self.exec_lock:
                        self.gui_send(t="hello", app="genus", version=VERSION, top=self.st.top, stage=self.st.stage,
                                      libs=self.st.libs, hdl=self.st.hdl)
                        self._gui_design()
                elif t == "cmd":
                    line = msg.get("line", "")
                    if line.strip().split()[:1] in (["exit"], ["quit"]):
                        console.write(f"{C.GRY}genus: 'exit' in the GUI closes the GUI only{C.RESET}")
                        self.gui_send(t="quit")
                        continue
                    console.write(f"{C.MAG}[GUI]{C.RESET} {C.BOLD}{line}{C.RESET}")
                    if msg.get("report"):
                        console.begin_capture()
                    okk = False
                    try:
                        okk = self.run_line(line, source="gui")
                    finally:
                        text = console.end_capture() if msg.get("report") else ""
                    if msg.get("report"):
                        for l in text.splitlines():
                            console.write(l)
                        self.gui_send(t="report", title=msg.get("title") or line, text=text, ok=okk)
                    self.gui_send(t="result", ok=okk, line=line)
            except Exception as e:
                console.log_only(f"genus gui worker: {e}")

    def _dump_json(self) -> Path | None:
        """Mapped netlist → Yosys JSON (with lib cell port directions) for the schematic view."""
        if not self.st.at_least("mapped"):
            return None
        js = self.sdir / f"{self.st.top}.mapped.json"
        script = ("".join(f"read_liberty -lib {_yq(l)}\n" for l in self.st.libs) +
                  f"read_rtlil {_yq(self._ck('mapped'))}\nhierarchy -top {self.st.top}\nwrite_json {_yq(js)}\n")
        r = self._yosys(script, "json", show_filter=lambda l: False)
        return js if r.ok and js.exists() else None

    def _gui_design(self) -> None:
        if not (self.gui and self.gui.is_open):
            return
        js = self.sdir / f"{self.st.top}.mapped.json"
        if self.st.at_least("mapped") and not js.exists():
            self._dump_json()
        seq = sorted(libscan.seq_cells(self.st.libs[0])) if self.st.libs else []
        self.gui_send(t="design", json=str(js) if js.exists() else "", top=self.st.top, stage=self.st.stage,
                      area=self.st.area, timing=self.st.timing, seq=seq, sdc=self._all_sdc() if self.st.top else "")


# ════════════════════════════════ helpers ════════════════════════════════
def _yq(x) -> str:
    """Path for a Yosys script. Yosys passes (tee, abc, dfflibmap …) do NOT strip quotes — only
    frontends do — so paths are written bare; quote only if they contain whitespace."""
    s = str(x)
    return '"' + s + '"' if any(ch.isspace() for ch in s) else s


def _safe_read(p) -> str:
    try:
        return Path(p).read_text(errors="replace")
    except (OSError, TypeError):
        return ""


def _fmt(v, nd=3) -> str:
    if v is None:
        return "NA"
    return f"{v:.{nd}f}" if nd else f"{int(v)}"


def _parse_stat(txt: str) -> dict:
    d: dict = {"types": {}}
    pats = {"wires": r"Number of wires:\s+(\d+)", "wire_bits": r"Number of wire bits:\s+(\d+)",
            "ports": r"Number of ports:\s+(\d+)", "port_bits": r"Number of port bits:\s+(\d+)",
            "memories": r"Number of memories:\s+(\d+)", "cells": r"Number of cells:\s+(\d+)",
            "area": r"Chip area for (?:module|top module) '?\\?\S*'?:\s+([\d.]+)",
            "seq_area": r"of which used for sequential elements:\s+([\d.]+)"}
    for k, p in pats.items():
        m = re.search(p, txt)
        if m:
            d[k] = float(m.group(1)) if "." in m.group(1) else int(m.group(1))
    # new yosys (0.4x+) format: "      23 203.946  cells" / old: "Number of cells: 23"
    if "cells" not in d:
        m = re.search(r"^\s+(\d+)\s+[\d.]*\s*cells\s*$", txt, re.M)
        if m:
            d["cells"] = int(m.group(1))
    if "area" not in d:
        m = re.search(r"Chip area[^:]*:\s+([\d.]+)", txt)
        if m:
            d["area"] = float(m.group(1))
    for k, p in (("wires", r"^\s+(\d+)\s+[\d.]*\s*wires\s*$"), ("wire_bits", r"^\s+(\d+)\s+[\d.]*\s*wire bits\s*$"),
                 ("ports", r"^\s+(\d+)\s+[\d.]*\s*ports\s*$"), ("port_bits", r"^\s+(\d+)\s+[\d.]*\s*port bits\s*$")):
        if k not in d:
            m = re.search(p, txt, re.M)
            if m:
                d[k] = int(m.group(1))
    # cell types: old "     sky130_fd_sc_hd__xor2_1      3"  / new "       3  18.768  sky130_fd_sc_hd__xor2_1"
    for m in re.finditer(r"^\s+([$\\\w.]+)\s+(\d+)\s*$", txt, re.M):
        name = m.group(1).lstrip("\\")
        if name not in ("cells", "wires", "ports"):
            d["types"][name] = d["types"].get(name, 0) + int(m.group(2))
    for m in re.finditer(r"^\s+(\d+)\s+(?:[\d.]+\s+)?([$\\][\w$.]+|[A-Za-z_][\w$.]*__[\w$.]+|\$_\w+_)\s*$", txt, re.M):
        name = m.group(2).lstrip("\\")
        d["types"].setdefault(name, int(m.group(1)))
    seq = sum(n for t, n in d["types"].items() if re.search(r"(?i)(__s?df|__dl|dff|latch|\$_s?dff|\$_dlatch)", t))
    d["seq"] = seq
    return d


def _genus_header(name: str, libs: list[str]) -> str:
    return (f"{C.GRY}============================================================\n"
            f"  Generated by:      RTLFlow genus {VERSION}\n"
            f"  Generated on:      {time.strftime('%b %d %Y  %I:%M:%S %p')}\n"
            f"  Module:            {name}\n"
            f"  Technology library: {', '.join(Path(l).stem for l in libs) or '-'}\n"
            f"============================================================{C.RESET}")


def _color_timing(line: str) -> str:
    if line.rstrip().endswith("(VIOLATED)"):
        return f"{C.RED}{C.BOLD}{line}{C.RESET}"
    if line.rstrip().endswith("(MET)"):
        return f"{C.GRN}{C.BOLD}{line}{C.RESET}"
    if line.startswith(("Startpoint:", "Endpoint:")):
        return f"{C.BOLD}{line}{C.RESET}"
    return colorize_backend_line(line)


def run_genus(gui: bool = False, files: list[str] | None = None, batch: bool = False) -> int:
    sh = GenusShell(gui=gui, files=files)
    if not sh.start():
        console.close_logs()
        return 1
    if batch:
        sh.on_start()
        sh.on_exit()
        return 0
    return sh.cmdloop()
