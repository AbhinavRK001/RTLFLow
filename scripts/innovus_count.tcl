# RTLFlow — Innovus bring-up for the exp_5 `count` design (Sky130 HD)
#   innovus 1> source ~/VLSI/rtlflow2/scripts/innovus_count.tcl
# Stops after status + floorPlan + placeDesign so you can inspect the GUI.
set PDKLIB $env(HOME)/.volare/sky130A/libs.ref/sky130_fd_sc_hd

read_libs     $PDKLIB/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
read_physical -lef $PDKLIB/lef/sky130_fd_sc_hd.lef
read_netlist  $env(HOME)/Desktop/SYSTEM_VERILOG/LAB/exp_5/.rtlflow/runs/synth/count_mapped.v
set_top       count
init_design
create_clock -name clk -period 10 [get_ports clk]
status

floorPlan -r 1.0 0.5 5 5 5 5
addWellTap
globalNetConnect
sroute
placeDesign
report_timing
