"""innovus — Innovus-style implementation shell on ONE persistent OpenROAD session.

    rtlflow> innovus            → innovus N> prompt  +  layout GUI (same session)
    rtlflow> innovus -nowin     → terminal only (like `innovus -nowin`)
    innovus 3> win              → (re)open the GUI any time;  win off / gui_hide to close

Terminal and GUI both call Shell.run_line(); exec_lock serialises them onto the
single OpenROAD process. After every command that changes the database the
shell writes <project>/.rtlflow/runs/pnr/live.def and tells the GUI to reload —
the picture is always the real OpenROAD DB, never synthetic geometry.
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .. import VERSION, CODENAME, env
from ..backends.openroad import BackendDead, OpenROADSession, Result
from ..core.msg import C, colorize_backend_line, console, error, info, note, ok, out, rule, table, warn
from ..core.project import Project
from ..core.shell import CmdError, Shell, command, expand_files, parse_opts
from ..gui.host import GuiHost, display_available
from ..tech import GENERIC, TechProfile, match_profile

PRELUDE = Path(__file__).resolve().parent.parent / "backends" / "or_prelude.tcl"

STAGES = ["empty", "loaded", "init", "floorplan", "powerplan", "place", "cts", "route", "filled", "signoff"]
STAGE_LABEL = {
    "empty": "no design", "loaded": "libraries/netlist read", "init": "design initialised",
    "floorplan": "floorplanned", "powerplan": "power planned", "place": "placed",
    "cts": "clock tree built", "route": "routed", "filled": "filled", "signoff": "signoff",
}

# raw OpenROAD commands that change geometry → refresh the GUI afterwards
_MODIFYING = {
    "initialize_floorplan", "place_pins", "tapcell", "pdngen", "global_placement", "detailed_placement",
    "clock_tree_synthesis", "global_route", "detailed_route", "filler_placement", "repair_design",
    "repair_timing", "repair_clock_nets", "remove_buffers", "read_def", "read_db", "place_inst",
    "improve_placement", "optimize_mirroring", "insert_tiecells", "place_endcaps", "cut_rows",
    "remove_fillers", "rtl_macro_placer", "macro_placement", "make_tracks", "link_design",
}


@dataclass
class PnRState:
    libs: list[str] = field(default_factory=list)
    tech_lefs: list[str] = field(default_factory=list)
    cell_lefs: list[str] = field(default_factory=list)
    netlists: list[str] = field(default_factory=list)
    sdcs: list[str] = field(default_factory=list)
    top: str = ""
    stage: str = "empty"
    initialized: bool = False
    tech_loaded: bool = False
    clocks: int = 0
    util: float = 0.0
    site: str = ""
    profile: str = "generic"
    metrics: dict = field(default_factory=dict)
    timing: dict = field(default_factory=dict)
    drc: int | None = None
    parasitics: str = "none"      # none / placement / global_route / rcx
    pdn: list = field(default_factory=list)

    @property
    def lefs(self) -> list[str]:
        return self.tech_lefs + self.cell_lefs

    def at_least(self, stage: str) -> bool:
        return STAGES.index(self.stage) >= STAGES.index(stage)


class InnovusShell(Shell):
    tool = "innovus"
    msg_prefix = "RFI"
    history_name = ".rtlflow_history_innovus"

    def __init__(self, gui: bool = True, init_files: list[str] | None = None,
                 batch: bool = False) -> None:
        super().__init__()
        self.want_gui = gui and not batch
        self.batch = batch
        self.init_files = init_files or []
        self.project = Project()
        self.st = PnRState()
        self.profile: TechProfile = GENERIC
        self.session: OpenROADSession | None = None
        self.gui: GuiHost | None = None
        self._gui_q: "queue.Queue[dict]" = queue.Queue()
        self._gui_worker: threading.Thread | None = None
        self._layout_seq = 0
        self._busy_src = ""
        self.cwd_changed_hooks.append(self._on_cd)
        self._tl = threading.local()

    # ════════════════════════════════════ lifecycle ════════════════════════════════════
    def prompt(self) -> str:
        return f"{C.BOLD}{C.BLU}innovus {self.counter}>{C.RESET} "

    def help_order(self) -> list[str]:
        return ["Design Import", "Floorplan", "Power", "Placement", "Clock", "Route", "Timing",
                "Optimization", "Verify", "Output", "GUI", "Session", "Shell"]

    def banner(self) -> None:
        orp = env.tool("openroad")
        out("")
        out(f"{C.BOLD}{C.WHT}           RTLFlow Innovus(R) Implementation System{C.RESET}")
        out(f"{C.GRY}      Version {VERSION} ({CODENAME}) · physical backend: OpenROAD {self.session.version if self.session else ''}{C.RESET}")
        out(rule())
        rows = [
            ("Backend", f"openroad  {env.shorten(orp.path, 52)}" + (f"  (pid {self.session.proc.pid})" if self.session and self.session.proc else "")),
            ("Project", str(self.project.root).replace(str(Path.home()), "~")),
            ("Log file", self.project.rel(console.log_path) if console.log_path else "-"),
            ("Cmd file", self.project.rel(console.cmd_path) if console.cmd_path else "-"),
        ]
        for k, v in rows:
            out(f"  {C.GRY}{k:<10}{C.RESET}{v}")

    def start(self) -> bool:
        console.open_logs(self.project.logs, "innovus")
        exe = env.find_tool("openroad")
        if not exe:
            error("RFI-ENV-001", "OpenROAD not found on PATH, in /nix/store or common locations.")
            out("  Fix: rtlflow> tools -rescan     or   export RTLFLOW_OPENROAD=/path/to/openroad")
            return False
        env.setup_environment()
        self.session = OpenROADSession(exe, self.project.pnr, on_line=self._backend_line)
        try:
            self.session.start()
        except BackendDead as e:
            error("RFI-ENV-002", f"OpenROAD failed to start: {e}")
            return False
        self._load_prelude()
        self.banner()
        if self.want_gui:
            self.c_win([])
        else:
            out(f"  {C.GRY}GUI       {C.RESET}off (-nowin)  ·  type 'win' to open it")
        out(rule())
        self._hint_handoff()
        return True

    def _load_prelude(self) -> None:
        r = self.session.run(PRELUDE.read_text(), stream=False)
        if not r.ok:
            warn("RFI-ENV-010", f"Helper prelude failed to load: {r.value}")

    def _hint_handoff(self) -> None:
        hs = sorted(self.project.handoff.glob("*.invs_setup.tcl")) if self.project.dir.exists() else []
        if hs:
            h = hs[-1]
            note(f"  Genus handoff found → {C.BOLD}source {self.project.rel(h)}{C.RESET}{C.CYA}   (sets init_* vars, then init_design)")
        mapped = sorted(self.project.synth.glob("*_mapped.v")) if self.project.dir.exists() else []
        if not hs and mapped:
            note(f"  Synthesised netlist found → {self.project.rel(mapped[-1])}")
        out(f"  {C.GRY}Type 'help' for commands, 'man <cmd>' for usage. 'status' shows the DB state.{C.RESET}")
        out("")

    def on_start(self) -> None:
        for f in self.init_files:
            self.c_source([f])

    def on_exit(self) -> None:
        self._persist()
        if self.gui:
            self.gui.close()
        if self.session:
            self.session.stop()
        out(f"{C.GRY}innovus: session closed · log {self.project.rel(console.log_path) if console.log_path else ''}{C.RESET}")
        console.close_logs()

    def _on_cd(self) -> None:
        warn("RFI-SES-005", "Working directory changed; the project (.rtlflow) stays at "
             f"{self.project.root}")

    # ═════════════════════════════════ backend plumbing ═════════════════════════════════
    def _backend_line(self, line: str) -> None:
        console.write(colorize_backend_line(line))

    def tcl(self, script: str, *, what: str = "", quiet: bool = False, msg_id: str = "RFI-OR-001") -> Result:
        if not self.session or not self.session.alive:
            raise CmdError("OpenROAD session is not running (use 'restart_backend')", "RFI-OR-000")
        try:
            r = self.session.run(script, stream=not quiet)
        except BackendDead as e:
            error("RFI-OR-DEAD", f"{e}. The design database is gone — 'restart_backend' starts a fresh session "
                  f"(or 'restoreDesign <checkpoint>').")
            self.st.initialized = False
            self.st.stage = "empty"
            self._push_state()
            raise CmdError("")
        if not r.ok and not quiet:
            msg = r.value.strip().splitlines()[0] if r.value.strip() else "unknown error"
            error(msg_id, f"{what or 'OpenROAD'} failed: {msg}")
        return r

    def q(self, script: str, default: str = "") -> str:
        return self.session.query(script, default) if self.session else default

    def qdict(self, script: str) -> dict:
        toks = _tcl_list(self.q(script, ""))
        return {toks[i]: toks[i + 1] for i in range(0, len(toks) - 1, 2)}

    def default(self, name: str, args: list[str], line: str, source: str) -> bool:
        """Anything not an innovus command goes straight to the OpenROAD Tcl interpreter."""
        r = self.tcl(line, what=name)
        if r.ok and r.value.strip():
            out(r.value)
        if name in _MODIFYING and r.ok:
            self._refresh_metrics()
            self._update_gui(stage=None)
        return r.ok

    def run_line(self, line: str, source: str = "terminal") -> bool:
        """Wrap every command with busy/idle notifications so the GUI mirrors the session."""
        tl = self._tl
        top = not getattr(tl, "depth", 0)
        tl.depth = getattr(tl, "depth", 0) + 1
        if top and line.strip() and not line.strip().startswith("#"):
            self.gui_send(t="busy", cmd=line.strip()[:120], source=source)
        okk = False
        try:
            okk = super().run_line(line, source)
            return okk
        finally:
            tl.depth -= 1
            if top and line.strip() and not line.strip().startswith("#"):
                self.gui_send(t="idle", cmd=line.strip()[:120], ok=okk)

    # ══════════════════════════════════ GUI plumbing ═══════════════════════════════════
    def _gui_message(self, msg: dict) -> None:
        """Called on the IPC reader thread."""
        t = msg.get("t")
        if t == "ready":
            self._gui_q.put({"t": "__sync"})
        elif t in ("cmd", "request_state", "timing_paths"):
            self._gui_q.put(msg)
        elif t == "closing":
            pass

    def _gui_worker_loop(self) -> None:
        while True:
            msg = self._gui_q.get()
            if msg is None:
                return
            t = msg.get("t")
            try:
                if t == "__sync" or t == "request_state":
                    with self.exec_lock:
                        self._push_state()
                        self._update_gui(stage=None, force=True)
                elif t == "cmd":
                    line = msg.get("line", "").strip()
                    if not line:
                        continue
                    console.write(f"{C.MAG}[GUI]{C.RESET} {C.BOLD}{line}{C.RESET}")
                    if msg.get("report"):
                        console.begin_capture()
                    okk = False
                    try:
                        okk = self.run_line(line, source="gui")
                    except Exception:
                        okk = False
                    finally:
                        text = console.end_capture() if msg.get("report") else ""
                    if msg.get("report"):
                        # echo captured text to terminal too — terminal is the console of record
                        for l in text.splitlines():
                            console.write(colorize_backend_line(l))
                        self.gui_send(t="report", title=msg.get("title") or line, text=text, ok=okk)
                    self.gui_send(t="result", id=msg.get("id"), ok=okk, line=line)
                elif t == "timing_paths":
                    with self.exec_lock:
                        self._send_timing_paths(int(msg.get("n", 20)))
            except Exception as e:  # never kill the worker
                console.log_only(f"gui worker error: {e}")

    def gui_send(self, **msg) -> None:
        if self.gui and self.gui.is_open:
            self.gui.send(**msg)

    def _gui_closed(self, code: int) -> None:
        console.write(f"{C.GRY}innovus: GUI window closed — type 'win' to reopen (session keeps running){C.RESET}")
        console.listeners[:] = [l for l in console.listeners if getattr(l, "_rf_gui", False) is False]

    def _transcript_listener(self):
        def _l(text: str) -> None:
            if self.gui and self.gui.is_open:
                self.gui.send(t="log", text=text)
        _l._rf_gui = True  # type: ignore[attr-defined]
        return _l

    def _push_state(self) -> None:
        s = asdict(self.st)
        s["lefs"] = self.st.lefs
        s["stage_label"] = STAGE_LABEL.get(self.st.stage, self.st.stage)
        s["backend"] = f"OpenROAD {self.session.version}" if self.session else ""
        s["project"] = str(self.project.root)
        self.gui_send(t="state", state=s)

    def _update_gui(self, stage: str | None, force: bool = False) -> None:
        """Write live.def from the DB and tell the GUI to reload it."""
        if not (self.gui and self.gui.is_open):
            return
        if not self.st.initialized:
            self._push_state()
            return
        live = self.project.pnr / "live.def"
        r = self.tcl(f"write_def {{{live}}}", quiet=True)
        if not r.ok:
            console.log_only(f"live.def write failed: {r.value}")
            return
        self._layout_seq += 1
        self.gui_send(t="layout", **{"def": str(live), "lefs": self.st.lefs, "stage": self.st.stage,
                                     "seq": self._layout_seq})
        self._push_state()

    # ════════════════════════════════ state helpers ════════════════════════════════════
    def _require(self, stage: str, cmd: str) -> None:
        need = {
            "init": ("init_design", "RFI-IMP-101", "No design is initialised"),
            "floorplan": ("floorPlan", "RFI-FP-101", "Design has no floorplan"),
            "place": ("placeDesign", "RFI-PL-101", "Design is not placed"),
            "cts": ("ccopt_design", "RFI-CTS-101", "Clock tree not built"),
            "route": ("routeDesign", "RFI-RT-101", "Design is not routed"),
        }
        if not self.st.initialized and stage != "empty":
            raise CmdError("No design is initialised. Run read_libs → read_physical -lef → "
                           "read_netlist → set_top → init_design first.", "RFI-IMP-101")
        if stage in need and not self.st.at_least(stage):
            prev, mid, text = need[stage]
            raise CmdError(f"{text}. Run '{prev}' first.", mid)

    def _set_stage(self, stage: str) -> None:
        self.st.stage = stage
        self.project.stage("innovus", stage, top=self.st.top)
        self._persist()

    def _persist(self) -> None:
        try:
            self.project.set(innovus=asdict(self.st))
        except Exception:
            pass

    def _refresh_metrics(self) -> None:
        d = self.qdict("rf::design_info")
        if d.get("loaded") == "1":
            dbu = float(d.get("dbu", 1000) or 1000)
            m = {"insts": int(d.get("insts", 0)), "placed": int(d.get("placed", 0)),
                 "nets": int(d.get("nets", 0)), "terms": int(d.get("terms", 0)),
                 "rows": int(d.get("rows", 0)), "seq": int(d.get("seq", 0)), "dbu": dbu}
            try:
                m["die"] = [float(d[k]) / dbu for k in ("die_x0", "die_y0", "die_x1", "die_y1")]
            except (KeyError, ValueError):
                pass
            try:
                core = [float(d[k]) / dbu for k in ("core_x0", "core_y0", "core_x1", "core_y1")]
                m["core"] = core
                core_area = (core[2] - core[0]) * (core[3] - core[1])
                std = float(d.get("std_area_dbu2", 0)) / (dbu * dbu)
                m["std_area"] = round(std, 3)
                if core_area > 0:
                    m["util"] = round(std / core_area, 4)
            except (KeyError, ValueError):
                pass
            self.st.metrics = m

    def _refresh_timing(self) -> dict:
        t = self.qdict("rf::timing_summary")
        self.st.clocks = int(t.get("clocks", "0") or 0) if t.get("clocks", "0").isdigit() else 0
        if self.st.clocks and t.get("wns") in (None, "NA"):
            # last resort: parse report_worst_slack text
            r = self.tcl("report_worst_slack -max -digits 4", quiet=True)
            m = re.search(r"worst slack\s+(-?[\d.]+)", r.text)
            if m:
                t["wns"] = m.group(1)
        self.st.timing = t
        return t

    def _derive_profile(self) -> None:
        text = " ".join(self.st.lefs + self.st.libs)
        self.profile = match_profile(text)
        if self.profile is GENERIC:
            masters = _tcl_list(self.q("rf::masters {.}"))
            if masters:
                self.profile = match_profile(masters[0])
        self.st.profile = self.profile.name

    def _site(self) -> str:
        if self.profile.site:
            return self.profile.site
        s = _tcl_list(self.q("rf::sites"))
        cands = [(s[i], s[i + 1], int(s[i + 3])) for i in range(0, len(s) - 3, 4)]
        core = [c for c in cands if "CORE" in c[1].upper()] or cands
        if not core:
            raise CmdError("No placement SITE found in the loaded LEFs", "RFI-FP-110")
        core.sort(key=lambda c: c[2])           # smallest height = standard row
        return core[0][0]

    def _routing_layers(self) -> list[tuple[str, str]]:
        l = _tcl_list(self.q("rf::layers"))
        return [(l[i], l[i + 1]) for i in range(0, len(l) - 2, 3)]

    def _io_layers(self) -> tuple[str, str]:
        if self.profile.io_hlayer and self.profile.io_vlayer:
            return self.profile.io_hlayer, self.profile.io_vlayer
        layers = self._routing_layers()
        h = [n for n, d in layers if d.upper().startswith("H")]
        v = [n for n, d in layers if d.upper().startswith("V")]
        return (h[1] if len(h) > 1 else (h[0] if h else layers[0][0]),
                v[0] if v else layers[-1][0])

    def _route_range(self) -> tuple[str, str]:
        if self.profile.min_route and self.profile.max_route:
            return self.profile.min_route, self.profile.max_route
        layers = [n for n, _ in self._routing_layers()]
        if not layers:
            raise CmdError("Technology has no routing layers", "RFI-RT-110")
        lo = layers[1] if layers[0].lower().startswith("li") and len(layers) > 1 else layers[0]
        return lo, layers[-1]

    # ═══════════════════════════════════ Design Import ═════════════════════════════════
    @command("read_libs", cat="Design Import", usage="read_libs [-lib] <liberty files...>",
             help="Read timing libraries (.lib) into OpenSTA")
    def c_read_libs(self, args):
        opts, pos = parse_opts(args, flags={"-lib"}, valued={"-max_libs": "+", "-min_libs": "+", "-typ_libs": "+"})
        files = expand_files(pos + opts.get("-max_libs", []) + opts.get("-typ_libs", []))
        if not files:
            p = env.pdk()
            lib = p.liberty()
            if not lib:
                raise CmdError("usage: read_libs <file.lib ...>  (no PDK default found)")
            note(f"No library given — using PDK default {lib}")
            files = [str(lib)]
        for f in files:
            if not Path(f).exists():
                raise CmdError(f"File not found: {f}", "RFI-IMP-201")
        for f in files:
            if f in self.st.libs:
                info(f"  {Path(f).name} already loaded")
                continue
            t0 = time.time()
            r = self.tcl(f"read_liberty {{{f}}}", what="read_liberty", msg_id="RFI-IMP-202")
            if not r.ok:
                return False
            self.st.libs.append(f)
            ok(f"  Liberty  {Path(f).name}  ({time.time() - t0:.1f}s)")
        self._stage_loaded()
        return True

    @command("read_physical", cat="Design Import", usage="read_physical -lef {tech.lef cells.lef ...}",
             help="Read technology + cell LEF (tech LEF auto-found for open_pdks)")
    def c_read_physical(self, args):
        opts, pos = parse_opts(args, valued={"-lef": "+", "-tlef": "+"})
        files = expand_files(opts.get("-tlef", []) + opts.get("-lef", []) + pos)
        if not files:
            raise CmdError("usage: read_physical -lef {files}")
        for f in files:
            if not Path(f).exists():
                raise CmdError(f"File not found: {f}", "RFI-IMP-211")
        tech = [f for f in files if _lef_has_tech(f)]
        cells = [f for f in files if f not in tech]
        if not tech and not self.st.tech_loaded:
            auto = _find_tech_lef(cells)
            if auto:
                note(f"  Tech LEF not given — using matching technology LEF:\n    {auto}")
                tech = [auto]
            else:
                warn("RFI-IMP-212", "No technology LEF (LAYER/SITE definitions) supplied; floorPlan will "
                     "fail with ORD-0050. Pass it first: read_physical -lef {tech.tlef cells.lef}")
        for f in tech + cells:
            if f in self.st.lefs:
                info(f"  {Path(f).name} already loaded")
                continue
            is_tech = f in tech
            cmd = f"read_lef {{{f}}}" if not is_tech else f"read_lef -tech {{{f}}}"
            r = self.tcl(cmd, what="read_lef", quiet=True)
            if not r.ok and is_tech:          # older OpenROAD without -tech
                r = self.tcl(f"read_lef {{{f}}}", what="read_lef", quiet=True)
            for l in r.lines:
                if "[ERROR" in l or "[WARNING" in l:
                    console.write(colorize_backend_line(l))
            if not r.ok:
                error("RFI-IMP-213", f"read_lef {Path(f).name} failed: {r.value.strip()}")
                return False
            (self.st.tech_lefs if is_tech else self.st.cell_lefs).append(f)
            ok(f"  {'Tech LEF' if is_tech else 'Cell LEF'} {Path(f).name}")
        self.st.tech_loaded = self.q("rf::tech_loaded", "0") == "1"
        if not self.st.tech_loaded:
            warn("RFI-IMP-214", "OpenROAD reports no technology layers loaded yet")
        else:
            n = self.q("rf::lib_count", "0")
            info(f"  Technology: {len(self._routing_layers())} routing layers · {n} macros/cells")
        self._stage_loaded()
        return True

    @command("read_netlist", cat="Design Import", usage="read_netlist [-top name] <netlist.v ...>",
             help="Read gate-level Verilog netlist")
    def c_read_netlist(self, args):
        opts, pos = parse_opts(args, valued={"-top": 1})
        files = expand_files(pos)
        if not files:
            mapped = sorted(self.project.synth.glob("*_mapped.v"))
            if not mapped:
                raise CmdError("usage: read_netlist <netlist.v>")
            files = [str(mapped[-1])]
            note(f"Using synthesised netlist {self.project.rel(files[0])}")
        for f in files:
            if not Path(f).exists():
                raise CmdError(f"File not found: {f}", "RFI-IMP-221")
            r = self.tcl(f"read_verilog {{{f}}}", what="read_verilog", msg_id="RFI-IMP-222")
            if not r.ok:
                return False
            self.st.netlists.append(f)
            ok(f"  Netlist  {Path(f).name}")
            if not self.st.top:
                m = re.search(r"^([A-Za-z_]\w*)_mapped\.v$", Path(f).name)
                guess = m.group(1) if m else _first_module(f)
                if guess:
                    self.st.top = guess
                    info(f"  Top cell guessed as '{guess}' (override with set_top)")
        if opts.get("-top"):
            self.st.top = opts["-top"]
        self._stage_loaded()
        return True

    @command("set_top", cat="Design Import", usage="set_top <module>", help="Set the top cell for init_design")
    def c_set_top(self, args):
        if not args:
            out(self.st.top or "(unset)")
            return True
        self.st.top = args[0]
        self.vars["init_top_cell"] = args[0]
        ok(f"  Top cell = {args[0]}")
        return True

    @command("read_sdc", cat="Design Import", usage="read_sdc <file.sdc ...>", help="Read timing constraints")
    def c_read_sdc(self, args):
        files = expand_files(args)
        if not files:
            raise CmdError("usage: read_sdc <file.sdc>")
        for f in files:
            if not Path(f).exists():
                raise CmdError(f"File not found: {f}", "RFI-IMP-231")
            if f not in self.st.sdcs:
                self.st.sdcs.append(f)
        if self.st.initialized:
            return self._apply_sdc(files)
        info("  SDC will be applied at init_design")
        return True

    def _apply_sdc(self, files: list[str]) -> bool:
        for f in files:
            r = self.tcl(f"read_sdc {{{f}}}", what="read_sdc", msg_id="RFI-IMP-232")
            if not r.ok:
                return False
            ok(f"  SDC      {Path(f).name}")
        self._refresh_timing()
        info(f"  {self.st.clocks} clock(s) defined")
        return True

    def _stage_loaded(self) -> None:
        if self.st.stage == "empty":
            self.st.stage = "loaded"
        self._push_state()

    def on_set(self, name: str, value: str) -> None:
        # keep OpenROAD's interpreter in sync for scripts using $vars
        if self.session and self.session.alive:
            self.q(f"set {{{name}}} {{{value}}}")

    @command("init_design", cat="Design Import", usage="init_design [-setup {views}]",
             help="Link the design (uses read_* data or Innovus init_* variables)")
    def c_init_design(self, args):
        """Classic Innovus variables are honoured when the read_* commands were not used:
             set init_lef_file   {tech.tlef cells.lef}
             set init_verilog    netlist.v
             set init_top_cell   top
             set init_lib_file   {typ.lib}          (RTLFlow extension)
             set init_mmmc_file  view.tcl           (create_library_set / create_constraint_mode parsed)
             set init_sdc_file   {top.sdc}          (RTLFlow extension)
        """
        parse_opts(args, valued={"-setup": "+", "-hold": "+"})
        if self.st.initialized:
            warn("RFI-IMP-301", "Design already initialised — use freeDesign to start over")
            return True
        v = self.vars
        mm_libs, mm_sdcs = ([], [])
        if v.get("init_mmmc_file"):
            mm_libs, mm_sdcs = _parse_mmmc(Path(os.path.expanduser(v["init_mmmc_file"])))
        if not self.st.libs and (v.get("init_lib_file") or mm_libs):
            if not self.c_read_libs(expand_files([v.get("init_lib_file", "")]) + mm_libs):
                return False
        if not self.st.lefs and v.get("init_lef_file"):
            if not self.c_read_physical(["-lef", v["init_lef_file"]]):
                return False
        if not self.st.netlists and v.get("init_verilog"):
            if not self.c_read_netlist(expand_files([v["init_verilog"]])):
                return False
        for s in expand_files([v.get("init_sdc_file", "")]) + mm_sdcs:
            if s not in self.st.sdcs:
                self.st.sdcs.append(s)
        if v.get("init_top_cell"):
            self.st.top = v["init_top_cell"]

        missing = []
        if not self.st.libs:
            missing.append("timing library   (read_libs)")
        if not self.st.tech_loaded:
            missing.append("technology LEF   (read_physical -lef)")
        if not self.st.netlists:
            missing.append("netlist          (read_netlist)")
        if not self.st.top:
            missing.append("top cell         (set_top)")
        if missing:
            error("RFI-IMP-302", "init_design cannot run — missing:")
            for m in missing:
                out(f"      · {m}")
            return False

        note(f"Linking design '{self.st.top}' …")
        r = self.tcl(f"link_design {{{self.st.top}}}", what="link_design", msg_id="RFI-IMP-303")
        if not r.ok:
            return False
        self.st.initialized = True
        self._derive_profile()
        # parasitic models from profile (per-layer RC, wire RC for estimation)
        if self.profile.layer_rc:
            rc = "\n".join(f"catch {{set_layer_rc -layer {l} -capacitance {c} -resistance {res}}}"
                           for l, c, res in self.profile.layer_rc)
            rc += "\n" + "\n".join(f"catch {{set_layer_rc -via {vname} -resistance {res}}}"
                                   for vname, res in self.profile.via_rc)
            self.tcl(rc, quiet=True)
        if self.profile.wire_rc_signal:
            self.tcl(f"catch {{set_wire_rc -signal -layer {self.profile.wire_rc_signal}}}\n"
                     f"catch {{set_wire_rc -clock -layer {self.profile.wire_rc_clock or self.profile.wire_rc_signal}}}",
                     quiet=True)
        if self.st.sdcs:
            self._apply_sdc(self.st.sdcs)
        else:
            self._refresh_timing()
        self._refresh_metrics()
        self._set_stage("init")
        m = self.st.metrics
        out(table([
            ("Top cell", self.st.top),
            ("Instances", f"{m.get('insts', '?')}  ({m.get('seq', '?')} sequential)"),
            ("Nets / IO terms", f"{m.get('nets', '?')} / {m.get('terms', '?')}"),
            ("Std-cell area", f"{m.get('std_area', '?')} µm²"),
            ("Tech profile", self.profile.name),
            ("Clocks", str(self.st.clocks) + ("" if self.st.clocks else "  (no SDC yet — create_clock / read_sdc)")),
        ], headers=("init_design", "")))
        ok(f"Design '{self.st.top}' initialised in the OpenROAD database.")
        self._update_gui("init")
        return True

    @command("freeDesign", cat="Design Import", help="Discard the database and restart OpenROAD")
    def c_free_design(self, args):
        return self.c_restart_backend(["-quiet"])

    @command("restart_backend", cat="Session", usage="restart_backend", help="Restart the OpenROAD process (clears DB)")
    def c_restart_backend(self, args):
        if self.session:
            self.session.stop()
        self.st = PnRState()
        self.session = OpenROADSession(env.find_tool("openroad"), self.project.pnr, on_line=self._backend_line)
        try:
            self.session.start()
        except BackendDead as e:
            raise CmdError(str(e), "RFI-OR-002")
        self._load_prelude()
        ok(f"OpenROAD session restarted (pid {self.session.proc.pid}) — database is empty")
        self._push_state()
        self.gui_send(t="clear")
        return True

    # ═════════════════════════════════════ Floorplan ═══════════════════════════════════
    @command("floorPlan", cat="Floorplan", aliases=["floorplan", "create_floorplan"],
             usage="floorPlan [-site name] [-r aspect util [l b r t]] [-s W H l b r t] [-d W H l b r t]",
             help="Create die/core, rows, tracks and place IO pins")
    def c_floorplan(self, args):
        """Innovus semantics:
             -r aspect util l b r t   aspect = H/W, util = std-cell utilisation, margins core→die (µm)
             -s W H l b r t           core width/height given
             -d W H l b r t           die width/height given
           Defaults: -r 1.0 0.5 5 5 5 5  (small blocks route far better below 60% util)
        """
        self._require("init", "floorPlan")
        opts, pos = parse_opts(args, flags={"-noPins", "-flip"}, valued={"-site": 1, "-r": "+", "-s": "+", "-d": "+",
                                                                  "-coreMarginsBy": 1})
        site = opts.get("-site") or self._site()
        self.st.site = site
        spec = ""
        if "-s" in opts or "-d" in opts:
            key = "-s" if "-s" in opts else "-d"
            vals = [float(x) for x in opts[key]]
            if len(vals) < 2:
                raise CmdError(f"{key} needs W H [l b r t]")
            W, H = vals[0], vals[1]
            l, b, r_, t = (vals[2:6] + [5.0] * 4)[:4]
            if key == "-s":
                die = (0, 0, W + l + r_, H + b + t); core = (l, b, l + W, b + H)
            else:
                die = (0, 0, W, H); core = (l, b, W - r_, H - t)
            spec = (f"initialize_floorplan -site {site} -die_area {{{' '.join(map(_f, die))}}} "
                    f"-core_area {{{' '.join(map(_f, core))}}}")
            self.st.util = 0.0
        else:
            vals = [float(x) for x in opts.get("-r", [])]
            aspect = vals[0] if len(vals) > 0 else 1.0
            util = vals[1] if len(vals) > 1 else 0.5
            l, b, r_, t = (vals[2:6] + [5.0] * 4)[:4]
            if util > 1:
                util /= 100.0
            if not 0.05 <= util <= 0.95:
                raise CmdError(f"Utilisation {util} out of range (0.05 … 0.95)", "RFI-FP-102")
            self.st.util = util
            spec = (f"initialize_floorplan -site {site} -utilization {util * 100:.1f} "
                    f"-aspect_ratio {aspect} -core_space {{{_f(b)} {_f(t)} {_f(l)} {_f(r_)}}}")
        note(f"Floorplanning (site {site}) …")
        if not self.tcl(spec, what="initialize_floorplan", msg_id="RFI-FP-103").ok:
            return False
        # tracks
        if self.profile.tracks:
            tr = "\n".join(f"make_tracks {l} -x_offset {xo} -x_pitch {xp} -y_offset {yo} -y_pitch {yp}"
                           for l, xo, xp, yo, yp in self.profile.tracks)
        else:
            tr = "make_tracks"
        if not self.tcl(tr, what="make_tracks", msg_id="RFI-FP-104").ok:
            return False
        if not opts.get("-noPins"):
            if not self._place_pins():
                return False
        self._refresh_metrics()
        self._set_stage("floorplan")
        m = self.st.metrics
        die, core = m.get("die"), m.get("core")
        rows = [("Site", site), ("Rows", m.get("rows", "?"))]
        if die:
            rows.append(("Die", f"{die[2] - die[0]:.2f} × {die[3] - die[1]:.2f} µm"))
        if core:
            rows.append(("Core", f"({core[0]:.2f}, {core[1]:.2f}) – ({core[2]:.2f}, {core[3]:.2f})"))
        if m.get("util"):
            rows.append(("Utilisation", f"{m['util'] * 100:.1f} %"))
        rows.append(("IO pins", m.get("terms", "?")))
        out(table(rows, headers=("floorPlan", "")))
        ok("Floorplan created.")
        if self.profile.tap_cell:
            info(f"  Next: addWellTap (tap cell {self.profile.tap_cell}) · globalNetConnect · sroute · placeDesign")
        self._update_gui("floorplan")
        return True

    def _place_pins(self, extra: str = "") -> bool:
        h, v = self._io_layers()
        r = self.tcl(f"place_pins -hor_layers {h} -ver_layers {v} {extra}", what="place_pins", msg_id="RFI-FP-105")
        return r.ok

    @command("assignIoPins", cat="Floorplan", usage="assignIoPins [-random]", help="(Re)place IO pins on the die edge")
    def c_assign_io(self, args):
        self._require("floorplan", "assignIoPins")
        opts, _ = parse_opts(args, flags={"-random"})
        okk = self._place_pins("-random" if opts.get("-random") else "")
        if okk:
            ok("IO pins placed.")
            self._update_gui(None)
        return okk

    @command("addWellTap", cat="Floorplan", usage="addWellTap [-cell name] [-cellInterval µm]",
             help="Insert well-tap cells in every row")
    def c_add_well_tap(self, args):
        self._require("floorplan", "addWellTap")
        opts, _ = parse_opts(args, valued={"-cell": 1, "-cellInterval": 1, "-prefix": 1})
        cell = opts.get("-cell") or self.profile.tap_cell
        if not cell:
            cands = _tcl_list(self.q("rf::masters {(?i)tap}"))
            cell = cands[0] if cands else None
        if not cell:
            raise CmdError("No tap cell known for this library — use -cell <name>", "RFI-FP-120")
        dist = opts.get("-cellInterval") or self.profile.tap_distance
        r = self.tcl(f"tapcell -distance {dist} -tapcell_master {cell}", what="tapcell", msg_id="RFI-FP-121")
        if r.ok:
            self._refresh_metrics()
            ok(f"Well taps inserted ({cell}, every {dist} µm).")
            self._update_gui(None)
        return r.ok

    @command("addEndCap", cat="Floorplan", hidden=True, help="Insert end-cap cells (not needed for sky130)")
    def c_add_endcap(self, args):
        info("sky130 standard cells need no end-caps; addWellTap covers tap requirements.")
        return True

    # ═══════════════════════════════════════ Power ═════════════════════════════════════
    @command("globalNetConnect", cat="Power", usage="globalNetConnect [net -type pgpin -pin pin -all]",
             help="Connect cell power/ground pins to global nets (VPWR/VGND auto)")
    def c_gnc(self, args):
        self._require("init", "globalNetConnect")
        pw, gd = self.profile.power_nets
        conns = []
        if args:
            opts, pos = parse_opts(args, flags={"-all", "-override"}, valued={"-type": 1, "-pin": 1, "-inst": 1})
            net = pos[0] if pos else None
            pin = opts.get("-pin", net)
            if not net:
                raise CmdError("usage: globalNetConnect NET -type pgpin -pin PIN -all")
            is_pwr = net.upper() in ("VDD", "VPWR", "VCC", "VPB") or net == pw
            conns.append((net, pin, is_pwr))
        else:
            conns = [(pw, pw, True), (gd, gd, False)]
            if self.profile.well_nets and len(self.profile.well_nets) == 2:
                conns += [(pw, self.profile.well_nets[0], True), (gd, self.profile.well_nets[1], False)]
        tcl = []
        for net, pin, pwr in conns:
            tcl.append(f"add_global_connection -net {{{net}}} -pin_pattern {{^{pin}$}} {'-power' if pwr else '-ground'}")
        tcl.append("global_connect")
        r = self.tcl("\n".join(tcl), what="global_connect", msg_id="RFI-PW-101")
        if r.ok:
            ok("Global net connections: " + ", ".join(f"{p}→{n}" for n, p, _ in conns))
        return r.ok

    @command("addStripe", cat="Power", usage="addStripe -layer L -width W -spacing S -set_to_set_distance P [-start_offset O]",
             help="Add a power stripe pair to the PDN spec (built by sroute)")
    def c_add_stripe(self, args):
        self._require("floorplan", "addStripe")
        opts, _ = parse_opts(args, valued={"-layer": 1, "-width": 1, "-spacing": 1, "-set_to_set_distance": 1,
                                           "-start_offset": 1, "-nets": "+"})
        if "-layer" not in opts:
            raise CmdError("addStripe needs -layer")
        self.st.pdn.append({"layer": opts["-layer"], "width": opts.get("-width", "1.6"),
                            "spacing": opts.get("-spacing", "1.7"),
                            "pitch": opts.get("-set_to_set_distance", "20"),
                            "offset": opts.get("-start_offset", "2")})
        ok(f"Stripe spec added on {opts['-layer']} — run 'sroute' to build the power grid")
        return True

    @command("sroute", cat="Power", usage="sroute [-nets {VPWR VGND}]",
             help="Build the power grid: follow-pin rails + any addStripe straps (pdngen)")
    def c_sroute(self, args):
        self._require("floorplan", "sroute")
        pw, gd = self.profile.power_nets
        lo, _ = self._route_range()
        tcl = [
            "catch {pdngen -reset}",
            f"set_voltage_domain -name CORE -power {pw} -ground {gd}",
            "define_pdn_grid -name stdcell_grid -starts_with POWER -voltage_domains CORE",
            f"add_pdn_stripe -grid stdcell_grid -layer {lo} -followpins"
            + (f" -width {self.profile.rail_width}" if self.profile.rail_width else ""),
        ]
        prev = lo
        for s in self.st.pdn:
            tcl.append(f"add_pdn_stripe -grid stdcell_grid -layer {s['layer']} -width {s['width']} "
                       f"-spacing {s['spacing']} -pitch {s['pitch']} -offset {s['offset']}")
            tcl.append(f"add_pdn_connect -grid stdcell_grid -layers {{{prev} {s['layer']}}}")
            prev = s["layer"]
        tcl.append("pdngen")
        r = self.tcl("\n".join(tcl), what="pdngen", msg_id="RFI-PW-110")
        if r.ok:
            if self.st.stage == "floorplan":
                self._set_stage("powerplan")
            ok("Power grid built.")
            self._update_gui(None)
        else:
            info("  Hint: run globalNetConnect first so VPWR/VGND exist")
        return r.ok

    # ═════════════════════════════════════ Placement ═══════════════════════════════════
    @command("placeDesign", cat="Placement", aliases=["place_design", "place_opt_design"],
             usage="placeDesign [-density 0.x] [-noOpt] [-timingDriven]",
             help="Global + detailed placement (RePlAce + OpenDP), then electrical repair")
    def c_place(self, args):
        self._require("floorplan", "placeDesign")
        opts, _ = parse_opts(args, flags={"-noOpt", "-timingDriven", "-routabilityDriven", "-noPrePlaceOpt",
                                          "-incremental"}, valued={"-density": 1})
        util = self.st.metrics.get("util") or self.st.util or 0.5
        dens = float(opts.get("-density") or min(0.95, max(util + 0.1, 0.6)))
        flags = []
        if opts.get("-timingDriven") and self.st.clocks:
            flags.append("-timing_driven")
        if opts.get("-routabilityDriven"):
            flags.append("-routability_driven")
        note(f"Global placement (target density {dens:.2f}) …")
        r = self.tcl(f"global_placement -density {dens:.3f} {' '.join(flags)}", what="global_placement",
                     msg_id="RFI-PL-102")
        if not r.ok:
            info("  Hint: try a lower utilisation floorplan (floorPlan -r 1.0 0.4) or -density 0.8")
            return False
        if not opts.get("-noOpt") and self.st.libs:
            note("Pre-CTS electrical repair (repair_design) …")
            self.tcl("estimate_parasitics -placement\nrepair_design", what="repair_design", quiet=False)
        note("Detailed placement …")
        r = self.tcl("detailed_placement\ncatch {optimize_mirroring}\ncheck_placement -verbose",
                     what="detailed_placement", msg_id="RFI-PL-103")
        if not r.ok:
            return False
        self.tcl("estimate_parasitics -placement", quiet=True)
        self.st.parasitics = "placement"
        self._refresh_metrics()
        self._set_stage("place")
        t = self._refresh_timing()
        m = self.st.metrics
        rows = [("Placed", f"{m.get('placed', '?')} / {m.get('insts', '?')} instances"),
                ("Utilisation", f"{m.get('util', 0) * 100:.1f} %" if m.get("util") else "-")]
        if t.get("wns") not in (None, "NA"):
            rows.append(("WNS (setup, est.)", f"{t['wns']} ns"))
        out(table(rows, headers=("placeDesign", "")))
        ok("Placement complete — cells shown in the GUI are OpenROAD's real locations.")
        self._update_gui("place")
        return True

    @command("refinePlace", cat="Placement", aliases=["refine_place"], help="Legalise placement (detailed_placement)")
    def c_refine(self, args):
        self._require("place", "refinePlace")
        r = self.tcl("detailed_placement\ncheck_placement", what="detailed_placement")
        if r.ok:
            ok("Placement legalised.")
            self._update_gui(None)
        return r.ok

    @command("checkPlace", cat="Placement", aliases=["check_placement"], help="Check placement legality")
    def c_checkplace(self, args):
        self._require("place", "checkPlace")
        r = self.tcl("check_placement -verbose", what="check_placement")
        if r.ok:
            ok("checkPlace: no violations.")
        return r.ok

    @command("addTieHiLo", cat="Placement", usage="addTieHiLo [-cell {hi lo}]", help="Insert tie-hi/lo cells")
    def c_tie(self, args):
        self._require("place", "addTieHiLo")
        if not self.profile.tie_cell:
            raise CmdError("No tie cell known for this library")
        c, hi, lo = self.profile.tie_cell
        r = self.tcl(f"insert_tiecells {c}/{hi} -prefix TIE_ONE_\ninsert_tiecells {c}/{lo} -prefix TIE_ZERO_\n"
                     "detailed_placement", what="insert_tiecells")
        if r.ok:
            self._update_gui(None)
        return r.ok

    # ═══════════════════════════════════════ Clock ═════════════════════════════════════
    @command("ccopt_design", cat="Clock", aliases=["clock_opt_design", "clockDesign"],
             usage="ccopt_design [-buffers {cells}] [-root_buf cell] [-cts]",
             help="Clock tree synthesis (TritonCTS) + legalisation")
    def c_ccopt(self, args):
        self._require("place", "ccopt_design")
        opts, _ = parse_opts(args, flags={"-cts", "-noOpt"}, valued={"-buffers": "+", "-root_buf": 1})
        self._refresh_timing()
        if not self.st.clocks:
            raise CmdError("No clocks defined. Use create_clock or read_sdc before CTS.", "RFI-CTS-102")
        bufs = opts.get("-buffers") or self._clock_buffers()
        if not bufs:
            raise CmdError("No clock buffers found in the loaded libraries — pass -buffers {cells}", "RFI-CTS-103")
        root = opts.get("-root_buf") or (self.profile.root_buf if self.profile.root_buf and
                                         self.q(f"rf::masters {{^{self.profile.root_buf}$}}") else bufs[-1])
        note(f"Clock tree synthesis · buffers: {' '.join(bufs)} · root: {root}")
        r = self.tcl(f"clock_tree_synthesis -buf_list {{{' '.join(bufs)}}} -root_buf {root} -sink_clustering_enable",
                     what="clock_tree_synthesis", msg_id="RFI-CTS-104")
        if not r.ok:
            return False
        self.tcl("set_propagated_clock [all_clocks]\ncatch {repair_clock_nets}\nestimate_parasitics -placement",
                 what="post-CTS", quiet=True)
        r = self.tcl("detailed_placement\ncheck_placement", what="post-CTS legalisation", msg_id="RFI-CTS-105")
        if not r.ok:
            return False
        if not opts.get("-noOpt") and self.st.libs:
            self.tcl("estimate_parasitics -placement\ncatch {repair_timing -setup}\ndetailed_placement",
                     what="post-CTS opt", quiet=True)
        self.st.parasitics = "placement"
        self._refresh_metrics()
        self._set_stage("cts")
        t = self._refresh_timing()
        self.tcl("catch {report_cts}", what="report_cts")
        rows = [("Clock buffers", " ".join(bufs)), ("Instances now", self.st.metrics.get("insts", "?"))]
        if t.get("wns") not in (None, "NA"):
            rows.append(("WNS / WHS", f"{t['wns']} / {t.get('whs', 'NA')} ns"))
        out(table(rows, headers=("ccopt_design", "")))
        ok("Clock tree built — clock nets highlighted in the GUI.")
        self._update_gui("cts")
        return True

    def _clock_buffers(self) -> list[str]:
        from ..tech import matches_any
        raw = _tcl_list(self.q(f"rf::masters {{{self.profile.clkbuf_regex}}}"))
        cells = [(raw[i], int(raw[i + 1])) for i in range(0, len(raw) - 1, 2)]
        cells = [c for c in cells if not matches_any(c[0], self.profile.dont_use)
                 and not re.search(r"(?i)inv|gate|dly|kapwr|lp", c[0])]
        cells.sort(key=lambda c: c[1])
        names = [c[0] for c in cells]
        # keep the small/medium sizes for the tree body (like OpenLane: 1,2,4,8)
        if len(names) > 4:
            names = names[:4]
        return names

    @command("report_ccopt_clock_trees", cat="Clock", aliases=["report_clock_tree"], help="Report clock tree (report_cts)")
    def c_report_cts(self, args):
        self._require("cts", "report_ccopt_clock_trees")
        return self.tcl("report_cts\nreport_clock_skew", what="report_cts").ok

    # ═══════════════════════════════════════ Route ═════════════════════════════════════
    @command("routeDesign", cat="Route", aliases=["route_design", "route_opt_design"],
             usage="routeDesign [-globalDetail|-global|-detail] [-drcIter N]",
             help="Global (FastRoute) + detailed (TritonRoute) routing")
    def c_route(self, args):
        self._require("place", "routeDesign")
        if not self.st.at_least("cts") and self.st.clocks:
            warn("RFI-RT-102", "Routing before ccopt_design — clock will be routed as an ideal net tree")
        opts, _ = parse_opts(args, flags={"-globalDetail", "-global", "-detail"}, valued={"-drcIter": 1})
        lo, hi = self._route_range()
        guide = self.project.pnr / f"{self.st.top}.route.guide"
        drc_rpt = self.project.pnr / "reports" / f"{self.st.top}.route.drc.rpt"
        drc_rpt.parent.mkdir(parents=True, exist_ok=True)
        if not opts.get("-detail"):
            note(f"Global routing ({lo}–{hi}) …")
            r = self.tcl(f"set_routing_layers -signal {lo}-{hi} -clock {lo}-{hi}\n"
                         f"catch {{set_global_routing_layer_adjustment {lo}-{hi} 0.2}}\n"
                         f"global_route -congestion_iterations 50\n"
                         f"catch {{write_guides {{{guide}}}}}",
                         what="global_route", msg_id="RFI-RT-103")
            if not r.ok:
                return False
            self.tcl("estimate_parasitics -global_routing", quiet=True)
            self.st.parasitics = "global_route"
            if opts.get("-global"):
                self._update_gui(None)
                ok("Global routing done (routing guides only; run routeDesign -detail next).")
                return True
        note("Detailed routing (TritonRoute) — progress streams below …")
        it = f"-droute_end_iter {opts['-drcIter']}" if opts.get("-drcIter") else ""
        r = self.tcl(f"detailed_route -output_drc {{{drc_rpt}}} -bottom_routing_layer {lo} "
                     f"-top_routing_layer {hi} -verbose 1 {it}", what="detailed_route", msg_id="RFI-RT-104")
        if not r.ok:
            return False
        self.st.drc = _count_drc(drc_rpt)
        # post-route netlist for tempus / pegasus LVS
        self.tcl(f"write_verilog {{{self.project.pnr / (self.st.top + '.pnr.v')}}}", quiet=True)
        self._refresh_metrics()
        self._set_stage("route")
        t = self._refresh_timing()
        rows = [("Routing layers", f"{lo} – {hi}"),
                ("DRC violations", f"{self.st.drc}" if self.st.drc is not None else "n/a"),
                ("Parasitics", "global-route estimate (run extractRC for RCX)")]
        if t.get("wns") not in (None, "NA"):
            rows.append(("WNS / WHS", f"{t['wns']} / {t.get('whs', 'NA')} ns"))
        out(table(rows, headers=("routeDesign", "")))
        if self.st.drc:
            warn("RFI-RT-105", f"Routing finished with {self.st.drc} DRC markers — see {self.project.rel(drc_rpt)}")
        else:
            ok("Routing complete — real route geometry now shown in the GUI.")
        self._update_gui("route")
        return True

    @command("earlyGlobalRoute", cat="Route", help="Global route only (congestion estimate)")
    def c_egr(self, args):
        return self.c_route(["-global"])

    @command("addFiller", cat="Route", usage="addFiller [-cell {cells}]", help="Insert filler cells")
    def c_filler(self, args):
        self._require("place", "addFiller")
        opts, _ = parse_opts(args, valued={"-cell": "+", "-prefix": 1})
        cells = opts.get("-cell") or self.profile.fill_cells
        if not cells:
            raw = _tcl_list(self.q("rf::masters {(?i)fill}"))
            cells = raw[0::2]
        if not cells:
            raise CmdError("No filler cells known — pass -cell {cells}")
        r = self.tcl(f"filler_placement {{{' '.join(cells)}}}\ncheck_placement", what="filler_placement")
        if r.ok:
            self._refresh_metrics()
            if self.st.stage == "route":
                self._set_stage("filled")
            ok("Fillers inserted.")
            self._update_gui(None)
        return r.ok

    # ═══════════════════════════════════════ Timing ════════════════════════════════════
    @command("create_clock", cat="Timing", usage="create_clock -name clk -period 10 [get_ports clk]",
             help="Define a clock (SDC, forwarded to OpenSTA)")
    def c_create_clock(self, args, _line=None):
        self._require("init", "create_clock")
        line = "create_clock " + " ".join(_tcl_quote(a) for a in args)
        r = self.tcl(line, what="create_clock")
        if r.ok:
            self._refresh_timing()
            if self.st.at_least("cts"):
                self.tcl("catch {set_propagated_clock [all_clocks]}", quiet=True)
            ok(f"Clock defined ({self.st.clocks} total)")
        return r.ok

    @command("timeDesign", cat="Timing", aliases=["time_design"],
             usage="timeDesign [-prePlace|-preCTS|-postCTS|-postRoute|-signoff] [-hold] [-numPaths N]",
             help="Timing summary table + reports (Innovus-style)")
    def c_time_design(self, args):
        self._require("init", "timeDesign")
        opts, _ = parse_opts(args, flags={"-prePlace", "-preCTS", "-postCTS", "-postRoute", "-signoff", "-hold",
                                          "-expandedViews"}, valued={"-numPaths": 1, "-outDir": 1, "-prefix": 1})
        stage = next((k[1:] for k in ("-prePlace", "-preCTS", "-postCTS", "-postRoute", "-signoff") if opts.get(k)),
                     {"init": "prePlace", "floorplan": "prePlace", "powerplan": "prePlace", "place": "preCTS",
                      "cts": "postCTS", "route": "postRoute", "filled": "postRoute", "signoff": "signoff"}
                     .get(self.st.stage, "prePlace"))
        self._ensure_parasitics()
        t = self._refresh_timing()
        if not self.st.clocks:
            raise CmdError("No clocks defined — timeDesign needs constraints (create_clock / read_sdc)", "RFI-TIM-101")
        rpt_dir = Path(opts.get("-outDir") or (self.project.pnr / "timingReports"))
        rpt_dir.mkdir(parents=True, exist_ok=True)
        prefix = opts.get("-prefix") or f"{self.st.top}_{stage}"
        mode = "hold" if opts.get("-hold") else "setup"
        n = int(opts.get("-numPaths") or 10)
        pd = "min" if mode == "hold" else "max"
        r = self.tcl(f"report_checks -path_delay {pd} -group_path_count {n} -format full_clock_expanded "
                     f"-fields {{slew cap input_pins fanout}} -digits 3", quiet=True)
        if not r.ok:
            r = self.tcl(f"report_checks -path_delay {pd} -group_count {n} -format full_clock_expanded "
                         f"-fields {{slew cap input_pins fanout}} -digits 3", quiet=True)
        (rpt_dir / f"{prefix}.{'hold' if mode == 'hold' else 'setup'}.tarpt").write_text(r.text)
        drv = self.tcl("report_check_types -max_slew -max_capacitance -max_fanout -violators", quiet=True)
        (rpt_dir / f"{prefix}.drv.rpt").write_text(drv.text)
        nvio = sum(1 for l in drv.text.splitlines() if re.search(r"\(VIOLATED\)", l))
        wns = t.get("whs" if mode == "hold" else "wns", "NA")
        tns = t.get("tns", "NA") if mode == "setup" else "-"
        viol = t.get("viol", "NA") if mode == "setup" else "-"

        def colr(v):
            try:
                return f"{C.RED}{v}{C.RESET}" if float(v) < 0 else f"{C.GRN}{v}{C.RESET}"
            except ValueError:
                return v
        out("")
        out(f"{C.BOLD}------------------------------------------------------------{C.RESET}")
        out(f"{C.BOLD}     timeDesign Summary  ({stage}, {mode}){C.RESET}")
        out(f"{C.BOLD}------------------------------------------------------------{C.RESET}")
        out(table([("WNS (ns):", colr(wns)), ("TNS (ns):", colr(tns)), ("Violating Paths:", viol),
                   ("All Paths:", "reg2reg / in2reg / reg2out / in2out")], headers=(f"{mode.title()} mode", "all")))
        out(table([("max_tran / max_cap / max_fanout", f"{nvio} violations"),
                   ("Parasitics", self.st.parasitics)], headers=("DRVs", "")))
        info(f"  Reports: {self.project.rel(rpt_dir)}/{prefix}.*")
        self._highlight_from_report(r.text, label=f"{stage} worst {mode} path")
        return True

    @command("report_timing", cat="Timing", usage="report_timing [-max_paths N] [-early|-late] [-nworst N] [-unconstrained]",
             help="Detailed path report (OpenSTA report_checks); worst path highlighted in GUI")
    def c_report_timing(self, args):
        self._require("init", "report_timing")
        opts, pos = parse_opts(args, flags={"-early", "-late", "-unconstrained", "-path_type_full", "-net"},
                               valued={"-max_paths": 1, "-nworst": 1, "-from": 1, "-to": 1, "-through": 1,
                                       "-path_type": 1})
        self._ensure_parasitics()
        pd = "min" if opts.get("-early") else "max"
        n = int(opts.get("-max_paths") or 1)
        extra = ""
        for k in ("-from", "-to", "-through"):
            if opts.get(k):
                extra += f" {k} {opts[k]}"
        if opts.get("-unconstrained"):
            extra += " -unconstrained"
        fields = "{slew cap input_pins fanout nets}" if opts.get("-net") else "{slew cap input_pins fanout}"
        base = f"report_checks -path_delay {pd} -format full_clock_expanded -fields {fields} -digits 3{extra}"
        r = self.tcl(f"{base} -group_path_count {n}", quiet=True)
        if not r.ok:
            r = self.tcl(f"{base} -group_count {n}", quiet=True)
        if not r.ok:
            error("RFI-TIM-110", f"report_timing failed: {r.value.strip()}")
            return False
        for l in r.lines:
            console.write(_color_timing(l))
        if not r.text.strip() or "No paths found" in r.text:
            warn("RFI-TIM-111", "No constrained paths — define clocks (create_clock / read_sdc)")
        self._highlight_from_report(r.text, label="report_timing worst path")
        return True

    def _ensure_parasitics(self) -> None:
        if not self.st.at_least("place"):
            return
        if self.st.parasitics == "none":
            self.tcl("estimate_parasitics -placement", quiet=True)
            self.st.parasitics = "placement"

    def _highlight_from_report(self, text: str, label: str) -> None:
        paths = _parse_paths(text)
        if paths:
            p = paths[0]
            self.gui_send(t="highlight", insts=p["insts"], nets=p.get("nets", []), label=label,
                          slack=p.get("slack"), startpoint=p.get("start"), endpoint=p.get("end"))

    def _send_timing_paths(self, n: int) -> None:
        if not self.st.initialized:
            self.gui_send(t="timing_paths", paths=[], error="No design initialised")
            return
        self._ensure_parasitics()
        self._refresh_timing()
        base = "report_checks -path_delay max -format full_clock_expanded -fields {slew cap input_pins fanout} -digits 3"
        r = self.tcl(f"{base} -group_path_count {n} -endpoint_path_count 1", quiet=True)
        if not r.ok:
            r = self.tcl(f"{base} -group_count {n} -endpoint_count 1", quiet=True)
        paths = _parse_paths(r.text) if r.ok else []
        self.gui_send(t="timing_paths", paths=paths, summary=self.st.timing,
                      error=None if r.ok else r.value.strip())

    @command("report_qor", cat="Timing", help="QoR summary: timing, area, DRVs, stage")
    def c_report_qor(self, args):
        self._require("init", "report_qor")
        self._ensure_parasitics()
        self._refresh_metrics()
        t = self._refresh_timing()
        m = self.st.metrics
        rows = [("Design", self.st.top), ("Stage", STAGE_LABEL.get(self.st.stage)),
                ("Instances", f"{m.get('insts', '?')} ({m.get('seq', '?')} seq)"),
                ("Std-cell area", f"{m.get('std_area', '?')} µm²"),
                ("Utilisation", f"{m['util'] * 100:.1f} %" if m.get("util") else "-"),
                ("Clocks", self.st.clocks),
                ("WNS (setup)", f"{t.get('wns', 'NA')} ns"), ("TNS", f"{t.get('tns', 'NA')} ns"),
                ("WHS (hold)", f"{t.get('whs', 'NA')} ns"),
                ("Violating endpoints", t.get("viol", "NA")),
                ("Route DRC markers", self.st.drc if self.st.drc is not None else "-"),
                ("Parasitics", self.st.parasitics)]
        out(table(rows, headers=("report_qor", "")))
        info("  WNS/TNS come from OpenSTA worst_slack/total_negative_slack queries — identical to report_timing's slack.")
        return True

    @command("report_constraints", cat="Timing", usage="report_constraints [-all_violators]",
             help="Constraint/DRV violations (report_check_types)")
    def c_report_constraints(self, args):
        self._require("init", "report_constraints")
        self._ensure_parasitics()
        opts, _ = parse_opts(args, flags={"-all_violators", "-verbose"})
        v = " -violators" if opts.get("-all_violators") else ""
        return self.tcl(f"report_check_types -max_slew -max_capacitance -max_fanout -max_delay -min_delay{v}",
                        what="report_check_types").ok

    @command("report_power", cat="Timing", help="Power report (OpenSTA, default activity)")
    def c_report_power(self, args):
        self._require("init", "report_power")
        self._ensure_parasitics()
        return self.tcl("report_power", what="report_power").ok

    @command("report_area", cat="Timing", help="Design area & utilisation")
    def c_report_area(self, args):
        self._require("init", "report_area")
        return self.tcl("report_design_area\ncatch {report_cell_usage}", what="report_area").ok

    @command("report_clocks", cat="Timing", aliases=["report_clock"], help="Clock definitions")
    def c_report_clocks(self, args):
        self._require("init", "report_clocks")
        return self.tcl("report_clock_properties", what="report_clock_properties").ok

    @command("optDesign", cat="Optimization", aliases=["opt_design"],
             usage="optDesign -preCTS|-postCTS|-postRoute [-hold] [-drv]",
             help="Repair DRVs and setup (or hold) timing (Resizer)")
    def c_opt(self, args):
        self._require("place", "optDesign")
        opts, _ = parse_opts(args, flags={"-preCTS", "-postCTS", "-postRoute", "-hold", "-drv", "-setup", "-incr"})
        self._ensure_parasitics()
        steps = []
        if opts.get("-drv") or not (opts.get("-hold")):
            steps.append("repair_design")
        steps.append("repair_timing -hold" if opts.get("-hold") else "repair_timing -setup")
        steps.append("detailed_placement\ncheck_placement")
        if self.st.at_least("route"):
            warn("RFI-OPT-102", "Post-route optimisation modifies placement; re-run routeDesign -detail afterwards")
        r = self.tcl("\n".join(steps), what="optDesign", msg_id="RFI-OPT-101")
        if r.ok:
            self._refresh_metrics()
            self._refresh_timing()
            ok(f"optDesign done — WNS {self.st.timing.get('wns', 'NA')} ns")
            self._update_gui(None)
        return r.ok

    @command("ecoPlace", cat="Optimization", aliases=["eco_place"], help="ECO placement legalisation")
    def c_eco_place(self, args):
        return self.c_refine(args)

    @command("ecoRoute", cat="Optimization", aliases=["eco_route"], help="ECO (incremental) detailed route")
    def c_eco_route(self, args):
        return self.c_route(["-detail"])

    # ═══════════════════════════════════════ Verify ════════════════════════════════════
    @command("verify_drc", cat="Verify", aliases=["verifyGeometry", "verify_geometry"],
             help="Routing DRC (TritonRoute checker) + placement legality")
    def c_verify_drc(self, args):
        self._require("place", "verify_drc")
        okp = self.tcl("check_placement", what="check_placement").ok
        if not self.st.at_least("route"):
            if okp:
                ok("verify_drc: placement legal (routing DRC runs after routeDesign)")
            return okp
        rpt = self.project.pnr / "reports" / f"{self.st.top}.route.drc.rpt"
        n = _count_drc(rpt)
        self.st.drc = n
        if n:
            warn("RFI-VFY-101", f"{n} DRC violation(s) — {self.project.rel(rpt)} (markers shown in GUI)")
            self.gui_send(t="drc", file=str(rpt))
        else:
            ok("verify_drc: 0 violations")
        return okp and not n

    @command("verify_antenna", cat="Verify", aliases=["verifyProcessAntenna"], help="Antenna check (check_antennas)")
    def c_antenna(self, args):
        self._require("route", "verify_antenna")
        return self.tcl("check_antennas", what="check_antennas").ok

    @command("verify_connectivity", cat="Verify", aliases=["verifyConnectivity"], help="Open/unrouted net check")
    def c_connectivity(self, args):
        self._require("route", "verify_connectivity")
        r = self.tcl("check_placement\nreport_floating_nets -verbose", what="connectivity", quiet=True)
        for l in r.lines:
            console.write(colorize_backend_line(l))
        if r.ok:
            ok("verify_connectivity: no floating nets reported")
        else:
            warn("RFI-VFY-120", f"connectivity check: {r.value.strip()}")
        return r.ok

    @command("checkDesign", cat="Verify", aliases=["check_design"], usage="checkDesign [-all]",
             help="Netlist/constraint sanity (check_setup)")
    def c_check_design(self, args):
        self._require("init", "checkDesign")
        return self.tcl("check_setup -verbose", what="check_setup").ok

    # ═══════════════════════════════════════ Extract ═══════════════════════════════════
    @command("extractRC", cat="Timing", aliases=["extract_rc"], usage="extractRC [-rules file]",
             help="Parasitic extraction: OpenRCX when routed + rules exist, else estimates")
    def c_extract(self, args):
        self._require("place", "extractRC")
        opts, _ = parse_opts(args, valued={"-rules": 1})
        if self.st.at_least("route"):
            rules = opts.get("-rules") or (str(env.pdk().rcx_rules()) if env.pdk().rcx_rules() else None)
            if rules and Path(rules).exists():
                note(f"OpenRCX with {Path(rules).name} …")
                r = self.tcl(f"define_process_corner -ext_model_index 0 X\n"
                             f"extract_parasitics -ext_model_file {{{rules}}}", what="extract_parasitics")
                if r.ok:
                    spef = self.project.pnr / f"{self.st.top}.spef"
                    self.tcl(f"write_spef {{{spef}}}\nread_spef {{{spef}}}", quiet=True)
                    self.st.parasitics = "rcx"
                    ok(f"Extraction done → {self.project.rel(spef)}")
                    return True
                warn("RFI-EXT-101", "RCX failed; falling back to global-route estimates")
            else:
                warn("RFI-EXT-102", "No OpenRCX rules file found for this PDK; using global-route estimates")
            r = self.tcl("estimate_parasitics -global_routing", what="estimate_parasitics")
            self.st.parasitics = "global_route" if r.ok else self.st.parasitics
            return r.ok
        r = self.tcl("estimate_parasitics -placement", what="estimate_parasitics")
        if r.ok:
            self.st.parasitics = "placement"
            ok("Placement-based parasitic estimate updated.")
        return r.ok

    # ═══════════════════════════════════════ Output ════════════════════════════════════
    @command("defOut", cat="Output", aliases=["write_def"], usage="defOut [-routing] <file.def>", help="Write DEF")
    def c_defout(self, args):
        self._require("init", "defOut")
        opts, pos = parse_opts(args, flags={"-routing", "-floorplan", "-netlist"})
        fn = pos[0] if pos else str(self.project.pnr / f"{self.st.top}.def")
        r = self.tcl(f"write_def {{{fn}}}", what="write_def")
        if r.ok:
            ok(f"DEF written: {fn}")
        return r.ok

    @command("saveNetlist", cat="Output", aliases=["write_netlist", "write_hdl"], usage="saveNetlist <file.v> [-includePowerGround]",
             help="Write the current (post-opt) netlist")
    def c_save_netlist(self, args):
        self._require("init", "saveNetlist")
        opts, pos = parse_opts(args, flags={"-includePowerGround", "-excludeLeafCell"})
        fn = pos[0] if pos else str(self.project.pnr / f"{self.st.top}.pnr.v")
        pg = "-include_pwr_gnd " if opts.get("-includePowerGround") else ""
        r = self.tcl(f"write_verilog {pg}{{{fn}}}", what="write_verilog")
        if r.ok:
            ok(f"Netlist written: {fn}")
        return r.ok

    @command("write_sdf", cat="Output", usage="write_sdf <file.sdf>", help="Write SDF delays")
    def c_write_sdf(self, args):
        self._require("place", "write_sdf")
        fn = args[0] if args else str(self.project.pnr / f"{self.st.top}.sdf")
        r = self.tcl(f"write_sdf {{{fn}}}", what="write_sdf")
        if r.ok:
            ok(f"SDF written: {fn}")
        return r.ok

    @command("write_sdc", cat="Output", usage="write_sdc <file.sdc>", help="Write current constraints")
    def c_write_sdc(self, args):
        self._require("init", "write_sdc")
        fn = args[0] if args else str(self.project.pnr / f"{self.st.top}.sdc")
        r = self.tcl(f"write_sdc {{{fn}}}", what="write_sdc")
        if r.ok:
            ok(f"SDC written: {fn}")
        return r.ok

    @command("write_lef_abstract", cat="Output", aliases=["write_lef"], usage="write_lef_abstract <file.lef>",
             help="Write an abstract LEF of this block")
    def c_write_lef(self, args):
        self._require("place", "write_lef_abstract")
        fn = args[0] if args else str(self.project.pnr / f"{self.st.top}.lef")
        r = self.tcl(f"write_abstract_lef {{{fn}}}", what="write_abstract_lef")
        if r.ok:
            ok(f"LEF abstract written: {fn}")
        return r.ok

    @command("streamOut", cat="Output", aliases=["write_gds"], usage="streamOut <file.gds> [-view]",
             help="Stream GDSII via KLayout (DEF + LEF + std-cell GDS merge)")
    def c_stream_out(self, args):
        self._require("route", "streamOut")
        opts, pos = parse_opts(args, flags={"-view", "-noView"}, valued={"-mapFile": 1, "-merge": "+", "-units": 1})
        from ..backends import klayout as kl
        gds = Path(pos[0] if pos else self.project.pnr / f"{self.st.top}.gds").resolve()
        dfn = self.project.pnr / f"{self.st.top}.final.def"
        if not self.tcl(f"write_def {{{dfn}}}", what="write_def").ok:
            return False
        okk, msg = kl.def2gds(dfn, self.st.lefs, gds, self.st.top, merge=opts.get("-merge"),
                              map_file=opts.get("-mapFile"))
        if not okk:
            error("RFI-OUT-201", msg)
            return False
        ok(f"GDSII written: {gds}")
        if opts.get("-view"):
            kl.view(gds)
        return True

    @command("saveDesign", cat="Session", aliases=["save_design", "write_db"], usage="saveDesign <name>",
             help="Checkpoint the database (write_db) + shell state")
    def c_save(self, args):
        self._require("init", "saveDesign")
        name = args[0] if args else f"{self.st.top}_{self.st.stage}"
        base = self.project.pnr / "dbs" / name
        base.parent.mkdir(parents=True, exist_ok=True)
        r = self.tcl(f"write_db {{{base}.odb}}", what="write_db")
        if r.ok:
            self.tcl(f"write_def {{{base}.def}}", quiet=True)   # for voltus/pegasus viewers
            import json
            (base.with_suffix(".rtlflow.json")).write_text(json.dumps(asdict(self.st), indent=1))
            ok(f"Design saved: {self.project.rel(str(base) + '.odb')}")
        return r.ok

    @command("restoreDesign", cat="Session", aliases=["restore_design", "read_db"], usage="restoreDesign <name|file.odb>",
             help="Restore a saved checkpoint into a fresh session")
    def c_restore(self, args):
        import json
        dbs = self.project.pnr / "dbs"
        if not args:
            items = sorted(dbs.glob("*.odb")) if dbs.exists() else []
            if not items:
                raise CmdError("No checkpoints in .rtlflow/runs/pnr/dbs")
            for p in items:
                out(f"  {p.stem}")
            return True
        p = Path(args[0])
        if not p.suffix:
            p = dbs / f"{args[0]}.odb"
        if not p.exists():
            raise CmdError(f"No such checkpoint: {p}")
        meta = p.with_suffix(".rtlflow.json")
        self.c_restart_backend([])
        if meta.exists():
            try:
                self.st = PnRState(**json.loads(meta.read_text()))
            except Exception:
                pass
        for lib in self.st.libs:
            self.tcl(f"read_liberty {{{lib}}}", quiet=True)
        r = self.tcl(f"read_db {{{p}}}", what="read_db")
        if not r.ok:
            return False
        for s in self.st.sdcs:
            self.tcl(f"read_sdc {{{s}}}", quiet=True)
        self.st.initialized = True
        self._derive_profile()
        if self.st.at_least("cts"):
            self.tcl("catch {set_propagated_clock [all_clocks]}", quiet=True)
        self._refresh_metrics()
        self._refresh_timing()
        ok(f"Restored {p.name} — stage: {STAGE_LABEL.get(self.st.stage)}")
        self._update_gui(None, force=True)
        return True

    # ════════════════════════════════════════ GUI ══════════════════════════════════════
    @command("win", cat="GUI", aliases=["gui_show"], usage="win [off]", help="Open (or close) the layout GUI")
    def c_win(self, args):
        if args and args[0].lower() in ("off", "0", "false", "hide"):
            return self.c_gui_hide([])
        if self.gui and self.gui.is_open:
            self.gui_send(t="raise")
            info("GUI already open")
            return True
        if not display_available():
            warn("RFI-GUI-001", "No display available ($DISPLAY/$WAYLAND_DISPLAY unset) — continuing without GUI")
            return False
        self.gui = GuiHost("innovus_gui", self.project.logs / "innovus_gui.log", self._gui_message, self._gui_closed)
        okk, why = self.gui.launch(["--title", f"{self.st.top or 'RTLFlow'}", "--project", str(self.project.root)])
        if not okk:
            if "no module named" in why.lower() and "tkinter" in why.lower():
                error("RFI-GUI-002", "Python Tk is missing. Install it once:  sudo apt install python3-tk")
            else:
                error("RFI-GUI-003", f"GUI failed to start: {why}  (see {self.project.rel(self.gui.log_path)})")
            return False
        if not self._gui_worker or not self._gui_worker.is_alive():
            self._gui_worker = threading.Thread(target=self._gui_worker_loop, name="gui-exec", daemon=True)
            self._gui_worker.start()
        console.listeners.append(self._transcript_listener())
        self.gui_send(t="hello", app="innovus", version=VERSION, backend=f"OpenROAD {self.session.version}",
                      project=str(self.project.root))
        out(f"  {C.GRY}GUI       {C.RESET}{C.GRN}open{C.RESET} (pid {self.gui.proc.pid}) — same OpenROAD session as this shell")
        return True

    @command("gui_hide", cat="GUI", help="Close the GUI window (session keeps running)")
    def c_gui_hide(self, args):
        if self.gui and self.gui.is_open:
            console.listeners[:] = [l for l in console.listeners if not getattr(l, "_rf_gui", False)]
            self.gui.close()
            info("GUI closed — 'win' reopens it")
        return True

    @command("fit", cat="GUI", aliases=["gui_fit", "zoomOut_fit"], help="Zoom GUI to fit the die")
    def c_fit(self, args):
        self.gui_send(t="fit")
        return True

    @command("zoomBox", cat="GUI", usage="zoomBox x1 y1 x2 y2", help="Zoom GUI to a box (µm)")
    def c_zoombox(self, args):
        if len(args) < 4:
            raise CmdError("usage: zoomBox x1 y1 x2 y2")
        self.gui_send(t="zoom", box=[float(a) for a in args[:4]])
        return True

    @command("selectInst", cat="GUI", aliases=["select_inst"], usage="selectInst <name|pattern>", help="Select & zoom to instance(s)")
    def c_select_inst(self, args):
        if not args:
            raise CmdError("usage: selectInst <name>")
        self.gui_send(t="select", insts=args, zoom=True)
        return True

    @command("selectNet", cat="GUI", aliases=["select_net", "highlight_net"], usage="selectNet <name>", help="Highlight a net in the GUI")
    def c_select_net(self, args):
        if not args:
            raise CmdError("usage: selectNet <name>")
        self.gui_send(t="highlight", insts=[], nets=args, label=f"net {' '.join(args)}")
        return True

    @command("deselectAll", cat="GUI", aliases=["gui_clear_highlight", "dehighlight"], help="Clear GUI selection/highlight")
    def c_deselect(self, args):
        self.gui_send(t="clear_highlight")
        return True

    @command("gui_dump", cat="GUI", aliases=["dumpToGIF", "save_screenshot"], usage="gui_dump <file.ps|.png>",
             help="Save the layout view to a file")
    def c_gui_dump(self, args):
        fn = os.path.abspath(args[0] if args else str(self.project.pnr / f"{self.st.top}_{self.st.stage}.png"))
        self.gui_send(t="dump", file=fn)
        info(f"GUI snapshot requested → {fn}")
        return True

    @command("gui_refresh", cat="GUI", aliases=["redraw"], help="Re-read the DB into the GUI")
    def c_gui_refresh(self, args):
        self._refresh_metrics()
        self._update_gui(None, force=True)
        return True

    @command("view_gds", cat="GUI", usage="view_gds [file.gds]", help="Open a GDS in KLayout (like opening Virtuoso)")
    def c_view_gds(self, args):
        from ..backends import klayout as kl
        fn = Path(args[0]) if args else self.project.pnr / f"{self.st.top}.gds"
        if not fn.exists():
            raise CmdError(f"{fn} not found — run streamOut first")
        return kl.view(fn)

    # ═══════════════════════════════════════ Session ═══════════════════════════════════
    @command("status", cat="Session", aliases=["design_status", "dbGet_status"], help="Show database state")
    def c_status(self, args):
        s = self.st
        tick = f"{C.GRN}✓{C.RESET}"
        cross = f"{C.RED}✗{C.RESET}"
        if s.initialized:
            self._refresh_metrics()
        m = s.metrics

        def names(lst):
            return ", ".join(Path(x).name for x in lst) if lst else "-"
        rows = [
            ("OpenROAD session", f"{tick} pid {self.session.proc.pid} · {self.session.version}" if self.session and self.session.alive else cross),
            ("Liberty", f"{tick if s.libs else cross} {names(s.libs)}"),
            ("Tech LEF", f"{tick if s.tech_loaded else cross} {names(s.tech_lefs)}"),
            ("Cell LEF", f"{tick if s.cell_lefs else cross} {names(s.cell_lefs)}"),
            ("Netlist", f"{tick if s.netlists else cross} {names(s.netlists)}"),
            ("Top cell", f"{tick if s.top else cross} {s.top or '-'}"),
            ("Design DB", f"{tick if s.initialized else cross} {'linked' if s.initialized else 'not initialised'}"),
            ("Constraints", f"{tick if s.clocks else C.YEL + '!' + C.RESET} {s.clocks} clock(s) {names(s.sdcs) if s.sdcs else ''}"),
            ("Stage", f"{C.BOLD}{STAGE_LABEL.get(s.stage, s.stage)}{C.RESET}"),
        ]
        if s.initialized:
            rows.append(("Instances", f"{m.get('insts', '?')}  placed {m.get('placed', '?')}"))
            if m.get("die") and m["die"][2] > m["die"][0]:
                d = m["die"]
                rows.append(("Die", f"{d[2] - d[0]:.2f} × {d[3] - d[1]:.2f} µm"))
            if m.get("util"):
                rows.append(("Utilisation", f"{m['util'] * 100:.1f} %"))
            if s.timing.get("wns") not in (None, "NA"):
                rows.append(("WNS / TNS", f"{s.timing['wns']} / {s.timing.get('tns', 'NA')} ns"))
            rows.append(("Tech profile", s.profile + (f" · site {s.site}" if s.site else "")))
        rows.append(("GUI", f"{tick} open" if self.gui and self.gui.is_open else "closed ('win' to open)"))
        out(table(rows, headers=("innovus status", "")))
        nxt = {"empty": "read_libs …", "loaded": "init_design", "init": "floorPlan", "floorplan": "placeDesign",
               "powerplan": "placeDesign", "place": "ccopt_design", "cts": "routeDesign",
               "route": "timeDesign -postRoute / addFiller / streamOut"}.get(s.stage)
        if nxt:
            info(f"  Next step: {nxt}")
        self._push_state()
        return True

    @command("tech_profile", cat="Session", help="Show technology profile in use")
    def c_tech(self, args):
        p = self.profile
        rows = [("Profile", p.name), ("Site", self.st.site or p.site or "(derived)"),
                ("IO layers", f"{p.io_hlayer or '?'} / {p.io_vlayer or '?'}"),
                ("Route layers", f"{p.min_route or '?'} – {p.max_route or '?'}"),
                ("Tap cell", p.tap_cell or "-"), ("Tie cell", p.tie_cell[0] if p.tie_cell else "-"),
                ("CTS root buffer", p.root_buf or "(largest clkbuf)"),
                ("Fill cells", " ".join(p.fill_cells) or "-")]
        out(table(rows, headers=("tech_profile", "")))
        return True

    @command("getVersion", cat="Session", aliases=["version"], help="Version info")
    def c_version(self, args):
        out(f"RTLFlow innovus {VERSION} ({CODENAME}) · OpenROAD {self.session.version if self.session else '?'}")
        return True


# ══════════════════════════════════════ helpers ══════════════════════════════════════
def _f(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".")


def _tcl_list(s: str) -> list[str]:
    """Minimal Tcl list splitter (handles {braced} elements)."""
    outl, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        if s[i] == "{":
            depth, j = 1, i + 1
            while j < n and depth:
                if s[j] == "{": depth += 1
                elif s[j] == "}": depth -= 1
                j += 1
            outl.append(s[i + 1:j - 1]); i = j
        else:
            j = i
            while j < n and not s[j].isspace():
                j += 1
            outl.append(s[i:j]); i = j
    return outl


def _tcl_quote(a: str) -> str:
    if a.startswith("[") or a.startswith("{") or not a or " " not in a:
        return a if a else "{}"
    return "{" + a + "}"


def _lef_has_tech(path: str) -> bool:
    try:
        with open(path, errors="replace") as f:
            head = f.read(400_000)
    except OSError:
        return False
    return bool(re.search(r"^\s*LAYER\s+\S+\s*$[\s\S]*?^\s*TYPE\s+ROUTING", head, re.M))


def _find_tech_lef(cell_lefs: list[str]) -> str | None:
    """open_pdks layout: libs.ref/<lib>/lef/<lib>.lef  ↔  libs.ref/<lib>/techlef/<lib>__nom.tlef"""
    for c in cell_lefs:
        p = Path(os.path.abspath(c))
        tdir = p.parent.parent / "techlef"
        if tdir.is_dir():
            cands = sorted(tdir.glob("*__nom.tlef")) or sorted(tdir.glob("*.tlef"))
            if cands:
                return str(cands[0])
        for sib in sorted(p.parent.glob("*.tlef")):
            return str(sib)
    t = env.pdk().tech_lef()
    return str(t) if t else None


def _first_module(path: str) -> str | None:
    try:
        txt = Path(path).read_text(errors="replace")
    except OSError:
        return None
    mods = re.findall(r"^\s*module\s+([A-Za-z_]\w*)", txt, re.M)
    return mods[-1] if mods else None


def _parse_mmmc(path: Path) -> tuple[list[str], list[str]]:
    if not path.exists():
        return [], []
    txt = path.read_text()
    libs, sdcs = [], []
    for m in re.finditer(r"create_library_set[^\n]*-timing\s+(\{[^}]*\}|\S+)", txt):
        libs += m.group(1).strip("{}").split()
    for m in re.finditer(r"create_constraint_mode[^\n]*-sdc_files\s+(\{[^}]*\}|\S+)", txt):
        sdcs += m.group(1).strip("{}").split()
    return expand_files(libs, path.parent), expand_files(sdcs, path.parent)


def _count_drc(rpt: Path) -> int | None:
    if not rpt.exists():
        return None
    txt = rpt.read_text(errors="replace")
    return len(re.findall(r"(?im)^\s*violation type", txt))


_PIN_RE = re.compile(r"\s([^\s()]+)/([^\s/()]+)\s+\(([A-Za-z_][\w$]*)\)\s*$")
_NET_RE = re.compile(r"^\s*(?:[\d.]+\s+)*([^\s()]+)\s+\(net\)")


def _parse_paths(text: str) -> list[dict]:
    """Parse OpenSTA report_checks output into paths with instances on each."""
    paths: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Startpoint:"):
            if cur:
                paths.append(cur)
            cur = {"start": s.split(":", 1)[1].strip().split(" (")[0], "end": "", "insts": [], "nets": [],
                   "slack": None, "met": None, "group": ""}
            continue
        if cur is None:
            continue
        if s.startswith("Endpoint:"):
            cur["end"] = s.split(":", 1)[1].strip().split(" (")[0]
        elif s.startswith("Path Group:"):
            cur["group"] = s.split(":", 1)[1].strip()
        elif s.endswith("slack (MET)") or s.endswith("slack (VIOLATED)"):
            try:
                cur["slack"] = float(s.split()[0])
            except ValueError:
                pass
            cur["met"] = "MET" in s
        else:
            m = _PIN_RE.search(line)
            if m and m.group(3) not in ("in", "out", "inout", "net"):
                inst = m.group(1).replace("\\", "")
                if inst not in cur["insts"]:
                    cur["insts"].append(inst)
            mn = _NET_RE.search(line)
            if mn:
                cur["nets"].append(mn.group(1).replace("\\", ""))
    if cur:
        paths.append(cur)
    return paths


def _color_timing(line: str) -> str:
    if line.rstrip().endswith("(VIOLATED)"):
        return f"{C.RED}{C.BOLD}{line}{C.RESET}"
    if line.rstrip().endswith("(MET)"):
        return f"{C.GRN}{C.BOLD}{line}{C.RESET}"
    if line.startswith("Startpoint:") or line.startswith("Endpoint:"):
        return f"{C.BOLD}{line}{C.RESET}"
    return line


def run_innovus(gui: bool = True, files: list[str] | None = None, batch: bool = False) -> int:
    sh = InnovusShell(gui=gui, init_files=files, batch=batch)
    if not sh.start():
        console.close_logs()
        return 1
    if batch:
        sh.on_start()
        sh.on_exit()
        return 0
    return sh.cmdloop()
