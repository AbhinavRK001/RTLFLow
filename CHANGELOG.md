# Changelog

## 2.7.2 — 2026-09-28 (HiDPI: no more half-cut text)
- **Text clipped in every GUI on scaled / HiDPI displays**: Tk scales fonts with the display DPI but
  row heights, paddings and canvas geometry were fixed pixel values. `gui/theme.py` now measures the
  real font (`linespace`) and derives a scale factor used everywhere:
  - Treeview row height = font height + padding (fault browser, path/instance lists, design browser…)
  - Treeview column widths, button/tab/heading paddings, window size (clamped to the screen)
  - canvas drawings: chart margins and tick density (no overlapping axis labels), Tempus stage view
    (label column measured from the text), Voltus bars/legend, Modus scan-chain boxes sized to the
    cell names, waveform rows/name column, Innovus legend, Genus schematic text sized with the zoom
  - side panels (Pegasus, NCLaunch) keep their width; Innovus command line can no longer be pushed off-screen
  - override with `export RTLFLOW_GUI_SCALE=1.5` (or any factor) if you want bigger/smaller GUIs
- `simvision` with the NCLaunch GUI open and no GTKWave/Surfer installed loads the VCD into the built-in viewer.
- `modus connect_scan_chains` on a combinational design (e.g. alu4) prints a clear note instead of
  an internal error (RFM-INT-999).
- `innovus -gui` accepted (same as the default GUI mode).
- `bin/rtlflow` sets `PYTHONSAFEPATH=1` so a stray `rtlflow/` folder in the current directory can't
  shadow the package (`No module named rtlflow.__main__`).

## 2.7.1 — 2026-09-28 (fixes from the first real run on exp_2)
- **genus elaborate failed** with `Can't create file …elab.stat.txt`: Yosys passes (tee, abc, dfflibmap)
  don't strip quotes the way frontends do, so quoted paths broke `tee -o`, `-liberty` and `abc -script`.
  Paths are now written bare (quoted only if they contain spaces).
- **`clear` in nclaunch wiped your sources**: a method-name clash bound `clear` to `clear_sources`.
  Commands are now bound to the function that declares them; `clear` clears the screen again.
- `exit` / `quit` typed in a GUI's command line now closes that GUI only (was silently ignored).
- `read_tb -sv file` accepted; the testbench you read last becomes the simulation top.
- `elaborate <top>` (nclaunch and genus) checks the module exists and lists the available ones.
- Genus GUI: File → Read Libraries / Read HDL / Elaborate dialogs, toolbar buttons; the flow chips
  "Read HDL" and "Elaborate" open those dialogs.
- Yosys error lines are no longer printed twice.

## 2.7.0 "Signoff" — 2026-09-28

Every tool now has a shell **and** a GUI on the same session, plus a launcher to pick them.

### New tools
- **tempus** — persistent OpenSTA session: `read_design -from genus|innovus`, `report_analysis_summary`
  (setup/hold WNS/TNS/violations), `report_timing`, `report_slack_histogram`, `check_timing`,
  `report_constraints`, `write_sdf`. GUI: summary chips, endpoint slack histogram, path browser,
  per-stage delay view.
- **voltus** — OpenROAD session on the innovus checkpoint: `set_power_activity`, `read_activity_file`
  (VCD from nclaunch), `report_power [-instances]`, `analyze_rail` (PDNSim IR drop). GUI: power
  breakdown, per-instance power map and IR-drop map on the real layout.
- **modus** — RTLFlow ATPG engine: liberty-function gate model, `verify_test_structures`,
  `connect_scan_chains` (flop → scan flop, stitched chains, writes `<top>_scan.v`),
  `build_faultmodel`, `create_logic_tests` (random bit-parallel fault simulation + PODEM with
  redundancy proof), `report_fault_statistics`, `write_vectors`. GUI: coverage curve, fault
  browser, scan-chain diagram.
- **pegasus** — `run_drc` (KLayout PDK deck or Magic), `run_lvs` (Magic extraction + Netgen),
  `report_drc`, `view_results`. GUI: violation tree, markers on the layout, LVS verdict/report.

### GUIs
- **Launcher** (`rtlflow -gui`, `rtlflow> gui`, app-grid icon): tool cards with engine status and
  project progress; Launch = terminal shell + GUI, Terminal only = shell.
- **nclaunch GUI**: source manager (design/testbench roles, sim top, engine), compile/elaborate/
  simulate buttons, built-in VCD waveform viewer (buses, cursor, zoom), GTKWave button.
