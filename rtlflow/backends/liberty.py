"""Liberty helpers: lightweight scans used by genus (tie cells, buffers, units)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=8)
def scan(path: str) -> dict:
    """Return {'cells': {name: {'area': a, 'pins': {pin: (dir, func)}}}, 'time_unit': '1ns'}."""
    txt = Path(path).read_text(errors="replace")
    info = {"cells": {}, "time_unit": "1ns"}
    m = re.search(r'time_unit\s*:\s*"?([0-9.]+\s*[a-z]+)"?', txt)
    if m:
        info["time_unit"] = m.group(1).replace(" ", "")
    for cm in re.finditer(r'\n\s*cell\s*\(\s*"?([\w$]+)"?\s*\)\s*\{', txt):
        name = cm.group(1)
        start = cm.end()
        # find the matching brace (cells can be large; scan quickly)
        depth, i = 1, start
        n = len(txt)
        while i < n and depth:
            c = txt[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        body = txt[start:i]
        am = re.search(r"\barea\s*:\s*([0-9.eE+-]+)", body)
        pins = {}
        pm_list = list(re.finditer(r'\bpin\s*\(\s*"?([\w$\[\]]+)"?\s*\)\s*\{', body))
        tc = body.find("test_cell")
        for k, pm in enumerate(pm_list):
            if tc >= 0 and pm.start() > tc:
                break                       # skip the test_cell's duplicate pin list
            end = pm_list[k + 1].start() if k + 1 < len(pm_list) else len(body)
            seg = body[pm.end():min(end, pm.end() + 6000)]
            dm = re.search(r"\bdirection\s*:\s*\"?(\w+)", seg)
            fm = re.search(r'(?<![\w])function\s*:\s*"([^"]*)"', seg)
            if pm.group(1) not in pins:
                pins[pm.group(1)] = (dm.group(1) if dm else "", fm.group(1) if fm else "")
        ff = None
        fm2 = re.search(r'\b(ff|latch)\s*\(\s*"?(\w+)"?\s*,\s*"?(\w+)"?\s*\)\s*\{([^}]*)\}', body)
        if fm2 and (tc < 0 or fm2.start() < tc):
            g = fm2.group(4)

            def attr(n):
                m3 = re.search(n + r'\s*:\s*"([^"]*)"', g)
                return m3.group(1) if m3 else ""
            ff = {"kind": fm2.group(1), "q": fm2.group(2), "qn": fm2.group(3),
                  "next": attr("next_state") or attr("data_in"), "clock": attr("clocked_on") or attr("enable"),
                  "clear": attr("clear"), "preset": attr("preset")}
        info["cells"][name] = {"area": float(am.group(1)) if am else 0.0, "pins": pins,
                               "ff": fm2 is not None and fm2.group(1) == "ff",
                               "latch": fm2 is not None and fm2.group(1) == "latch", "seq": ff}
    return info


def tie_cells(path: str) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    """(hi_cell, hi_pin), (lo_cell, lo_pin) — smallest-area cells whose output function is constant."""
    cells = scan(path)["cells"]
    hi, lo = [], []
    for name, c in cells.items():
        for pin, (d, f) in c["pins"].items():
            if d == "output" and f.strip() == "1":
                hi.append((c["area"], name, pin))
            elif d == "output" and f.strip() == "0":
                lo.append((c["area"], name, pin))
    hi.sort(); lo.sort()
    return ((hi[0][1], hi[0][2]) if hi else None, (lo[0][1], lo[0][2]) if lo else None)


def buffer_cell(path: str) -> tuple[str, str, str] | None:
    """Smallest non-clock buffer: (cell, in_pin, out_pin)."""
    cells = scan(path)["cells"]
    best = None
    for name, c in cells.items():
        if re.search(r"(?i)clk|dly|probe|lpflow", name):
            continue
        ins = [p for p, (d, f) in c["pins"].items() if d == "input"]
        outs = [(p, f) for p, (d, f) in c["pins"].items() if d == "output"]
        if len(ins) == 1 and len(outs) == 1 and outs[0][1].strip().strip("()") == ins[0]:
            if best is None or c["area"] < best[0]:
                best = (c["area"], name, ins[0], outs[0][0])
    return best[1:] if best else None


def seq_cells(path: str) -> set[str]:
    return {n for n, c in scan(path)["cells"].items() if c["ff"] or c["latch"]}
