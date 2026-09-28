"""rtlflow — top-level suite shell (the 'Cadence launcher').

    $ rtlflow                 → rtlflow> prompt, tool discovery table
    rtlflow> genus [-gui]     → @genus:root: N>   (synthesis — Yosys/OpenSTA)
    rtlflow> nclaunch         → nclaunch N>       (simulation — Verilator/Icarus)
    rtlflow> innovus [-nowin] → innovus N> + layout GUI (implementation — OpenROAD)
    rtlflow> simvision        → waveform viewer (GTKWave/Surfer) on the last dump
    rtlflow> xrun ...         → one-shot compile+elaborate+simulate (Xcelium-style)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import CODENAME, VERSION, env
from .core.msg import C, console, error, info, note, ok, out, rule, table, warn
from .core.project import Project
from .core.shell import CmdError, Shell, command, parse_opts

LOGO = r"""
   ____  _____ _     _____ _
  |  _ \|_   _| |   |  ___| | _____      __
  | |_) | | | | |   | |_  | |/ _ \ \ /\ / /
  |  _ <  | | | |___|  _| | | (_) \ V  V /
  |_| \_\ |_| |_____|_|   |_|\___/ \_/\_/
"""


def print_tools(verbose: bool = False) -> None:
    out(f"{C.BOLD}[RTLFlow] Tool discovery{C.RESET}")
    for name, t in env.tool_status():
        role, equiv = env.TOOLS[name][2], env.TOOLS[name][3]
        mark = f"{C.GRN}✓{C.RESET}" if t.ok else (f"{C.RED}✗{C.RESET}" if name in env.REQUIRED else f"{C.GRY}-{C.RESET}")
        ver = f"{C.GRY}{t.version}{C.RESET}" if t.version else ""
        path = env.shorten(t.path, 56) if t.ok else f"{C.GRY}not found{C.RESET}"
        src = f"{C.GRY}[{t.source}]{C.RESET}" if verbose and t.source else ""
        line = f"  {name:<10} {mark} {path} {ver} {src}"
        if verbose:
            line += f"  {C.GRY}{role} ≈ {equiv}{C.RESET}"
        out(line)
    orp = env.tool("openroad")
    for name, (host, role, equiv) in env.BUILTIN.items():
        mark = f"{C.GRN}✓{C.RESET}" if orp.ok else f"{C.GRY}-{C.RESET}"
        out(f"  {name:<10} {mark} {C.GRY}built into OpenROAD{C.RESET}" + (f"  {C.GRY}{role} ≈ {equiv}{C.RESET}" if verbose else ""))
    p = env.pdk()
    out("")
    out(f"  {'PDK_ROOT':<10} {p.root.replace(str(Path.home()), '~') if p.root else C.YEL + 'not found' + C.RESET}")
    out(f"  {'PDK':<10} {p.name or '-'}" + (f"   {C.GRY}std cells: {p.stdcell}{C.RESET}" if p.stdcell else ""))
    if verbose and p.root:
        for label, f in (("liberty", p.liberty()), ("tech LEF", p.tech_lef()), ("cell LEF", p.cell_lef()),
                         ("RCX rules", p.rcx_rules()), ("KLayout", p.klayout_tech())):
            out(f"  {C.GRY}{label:<10} {env.shorten(str(f), 70) if f else '-'}{C.RESET}")


class SuiteShell(Shell):
    tool = "rtlflow"
    msg_prefix = "RFS"
    history_name = ".rtlflow_history"

    def prompt(self) -> str:
        return f"{C.BOLD}{C.GRN}rtlflow>{C.RESET} "

    def help_order(self) -> list[str]:
        return ["Tools", "Flow", "Environment", "Shell"]

    def banner(self) -> None:
        out(f"{C.BLU}{LOGO}{C.RESET}")
        out(f"  {C.BOLD}RTLFlow EDA Suite v{VERSION}{C.RESET}  {C.GRY}({CODENAME}) · open-source backends, Cadence-style front end{C.RESET}")
        out(rule())
        env.discover()
        env.setup_environment()
        print_tools()
        out(rule())
        proj = Project()
        out(f"  {C.GRY}Project {C.RESET}{str(proj.root).replace(str(Path.home()), '~')}"
            + (f"   {C.GRY}(top: {proj.get('top')}){C.RESET}" if proj.get("top") else ""))
        out(f"  {C.GRY}Tools   {C.RESET}nclaunch · genus · modus · tempus · innovus · voltus · pegasus   "
            f"{C.GRY}'gui' opens the launcher{C.RESET}")
        out("")

    direct = False     # launched as `innovus` / `rtlflow innovus` (no suite prompt to return to)

    def on_start(self) -> None:
        console.open_logs(Project().logs, "rtlflow")

    def _back(self) -> None:
        if not self.direct:
            console.open_logs(Project().logs, "rtlflow")
            out(f"{C.GRY}back in rtlflow{C.RESET}")

    # ─────────────────────────── tools ───────────────────────────
    @command("genus", cat="Tools", usage="genus [-gui] [-files script.tcl] [-batch]",
             help="Synthesis shell (Yosys + OpenSTA) — Genus-style")
    def c_genus(self, args):
        from .tools.genus import run_genus
        opts, pos = parse_opts(args, flags={"-gui", "-batch", "-no_gui"}, valued={"-files": "+", "-f": "+"})
        files = opts.get("-files", []) + opts.get("-f", []) + pos
        console.close_logs()
        try:
            run_genus(gui=bool(opts.get("-gui")), files=files, batch=bool(opts.get("-batch")))
        finally:
            self._back()
        return True

    @command("innovus", cat="Tools", usage="innovus [-nowin] [-files script.tcl] [-batch]",
             help="Implementation shell + layout GUI on one OpenROAD session")
    def c_innovus(self, args):
        from .tools.innovus import run_innovus
        opts, pos = parse_opts(args, flags={"-nowin", "-batch", "-win", "-no_gui", "-stylus"}, valued={"-files": "+", "-init": "+"})
        files = opts.get("-files", []) + opts.get("-init", []) + pos
        console.close_logs()
        try:
            run_innovus(gui=not (opts.get("-nowin") or opts.get("-no_gui")), files=files, batch=bool(opts.get("-batch")))
        finally:
            self._back()
        return True

    @command("nclaunch", cat="Tools", usage="nclaunch [-files script]", help="Simulation shell (Verilator / Icarus)",
             aliases=["xcelium"])
    def c_nclaunch(self, args):
        from .tools.nclaunch import run_nclaunch
        opts, pos = parse_opts(args, flags={"-gui", "-batch"}, valued={"-files": "+"})
        console.close_logs()
        try:
            run_nclaunch(files=opts.get("-files", []) + pos, gui=bool(opts.get("-gui")))
        finally:
            self._back()
        return True

    def _tool(self, modname: str, fn: str, args):
        import importlib
        opts, pos = parse_opts(args, flags={"-gui", "-batch", "-no_gui"}, valued={"-files": "+", "-f": "+"})
        runner_fn = getattr(importlib.import_module(f".tools.{modname}", __package__), fn)
        console.close_logs()
        try:
            runner_fn(gui=bool(opts.get("-gui")), files=opts.get("-files", []) + opts.get("-f", []) + pos)
        finally:
            self._back()
        return True

    @command("tempus", cat="Tools", usage="tempus [-gui] [-files script]", help="Signoff STA shell (OpenSTA) — Tempus-style")
    def c_tempus(self, args):
        return self._tool("tempus", "run_tempus", args)

    @command("modus", cat="Tools", usage="modus [-gui] [-files script]", help="DFT + ATPG shell (scan insertion, stuck-at ATPG)")
    def c_modus(self, args):
        return self._tool("modus", "run_modus", args)

    @command("voltus", cat="Tools", usage="voltus [-gui] [-files script]", help="Power + IR-drop shell (OpenSTA power, PDNSim)")
    def c_voltus(self, args):
        return self._tool("voltus", "run_voltus", args)

    @command("pegasus", cat="Tools", usage="pegasus [-gui] [-files script]", help="Physical verification: DRC (KLayout/Magic) + LVS (Netgen)")
    def c_pegasus(self, args):
        return self._tool("pegasus", "run_pegasus", args)

    @command("gui", cat="Tools", aliases=["launcher"], help="Open the RTLFlow Launcher window (pick a tool to run)")
    def c_gui(self, args):
        from .gui.host import display_available
        if not display_available():
            raise CmdError("No display available")
        import os as _os
        env_ = dict(_os.environ)
        env_["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + (_os.pathsep + env_["PYTHONPATH"] if env_.get("PYTHONPATH") else "")
        subprocess.Popen([sys.executable, "-m", "rtlflow.gui.launcher", "--project", str(Project().root)], env=env_,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        ok("RTLFlow Launcher opened")
        return True

    @command("xrun", cat="Tools", aliases=["irun", "ncverilog"],
             usage="xrun [-sv] files... -top tb [-gui] [-verilator|-icarus] [-define X]",
             help="One-shot compile/elaborate/simulate (Xcelium-style); -gui opens waves")
    def c_xrun(self, args):
        from .tools.nclaunch import xrun
        return xrun(args)

    @command("simvision", cat="Tools", usage="simvision [dump.vcd|.fst]", help="Waveform viewer (GTKWave / Surfer)")
    def c_simvision(self, args):
        from .tools.nclaunch import simvision
        return simvision(args)

    @command("klayout", cat="Tools", usage="klayout [file.gds|.def]", help="Open KLayout (layout viewer / DRC)",
             aliases=["virtuoso"])
    def c_klayout(self, args):
        exe = env.find_tool("klayout")
        if not exe:
            raise CmdError("KLayout not found (tools -rescan)")
        p = env.pdk()
        cmd = [exe] + args
        if p.klayout_props() and args:
            cmd += ["-l", str(p.klayout_props())]
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        ok("KLayout launched")
        return True

    # ─────────────────────────── flow / project ───────────────────────────
    @command("status", cat="Flow", aliases=["project"], help="Project state across genus / nclaunch / innovus")
    def c_status(self, args):
        p = Project()
        if not p.exists():
            info(f"No RTLFlow project here yet ({p.root}). It is created by the first genus/nclaunch/innovus run.")
            return True
        d = p.data
        rows = [("Project", str(p.root).replace(str(Path.home()), "~")), ("Top", d.get("top", "-"))]
        g = d.get("genus", {})
        if g:
            rows += [("RTL", ", ".join(Path(f).name for f in g.get("hdl", [])) or "-"),
                     ("Synthesis", g.get("stage", "-")), ("Netlist", p.rel(g["netlist"]) if g.get("netlist") else "-")]
        s = d.get("sim", {})
        if s:
            rows += [("Simulation", f"{s.get('engine', '-')} · {s.get('result', '-')}"),
                     ("Waves", p.rel(s["wave"]) if s.get("wave") else "-")]
        i = d.get("innovus", {})
        if i:
            rows += [("Implementation", i.get("stage", "-")),
                     ("WNS", (i.get("timing") or {}).get("wns", "-"))]
        out(table(rows, headers=("RTLFlow project", "")))
        hist = d.get("history", [])[-8:]
        if hist:
            out(f"  {C.GRY}recent:{C.RESET}")
            for h in hist:
                out(f"  {C.GRY}{h['time']}  {h['tool']:<8} {h['stage']}{C.RESET}")
        return True

    # ─────────────────────────── environment ───────────────────────────
    @command("tools", cat="Environment", usage="tools [-rescan] [-v]", help="Show discovered tools and PDK")
    def c_tools(self, args):
        opts, _ = parse_opts(args, flags={"-rescan", "-v", "-verbose"})
        if opts.get("-rescan"):
            note("Rescanning PATH, /nix/store and common locations …")
            env.discover(refresh=True)
            env.setup_environment()
        print_tools(verbose=bool(opts.get("-v") or opts.get("-verbose")))
        return True

    @command("doctor", cat="Environment", help="Deep environment check (runs each tool)")
    def c_doctor(self, args):
        env.discover(refresh=True)
        env.setup_environment()
        print_tools(verbose=True)
        out("")
        out(rule("checks"))
        good = True
        orp = env.tool("openroad")
        if orp.ok:
            r = subprocess.run([orp.path, "-no_init", "-no_splash", "-exit"], input="puts OK\n", capture_output=True,
                               text=True, timeout=60)
            okk = "OK" in (r.stdout + r.stderr)
            if okk:
                ok("  openroad   runs headless ✓")
            else:
                error("RFS-DOC-001", "openroad failed to run: " + (r.stderr or r.stdout)[-300:])
            good &= okk
        from .gui.host import display_available, tk_available
        tk_ok, tk_ver = tk_available()
        if tk_ok:
            ok(f"  Python Tk  {tk_ver} ✓  (GUI available)")
        else:
            error("RFS-DOC-002", "Python Tk missing — GUIs disabled. Fix: sudo apt install python3-tk")
            good = False
        if display_available():
            ok(f"  display    {os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY')} ✓")
        else:
            warn("RFS-DOC-003", "No DISPLAY/WAYLAND_DISPLAY — innovus will run -nowin")
        p = env.pdk()
        for label, f in (("liberty", p.liberty()), ("tech LEF", p.tech_lef()), ("cell LEF", p.cell_lef())):
            if f and Path(f).exists():
                ok(f"  {label:<10} {env.shorten(str(f), 64)} ✓")
            else:
                warn("RFS-DOC-004", f"{label} not found in PDK")
        out("")
        if good:
            ok("Environment OK — no nix-shell needed.")
        else:
            warn("RFS-DOC-009", "Some checks failed (see above)")
        return good

    @command("version", cat="Environment", aliases=["getVersion"], help="Version info")
    def c_version(self, args):
        out(f"RTLFlow EDA Suite {VERSION} ({CODENAME})  ·  Python {sys.version.split()[0]}")
        for name, t in env.tool_status():
            if t.ok:
                out(f"  {name:<10} {t.version or '?'}")
        return True

    @command("setenv_pdk", cat="Environment", usage="setenv_pdk <PDK_ROOT> [PDK] [STD_CELL_LIBRARY]",
             help="Point RTLFlow at a different PDK for this session")
    def c_setpdk(self, args):
        if not args:
            raise CmdError("usage: setenv_pdk <PDK_ROOT> [PDK] [STD_CELL_LIBRARY]")
        os.environ["PDK_ROOT"] = os.path.expanduser(args[0])
        if len(args) > 1:
            os.environ["PDK"] = args[1]
        if len(args) > 2:
            os.environ["STD_CELL_LIBRARY"] = args[2]
        env._pdk = None  # type: ignore[attr-defined]
        env.pdk()
        print_tools()
        return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        out(__doc__)
        return 0
    if argv and argv[0] in ("-v", "--version", "version"):
        out(f"RTLFlow {VERSION} ({CODENAME})")
        return 0
    # direct tool entry:  rtlflow innovus -nowin   /  bin/innovus
    if argv and argv[0] in ("-gui", "--gui", "gui", "launcher"):
        from .gui import launcher
        env.discover(versions=False)
        env.setup_environment()
        return launcher.main(["--project", str(Project().root)])
    if argv and argv[0] in ("genus", "innovus", "nclaunch", "xrun", "irun", "simvision", "tools", "doctor",
                            "tempus", "modus", "voltus", "pegasus"):
        env.discover(versions=argv[0] in ("tools", "doctor"))
        env.setup_environment()
        sh = SuiteShell()
        sh.direct = True
        cmd = " ".join(_q(a) for a in argv)
        okk = sh.run_line(cmd)
        return 0 if okk else 1
    sh = SuiteShell()
    sh.banner()
    return sh.cmdloop()


def _q(a: str) -> str:
    return "{" + a + "}" if (" " in a or not a) else a
