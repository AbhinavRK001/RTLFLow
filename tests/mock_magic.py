#!/usr/bin/env python3
"""Mock magic: runs the extraction script's ext2spice target (for pegasus LVS plumbing)."""
import re, sys
script = open(sys.argv[-1]).read()
m = re.search(r"ext2spice -o \{([^}]*)\}", script)
if m:
    open(m.group(1), "w").write("* mock extracted netlist\n.subckt count clk rst\n.ends\n")
m = re.search(r"set fp \[open \{([^}]*)\} w\]", script)
if m:
    open(m.group(1), "w").write("VIOLATION\tLocal interconnect spacing < 0.17um (li.3)\t9.2\t11.1\t9.5\t11.4\n")
