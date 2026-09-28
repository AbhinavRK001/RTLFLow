#!/usr/bin/env python3
"""Mock `openroad` for exercising RTLFlow's innovus shell + GUI without OpenROAD.

Speaks the same stdin/stdout protocol as the real tool as used by
rtlflow.backends.openroad (Tcl lines with base64 payload + sentinel), and
emulates just enough commands, with OpenROAD-style messages and errors
(e.g. ORD-0050 when floorplanning with no tech). Geometry comes from
tests/fixture.py using real sky130 LEF sizes.

    RTLFLOW_OPENROAD=/path/to/tests/mock_openroad.py rtlflow
"""

from __future__ import annotations

import base64
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture import Fixture, CELLS  # noqa: E402

if "-version" in sys.argv and __name__ == "__main__":
    print("mock-openroad 2.0-rtlflow-test")
    sys.exit(0)

S = {"tech": False, "lefs": [], "libs": 0, "netlist": False, "linked": False, "stage": "none",
     "clocks": 0, "fix": None, "top": None}
PL_SENT = re.compile(r"binary decode base64 ([A-Za-z0-9+/=]+)\]\} ::__rf_msg\]; puts \"\\n<<RF_END (\d+)")


class TclError(Exception):
    pass


def out(s=""):
    sys.stdout.write(s + "\n")
    sys.stdout.flush()


def fx() -> Fixture:
    if S["fix"] is None:
        S["fix"] = Fixture(S["lefs"])
    return S["fix"]


def need_tech_design():
    errs = []
    if not S["tech"]:
        errs.append("[ERROR ORD-0050] No tech is loaded")
    if not S["linked"]:
        errs.append("[ERROR IFP-0038] No design is loaded")
    if errs:
        for e in errs:
            out(e)
        raise TclError(errs[-1])


def unbrace(s: str) -> str:
    s = s.strip()
    return s[1:-1] if s.startswith("{") and s.endswith("}") else s


TIMING = """Startpoint: _10_ (rising edge-triggered flip-flop clocked by clk)
Endpoint: _12_ (rising edge-triggered flip-flop clocked by clk)
Path Group: clk
Path Type: max

Fanout     Cap    Slew   Delay    Time   Description
-----------------------------------------------------------------------------
                          0.00    0.00   clock clk (rise edge)
                          0.00    0.00   clock source latency
                  0.00    0.00    0.00 ^ clk (in)
     1    0.00                           clk (net)
                  0.05    0.12    0.12 ^ {ck}/X (sky130_fd_sc_hd__clkbuf_4)
                  0.04    0.02    0.14 ^ _10_/CLK (sky130_fd_sc_hd__dfxtp_1)
                  0.09    0.33    0.47 v _10_/Q (sky130_fd_sc_hd__dfxtp_1)
     3    0.01                           _10_q (net)
                  0.09    0.00    0.47 v _17_/A (sky130_fd_sc_hd__xor2_1)
                  0.14    0.21    0.68 ^ _17_/X (sky130_fd_sc_hd__xor2_1)
                  0.14    0.00    0.68 ^ _21_/B (sky130_fd_sc_hd__and3_1)
                  0.06    0.22    0.90 ^ _21_/X (sky130_fd_sc_hd__and3_1)
                  0.06    0.00    0.90 ^ _14_/S (sky130_fd_sc_hd__mux2_1)
                  0.07    0.34    1.24 v _14_/X (sky130_fd_sc_hd__mux2_1)
                  0.07    0.00    1.24 v _12_/D (sky130_fd_sc_hd__dfxtp_1)
                                  1.24   data arrival time

                         10.00   10.00   clock clk (rise edge)
                          0.00   10.00   clock network delay (propagated)
                         -0.25    9.75   clock uncertainty
                          0.00    9.75   clock reconvergence pessimism
                                  9.75 ^ _12_/CLK (sky130_fd_sc_hd__dfxtp_1)
                         -0.12    9.63   library setup time
                                  9.63   data required time
-----------------------------------------------------------------------------
                                  9.63   data required time
                                 -1.24   data arrival time
-----------------------------------------------------------------------------
                                  {slack}   slack (MET)
"""


