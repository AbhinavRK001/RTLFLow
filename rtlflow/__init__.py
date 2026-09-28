"""RTLFlow — a Cadence/Synopsys-style EDA suite on open-source backends.

    rtlflow ─┬─ nclaunch (Verilator / Icarus)     simulation + waveform viewer
             ├─ genus    (Yosys + OpenSTA)        synthesis + schematic
             ├─ modus    (RTLFlow ATPG engine)    scan insertion + stuck-at ATPG
             ├─ tempus   (OpenSTA)                signoff timing
             ├─ innovus  (OpenROAD)               implementation ⇄ layout GUI
             ├─ voltus   (OpenSTA power, PDNSim)  power + IR drop
             └─ pegasus  (KLayout/Magic, Netgen)  DRC + LVS
    rtlflow -gui → launcher window to pick any of them
"""

VERSION = "2.7.2"
CODENAME = "Signoff"
BUILD_DATE = "2026-09-28"

__version__ = VERSION
