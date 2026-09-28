"""KLayout integration: DEF→GDS streaming (streamOut) and layout viewing (view_gds)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .. import env
from .runner import run

_DEF2GDS = r'''
import pya, sys
# rd: def_file, lef_files (;-sep), gds_out, top, merge (;-sep), lyt, map_file
lefs = [x for x in lef_files.split(";") if x]
merges = [x for x in merge.split(";") if x]
opts = pya.LoadLayoutOptions()
if lyt:
    tech = pya.Technology()
    tech.load(lyt)
    opts = tech.load_layout_options
cfg = opts.lefdef_config
cfg.lef_files = lefs
try:
    cfg.macro_resolution_mode = 1
except Exception:
    pass
if map_file:
    cfg.map_file = map_file
ly = pya.Layout()
ly.read(def_file, opts)
top_cell = ly.cell(top)
if top_cell is None:
    print("ERROR: top cell %s not found in DEF" % top); sys.exit(2)
# drop LEF abstracts so real std-cell GDS geometry replaces them
for c in ly.each_cell():
    if c.cell_index() != top_cell.cell_index() and not c.name.startswith("VIA") and "_VIA" not in c.name:
        c.clear()
for g in merges:
    ly.read(g)
ly.write(gds_out)
print("GDS_OK %s" % gds_out)
'''


def def2gds(def_file: Path, lefs: list[str], gds: Path, top: str, merge: list[str] | None = None,
            map_file: str | None = None) -> tuple[bool, str]:
    exe = env.find_tool("klayout")
    if not exe:
        return False, "KLayout not found — needed for streamOut"
    p = env.pdk()
    merges = merge or ([str(p.cell_gds())] if p.cell_gds() else [])
    if not merges:
        return False, "No standard-cell GDS found to merge (pass -merge {cells.gds})"
    lyt = str(p.klayout_tech()) if p.klayout_tech() else ""
    if not map_file and p.pdk_dir:
        maps = sorted((p.pdk_dir / "libs.tech" / "klayout").rglob("*.map"))
        map_file = str(maps[0]) if maps else ""
    script = gds.parent / "rtlflow_def2gds.py"
    gds.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(_DEF2GDS)
    cmd = [exe, "-zz", "-rd", f"def_file={def_file}", "-rd", f"lef_files={';'.join(lefs)}",
           "-rd", f"gds_out={gds}", "-rd", f"top={top}", "-rd", f"merge={';'.join(merges)}",
           "-rd", f"lyt={lyt}", "-rd", f"map_file={map_file or ''}", "-r", str(script)]
    r = run(cmd, show=lambda l: "ERROR" in l or "GDS_OK" in l)
    if r.ok and gds.exists():
        return True, str(gds)
    return False, "KLayout DEF→GDS failed:\n" + "\n".join(r.lines[-15:])


def view(path: Path) -> bool:
    exe = env.find_tool("klayout")
    if not exe:
        return False
    p = env.pdk()
    args = [exe, str(path)]
    if p.klayout_props():
        args += ["-l", str(p.klayout_props())]
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True
