"""tempus — Tempus-style signoff STA shell on a persistent OpenSTA session.

    rtlflow> tempus            → tempus N>        (terminal)
    tempus 1> gui_show         → Tempus GUI: slack histogram, path browser, stage delays
    rtlflow> tempus -gui

Load a design by hand (read_libs / read_netlist / link_design / read_sdc /
read_spef) or in one step from the other tools:
    read_design -from genus     (mapped netlist + SDC from synthesis)
    read_design -from innovus   (post-route netlist + SPEF + SDC from implementation)
Anything that is not a tempus command goes straight to OpenSTA's Tcl.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import CODENAME, VERSION, env
from ..backends.openroad import BackendDead, OpenROADSession
from ..core.guishell import GuiMixin
from ..core.msg import C, colorize_backend_line, console, error, info, note, ok, out, rule, table, warn
from ..core.project import Project
from ..core.shell import CmdError, Shell, command, expand_files, parse_opts
from .innovus import _color_timing, _parse_paths, _tcl_list

_POINT = re.compile(r"(-?[\d.]+)\s+(-?[\d.]+)\s+([v^])\s+(\S+)\s+\(([^)]+)\)\s*$")
_END = re.compile(r"^(\S+)\s+\(([^)]*)\)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*(\(MET\)|\(VIOLATED\))?\s*$")


@dataclass
class TempusState:
    libs: list[str] = field(default_factory=list)
    netlists: list[str] = field(default_factory=list)
    sdcs: list[str] = field(default_factory=list)
    spef: str = ""
    top: str = ""
    linked: bool = False
    mode: str = "setup"
    source: str = ""
    summary: dict = field(default_factory=dict)


class TempusShell(GuiMixin, Shell):
    tool = "tempus"
    msg_prefix = "RFT"
    history_name = ".rtlflow_history_tempus"
    gui_module = "tempus_gui"
    gui_label = "Tempus GUI"

    def __init__(self, gui: bool = False, files: list[str] | None = None) -> None:
        super().__init__()
        self._gui_init()
        self.project = Project()
        self.st = TempusState()
        self.want_gui = gui
        self.files = files or []
        self.session: OpenROADSession | None = None

    def prompt(self) -> str:
        return f"{C.BOLD}{C.YEL}tempus {self.counter}>{C.RESET} "

    def help_order(self) -> list[str]:
        return ["Design", "Analysis", "Reports", "Output", "GUI", "Shell"]

    # ─────────── lifecycle ───────────
    def start(self) -> bool:
        console.open_logs(self.project.logs, "tempus")
        exe = env.find_tool("sta") or env.find_tool("openroad")
        if not exe:
            error("RFT-ENV-001", "Neither OpenSTA (sta) nor OpenROAD found")
            return False
        self.session = OpenROADSession(exe, self.project.path("runs", "sta"),
                                       on_line=lambda l: console.write(colorize_backend_line(l)))
        try:
            self.session.start()
        except BackendDead as e:
            error("RFT-ENV-002", f"OpenSTA failed to start: {e}")
            return False
        out("")
        out(f"{C.BOLD}{C.WHT}           RTLFlow Tempus(R) Timing Signoff Solution{C.RESET}")
        out(f"{C.GRY}      Version {VERSION} ({CODENAME}) · engine: OpenSTA {self.session.version}{C.RESET}")
        out(rule())
        out(f"  {C.GRY}Engine    {C.RESET}{env.shorten(exe, 60)} (pid {self.session.proc.pid})")
        out(f"  {C.GRY}Log file  {C.RESET}{self.project.rel(console.log_path)}")
        g, i = self.project.get("genus") or {}, self.project.get("innovus") or {}
        if i.get("stage") in ("route", "filled", "signoff"):
            note("  Routed design available → read_design -from innovus")
        elif g.get("netlist"):
            note("  Synthesised netlist available → read_design -from genus")
        out(rule())
        if self.want_gui:
            self.gui_open()
        return True

    def on_start(self) -> None:
        for f in self.files:
            self.c_source([f])

    def on_exit(self) -> None:
        if self.st.linked:
            self.project.set(tempus=asdict(self.st))
        self.gui_close()
        if self.session:
            self.session.stop()
        console.close_logs()

    def tcl(self, script: str, quiet: bool = False, what: str = ""):
        if not self.session or not self.session.alive:
            raise CmdError("OpenSTA session is not running", "RFT-STA-000")
        try:
            r = self.session.run(script, stream=not quiet)
        except BackendDead as e:
            raise CmdError(str(e), "RFT-STA-DEAD")
        if not r.ok and not quiet:
            error("RFT-STA-001", f"{what or 'OpenSTA'}: {r.value.strip().splitlines()[0] if r.value.strip() else 'failed'}")
        return r

    def default(self, name, args, line, source):
        r = self.tcl(line, what=name)
        if r.ok and r.value.strip():
            out(r.value)
        return r.ok

    def _need_link(self) -> None:
        if not self.st.linked:
            raise CmdError("No design linked — read_design -from genus|innovus, or read_libs/read_netlist/link_design",
                           "RFT-DES-001")

    # ─────────── design ───────────
    @command("read_libs", cat="Design", usage="read_libs <file.lib ...>", help="Read liberty timing libraries")
    def c_read_libs(self, args):
        files = expand_files(args) or ([str(env.pdk().liberty())] if env.pdk().liberty() else [])
        if not files:
            raise CmdError("usage: read_libs <file.lib>")
        for f in files:
            if not Path(f).exists():
                raise CmdError(f"File not found: {f}")
            if f in self.st.libs:
                continue
            if not self.tcl(f"read_liberty {{{f}}}", what="read_liberty").ok:
                return False
            self.st.libs.append(f)
            ok(f"  Liberty  {Path(f).name}")
        return True

    @command("read_netlist", cat="Design", usage="read_netlist <netlist.v> [-top name]", help="Read gate-level Verilog")
    def c_read_netlist(self, args):
        opts, pos = parse_opts(args, valued={"-top": 1})
        for f in expand_files(pos):
            if not Path(f).exists():
                raise CmdError(f"File not found: {f}")
            if not self.tcl(f"read_verilog {{{f}}}", what="read_verilog").ok:
                return False
            self.st.netlists.append(f)
            ok(f"  Netlist  {Path(f).name}")
            if not self.st.top:
                m = re.search(r"^([A-Za-z_]\w*)(?:_mapped|\.pnr)?\.v$", Path(f).name)
                self.st.top = m.group(1) if m else ""
        if opts.get("-top"):
            self.st.top = opts["-top"]
        return True

    @command("link_design", cat="Design", aliases=["init_design", "set_top"], usage="link_design [top]", help="Link the netlist")
    def c_link(self, args):
        top = args[0] if args else self.st.top
        if not top:
            raise CmdError("usage: link_design <top>")
        self.st.top = top
        if not self.tcl(f"link_design {top}", what="link_design").ok:
            return False
        self.st.linked = True
        ok(f"  Linked '{top}'")
        return True

    @command("read_sdc", cat="Design", usage="read_sdc <file.sdc>", help="Read constraints")
    def c_read_sdc(self, args):
        self._need_link()
        for f in expand_files(args):
            if not self.tcl(f"read_sdc {{{f}}}", what="read_sdc").ok:
                return False
            self.st.sdcs.append(f)
            ok(f"  SDC      {Path(f).name}")
        return True

    @command("read_spef", cat="Design", usage="read_spef <file.spef>", help="Read extracted parasitics")
    def c_read_spef(self, args):
        self._need_link()
        if not args:
            raise CmdError("usage: read_spef <file.spef>")
        f = expand_files(args)[0]
        if not self.tcl(f"read_spef {{{f}}}", what="read_spef").ok:
            return False
        self.st.spef = f
        self.tcl("catch {set_propagated_clock [all_clocks]}", quiet=True)
        ok(f"  SPEF     {Path(f).name}  (clocks propagated)")
        return True

    @command("read_design", cat="Design", usage="read_design -from genus|innovus", help="Load a design from another RTLFlow tool")
    def c_read_design(self, args):
        opts, _ = parse_opts(args, valued={"-from": 1})
        src = (opts.get("-from") or "").lower()
        p = self.project
        if src == "genus":
            g = p.get("genus") or {}
            top = g.get("top")
            libs = g.get("libs", [])
            nl = p.synth / f"{top}_mapped.v"
            sdc = p.synth / f"{top}.sdc"
            spef = None
        elif src == "innovus":
            i = p.get("innovus") or {}
            top = i.get("top")
            libs = i.get("libs", [])
            nl = p.pnr / f"{top}.pnr.v"
            sdc = Path(i["sdcs"][0]) if i.get("sdcs") else p.synth / f"{top}.sdc"
            spef = p.pnr / f"{top}.spef"
            if not nl.exists():
                raise CmdError(f"{p.rel(nl)} not found — in innovus run 'saveNetlist' (and 'extractRC' for SPEF)", "RFT-DES-010")
        else:
            raise CmdError("usage: read_design -from genus|innovus")
        if not top or not nl.exists():
            raise CmdError(f"No {src} results in this project", "RFT-DES-011")
        note(f"Loading '{top}' from {src} …")
        okk = self.c_read_libs(libs) and self.c_read_netlist([str(nl)]) and self.c_link([top])
        if okk and sdc and Path(sdc).exists():
            okk = self.c_read_sdc([str(sdc)])
        elif okk:
            warn("RFT-DES-012", "No SDC found — define clocks with create_clock")
        if okk and spef and spef.exists():
            okk = self.c_read_spef([str(spef)])
        elif okk and src == "innovus":
            warn("RFT-DES-013", "No SPEF — run extractRC in innovus for signoff parasitics (using wire-load-free delays)")
        self.st.source = src
        if okk:
            self.c_summary([])
            self.gui_sync()
        return okk

    # ─────────── analysis ───────────
    def _summary(self) -> dict:
        s = {}
        for key, cmd in (("wns", "sta::worst_slack -max"), ("whs", "sta::worst_slack -min"),
                         ("tns", "sta::total_negative_slack -max"), ("tns_hold", "sta::total_negative_slack -min"),
                         ("viol", "sta::endpoint_violation_count max"), ("viol_hold", "sta::endpoint_violation_count min")):
            r = self.tcl(cmd, quiet=True)
            try:
                v = float(r.value) if r.ok else None
                if v is not None and key.startswith(("wns", "whs", "tns")) and v != 0 and abs(v) < 1e-6:
                    v *= 1e9          # SI seconds → ns (older OpenSTA returns internal units)
                if v is not None and abs(v) > 1e20:
                    v = None          # INF: no paths
                s[key] = v
            except ValueError:
                s[key] = None
        if s.get("wns") is None:
            r = self.tcl("report_worst_slack -max -digits 4", quiet=True)
            m = re.search(r"worst slack\s+(-?[\d.]+)", r.text)
            s["wns"] = float(m.group(1)) if m else None
        r = self.tcl("llength [all_clocks]", quiet=True)
        s["clocks"] = int(r.value) if r.ok and r.value.strip().isdigit() else 0
        self.st.summary = s
        return s

    @command("report_analysis_summary", cat="Analysis", aliases=["update_timing", "report_summary"],
             help="Setup/hold WNS, TNS and violating endpoints (Tempus-style table)")
    def c_summary(self, args):
        self._need_link()
        s = self._summary()

        def f(v, nd=3):
            if v is None:
                return "NA"
            col = C.RED if v < 0 else C.GRN
            return f"{col}{v:.{nd}f}{C.RESET}"
        vi = lambda v: "NA" if v is None else str(int(v))  # noqa: E731
        out(table([("WNS (ns)", f(s.get("wns")), f(s.get("whs"))),
                   ("TNS (ns)", f(s.get("tns")), f(s.get("tns_hold"))),
                   ("Violating endpoints", vi(s.get("viol")), vi(s.get("viol_hold")))],
                  headers=("Analysis summary", "Setup", "Hold")))
        info(f"  {s['clocks']} clock(s) · parasitics: {'SPEF ' + Path(self.st.spef).name if self.st.spef else 'none (ideal wires)'}")
        self.gui_send(t="summary", summary=s)
        return True

    @command("set_analysis_mode", cat="Analysis", usage="set_analysis_mode -setup|-hold", help="Default check type for GUI/report_timing")
    def c_mode(self, args):
        self.st.mode = "hold" if "-hold" in args else "setup"
        ok(f"  analysis mode = {self.st.mode}")
        return True

    @command("report_timing", cat="Reports", usage="report_timing [-max_paths N] [-early|-late] [-from x] [-to y] [-through z]",
             help="Detailed timing paths")
    def c_report_timing(self, args):
        self._need_link()
        opts, _ = parse_opts(args, flags={"-early", "-late", "-unconstrained"},
                             valued={"-max_paths": 1, "-nworst": 1, "-from": 1, "-to": 1, "-through": 1})
        pd = "min" if opts.get("-early") or (self.st.mode == "hold" and not opts.get("-late")) else "max"
        n = int(opts.get("-max_paths") or 1)
        extra = "".join(f" {k} {opts[k]}" for k in ("-from", "-to", "-through") if opts.get(k))
        if opts.get("-unconstrained"):
            extra += " -unconstrained"
        r = self._checks(pd, n, extra, fields=True)
        for l in r.lines:
            console.write(_color_timing(l))
        paths = _parse_paths(r.text)
        if paths:
            self.gui_send(t="highlight", path=paths[0])
        return r.ok

    def _checks(self, pd: str, n: int, extra: str = "", fields: bool = False, endpoint: bool = False):
        fl = " -fields {slew cap input_pins fanout}" if fields else ""
        base = f"report_checks -path_delay {pd} -format full_clock_expanded{fl} -digits 3{extra}"
        ep = " -endpoint_path_count 1" if endpoint else ""
        r = self.tcl(f"{base} -group_path_count {n}{ep}", quiet=True)
        if not r.ok:
            ep2 = " -endpoint_count 1" if endpoint else ""
            r = self.tcl(f"{base} -group_count {n}{ep2}", quiet=True)
        return r

    @command("report_constraints", cat="Reports", aliases=["report_constraint"], usage="report_constraints [-all_violators]",
             help="DRV and timing constraint violations")
    def c_constraints(self, args):
        self._need_link()
        v = " -violators" if "-all_violators" in args else ""
        return self.tcl(f"report_check_types -max_slew -max_capacitance -max_fanout -max_delay -min_delay{v}").ok

    @command("check_timing", cat="Reports", help="Unconstrained endpoints, missing clocks, loops (check_setup)")
    def c_check(self, args):
        self._need_link()
        r = self.tcl("check_setup -verbose")
        if r.ok and not r.text.strip():
            ok("check_timing: no issues")
        return r.ok

    @command("report_clocks", cat="Reports", aliases=["report_clock"], help="Clock definitions")
    def c_clocks(self, args):
        self._need_link()
        return self.tcl("report_clock_properties").ok

    @command("report_power", cat="Reports", help="Power (OpenSTA default activity)")
    def c_power(self, args):
        self._need_link()
        return self.tcl("report_power -digits 4").ok

    @command("report_slack_histogram", cat="Reports", usage="report_slack_histogram [-hold] [-bins N]",
             help="Endpoint slack distribution")
    def c_hist(self, args):
        self._need_link()
        opts, _ = parse_opts(args, flags={"-hold"}, valued={"-bins": 1})
        slacks = [e["slack"] for e in self._endpoints("min" if opts.get("-hold") else "max")]
        if not slacks:
            warn("RFT-REP-001", "No constrained endpoints")
            return True
        bins = int(opts.get("-bins") or 10)
        lo, hi = min(slacks), max(slacks)
        w = (hi - lo) / bins or 1.0
        counts = [0] * bins
        for s in slacks:
            counts[min(bins - 1, int((s - lo) / w))] += 1
        mx = max(counts)
        for i, c in enumerate(counts):
            a = lo + i * w
            bar = "█" * int(40 * c / mx) if mx else ""
            col = C.RED if a + w <= 0 else C.GRN
            out(f"  {a:8.3f} .. {a + w:8.3f}  {col}{bar}{C.RESET} {c}")
        info(f"  {len(slacks)} endpoints")
        return True

    def _endpoints(self, pd: str, n: int = 5000) -> list[dict]:
        r = self.tcl(f"report_checks -path_delay {pd} -format end -group_path_count {n} -endpoint_path_count 1", quiet=True)
        if not r.ok:
            r = self.tcl(f"report_checks -path_delay {pd} -format end -group_count {n} -endpoint_count 1", quiet=True)
        eps = []
        for l in r.lines:
            m = _END.match(l.strip())
            if m:
                eps.append({"pin": m.group(1), "req": float(m.group(3)), "arr": float(m.group(4)), "slack": float(m.group(5))})
        if not eps:   # fall back to full paths
            for p in _parse_paths(self._checks(pd, min(n, 200), endpoint=True).text):
                if p.get("slack") is not None:
                    eps.append({"pin": p["end"], "slack": p["slack"]})
        return eps

    # ─────────── output ───────────
    @command("write_sdf", cat="Output", usage="write_sdf <file.sdf>", help="Write SDF")
    def c_sdf(self, args):
        self._need_link()
        fn = args[0] if args else str(self.project.path("runs", "sta") / f"{self.st.top}.sdf")
        r = self.tcl(f"write_sdf {{{fn}}}")
        if r.ok:
            ok(f"SDF written: {fn}")
        return r.ok

    @command("status", cat="Design", help="What is loaded")
    def c_status(self, args):
        s = self.st
        rows = [("Engine", f"OpenSTA {self.session.version if self.session else '?'}"),
                ("Libraries", ", ".join(Path(x).name for x in s.libs) or "-"),
                ("Netlist", ", ".join(Path(x).name for x in s.netlists) or "-"),
                ("Top / linked", f"{s.top or '-'} / {'yes' if s.linked else 'no'}"),
                ("SDC", ", ".join(Path(x).name for x in s.sdcs) or "-"),
                ("SPEF", Path(s.spef).name if s.spef else "-"), ("Mode", s.mode), ("Source", s.source or "manual")]
        out(table(rows, headers=("tempus status", "")))
        return True

    # ─────────── GUI ───────────
    def gui_sync(self) -> None:
        self.gui_send(t="state", state={"top": self.st.top, "linked": self.st.linked, "spef": self.st.spef,
                                        "source": self.st.source, "mode": self.st.mode})
        if self.st.linked:
            self._gui_data(self.st.mode)

    def _gui_data(self, mode: str, n: int = 50) -> None:
        pd = "min" if mode == "hold" else "max"
        s = self._summary()
        eps = self._endpoints(pd)
        r = self._checks(pd, n, endpoint=True)
        paths = _parse_paths(r.text)
        blocks = r.text.split("Startpoint:")[1:]
        for p, b in zip(paths, blocks):
            pts = []
            for line in b.splitlines():
                m = _POINT.search(line)
                if m:
                    pts.append({"incr": float(m.group(1)), "time": float(m.group(2)), "edge": m.group(3),
                                "pin": m.group(4), "cell": m.group(5)})
            p["points"] = pts
            p["text"] = "Startpoint:" + b
        self.gui_send(t="timing", mode=mode, summary=s, endpoints=eps, paths=paths)

    def gui_action(self, msg: dict) -> None:
        if msg.get("t") == "refresh":
            if self.st.linked:
                self._gui_data(msg.get("mode", self.st.mode), int(msg.get("n", 50)))
            else:
                self.gui_send(t="timing", mode="setup", summary={}, endpoints=[], paths=[],
                              error="No design linked")


def run_tempus(gui: bool = False, files: list[str] | None = None) -> int:
    sh = TempusShell(gui=gui, files=files)
    if not sh.start():
        console.close_logs()
        return 1
    return sh.cmdloop()
