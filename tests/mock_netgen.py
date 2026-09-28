#!/usr/bin/env python3
"""Mock netgen batch LVS: writes a matching report."""
import re, sys
s = open(sys.argv[-1]).read()
m = re.search(r'lvs .* \{[^}]*\} \{([^}]*)\}', s)
rep = "\n".join(["Circuit 1 cell sizes and Circuit 2 cell sizes match.", "Netlists match uniquely.",
                 "Final result: Circuits match uniquely.", ""])
if m:
    open(m.group(1), "w").write(rep)
print("Final result: Circuits match uniquely.")
