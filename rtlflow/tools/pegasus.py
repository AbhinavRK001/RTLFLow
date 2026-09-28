"""pegasus — Pegasus-style physical verification shell: DRC (KLayout or Magic) + LVS (Magic extraction + Netgen).

    rtlflow> pegasus
    pegasus 1> read_layout                 (default: innovus streamOut → .rtlflow/runs/pnr/<top>.gds)
    pegasus 2> run_drc [-engine klayout|magic]
    pegasus 3> run_lvs                     (layout extraction vs post-route netlist)
    pegasus 4> gui_show                    (violation browser on the real layout, LVS result)
Decks come from the PDK (open_pdks/volare layout): libs.tech/klayout/drc, libs.tech/magic, libs.tech/netgen.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import CODENAME, VERSION, env
from ..backends import runner
from ..core.guishell import GuiMixin
from ..core.msg import C, console, error, info, note, ok, out, rule, table, warn
from ..core.project import Project
from ..core.shell import CmdError, Shell, command, expand_files, parse_opts


@dataclass
class PegasusState:
    top: str = ""
    gds: str = ""
    netlist: str = ""
    drc_engine: str = ""
    drc_total: int | None = None
    drc_rules: dict = field(default_factory=dict)
    lvs: str = ""


class PegasusShell(GuiMixin, Shell):
    tool = "pegasus"
    msg_prefix = "RFP"
    history_name = ".rtlflow_history_pegasus"
    gui_module = "pegasus_gui"
    gui_label = "Pegasus GUI"

    def __init__(self, gui: bool = False, files: list[str] | None = None) -> None:
        super().__init__()
        self._gui_init()
        self.project = Project()
        self.st = PegasusState()
        self.want_gui = gui
        self.files = files or []
        self.markers: list[dict] = []
        self.lvs_text = ""

    def prompt(self) -> str:
        return f"{C.BOLD}{C.CYA}pegasus {self.counter}>{C.RESET} "

    def help_order(self) -> list[str]:
        return ["Setup", "DRC", "LVS", "GUI", "Shell"]

    @property
    def vdir(self) -> Path:
        return self.project.path("runs", "signoff")

    def start(self) -> bool:
        console.open_logs(self.project.logs, "pegasus")
        out("")
        out(f"{C.BOLD}{C.WHT}           RTLFlow Pegasus(TM) Verification System{C.RESET}")
        out(f"{C.GRY}      Version {VERSION} ({CODENAME}) · DRC: KLayout / Magic · LVS: Magic + Netgen{C.RESET}")
        out(rule())
        p = env.pdk()
        for name in ("klayout", "magic", "netgen"):
            t = env.tool(name)
            out(f"  {C.GRY}{name:<9}{C.RESET}{'✓ ' + env.shorten(t.path, 56) if t.ok else C.YEL + 'not found' + C.RESET}")
        out(f"  {C.GRY}KLayout deck {C.RESET}{self._klayout_deck() or '-'}")
        out(f"  {C.GRY}Magic rc     {C.RESET}{self._magicrc() or '-'}")
        out(f"  {C.GRY}Netgen setup {C.RESET}{self._netgen_setup() or '-'}")
        out(rule())
        i = self.project.get("innovus") or {}
        if i.get("top") and (self.project.pnr / f"{i['top']}.gds").exists():
            note(f"  GDS available → read_layout   ({self.project.rel(self.project.pnr / (i['top'] + '.gds'))})")
        elif i.get("top"):
            note("  No GDS yet — in innovus run: streamOut")
        if self.want_gui:
            self.gui_open()
        return True

    def on_start(self) -> None:
        for f in self.files:
            self.c_source([f])

    def on_exit(self) -> None:
        if self.st.top:
            self.project.set(pegasus=asdict(self.st))
        self.gui_close()
        console.close_logs()

    # ─────────── PDK decks ───────────
    def _tech(self) -> Path | None:
        p = env.pdk()
        return p.pdk_dir / "libs.tech" if p.pdk_dir else None

    def _klayout_deck(self) -> str | None:
        t = self._tech()
        if not t:
            return None
        d = t / "klayout" / "drc"
        for pat in ("*_mr.drc", "*.drc", "*.lydrc"):
            c = sorted(d.glob(pat)) if d.is_dir() else []
            if c:
                return str(c[0])
        c = sorted((t / "klayout").rglob("*.lydrc")) if (t / "klayout").is_dir() else []
        return str(c[0]) if c else None

    def _magicrc(self) -> str | None:
        t = self._tech()
        c = sorted((t / "magic").glob("*.magicrc")) if t and (t / "magic").is_dir() else []
        return str(c[0]) if c else None

    def _netgen_setup(self) -> str | None:
        t = self._tech()
        c = sorted((t / "netgen").glob("*_setup.tcl")) if t and (t / "netgen").is_dir() else []
        return str(c[0]) if c else None

    def _cell_spice(self) -> str | None:
        p = env.pdk()
        lr = p.libref
        c = sorted((lr / "spice").glob("*.spice")) if lr and (lr / "spice").is_dir() else []
        return str(c[0]) if c else None

    # ─────────── setup ───────────
    @command("read_layout", cat="Setup", aliases=["read_gds"], usage="read_layout [file.gds] [-top name]", help="Layout to verify")
    def c_read_layout(self, args):
        opts, pos = parse_opts(args, valued={"-top": 1})
        i = self.project.get("innovus") or {}
        top = opts.get("-top") or i.get("top") or self.st.top
        gds = Path(expand_files(pos)[0]) if pos else self.project.pnr / f"{top}.gds"
        if not gds.exists():
            raise CmdError(f"{gds} not found — run streamOut in innovus first", "RFP-IN-001")
        self.st.top, self.st.gds = top, str(gds)
        nl = self.project.pnr / f"{top}.pnr.v"
        if nl.exists() and not self.st.netlist:
            self.st.netlist = str(nl)
        ok(f"  Layout {gds.name} · top '{top}'" + (f" · netlist {nl.name}" if nl.exists() else ""))
        self.gui_sync()
        return True

    @command("read_netlist", cat="Setup", usage="read_netlist <netlist.v|.spice>", help="Schematic side for LVS")
    def c_read_netlist(self, args):
        if not args:
            raise CmdError("usage: read_netlist <file>")
        f = expand_files(args)[0]
        if not Path(f).exists():
            raise CmdError(f"File not found: {f}")
        self.st.netlist = f
        ok(f"  LVS netlist {Path(f).name}")
        return True

    def _need(self):
        if not self.st.gds:
            raise CmdError("No layout — read_layout", "RFP-IN-002")

    # ─────────── DRC ───────────
    @command("run_drc", cat="DRC", aliases=["verify_drc"], usage="run_drc [-engine klayout|magic]",
             help="Design-rule check with the PDK signoff deck")
    def c_drc(self, args):
        self._need()
        opts, _ = parse_opts(args, valued={"-engine": 1})
        eng = opts.get("-engine") or ("klayout" if env.find_tool("klayout") and self._klayout_deck() else "magic")
        self.markers = []
        if eng == "klayout":
            okk = self._drc_klayout()
        elif eng == "magic":
            okk = self._drc_magic()
        else:
            raise CmdError("engine must be klayout or magic")
        if not okk:
            return False
        self.st.drc_engine = eng
        rules: dict[str, int] = {}
        for m in self.markers:
            rules[m["rule"]] = rules.get(m["rule"], 0) + 1
        self.st.drc_rules, self.st.drc_total = rules, len(self.markers)
        if rules:
            out(table(sorted(rules.items(), key=lambda kv: -kv[1]), headers=("Rule", "Violations")))
            warn("RFP-DRC-100", f"{len(self.markers)} DRC violation(s) ({eng})")
        else:
            ok(f"DRC clean ({eng}) — 0 violations")
        self.gui_sync()
        return True

    def _drc_klayout(self) -> bool:
        exe, deck = env.find_tool("klayout"), self._klayout_deck()
        if not exe or not deck:
            error("RFP-DRC-001", "KLayout or its DRC deck not found (try -engine magic)")
            return False
        rep = self.vdir / f"{self.st.top}.drc.lyrdb"
        note(f"KLayout DRC: {Path(deck).name} …")
        cmd = [exe, "-b", "-r", deck, "-rd", f"input={self.st.gds}", "-rd", f"topcell={self.st.top}",
               "-rd", f"report={rep}", "-rd", "feol=true", "-rd", "beol=true", "-rd", "offgrid=true",
               "-rd", "floating_metal=false", "-rd", "seal=false", "-rd", f"thr={os.cpu_count() or 2}"]
        r = runner.run(cmd, cwd=self.vdir, log=self.vdir / "drc_klayout.log", show=runner.important)
        if not rep.exists():
            error("RFP-DRC-002", f"KLayout produced no report (log {self.project.rel(self.vdir / 'drc_klayout.log')})")
            return False
        self.markers = _parse_lyrdb(rep)
        return True

    def _drc_magic(self) -> bool:
        exe, rc = env.find_tool("magic"), self._magicrc()
        if not exe or not rc:
            error("RFP-DRC-003", "Magic or the PDK magicrc not found")
            return False
        rep = self.vdir / f"{self.st.top}.drc.magic.rpt"
        tcl = self.vdir / "rtlflow_drc.tcl"
        tcl.write_text(f"""
