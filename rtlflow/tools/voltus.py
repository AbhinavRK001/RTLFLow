"""voltus — Voltus-style power & IR-drop shell on OpenROAD (OpenSTA power + PDNSim).

    rtlflow> voltus               → voltus N>
    voltus 1> read_design -from innovus    (latest saveDesign checkpoint, else live DEF)
    voltus 2> set_power_activity -global 0.2
    voltus 3> report_power                 (group breakdown)
    voltus 4> report_power -instances      (per-instance → power map in GUI)
    voltus 5> analyze_rail -net VPWR -voltage 1.8   (IR drop; needs a power grid: innovus sroute)
    voltus 6> gui_show
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

PRELUDE = Path(__file__).resolve().parent.parent / "backends" / "or_prelude.tcl"
_NUM = r"(-?[\d.]+(?:[eE][-+]?\d+)?)"


@dataclass
class VoltusState:
    top: str = ""
    loaded: bool = False
    source: str = ""
    lefs: list[str] = field(default_factory=list)
    libs: list[str] = field(default_factory=list)
    def_file: str = ""
    activity: str = "default"
    power: dict = field(default_factory=dict)
    ir: dict = field(default_factory=dict)


class VoltusShell(GuiMixin, Shell):
    tool = "voltus"
    msg_prefix = "RFV"
    history_name = ".rtlflow_history_voltus"
    gui_module = "voltus_gui"
    gui_label = "Voltus GUI"

    def __init__(self, gui: bool = False, files: list[str] | None = None) -> None:
        super().__init__()
        self._gui_init()
        self.project = Project()
        self.st = VoltusState()
        self.want_gui = gui
        self.files = files or []
        self.session: OpenROADSession | None = None
        self.inst_power: dict[str, float] = {}
        self.inst_volt: dict[str, float] = {}

    def prompt(self) -> str:
        return f"{C.BOLD}{C.RED}voltus {self.counter}>{C.RESET} "

    def help_order(self) -> list[str]:
        return ["Design", "Power", "Rail", "GUI", "Shell"]

    @property
    def rdir(self) -> Path:
        return self.project.path("runs", "power")

    def start(self) -> bool:
        console.open_logs(self.project.logs, "voltus")
        exe = env.find_tool("openroad")
        if not exe:
            error("RFV-ENV-001", "OpenROAD not found")
            return False
        self._new_session(exe)
        out("")
        out(f"{C.BOLD}{C.WHT}           RTLFlow Voltus(TM) IC Power Integrity Solution{C.RESET}")
        out(f"{C.GRY}      Version {VERSION} ({CODENAME}) · power: OpenSTA · rail: PDNSim (OpenROAD {self.session.version}){C.RESET}")
        out(rule())
        out(f"  {C.GRY}Log file  {C.RESET}{self.project.rel(console.log_path)}")
        i = self.project.get("innovus") or {}
        if i.get("stage") in ("place", "cts", "route", "filled", "signoff"):
            note(f"  Implemented design available (stage: {i.get('stage')}) → read_design -from innovus")
        out(rule())
        if self.want_gui:
            self.gui_open()
        return True

    def _new_session(self, exe: str) -> None:
        if self.session:
            self.session.stop()
        self.session = OpenROADSession(exe, self.rdir, on_line=lambda l: console.write(colorize_backend_line(l)))
        self.session.start()
        self.session.run(PRELUDE.read_text(), stream=False)

    def on_start(self) -> None:
        for f in self.files:
            self.c_source([f])

    def on_exit(self) -> None:
        if self.st.loaded:
            self.project.set(voltus=asdict(self.st))
        self.gui_close()
        if self.session:
            self.session.stop()
        console.close_logs()

    def tcl(self, script: str, quiet: bool = False, what: str = ""):
        try:
            r = self.session.run(script, stream=not quiet)
        except BackendDead as e:
            raise CmdError(str(e), "RFV-OR-DEAD")
        if not r.ok and not quiet:
            error("RFV-OR-001", f"{what or 'OpenROAD'}: {r.value.strip().splitlines()[0] if r.value.strip() else 'failed'}")
        return r

    def default(self, name, args, line, source):
        r = self.tcl(line, what=name)
        if r.ok and r.value.strip():
            out(r.value)
        return r.ok

    def _need(self) -> None:
        if not self.st.loaded:
            raise CmdError("No design loaded — read_design -from innovus", "RFV-DES-001")

    # ─────────── design ───────────
    @command("read_design", cat="Design", usage="read_design -from innovus [checkpoint.odb]",
             help="Load the implemented design (DB checkpoint or live DEF) + libs + SDC + SPEF")
    def c_read_design(self, args):
        opts, pos = parse_opts(args, valued={"-from": 1})
        i = self.project.get("innovus") or {}
        if not i.get("top"):
            raise CmdError("No innovus results in this project", "RFV-DES-010")
        self._new_session(env.find_tool("openroad"))
        top = i["top"]
        libs = i.get("libs", [])
        lefs = (i.get("tech_lefs") or []) + (i.get("cell_lefs") or [])
        for l in libs:
            if not self.tcl(f"read_liberty {{{l}}}", what="read_liberty").ok:
                return False
        dbs = sorted((self.project.pnr / "dbs").glob("*.odb"), key=lambda p: p.stat().st_mtime) if (self.project.pnr / "dbs").exists() else []
        odb = Path(pos[0]) if pos else (dbs[-1] if dbs else None)
        live = self.project.pnr / "live.def"
        if odb and odb.exists():
            note(f"Reading checkpoint {self.project.rel(odb)}")
            if not self.tcl(f"read_db {{{odb}}}", what="read_db").ok:
                return False
            self.st.source = odb.name
        elif live.exists():
            note(f"No checkpoint — reading LEF + {self.project.rel(live)}")
            for j, l in enumerate(lefs):
                flag = "-tech " if j < len(i.get("tech_lefs") or []) else ""
                self.tcl(f"read_lef {flag}{{{l}}}", quiet=True)
            if not self.tcl(f"read_def {{{live}}}", what="read_def").ok:
                return False
            self.st.source = "live.def"
        else:
            raise CmdError("No innovus checkpoint or live.def — run innovus (saveDesign) first", "RFV-DES-011")
        for s in i.get("sdcs", []):
            self.tcl(f"read_sdc {{{s}}}", quiet=True)
        spef = self.project.pnr / f"{top}.spef"
        if spef.exists():
            self.tcl(f"read_spef {{{spef}}}", quiet=True)
            info(f"  SPEF {spef.name}")
        else:
            self.tcl("catch {estimate_parasitics -placement}", quiet=True)
        self.tcl("catch {set_propagated_clock [all_clocks]}", quiet=True)
        self.st.top, self.st.libs, self.st.lefs, self.st.loaded = top, libs, lefs, True
        d = self.rdir / "voltus.def"
        self.tcl(f"write_def {{{d}}}", quiet=True)
        self.st.def_file = str(d)
        ok(f"Design '{top}' loaded from {self.st.source}")
        self.gui_sync()
        return True

    # ─────────── power ───────────
    @command("set_power_activity", cat="Power", usage="set_power_activity -global <activity> [-duty 0.5] | -input <a>",
             help="Default switching activity (toggles/clock)")
    def c_activity(self, args):
        self._need()
        opts, pos = parse_opts(args, flags={"-global", "-input"}, valued={"-duty": 1, "-activity": 1})
        a = opts.get("-activity") or (pos[0] if pos else "0.1")
        scope = "-input" if opts.get("-input") else "-global"
        duty = f" -duty {opts['-duty']}" if opts.get("-duty") else ""
        r = self.tcl(f"set_power_activity {scope} -activity {a}{duty}", what="set_power_activity")
        if r.ok:
            self.st.activity = f"{scope} {a}"
            ok(f"  activity {scope} = {a}")
        return r.ok

    @command("read_activity_file", cat="Power", aliases=["read_vcd"], usage="read_activity_file <dump.vcd> [-scope tb/dut]",
             help="Annotate switching activity from a simulation VCD (e.g. from nclaunch)")
    def c_vcd(self, args):
        self._need()
        opts, pos = parse_opts(args, valued={"-scope": 1, "-format": 1})
        fn = pos[0] if pos else (self.project.get("sim") or {}).get("wave")
        if not fn or not Path(fn).exists():
            raise CmdError("usage: read_activity_file <file.vcd> -scope <tb/dut>")
        scope = f" -scope {opts['-scope']}" if opts.get("-scope") else ""
        r = self.tcl(f"read_vcd{scope} {{{fn}}}", what="read_vcd")
        if r.ok:
            self.st.activity = f"VCD {Path(fn).name}"
            ok(f"  activity from {Path(fn).name}")
        return r.ok

    @command("report_power", cat="Power", usage="report_power [-instances] [-top N]",
             help="Power by group; -instances also builds the power map")
    def c_report_power(self, args):
        self._need()
        opts, _ = parse_opts(args, flags={"-instances", "-hier"}, valued={"-top": 1})
        r = self.tcl("report_power -digits 6", what="report_power")
        if not r.ok:
            return False
        self.st.power = _parse_power(r.text)
        if opts.get("-instances"):
            self._inst_power(int(opts.get("-top") or 0))
        self.gui_send(t="power", power=self.st.power, inst=self.inst_power)
        return True

    def _inst_power(self, top: int = 0) -> None:
        r = self.tcl("report_power -instances [get_cells *] -digits 8", quiet=True)
        vals = {}
        for l in r.lines:
            m = re.match(rf"^\s*{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+(\S+)\s*$", l)
            if m:
                vals[m.group(5).replace("\\", "")] = float(m.group(4))
        self.inst_power = vals
        if vals:
            rows = sorted(vals.items(), key=lambda kv: -kv[1])[: top or 10]
            tot = sum(vals.values()) or 1
            out(table([(n, f"{p * 1e6:.3f}", f"{100 * p / tot:.1f}%") for n, p in rows],
                      headers=("Instance", "Total (µW)", "Share")))
            info(f"  {len(vals)} instances annotated — power map ready in the GUI")
        else:
            warn("RFV-PWR-010", "Per-instance power not available from this OpenSTA build")

    # ─────────── rail ───────────
    @command("analyze_rail", cat="Rail", aliases=["analyze_power_grid", "report_rail"],
             usage="analyze_rail -net VPWR [-voltage 1.8] [-source_type FULL|STRAPS|BUMPS]",
             help="Static IR-drop analysis (PDNSim) with per-instance voltages")
    def c_rail(self, args):
        self._need()
        opts, _ = parse_opts(args, valued={"-net": 1, "-voltage": 1, "-source_type": 1})
        net = opts.get("-net") or "VPWR"
        volt = opts.get("-voltage") or "1.8"
        vf = self.rdir / f"{self.st.top}.{net}.voltage.rpt"
        ef = self.rdir / f"{self.st.top}.{net}.errors.rpt"
        st = f" -source_type {opts['-source_type']}" if opts.get("-source_type") else ""
        self.tcl(f"catch {{set_pdnsim_net_voltage -net {net} -voltage {volt}}}", quiet=True)
        r = self.tcl(f"analyze_power_grid -net {net}{st} -voltage_file {{{vf}}} -error_file {{{ef}}}",
                     what="analyze_power_grid")
        if not r.ok:
            info("  IR analysis needs a power grid: in innovus run globalNetConnect + sroute before saveDesign")
            return False
        ir = {"net": net, "supply": float(volt)}
        for key, pat in (("worst_v", r"Worstcase voltage\s*:\s*" + _NUM), ("avg_v", r"Average voltage\s*:\s*" + _NUM),
                         ("avg_drop", r"Average IR drop\s*:\s*" + _NUM), ("worst_drop", r"Worstcase IR drop\s*:\s*" + _NUM)):
            m = re.search(pat, r.text, re.I)
            if m:
                ir[key] = float(m.group(1))
        self.inst_volt = _parse_voltages(vf)
        if self.inst_volt and "worst_v" not in ir:
            ir["worst_v"] = min(self.inst_volt.values()) if net.upper() not in ("VGND", "VSS", "GND") else max(self.inst_volt.values())
        self.st.ir = ir
        rows = [("Net", net), ("Supply", f"{volt} V")]
        if "worst_drop" in ir:
            rows.append(("Worst IR drop", f"{ir['worst_drop'] * 1000:.3f} mV"))
        if "avg_drop" in ir:
            rows.append(("Average IR drop", f"{ir['avg_drop'] * 1000:.3f} mV"))
        if "worst_v" in ir:
            rows.append(("Worst voltage", f"{ir['worst_v']:.4f} V"))
        rows.append(("Instances annotated", len(self.inst_volt)))
        out(table(rows, headers=("analyze_rail", "")))
        self.gui_send(t="ir", ir=ir, volt=self.inst_volt)
        return True

    @command("status", cat="Design", help="What is loaded")
    def c_status(self, args):
        s = self.st
        rows = [("Design", s.top or "-"), ("Loaded from", s.source or "-"), ("Activity", s.activity),
                ("Total power", f"{s.power.get('total', 0) * 1e3:.4f} mW" if s.power.get("total") else "-"),
                ("Worst IR drop", f"{s.ir['worst_drop'] * 1000:.3f} mV" if s.ir.get("worst_drop") is not None else "-")]
        out(table(rows, headers=("voltus status", "")))
        return True

    # ─────────── GUI ───────────
    def gui_sync(self) -> None:
        self.gui_send(t="design", top=self.st.top, loaded=self.st.loaded, def_file=self.st.def_file,
                      lefs=self.st.lefs, source=self.st.source)
        if self.st.power:
            self.gui_send(t="power", power=self.st.power, inst=self.inst_power)
        if self.st.ir:
            self.gui_send(t="ir", ir=self.st.ir, volt=self.inst_volt)


def _parse_power(text: str) -> dict:
    d: dict = {"groups": {}}
    for l in text.splitlines():
        m = re.match(rf"^\s*(Sequential|Combinational|Clock|Macro|Pad|Total)\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}", l)
        if m:
            vals = {"internal": float(m.group(2)), "switching": float(m.group(3)), "leakage": float(m.group(4)),
                    "total": float(m.group(5))}
            if m.group(1) == "Total":
                d.update(vals)
            else:
                d["groups"][m.group(1)] = vals
    return d


def _parse_voltages(path: Path) -> dict[str, float]:
    vals: dict[str, float] = {}
    if not path.exists():
        return vals
    for l in path.read_text(errors="replace").splitlines():
        parts = [p.strip() for p in re.split(r"[,\s]+", l.strip()) if p.strip()]
        if len(parts) < 2 or parts[0].lower().startswith(("instance", "#", "name")):
            continue
        try:
            v = float(parts[-1])
        except ValueError:
            continue
        if 0 <= v < 100:
            vals[parts[0].replace("\\", "")] = v
    return vals


def run_voltus(gui: bool = False, files: list[str] | None = None) -> int:
    sh = VoltusShell(gui=gui, files=files)
    if not sh.start():
        console.close_logs()
        return 1
    return sh.cmdloop()
