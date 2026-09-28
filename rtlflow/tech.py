"""Technology profiles — per-standard-cell-library implementation defaults.

A profile answers the questions a PnR flow needs that are not in the LEF:
placement site, IO pin layers, routing layer range, tap/tie/fill/clock cells,
track offsets, per-layer RC.  Anything a profile doesn't specify is derived
from the live OpenROAD database at run time (see innovus.py: _derive_*), so
unknown libraries still work — profiles only make known ones match their
reference flows (OpenLane / ORFS) exactly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class TechProfile:
    name: str
    cell_prefix: str
    site: str | None = None
    io_hlayer: str | None = None
    io_vlayer: str | None = None
    min_route: str | None = None
    max_route: str | None = None
    min_clk_route: str | None = None
    tap_cell: str | None = None
    tap_distance: float = 14.0
    tie_cell: tuple[str, str, str] | None = None      # (cell, hi_pin, lo_pin)
    fill_cells: list[str] = field(default_factory=list)
    clkbuf_regex: str = r"clk_?buf|ckbuf|CLKBUF|CKBUF|clkbuf"
    root_buf: str | None = None
    buf_cell: str | None = None
    dont_use: list[str] = field(default_factory=list)
    tracks: list[tuple[str, float, float, float, float]] = field(default_factory=list)
    layer_rc: list[tuple[str, float, float]] = field(default_factory=list)   # layer, cap, res
    via_rc: list[tuple[str, float]] = field(default_factory=list)
    wire_rc_signal: str | None = None
    wire_rc_clock: str | None = None
    power_nets: tuple[str, str] = ("VPWR", "VGND")
    well_nets: tuple[str, ...] = ("VPB", "VNB")
    rail_width: float | None = None          # follow-pin rail width (µm)


SKY130_HD = TechProfile(
    name="sky130_fd_sc_hd",
    cell_prefix="sky130_fd_sc_hd__",
    site="unithd",
    io_hlayer="met3", io_vlayer="met2",
    min_route="met1", max_route="met5", min_clk_route="met1",
    tap_cell="sky130_fd_sc_hd__tapvpwrvgnd_1", tap_distance=14.0,
    tie_cell=("sky130_fd_sc_hd__conb_1", "HI", "LO"),
    fill_cells=["sky130_fd_sc_hd__fill_1", "sky130_fd_sc_hd__fill_2",
                "sky130_fd_sc_hd__fill_4", "sky130_fd_sc_hd__fill_8"],
    root_buf="sky130_fd_sc_hd__clkbuf_16",
    buf_cell="sky130_fd_sc_hd__buf_4",
    dont_use=["*probe*", "*lpflow*", "*_bleeder*", "*__clkdlybuf*", "*__dlygate*",
              "*__dlymetal*", "*__macro_sparecell*"],
    tracks=[("li1", 0.23, 0.46, 0.17, 0.34), ("met1", 0.17, 0.34, 0.17, 0.34),
            ("met2", 0.23, 0.46, 0.23, 0.46), ("met3", 0.34, 0.68, 0.34, 0.68),
            ("met4", 0.46, 0.92, 0.46, 0.92), ("met5", 1.70, 3.40, 1.70, 3.40)],
    layer_rc=[("li1", 1.499e-04, 7.176e-02), ("met1", 1.72375e-04, 1.20565e-03),
              ("met2", 1.36233e-04, 1.22132e-03), ("met3", 2.14962e-04, 1.66281e-04),
              ("met4", 1.48128e-04, 1.68093e-04), ("met5", 1.54087e-04, 1.83558e-05)],
    via_rc=[("mcon", 9.249146e-3), ("via", 4.5e-3), ("via2", 3.368786e-3),
            ("via3", 0.376635e-3), ("via4", 0.00580e-3)],
    wire_rc_signal="met1", wire_rc_clock="met3",
    rail_width=0.48,
)

# Other sky130 flavours share the metal stack; only the site/cells differ.
SKY130_HDLL = TechProfile(**{**SKY130_HD.__dict__, "name": "sky130_fd_sc_hdll",
                             "cell_prefix": "sky130_fd_sc_hdll__", "site": "unithddbl",
                             "tap_cell": "sky130_fd_sc_hdll__tapvpwrvgnd_1",
                             "tie_cell": ("sky130_fd_sc_hdll__conb_1", "HI", "LO"),
                             "fill_cells": [f"sky130_fd_sc_hdll__fill_{i}" for i in (1, 2, 4, 8)],
                             "root_buf": "sky130_fd_sc_hdll__clkbuf_16",
                             "buf_cell": "sky130_fd_sc_hdll__buf_4"})
SKY130_HS = TechProfile(**{**SKY130_HD.__dict__, "name": "sky130_fd_sc_hs",
                           "cell_prefix": "sky130_fd_sc_hs__", "site": "unit",
                           "tap_cell": "sky130_fd_sc_hs__tapvpwrvgnd_1",
                           "tie_cell": ("sky130_fd_sc_hs__conb_1", "HI", "LO"),
                           "fill_cells": [f"sky130_fd_sc_hs__fill_{i}" for i in (1, 2, 4, 8)],
                           "root_buf": "sky130_fd_sc_hs__clkbuf_16",
                           "buf_cell": "sky130_fd_sc_hs__buf_4"})

PROFILES = [SKY130_HD, SKY130_HDLL, SKY130_HS]

GENERIC = TechProfile(name="generic", cell_prefix="")


def match_profile(text: str) -> TechProfile:
    """Pick a profile from a library/LEF path or a cell name."""
    for p in PROFILES:
        if p.name in text or p.cell_prefix in text:
            return p
    return GENERIC


def matches_any(name: str, patterns: list[str]) -> bool:
    for pat in patterns:
        rx = "^" + re.escape(pat).replace(r"\*", ".*").replace(r"\?", ".") + "$"
        if re.match(rx, name):
            return True
    return False