gds read {{{self.st.gds}}}
load {self.st.top}
select top cell
drc euclidean on
drc style drc(full)
drc check
drc catchup
set oscale [cif scale out]
set fp [open {{{rep}}} w]
set r [drc listall why]
foreach {{why boxes}} $r {{
  foreach b $boxes {{
    puts $fp "VIOLATION\\t$why\\t[expr {{[lindex $b 0]*$oscale}}]\\t[expr {{[lindex $b 1]*$oscale}}]\\t[expr {{[lindex $b 2]*$oscale}}]\\t[expr {{[lindex $b 3]*$oscale}}]"
  }}
}}
close $fp
quit -noprompt
""")
        note("Magic DRC (full style) …")
        r = runner.run([exe, "-dnull", "-noconsole", "-rcfile", rc, str(tcl)], cwd=self.vdir,
                       log=self.vdir / "drc_magic.log", show=runner.important, timeout=3600)
        if not rep.exists():
            error("RFP-DRC-004", f"Magic produced no report (log {self.project.rel(self.vdir / 'drc_magic.log')})")
            return False
        self.markers = []
        for l in rep.read_text().splitlines():
            p = l.split("\t")
            if len(p) == 6:
                self.markers.append({"rule": p[1], "box": [float(x) for x in p[2:6]]})
        return True

    @command("report_drc", cat="DRC", usage="report_drc [-rule pattern]", help="List DRC violations")
    def c_report_drc(self, args):
        if self.st.drc_total is None:
            raise CmdError("run_drc first")
        pat = args[1] if len(args) > 1 and args[0] == "-rule" else ""
        rows = [(m["rule"][:60], f"({m['box'][0]:.3f}, {m['box'][1]:.3f})", f"({m['box'][2]:.3f}, {m['box'][3]:.3f})")
                for m in self.markers if pat.lower() in m["rule"].lower()]
        out(table(rows[:300] or [("-", "-", "-")], headers=("Rule", "Lower-left µm", "Upper-right µm")))
        return True

    # ─────────── LVS ───────────
    @command("run_lvs", cat="LVS", aliases=["verify_lvs"], usage="run_lvs", help="Extract with Magic, compare with Netgen")
    def c_lvs(self, args):
        self._need()
        magic, netgen, rc, setup = env.find_tool("magic"), env.find_tool("netgen"), self._magicrc(), self._netgen_setup()
        if not (magic and netgen and rc and setup):
            error("RFP-LVS-001", "LVS needs magic + netgen + PDK magicrc and netgen setup")
            return False
        if not self.st.netlist:
            raise CmdError("No schematic netlist — read_netlist <top>.pnr.v (innovus writes it after routeDesign)")
        lay = self.vdir / f"{self.st.top}.layout.spice"
        ext = self.vdir / "rtlflow_extract.tcl"
        ext.write_text(f"""
