#!/usr/bin/env python3
"""Mock `yosys` for plumbing tests of the genus shell (NOT a synthesiser).

Understands `yosys -q -l LOG -s SCRIPT`; writes the files the script asks for
(RTLIL checkpoints, netlist, JSON, stat reports) with canned-but-realistic
content for the `count` design (numbers match the real RTLFlow 2.3 run).
"""

import re
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

STAT_MAPPED = """
=== count ===

        +----------Local Count, excluding submodules.
        |
       25 wires
       31 wire bits
        6 ports
       12 port bits
       23 cells
        4   sky130_fd_sc_hd__dfxtp_1
        3   sky130_fd_sc_hd__mux2_1
        3   sky130_fd_sc_hd__xor2_1
        1   sky130_fd_sc_hd__a21oi_1
        1   sky130_fd_sc_hd__a22o_1
        1   sky130_fd_sc_hd__and2_0
        1   sky130_fd_sc_hd__and3_1
        1   sky130_fd_sc_hd__buf_1
        1   sky130_fd_sc_hd__inv_1
        1   sky130_fd_sc_hd__mux2i_1
        1   sky130_fd_sc_hd__nand2_1
        1   sky130_fd_sc_hd__nand3_1
        1   sky130_fd_sc_hd__nor2_1
        1   sky130_fd_sc_hd__o21a_1
        1   sky130_fd_sc_hd__o21ai_0
        1   sky130_fd_sc_hd__xnor2_1

   Chip area for module '\\count': 203.945600
     of which used for sequential elements: 80.076800 (39.26%)
"""
STAT_GENERIC = """
=== count ===

   Number of wires:                 31
   Number of wire bits:             52
   Number of public wires:           6
   Number of ports:                  6
   Number of port bits:             12
   Number of cells:                 27
     $_AND_                          6
     $_DFF_P_                        4
     $_MUX_                          7
     $_XOR_                          4
     $_NOT_                          6
"""


def main():
    args = sys.argv[1:]
    if "-V" in args:
        print("Yosys 0.52 (mock for RTLFlow tests)")
        return 0
    log = args[args.index("-l") + 1] if "-l" in args else None
    script = Path(args[args.index("-s") + 1]).read_text()
    L = []
    for line in script.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            w = shlex.split(line)
        except ValueError:
            w = line.split()
        c = w[0]
        L.append(f"\n-- Executing: {line}")
        if c == "read_verilog":
            f = w[-1]
            txt = Path(f).read_text(errors="replace")
            if "syntax_error_here" in txt:
                L.append(f"ERROR: syntax error in {f}")
                Path(log).write_text("\n".join(L)) if log else None
                print(L[-1])
                return 1
            for m in re.findall(r"^\s*module\s+(\w+)", txt, re.M):
                L.append(f"Generating RTLIL representation for module `\\{m}'.")
        elif c == "hierarchy" and "-top" in w:
            top = w[w.index("-top") + 1]
            L.append(f"Top module:  \\{top}")
            L.append(f"Used module:     \\{top}")
        elif c in ("write_rtlil", "write_verilog", "write_json"):
            out = Path(w[-1])
            if c == "write_rtlil":
                out.write_text("# mock rtlil\n")
            elif c == "write_verilog":
                src = HERE.parent / "tests" / "_count_mapped.v"
                out.write_text(src.read_text() if src.exists() else "module count(); endmodule\n")
            else:
                j = Path("/tmp/count.json")
                out.write_text(j.read_text() if j.exists() else '{"modules":{}}')
        elif c == "tee" and "-o" in w:
            out = Path(w[w.index("-o") + 1])
            mapped = "-liberty" in line
            out.write_text(STAT_MAPPED if mapped else STAT_GENERIC)
            L.append(out.read_text())
        elif c == "check":
            L.append("Found and reported 0 problems.")
    if log:
        Path(log).write_text("\n".join(L) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
