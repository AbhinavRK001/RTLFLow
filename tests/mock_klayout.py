#!/usr/bin/env python3
"""Mock KLayout batch DRC: writes a .lyrdb with a few markers (for pegasus GUI tests)."""
import sys, re
args = sys.argv[1:]
rd = {}
for i, a in enumerate(args):
    if a == "-rd":
        k, _, v = args[i + 1].partition("="); rd[k] = v
if "-v" in args:
    print("KLayout 0.29.0 (mock)"); sys.exit(0)
rep = rd.get("report")
if rep:
    items = [("met1.2", "Metal1 spacing &lt; 0.14um", (12.1, 14.2, 12.5, 14.6)),
             ("met1.2", "Metal1 spacing &lt; 0.14um", (17.3, 20.0, 17.6, 20.3)),
             ("li.3", "li1 spacing &lt; 0.17um", (9.2, 11.1, 9.5, 11.4)),
             ("met2.5", "Metal2 area &lt; 0.0676um^2", (20.1, 8.3, 20.4, 8.6))]
    cats = "".join(f"<category><name>'{n}'</name><description>'{d}'</description></category>" for n, d, _ in items)
    its = "".join(f"<item><category>'{n}'</category><cell>top</cell><values><value>box: ({b[0]},{b[1]};{b[2]},{b[3]})</value></values></item>"
                  for n, d, b in items)
    open(rep, "w").write(f"<?xml version='1.0'?><report-database><categories>{cats}</categories><items>{its}</items></report-database>")
    print("DRC finished (mock): 4 violations")