def run_cmd(line: str) -> str:
    line = line.strip()
    if not line or line.startswith("#"):
        return ""
    m = re.match(r"catch \{(.*)\}(\s+\S+)?$", line, re.S)
    if m:
        try:
            run_cmd(m.group(1))
        except TclError:
            pass
        return "0"
    w = line.split()
    c = w[0]
    if c in ("fconfigure", "set_thread_count", "set_layer_rc", "set_wire_rc", "set_routing_layers",
             "set_global_routing_layer_adjustment", "set_propagated_clock", "optimize_mirroring",
             "estimate_parasitics", "add_global_connection", "set_voltage_domain", "define_pdn_grid",
             "add_pdn_stripe", "add_pdn_connect", "define_process_corner"):
        return ""
    if c == "set":
        if len(w) >= 3 and w[1] == "::__rf_version":
            return "mock-2.0"
        if w[1] == "::__rf_version":
            return "mock-2.0"
        return " ".join(w[2:])
    if c == "read_liberty":
        f = unbrace(" ".join(w[1:]))
        out(f"[INFO ODB-0227] mock: read liberty {os.path.basename(f)}")
        S["libs"] += 1
        return "1"
    if c == "read_lef":
        f = unbrace(w[-1])
        tech = "-tech" in w or f.endswith(".tlef")
        txt = Path(f).read_text(errors="replace")
        if tech or "TYPE ROUTING" in txt:
            S["tech"] = True
            out("[INFO ODB-0223]     Created 13 technology layers")
            out("[INFO ODB-0225]     Created 2 library sites")
        n = len(re.findall(r"^MACRO ", txt, re.M))
        if n:
            out(f"[INFO ODB-0226]     Created {n} library cells")
        out(f"[INFO ODB-0227] LEF file: {f}, created {n} library cells")
        S["lefs"].append(f)
        return ""
    if c == "read_verilog":
        S["netlist"] = True
        return ""
    if c == "link_design":
        if not S["netlist"]:
            out("[ERROR ORD-2010] no verilog read")
            raise TclError("no verilog read")
        S["linked"] = True
        S["top"] = unbrace(w[1])
        S["stage"] = "init"
        return ""
    if c in ("read_sdc", "create_clock"):
        S["clocks"] = 1
        return ""
    if c == "rf::tech_loaded":
        return "1" if S["tech"] else "0"
    if c == "rf::lib_count":
        return "441" if S["tech"] else "0"
    if c == "rf::layers":
        return "li1 VERTICAL 460 met1 HORIZONTAL 340 met2 VERTICAL 460 met3 HORIZONTAL 680 met4 VERTICAL 920 met5 HORIZONTAL 3400"
    if c == "rf::sites":
        return "unithd CORE 460 2720 unithddbl CORE 460 5440"
    if c == "rf::masters":
        pat = unbrace(" ".join(w[1:]))
        if not S["lefs"]:
            return ""
        lib = fx().lib
        res = []
        try:
            rx = re.compile(pat)
        except re.error:
            rx = re.compile(".")
        for n, M in lib.macros.items():
            if rx.search(n):
                res += [n, str(int(M.w * 1000))]
        return " ".join(res)
    if c == "rf::design_info":
        if not S["linked"]:
            return "loaded 0"
        f = fx()
        fp = S["stage"] not in ("init",)
        if fp and not hasattr(f, "die"):
            f.plan()
        d = f.die if fp else (0, 0, 0, 0)
        co = f.core if fp else (0, 0, 0, 0)
        ninst = len(f.pos) if hasattr(f, "pos") and S["stage"] in ("place", "cts", "route") else len(CELLS)
        std = f.area() * 1e6
        return (f"loaded 1 name count dbu 1000 die_x0 {int(d[0]*1000)} die_y0 {int(d[1]*1000)} die_x1 {int(d[2]*1000)} "
                f"die_y1 {int(d[3]*1000)} core_x0 {int(co[0]*1000)} core_y0 {int(co[1]*1000)} core_x1 {int(co[2]*1000)} "
                f"core_y1 {int(co[3]*1000)} insts {ninst} placed {ninst if S['stage'] in ('place','cts','route') else 0} "
                f"seq 4 std_area_dbu2 {std:.0f} nets 32 terms 12 rows {f.rows if fp else 0}")
    if c == "rf::timing_summary":
        if not S["clocks"]:
            return "clocks 0"
        wns = {"init": "7.9140", "floorplan": "7.9140", "place": "7.8012", "cts": "7.7380", "route": "7.7010"}.get(S["stage"], "7.9")
        return f"clocks 1 wns {wns} whs 0.2140 tns 0.0000 viol 0"
    if c == "initialize_floorplan":
        need_tech_design()
        m = re.search(r"-utilization ([\d.]+)", line)
        f = fx()
        if m:
            f.util = float(m.group(1)) / 100
        f.plan()
        out(f"[INFO IFP-0001] Added {f.rows} rows of {f.sites} site unithd.")
        S["stage"] = "floorplan"
        return ""
    if c == "make_tracks":
        need_tech_design()
        return ""
    if c == "place_pins":
        if S["stage"] == "init":
            out("[ERROR PPL-0059] Die area not set.")
            raise TclError("Die area not set")
        out("[INFO PPL-0001] Number of available slots 434")
        out("[INFO PPL-0002] Number of I/O             12")
        out("[INFO PPL-0003] Number of I/O w/sink      12")
        out("[INFO PPL-0005] Slots per section         200")
        out("[INFO PPL-0012] I/O nets HPWL: 183.42 um.")
        return ""
    if c == "tapcell":
        fx().taps = True
        out("[INFO TAP-0004] Inserted 16 tapcells.")
        return ""
    if c == "global_connect":
        return ""
    if c == "pdngen" or c == "catch" and "pdngen" in line:
        if "-reset" in line:
            return ""
        fx().pdn = True
        out("[INFO PDN-0001] Inserting grid: stdcell_grid")
        return ""
    if c == "global_placement":
        if S["stage"] not in ("floorplan", "powerplan"):
            need_tech_design()
        f = fx()
        out("[INFO GPL-0002] DBU: 1000")
        out("[INFO GPL-0003] SiteSize: 460 2720")
        out(f"[INFO GPL-0006] NumInstances: {len(CELLS)}")
        for it in (0, 50, 100, 150, 200):
            out(f"[NesterovSolve] Iter: {it:4d} overflow: {max(0.1, 0.9 - it / 250):.3f} HPWL: {183000 - it * 200}")
            time.sleep(0.05)
        out("[INFO GPL-1001] Finished with Overflow: 0.098")
        f.place()
        S["stage"] = "place"
        return ""
    if c in ("repair_design",):
        out("[INFO RSZ-0058] Using max wire length 3000um.")
        out("[INFO RSZ-0039] Resized 0 instances.")
        return ""
    if c == "detailed_placement":
        out("Placement Analysis")
        out("---------------------------------")
        out("total displacement          4.2 u")
        out("average displacement        0.2 u")
        return ""
    if c == "check_placement":
        return ""
    if c == "clock_tree_synthesis":
        if S["stage"] != "place":
            raise TclError("not placed")
        f = fx()
        f.cts = True
        f.place()
        out("[INFO CTS-0050] Root buffer is sky130_fd_sc_hd__clkbuf_16.")
        out("[INFO CTS-0010]  Clock net \"clk\" has 4 sinks.")
        out("[INFO CTS-0018]     Created 1 clock buffers.")
        S["stage"] = "cts"
        return ""
    if c in ("repair_clock_nets", "repair_timing"):
        return ""
    if c == "report_cts":
        out("[INFO CTS-0003] Total number of Clock Roots: 1.")
        out("[INFO CTS-0005] Total number of Sinks: 4.")
        return ""
    if c == "report_clock_skew":
        out("Clock clk\nLatency      CRPR       Skew\n_12_/CLK ^\n   0.14\n_10_/CLK ^\n   0.13      0.00       0.01")
        return ""
    if c == "global_route":
        out("[INFO GRT-0020] Min routing layer: met1")
        out("[INFO GRT-0021] Max routing layer: met5")
        out("[INFO GRT-0018] Total wirelength: 412 um")
        return ""
    if c == "detailed_route":
        f = fx()
        for it in range(3):
            out(f"[INFO DRT-0195] Start {it}th optimization iteration.")
            time.sleep(0.1)
            out(f"[INFO DRT-0199]   Number of violations = {max(0, 6 - it * 3)}.")
        m = re.search(r"-output_drc \{([^}]*)\}", line)
        if m:
            Path(m.group(1)).write_text("")
        out("[INFO DRT-0198] Complete detail routing.")
        S["stage"] = "route"
        return ""
    if c == "filler_placement":
        fx().fill = True
        out("[INFO DPL-0001] Placed 40 filler instances.")
        return ""
    if c == "write_def":
        path = unbrace(" ".join(w[1:]))
        f = fx()
        stage = {"init": "init", "floorplan": "floorplan", "place": "place", "cts": "cts", "route": "route"}.get(S["stage"], "init")
        if stage != "init" and not hasattr(f, "die"):
            f.plan()
        Path(path).write_text(f.def_text(stage))
        return ""
    if c in ("write_db", "write_verilog", "write_sdf", "write_spef", "write_sdc", "write_abstract_lef"):
        Path(unbrace(w[-1])).write_text(f"mock {c}\n")
        return ""
    if c == "report_checks" and "-format end" in line:
        out("Endpoint                                  Required    Arrival      Slack")
        out("-" * 74)
        import random as _r
        rr = _r.Random(9)
        for i in range(4):
            sl = 7.74 + i * 0.3
            out(f"_{10 + i}_/D (sky130_fd_sc_hd__dfxtp_1)       9.63       {9.63 - sl:.2f}       {sl:.2f} (MET)")
        for i in range(4):
            sl = 6.5 + rr.random() * 2
            out(f"q[{i}] (out)                               8.00       {8 - sl:.2f}       {sl:.2f} (MET)")
        return ""
    if c == "report_checks":
        if not S["clocks"]:
            out("No paths found.")
            return ""
        wns = {"init": "7.91", "floorplan": "7.91", "place": "7.80", "cts": "7.74", "route": "7.70"}.get(S["stage"], "7.9")
        txt = TIMING.replace("{ck}", "clkbuf_0_clk" if S["stage"] in ("cts", "route") else "clk").replace("{slack}", wns)
        if S["stage"] not in ("cts", "route"):
            txt = "\n".join(l for l in txt.splitlines() if "clkbuf" not in l)
        out(txt)
        return ""
    if c == "report_worst_slack":
        out("worst slack 7.74")
        return ""
    if c in ("report_check_types", "report_design_area", "report_clock_properties", "check_setup",
             "report_floating_nets", "check_antennas", "report_cell_usage", "report_tns", "report_wns"):
        out({"report_design_area": "Design area 204 u^2 51% utilization.",
             "report_power": "Group  Internal  Switching  Leakage  Total\nTotal  1.2e-05  4.1e-06  1.1e-10  1.6e-05 100.0%",
             "report_clock_properties": "Clock  Period  Waveform\nclk    10.000  0.000 5.000",
             "check_antennas": "[INFO ANT-0002] Found 0 net violations."}.get(c, ""))
        return ""
    if c in ("insert_tiecells", "extract_parasitics"):
        return ""
    if c == "exit":
        sys.exit(0)
    if c == "if":
        return ""
    if c == "llength":
        return "1" if S["clocks"] else "0"
    if c.startswith("sta::"):
        if not S["clocks"]:
            raise TclError("no clocks")
        return {"sta::worst_slack": "7.738" if "-max" in line else "0.214", "sta::total_negative_slack": "0.0",
                "sta::endpoint_violation_count": "0"}.get(c, "0")
    if c == "read_db" or c == "read_def":
        if not S["lefs"]:
            import os as _o
            base = Path(_o.environ.get("PDK_ROOT", "")) / "sky130A/libs.ref/sky130_fd_sc_hd"
            S["lefs"] = [str(base / "techlef/sky130_fd_sc_hd__nom.tlef"), str(base / "lef/sky130_fd_sc_hd.lef")]
            S["fix"] = None
        S.update(tech=True, linked=True, netlist=True, stage="route", top="count", clocks=1)
        f = fx(); f.plan(); f.cts = True; f.pdn = True; f.place()
        return ""
    if c == "set_power_activity" or c == "set_pdnsim_net_voltage" or c == "read_vcd":
        return ""
    if c == "analyze_power_grid":
        m = re.search(r"-voltage_file \{([^}]*)\}", line)
        f = fx()
        import random as _r
        rr = _r.Random(5)
        lines = ["Instance name, Voltage"]
        cx = (f.core[0] + f.core[2]) / 2
        for n, (mm, x, y, o) in f.pos.items():
            drop = 0.004 + 0.012 * (1 - abs(x - cx) / (f.core[2] - f.core[0])) + rr.random() * 0.001
            lines.append(f"{n}, {1.8 - drop:.5f}")
        if m:
            Path(m.group(1)).write_text("\n".join(lines) + "\n")
        out("########## IR report #################")
        out("Corner: default")
        out("Supply voltage   : 1.80e+00 V")
        out("Worstcase voltage: 1.78e+00 V")
        out("Average voltage  : 1.79e+00 V")
        out("Average IR drop  : 9.12e-03 V")
        out("Worstcase IR drop: 1.63e-02 V")
        out("######################################")
        return ""
    if c == "report_power" and "-instances" in line:
        f = fx()
        import random as _r
        rr = _r.Random(3)
        out("     Internal  Switching    Leakage      Total")
        out("        Power      Power      Power      Power (Watts)")
        out("-" * 60)
        for n, (mm, x, y, o) in f.pos.items():
            k = 3.0 if "df" in mm else (2.0 if "clkbuf" in mm else (0.05 if ("tap" in mm or "fill" in mm) else 1.0))
            a, b = rr.random() * 1e-6 * k, rr.random() * 4e-7 * k
            out(f" {a:.6e} {b:.6e} {1e-11:.6e} {a + b:.6e} {n}")
        return ""
    if c == "report_power":
        out("Group                  Internal  Switching    Leakage      Total")
        out("                          Power      Power      Power      Power (Watts)")
        out("----------------------------------------------------------------")
        out("Sequential             1.02e-05   1.10e-06   8.47e-11   1.13e-05  61.2%")
        out("Combinational          2.63e-06   2.19e-06   4.16e-11   4.82e-06  26.1%")
        out("Clock                  1.40e-06   9.10e-07   1.01e-11   2.31e-06  12.5%")
        out("Macro                  0.00e+00   0.00e+00   0.00e+00   0.00e+00   0.0%")
        out("Pad                    0.00e+00   0.00e+00   0.00e+00   0.00e+00   0.0%")
        out("----------------------------------------------------------------")
        out("Total                  1.42e-05   4.20e-06   1.36e-10   1.84e-05 100.0%")
        return ""
    if c == "read_spef":
        return ""
    if c == "namespace" or c == "proc":
        return ""
    out(f'invalid command name "{c}"')
    raise TclError(f'invalid command name "{c}"')


def run_script(script: str) -> str:
    if "namespace eval rf" in script:
        return ""
    res = ""
    for line in script.split("\n"):
        res = run_cmd(line)
    return res


def main():
    for raw in sys.stdin:
        m = PL_SENT.search(raw)
        if not m:
            if raw.strip() == "exit":
                return
            continue
        script = base64.b64decode(m.group(1)).decode()
        rid = m.group(2)
        try:
            val = run_script(script)
            rc = 0
        except TclError as e:
            val, rc = str(e), 1
        except Exception as e:   # mock bug → report as Tcl error, don't die
            val, rc = f"mock internal error: {e!r}", 1
        sys.stdout.write(f"\n<<RF_END {rid} {rc} {base64.b64encode(val.encode()).decode()}>>\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
