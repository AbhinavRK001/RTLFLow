#!/usr/bin/env python3
"""Build a *correct* gate-level Yosys-JSON netlist of the exp_5 counter (sky130 cells).
q <= rst ? 0 : load ? d : en ? q+1 : q      — used by modus / genus GUI tests."""
import json, sys
P = "sky130_fd_sc_hd__"
bits = iter(range(2, 10000))
nb = lambda: next(bits)
ports = {"clk": {"direction": "input", "bits": [nb()]}, "rst": {"direction": "input", "bits": [nb()]},
         "en": {"direction": "input", "bits": [nb()]}, "load": {"direction": "input", "bits": [nb()]},
         "d": {"direction": "input", "bits": [nb() for _ in range(4)]}}
q = [nb() for _ in range(4)]
ports["q"] = {"direction": "output", "bits": q}
cells, names = {}, {"clk": ports["clk"]["bits"], "rst": ports["rst"]["bits"], "en": ports["en"]["bits"],
                    "load": ports["load"]["bits"], "d": ports["d"]["bits"], "q": q}
k = [10]
def cell(t, conns, dirs):
    n = f"_{k[0]}_"; k[0] += 1
    cells[n] = {"type": P + t, "port_directions": dirs, "connections": {p: [b] for p, b in conns.items()}}
    return n
def net(name):
    b = nb(); names[name] = [b]; return b
rstn = net("rstn"); cell("inv_1", {"A": ports["rst"]["bits"][0], "Y": rstn}, {"A": "input", "Y": "output"})
c1 = q[0]
c2 = net("c2"); cell("and2_1", {"A": q[0], "B": q[1], "X": c2}, {"A": "input", "B": "input", "X": "output"})
c3 = net("c3"); cell("and3_1", {"A": q[0], "B": q[1], "C": q[2], "X": c3}, {"A": "input", "B": "input", "C": "input", "X": "output"})
carry = [None, c1, c2, c3]
for i in range(4):
    if i == 0:
        t = net("t0"); cell("inv_1", {"A": q[0], "Y": t}, {"A": "input", "Y": "output"})
    else:
        t = net(f"t{i}"); cell("xor2_1", {"A": q[i], "B": carry[i], "X": t}, {"A": "input", "B": "input", "X": "output"})
    m1 = net(f"m1_{i}"); cell("mux2_1", {"A0": q[i], "A1": t, "S": ports["en"]["bits"][0], "X": m1},
                              {"A0": "input", "A1": "input", "S": "input", "X": "output"})
    m2 = net(f"m2_{i}"); cell("mux2_1", {"A0": m1, "A1": ports["d"]["bits"][i], "S": ports["load"]["bits"][0], "X": m2},
                              {"A0": "input", "A1": "input", "S": "input", "X": "output"})
    dn = net(f"dn{i}"); cell("and2_1", {"A": rstn, "B": m2, "X": dn}, {"A": "input", "B": "input", "X": "output"})
    cell("dfxtp_1", {"CLK": ports["clk"]["bits"][0], "D": dn, "Q": q[i]}, {"CLK": "input", "D": "input", "Q": "output"})
netnames = {n: {"bits": b, "hide_name": 0} for n, b in names.items()}
json.dump({"modules": {"count": {"ports": ports, "cells": cells, "netnames": netnames}}},
          open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/count_good.json", "w"), indent=1)
print(len(cells), "cells")