- Shared GUI base (`gui/base.py`) and shell mixin (`core/guishell.py`): same busy/idle, `[GUI]` echo,
  transcript + command line in every tool.

### Other
- innovus writes `<top>.pnr.v` after routing and a DEF next to each `saveDesign` checkpoint.
- Liberty scanner reads `ff`/`latch` groups (next_state, clear, preset) and ignores test_cell pins.
- `install.sh` links the new tool names and installs a desktop entry.
- Fixed: GUI start failures were misreported as "Tk missing".

## 2.6.0 "Stylus" — 2026-09-28

Professional front end, Cadence-style GUI launching, same architecture as 2.5.1
(one persistent OpenROAD session, subprocess+socketpair GUI, self-contained env).

### Suite
- ASCII banner, discovery table with versions, `tools -v`, `doctor` (runs each
  tool headless, checks Tk, display and PDK files), `status` across tools.
- Cadence-style `<tool>.logN` / `<tool>.cmdN` per session in `.rtlflow/logs`.
- Numbered prompts: `innovus 3>`, `@genus:root: 7>`, `nclaunch 2>`.
- Tcl-ish shell: braces, quotes, `$var`/`$env(X)`, `;`, continuation lines,
  `source`, `set`/`puts`, `report_x > file` redirection, `man`, readline history
  and tab completion.
- `bin/` wrappers: `genus`, `innovus`, `nclaunch`, `xrun`, `simvision`;
  `install.sh --cadence-names`.

### innovus
- GUI opens with `innovus`; `-nowin`, `win`, `win off`, `gui_show/gui_hide`.
- Terminal ⇄ GUI: busy/idle mirroring, `[GUI] cmd` echo, GUI transcript +
  command line on the same session.
- Tech LEF auto-detection (fixes ORD-0050 when only the cell LEF is given).
- Innovus init vars (`init_lef_file`, `init_verilog`, `init_top_cell`,
  `init_mmmc_file`), `freeDesign`, `saveDesign`/`restoreDesign` (write_db/read_db).
- New: `addWellTap`, `globalNetConnect`, `addStripe`, `sroute` (pdngen),
  `assignIoPins`, `refinePlace`, `checkPlace`, `addTieHiLo`, `addFiller`,
  `earlyGlobalRoute`, `optDesign`, `timeDesign` (Innovus summary table + reports),
  `extractRC` (OpenRCX when rules exist), `write_sdf`, `write_lef_abstract`,
  `streamOut` (KLayout DEF→GDS merge), `view_gds`, `verify_antenna`,
  `verify_connectivity`, `checkDesign`, `tech_profile`.
- Tech profiles (`rtlflow/tech.py`) replace hard-coded Sky130 values; CTS
  buffers are now discovered from the loaded libraries (regex + dont_use).
- Unknown commands go straight to the OpenROAD Tcl interpreter (full Tcl).
- Timing: WNS/TNS from OpenSTA `worst_slack`/`total_negative_slack`
  (no report parsing) — fixes the 2.4 report_qor mismatch.

### innovus GUI
- Rewritten: dark EDA theme, menus (File/Edit/View/Floorplan/Power/Place/Clock/
  Route/Timing/Verify/Tools/Help), toolbar, flow bar, Layers/Design/Attributes
  panels, world view, status bar, transcript.
- Floorplan / Amoeba / Physical views; per-layer visibility; LEF pin shapes;
  orientation marks; unplaced-instance parking; clock-tree flylines; DRC markers;
  ruler; Timing Debug window with path highlighting; screenshot (`gui_dump`).
- LEF parser fixes (PROPERTYDEFINITIONS / VIA blocks, layer WIDTH).

### genus
- Stage chain via RTLIL checkpoints; `syn_opt` = delay-driven ABC re-map kept
  only when QoR improves; tie cells / buffers found from the liberty.
- `report_area`, `report_gates`, `report_timing`, `report_qor`, `report_power`,
  `report_hierarchy`, `report_clocks`, SDC commands, `write_hdl`, `write_sdc`,
  `write_design -innovus` (handoff + reference flow), `set_db`/`get_db`.
- Genus GUI (`gui_show`, `genus -gui`): schematic from the real Yosys JSON,
  design browser, report tabs, find, critical-path highlight.

### nclaunch
- `ncvlog/ncelab/ncsim` aliases, `xrun` one-shot, automatic waveform dump
  injection, `simvision` (GTKWave/Surfer), `-gui` on simulate/xrun.

## 2.5.1
- Self-contained environment discovery (no nix-shell); subprocess+socketpair GUI.
