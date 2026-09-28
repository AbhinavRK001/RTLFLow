"""nclaunch — Cadence NC/Xcelium-style simulation shell on Verilator / Icarus.

    rtlflow> nclaunch                 → nclaunch N>
    nclaunch 1> read_hdl -sv count.sv       (design)
    nclaunch 2> read_tb tba.sv              (testbench)
    nclaunch 3> elaborate tba               (sim top ≠ design top)
    nclaunch 4> simulate [-gui]             (-gui → SimVision-style waveform viewer)
Three-step Cadence style also works:  ncvlog …  → ncelab tba → ncsim tba [-gui]
One-shot:                             xrun -sv count.sv tba.sv -top tba -gui

Waves: if the testbench has no $dumpvars, RTLFlow adds a tiny dump module
(Icarus: extra root; Verilator: wrapper top) so simvision always has data.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import CODENAME, VERSION, env
from ..backends import runner
from ..core.msg import C, colorize_backend_line, console, error, info, note, ok, out, rule, table, warn
from ..core.project import Project
from ..core.guishell import GuiMixin
from ..core.shell import CmdError, Shell, command, expand_files, parse_opts


@dataclass
class SimState:
    rtl: list[str] = field(default_factory=list)
    tb: list[str] = field(default_factory=list)
    top: str = ""
    engine: str = "verilator"
    defines: list[str] = field(default_factory=list)
    incdirs: list[str] = field(default_factory=list)
    built: str = ""          # binary path
    wave: str = ""
    result: str = ""
    timescale: str = "1ns/1ps"


class NCLaunchShell(GuiMixin, Shell):
    tool = "nclaunch"
    msg_prefix = "RFN"
    history_name = ".rtlflow_history_nclaunch"

    gui_module = "nclaunch_gui"
    gui_label = "NCLaunch GUI"

    def __init__(self, gui: bool = False) -> None:
        super().__init__()
        self._gui_init()
        self.want_gui = gui
        self.project = Project()
        self.st = SimState()
        prev = self.project.get("sim") or {}
        for k in ("rtl", "tb", "top", "engine"):
            if prev.get(k):
                setattr(self.st, k, prev[k])

    def prompt(self) -> str:
        return f"{C.BOLD}{C.CYA}nclaunch {self.counter}>{C.RESET} "

    def help_order(self) -> list[str]:
        return ["Compile", "Simulate", "Waves", "Session", "Shell"]

    def start(self) -> bool:
        console.open_logs(self.project.logs, "nclaunch")
        v, i = env.tool("verilator"), env.tool("iverilog")
        out("")
        out(f"{C.BOLD}{C.WHT}           RTLFlow NCLaunch / Xcelium-style Simulation{C.RESET}")
        out(f"{C.GRY}      Version {VERSION} ({CODENAME}) · Verilator {v.version or '-'} · Icarus {i.version or '-'}{C.RESET}")
        out(rule())
        if not (v.ok or i.ok):
            error("RFN-ENV-001", "Neither Verilator nor Icarus Verilog found")
            return False
        out(f"  {C.GRY}Engines   {C.RESET}{'verilator ✓' if v.ok else 'verilator ✗'}   {'iverilog ✓' if i.ok else 'iverilog ✗'}"
            f"   {C.GRY}default:{C.RESET} {self.st.engine if (v.ok and self.st.engine == 'verilator') or not i.ok else 'icarus'}")
        out(f"  {C.GRY}Log file  {C.RESET}{self.project.rel(console.log_path)}")
        if self.st.rtl or self.st.tb:
            note(f"  Remembered: design {', '.join(Path(f).name for f in self.st.rtl)} · tb "
                 f"{', '.join(Path(f).name for f in self.st.tb)} · top {self.st.top or '?'}")
        out(rule())
        if not v.ok:
            self.st.engine = "icarus"
        if self.want_gui:
            self.gui_open()
        return True

    def on_exit(self) -> None:
        if self.st.rtl or self.st.tb:
            self.project.set(sim=asdict(self.st))
        self.gui_close()
        console.close_logs()

    # ─────────── GUI ───────────
    def gui_sync(self) -> None:
        root = self.project.root
        found = []
        for pat in ("*.sv", "*.v", "*.svh", "*.vh", "rtl/**/*.sv", "rtl/**/*.v", "tb/**/*.sv", "tb/**/*.v",
                    "src/**/*.sv", "src/**/*.v"):
            for f in root.glob(pat):
                s = str(f.resolve())
                if s not in found and ".rtlflow" not in s:
                    found.append(s)
        for f in self.st.rtl + self.st.tb:
            if f not in found:
                found.append(f)
        files = [{"path": f, "role": "design" if f in self.st.rtl else ("tb" if f in self.st.tb else ""),
                  "guess": "tb" if _looks_like_tb(f) else "design", "top": _first_module(f)} for f in sorted(found)]
        self.gui_send(t="sim_state", files=files, top=self.st.top, engine=self.st.engine, wave=self.st.wave,
                      result=self.st.result, built=bool(self.st.built))

    def gui_action(self, msg: dict) -> None:
        if msg.get("t") == "set_roles":
            self.st.rtl = [f for f, r in msg.get("roles", {}).items() if r == "design"]
            self.st.tb = [f for f, r in msg.get("roles", {}).items() if r == "tb"]
            self.st.built = ""
            if msg.get("top"):
                self.st.top = msg["top"]
            if msg.get("engine"):
                self.st.engine = msg["engine"]
            console.write(f"{C.MAG}[GUI]{C.RESET} sources: design {', '.join(Path(f).name for f in self.st.rtl) or '-'} · "
                          f"tb {', '.join(Path(f).name for f in self.st.tb) or '-'} · top {self.st.top or '?'} · {self.st.engine}")
            self.gui_sync()

    # ─────────── compile ───────────
    @command("read_hdl", cat="Compile", aliases=["ncvlog", "xmvlog"], usage="read_hdl [-sv] {files}",
             help="Add design sources")
    def c_read_hdl(self, args):
        opts, pos = parse_opts(args, flags={"-sv", "-v2001", "-linedebug", "-messages", "-nocopyright"},
                               valued={"-define": 1, "-incdir": "+", "-work": 1})
        files = [str(Path(f).resolve()) for f in expand_files(pos)]
        if not files:
            raise CmdError("usage: read_hdl -sv {files}")
        for f in files:
            if not Path(f).exists():
                raise CmdError(f"File not found: {f}")
            if f not in self.st.rtl and f not in self.st.tb:
                # classify: files with no ports & initial blocks → testbench
                (self.st.tb if _looks_like_tb(f) else self.st.rtl).append(f)
        if opts.get("-define"):
            self.st.defines.append(opts["-define"])
        self.st.incdirs += expand_files(opts.get("-incdir", []))
        return self._lint()

    @command("read_tb", cat="Compile", usage="read_tb {files}", help="Add testbench sources")
    def c_read_tb(self, args):
        _, pos = parse_opts(args, flags={"-sv", "-v2001", "-linedebug", "-messages", "-nocopyright"})
        files = [str(Path(f).resolve()) for f in expand_files(pos)]
        if not files:
            raise CmdError("usage: read_tb [-sv] {files}")
        for f in files:
            if not Path(f).exists():
                raise CmdError(f"File not found: {f}")
            if f in self.st.rtl:
                self.st.rtl.remove(f)
            if f not in self.st.tb:
                self.st.tb.append(f)
        new_top = _first_module(files[-1])
        if new_top and new_top != self.st.top:
            self.st.top = new_top          # the testbench you just read becomes the simulation top
            self.st.built = ""
        ok(f"  Testbench: {', '.join(Path(f).name for f in self.st.tb)}  (sim top: {self.st.top or '?'})")
        return True

    @command("compile", cat="Compile", help="Lint/parse the current design + testbench sources")
    def c_compile(self, args):
        if not (self.st.rtl or self.st.tb):
            raise CmdError("No sources — read_hdl / read_tb")
        return self._lint()

    def _lint(self) -> bool:
        exe = env.find_tool("verilator")
        srcs = self.st.rtl + self.st.tb
        for f in srcs:
            info(f"  file: {self.project.rel(f)}  [{'tb' if f in self.st.tb else 'design'}]")
        if not exe or not srcs:
            return True
        cmd = [exe, "--lint-only", "--timing", "-Wno-fatal", "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL"] + \
              [f"+define+{d}" for d in self.st.defines] + [f"-I{d}" for d in self.st.incdirs] + srcs
        if self.st.top:
            cmd += ["--top-module", self.st.top]
        r = runner.run(cmd, cwd=self.project.sim, show=lambda l: l.startswith("%Error"))
        errs = [l for l in r.lines if l.startswith("%Error")]
        warns = [l for l in r.lines if l.startswith("%Warning")]
        if errs:
            error("RFN-VLOG-001", f"{len(errs)} error(s) in HDL")
            return False
        ok(f"  ncvlog: {len(srcs)} file(s) parsed, {len(warns)} warning(s)")
        return True

    @command("elaborate", cat="Compile", aliases=["ncelab", "xmelab"], usage="elaborate [sim_top] [-engine verilator|icarus]",
             help="Build the simulation executable")
    def c_elaborate(self, args):
        opts, pos = parse_opts(args, flags={"-access", "-timescale", "+rwc", "-debug"}, valued={"-engine": 1})
        if pos:
            self.st.top = pos[0].split(".")[-1]
        if opts.get("-engine"):
            self.st.engine = opts["-engine"]
        if not self.st.top:
            self.st.top = _first_module(self.st.tb[0]) if self.st.tb else ""
        if not self.st.top:
            raise CmdError("Specify the simulation top: elaborate <tb_module>")
        srcs = self.st.rtl + self.st.tb
        if not srcs:
            raise CmdError("No sources — read_hdl / read_tb first")
        mods = sorted({m for f in srcs for m in _modules(f)})
        if self.st.top not in mods:
            tbs = [m for f in self.st.tb for m in _modules(f)]
            bad = self.st.top
            self.st.top = tbs[-1] if tbs else ""
            raise CmdError(f"Module '{bad}' is not in the loaded sources. Modules: {', '.join(mods) or '-'}"
                           + (f"  → try: elaborate {tbs[-1]}" if tbs else ""), "RFN-ELAB-002")
        build = self.project.sim / self.st.engine
        build.mkdir(parents=True, exist_ok=True)
        wave = self.project.sim / f"{self.st.top}.vcd"
        need_dump = not any(re.search(r"\$dumpvars|\$dumpfile", _read(f)) for f in self.st.tb)
        extra: list[str] = []
        top = self.st.top
        if need_dump:
            dm = build / "rtlflow_dump.sv"
            if self.st.engine == "icarus":
                dm.write_text(f"// RTLFlow auto waveform dump\nmodule rtlflow_dump;\n  initial begin\n"
                              f"    $dumpfile(\"{wave}\");\n    $dumpvars(0, {top});\n  end\nendmodule\n")
            else:
                dm.write_text(f"// RTLFlow auto waveform dump (wrapper top)\nmodule rtlflow_top;\n  {top} {top}();\n"
                              f"  initial begin\n    $dumpfile(\"{wave}\");\n    $dumpvars(0, rtlflow_top.{top});\n"
                              f"  end\nendmodule\n")
                top = "rtlflow_top"
            extra.append(str(dm))
            info("  Waveform: TB has no $dumpvars — RTLFlow dump module added")
        t0 = time.time()
        if self.st.engine == "verilator":
            exe = env.find_tool("verilator")
            if not exe:
                raise CmdError("verilator not found")
            cmd = [exe, "--binary", "--timing", "--trace", "-Wno-fatal", "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL",
                   "-Wno-MULTITOP", "-j", str(os.cpu_count() or 2), "--top-module", top, "--Mdir", str(build / "obj_dir"),
                   "-o", f"V{top}"] + [f"+define+{d}" for d in self.st.defines] + [f"-I{d}" for d in self.st.incdirs] + srcs + extra
            note(f"ncelab: building {self.st.top} with Verilator …")
            r = runner.run(cmd, cwd=build, log=build / "elab.log", show=runner.important)
            binp = build / "obj_dir" / f"V{top}"
        else:
            exe = env.find_tool("iverilog")
            if not exe:
                raise CmdError("iverilog not found")
            binp = build / f"{self.st.top}.vvp"
            roots = ["-s", self.st.top] + (["-s", "rtlflow_dump"] if need_dump else [])
            cmd = [exe, "-g2012", "-o", str(binp)] + roots + [f"-D{d}" for d in self.st.defines] + \
                  [f"-I{d}" for d in self.st.incdirs] + srcs + extra
            note(f"ncelab: building {self.st.top} with Icarus …")
            r = runner.run(cmd, cwd=build, log=build / "elab.log", show=True)
        if not r.ok or not binp.exists():
            error("RFN-ELAB-001", f"Elaboration failed (log {self.project.rel(build / 'elab.log')})")
            return False
        self.st.built = str(binp)
        self.st.wave = str(wave) if need_dump else ""
        ok(f"  Snapshot built in {time.time() - t0:.1f}s → {self.project.rel(binp)}")
        return True

    # ─────────── simulate ───────────
    @command("simulate", cat="Simulate", aliases=["ncsim", "xmsim", "run"], usage="simulate [-gui] [+plusargs]",
             help="Run the simulation (-gui opens waves afterwards)")
    def c_simulate(self, args):
        gui = "-gui" in args
        plus = [a for a in args if a.startswith("+")]
        rest = [a for a in args if a not in ("-gui",) and not a.startswith("+")]
        if rest and rest[0] != self.st.top:
            self.st.top = rest[0]
            self.st.built = ""
        if not self.st.built or not Path(self.st.built).exists():
            if not self.c_elaborate([]):
                return False
        binp = Path(self.st.built)
        cmd = [str(binp)] + plus if self.st.engine == "verilator" else [env.find_tool("vvp") or "vvp", str(binp)] + plus
        if self.st.engine != "verilator" and not shutil.which(cmd[0]):
            cmd[0] = str(Path(env.find_tool("iverilog")).parent / "vvp")
        out(rule(f"ncsim: {self.st.top}"))
        r = runner.run(cmd, cwd=self.project.sim, log=self.project.sim / "sim.log", show=True)
        out(rule())
        txt = r.text
        failed = r.rc != 0 or re.search(r"(?i)\b(fail(ed)?|error|mismatch|\$fatal)\b", txt) and not re.search(r"(?i)0 errors", txt)
        passed = re.search(r"(?i)\b(pass(ed)?|success)\b", txt)
        self.st.result = "FAIL" if failed and not passed else ("PASS" if passed else f"rc={r.rc}")
        wave = self.st.wave or _find_wave(self.project.sim) or _find_wave(Path.cwd())
        if wave:
            self.st.wave = str(wave)
        rows = [("Engine", self.st.engine), ("Result", self.st.result), ("Exit code", r.rc),
                ("Waveform", self.project.rel(wave) if wave else "none"), ("Log", self.project.rel(self.project.sim / "sim.log"))]
        out(table(rows, headers=("simulation", "")))
        self.project.set(sim=asdict(self.st))
        self.project.stage("nclaunch", "simulated", result=self.st.result)
        self.gui_sync()
        if wave:
            self.gui_send(t="wave", file=str(wave))
        if gui and not (self.gui and self.gui.is_open):
            simvision([str(wave)] if wave else [])
        return r.rc == 0

    @command("simvision", cat="Waves", aliases=["waves", "gtkwave"], usage="simvision [file.vcd]",
             help="Open the waveform viewer (GTKWave / Surfer)")
    def c_simvision(self, args):
        return simvision(args or ([self.st.wave] if self.st.wave else []))

    @command("status", cat="Session", help="Simulation setup")
    def c_status(self, args):
        rows = [("Design", ", ".join(Path(f).name for f in self.st.rtl) or "-"),
                ("Testbench", ", ".join(Path(f).name for f in self.st.tb) or "-"),
                ("Sim top", self.st.top or "-"), ("Engine", self.st.engine),
                ("Snapshot", self.project.rel(self.st.built) if self.st.built else "-"),
                ("Last result", self.st.result or "-"), ("Waveform", self.project.rel(self.st.wave) if self.st.wave else "-")]
        out(table(rows, headers=("nclaunch status", "")))
        return True

    @command("set_engine", cat="Session", usage="set_engine verilator|icarus", help="Choose simulator")
    def c_engine(self, args):
        if not args or args[0] not in ("verilator", "icarus"):
            raise CmdError("usage: set_engine verilator|icarus")
        self.st.engine = args[0]
        self.st.built = ""
        ok(f"Engine = {args[0]}")
        return True

    @command("clear_sources", cat="Session", help="Forget all sources")
    def c_clear_sources(self, args):
        self.st = SimState(engine=self.st.engine)
        ok("Sources cleared")
        return True

    @command("xrun", cat="Simulate", aliases=["irun", "ncverilog"], usage="xrun [-sv] files -top tb [-gui]",
             help="One-shot compile+elaborate+simulate")
    def c_xrun(self, args):
        return xrun(args, shell=self)


# ─────────────────────────── helpers / one-shot ───────────────────────────
def _read(f) -> str:
    try:
        return Path(f).read_text(errors="replace")
    except OSError:
        return ""


def _modules(f: str) -> list[str]:
    t = re.sub(r"//[^\n]*|/\*.*?\*/", "", _read(f), flags=re.S)
    return re.findall(r"^\s*module\s+(\w+)", t, re.M)


def _first_module(f: str) -> str:
    m = re.findall(r"^\s*module\s+(\w+)", re.sub(r"//[^\n]*", "", _read(f)), re.M)
    return m[0] if m else ""


def _looks_like_tb(f: str) -> bool:
    t = re.sub(r"//[^\n]*|/\*.*?\*/", "", _read(f), flags=re.S)
    m = re.search(r"^\s*module\s+(\w+)\s*(#\s*\(.*?\))?\s*(\(|;)", t, re.M | re.S)
    no_ports = bool(m and m.group(3) == ";") or bool(re.search(r"^\s*module\s+\w+\s*\(\s*\)\s*;", t, re.M))
    return (no_ports and ("initial" in t or "$finish" in t)) or bool(m and re.match(r"(?i)^(tb|test)", m.group(1)))


def _find_wave(d: Path) -> Path | None:
    c = sorted([*d.glob("**/*.vcd"), *d.glob("**/*.fst")], key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
    return c[0] if c else None


def simvision(args: list[str]) -> bool:
    proj = Project()
    wave = Path(args[0]) if args else (_find_wave(proj.sim) or _find_wave(Path.cwd()))
    if not wave or not Path(wave).exists():
        error("RFN-WAV-001", "No waveform found — run a simulation first (simulate / xrun)")
        return False
    viewer = env.find_tool("gtkwave") or env.find_tool("surfer")
    if not viewer:
        error("RFN-WAV-002", "No waveform viewer (install gtkwave or surfer)")
        return False
    subprocess.Popen([viewer, str(wave)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    ok(f"SimVision → {Path(viewer).name} {proj.rel(wave)}")
    return True


def xrun(args: list[str], shell: "NCLaunchShell | None" = None) -> bool:
    opts, pos = parse_opts(args, flags={"-sv", "-gui", "-verilator", "-icarus", "-access", "+rwc", "-clean",
                                        "-linedebug", "-nocopyright"},
                           valued={"-top": 1, "-define": 1, "-incdir": "+", "-timescale": 1})
    sh = shell or NCLaunchShell()
    own = shell is None
    if own:
        console.open_logs(sh.project.logs, "xrun")
    files = [str(Path(f).resolve()) for f in expand_files(pos)]
    if files:
        sh.st.rtl, sh.st.tb = [], []
        for f in files:
            (sh.st.tb if _looks_like_tb(f) else sh.st.rtl).append(f)
    if opts.get("-define"):
        sh.st.defines = [opts["-define"]]
    sh.st.incdirs = expand_files(opts.get("-incdir", []))
    if opts.get("-icarus"):
        sh.st.engine = "icarus"
    elif opts.get("-verilator"):
        sh.st.engine = "verilator"
    if not env.find_tool("verilator") and env.find_tool("iverilog"):
        sh.st.engine = "icarus"
    sh.st.top = opts.get("-top") or (_first_module(sh.st.tb[0]) if sh.st.tb else sh.st.top)
    sh.st.built = ""
    okk = sh._lint() and sh.c_elaborate([]) and sh.c_simulate(["-gui"] if opts.get("-gui") else [])
    if own:
        sh.project.set(sim=asdict(sh.st))
        console.close_logs()
    return bool(okk)


def run_nclaunch(files: list[str] | None = None, gui: bool = False) -> int:
    sh = NCLaunchShell(gui=gui)
    if not sh.start():
        console.close_logs()
        return 1
    for f in files or []:
        sh.c_source([f])
    return sh.cmdloop()
