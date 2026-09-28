#!/usr/bin/env python3
"""Mock OpenSTA CLI (`sta -no_init -no_splash -exit script.tcl`) for genus plumbing tests."""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
if "-version" in sys.argv:
    print("2.6.0-mock"); sys.exit(0)
script = Path(sys.argv[-1]).read_text()
import mock_openroad as M   # reuse canned timing report
for line in script.splitlines():
    s = line.strip()
    m = re.match(r'puts "RF_(\w+) ', s)
    if m:
        print(f"RF_{m.group(1)} " + {"WNS": "7.738", "WHS": "0.214", "TNS": "0.0", "VIOL": "0"}[m.group(1)])
    elif "report_checks" in s and not s.startswith("}"):
        print(M.TIMING.replace("{ck}", "clk").replace("{slack}", "7.74")
              .replace("                  0.05    0.12    0.12 ^ clk/X (sky130_fd_sc_hd__clkbuf_4)\n", ""))
        break
    elif s.startswith("report_power"):
        print("Group                  Internal  Switching    Leakage      Total\n"
              "Sequential             1.02e-05   1.10e-06   8.47e-11   1.13e-05  70.1%\n"
              "Combinational          2.63e-06   2.19e-06   4.16e-11   4.82e-06  29.9%\n"
              "Total                  1.28e-05   3.29e-06   1.26e-10   1.61e-05 100.0%")