gds read {{{self.st.gds}}}
load {self.st.top}
select top cell
extract do local
extract all
ext2spice lvs
ext2spice -o {{{lay}}}
quit -noprompt
""")
        note("Magic extraction …")
        r = runner.run([magic, "-dnull", "-noconsole", "-rcfile", rc, str(ext)], cwd=self.vdir,
                       log=self.vdir / "lvs_extract.log", show=runner.important, timeout=3600)
        if not lay.exists():
            error("RFP-LVS-002", "Extraction failed (see .rtlflow/runs/signoff/lvs_extract.log)")
            return False
        rep = self.vdir / f"{self.st.top}.lvs.rpt"
        cells = self._cell_spice()
        ng = self.vdir / "rtlflow_lvs.tcl"
        sch_read = (f"set sch [readnet spice {{{cells}}}]\nreadnet verilog {{{self.st.netlist}}} $sch\n" if cells and self.st.netlist.endswith(".v")
                    else f"set sch [readnet {'verilog' if self.st.netlist.endswith('.v') else 'spice'} {{{self.st.netlist}}}]\n")
        ng.write_text(f"set lay [readnet spice {{{lay}}}]\n{sch_read}"
                      f"lvs \"$lay {self.st.top}\" \"$sch {self.st.top}\" {{{setup}}} {{{rep}}}\nquit\n")
        note("Netgen LVS …")
        r = runner.run([netgen, "-batch", "source", str(ng)], cwd=self.vdir, log=self.vdir / "lvs_netgen.log",
                       show=lambda l: "match" in l.lower() or "error" in l.lower())
        txt = rep.read_text(errors="replace") if rep.exists() else r.text
        self.lvs_text = txt
        if re.search(r"Circuits match uniquely", txt) or re.search(r"Final result:\s*Circuits match", txt):
            self.st.lvs = "match"
            ok("LVS: Circuits match uniquely.")
        elif "match" in txt.lower() and "do not match" not in txt.lower() and "mismatch" not in txt.lower():
            self.st.lvs = "match (with warnings)"
            warn("RFP-LVS-100", "LVS: circuits match with warnings — see report")
        else:
            self.st.lvs = "mismatch"
            error("RFP-LVS-101", f"LVS: netlists do NOT match — {self.project.rel(rep)}")
        self.gui_sync()
        return self.st.lvs.startswith("match")

    @command("view_results", cat="DRC", aliases=["open_klayout"], help="Open the layout + DRC markers in KLayout")
    def c_view(self, args):
        self._need()
        exe = env.find_tool("klayout")
        if not exe:
            raise CmdError("KLayout not found")
        import subprocess
        cmd = [exe, self.st.gds]
        rep = self.vdir / f"{self.st.top}.drc.lyrdb"
        if rep.exists():
            cmd += ["-m", str(rep)]
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        ok("KLayout opened")
        return True

    @command("status", cat="Setup", help="Verification state")
    def c_status(self, args):
        s = self.st
        out(table([("Top", s.top or "-"), ("GDS", Path(s.gds).name if s.gds else "-"),
                   ("LVS netlist", Path(s.netlist).name if s.netlist else "-"),
                   ("DRC", f"{s.drc_total} violation(s) · {s.drc_engine}" if s.drc_total is not None else "not run"),
                   ("LVS", s.lvs or "not run")], headers=("pegasus status", "")))
        return True

    # ─────────── GUI ───────────
    def gui_sync(self) -> None:
        i = self.project.get("innovus") or {}
        top = self.st.top or i.get("top", "")
        d = self.project.pnr / f"{top}.final.def"
        if not d.exists():
            defs = [p for p in list(self.project.pnr.glob("*.def")) + list((self.project.pnr / "dbs").glob("*.def")) if p.exists()]
            d = max(defs, key=lambda p: p.stat().st_mtime) if defs else self.project.pnr / "live.def"
        self.gui_send(t="design", top=top, gds=self.st.gds, def_file=str(d) if d.exists() else "",
                      lefs=(i.get("tech_lefs") or []) + (i.get("cell_lefs") or []))
        self.gui_send(t="drc", markers=self.markers, total=self.st.drc_total, engine=self.st.drc_engine)
        self.gui_send(t="lvs", status=self.st.lvs, text=self.lvs_text)


def _parse_lyrdb(path: Path) -> list[dict]:
    out_m: list[dict] = []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        # tolerate slightly malformed databases: regex over <item> blocks
        txt = path.read_text(errors="replace")
        for m in re.finditer(r"<item>.*?<category>'?([^<']*)'?</category>(.*?)</item>", txt, re.S):
            nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", " ".join(re.findall(r"<value>(.*?)</value>", m.group(2))))]
            if len(nums) >= 2:
                out_m.append({"rule": m.group(1), "box": [min(nums[0::2]), min(nums[1::2]), max(nums[0::2]), max(nums[1::2])]})
        return out_m
    cat_desc = {}
    for c in root.iter("category"):
        n = c.findtext("name")
        if n:
            cat_desc[n.strip("'")] = (c.findtext("description") or n).strip("'")
    for it in root.iter("item"):
        cat = (it.findtext("category") or "").strip("'")
        nums: list[float] = []
        for v in it.iter("value"):
            nums += [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", v.text or "")]
        if len(nums) < 2:
            continue
        xs, ys = nums[0::2], nums[1::2]
        out_m.append({"rule": cat_desc.get(cat, cat) if len(cat_desc.get(cat, cat)) < 80 else cat,
                      "box": [min(xs), min(ys), max(xs), max(ys)]})
    return out_m


def run_pegasus(gui: bool = False, files: list[str] | None = None) -> int:
    sh = PegasusShell(gui=gui, files=files)
    if not sh.start():
        console.close_logs()
        return 1
    return sh.cmdloop()
