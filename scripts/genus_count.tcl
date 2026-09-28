# RTLFlow — Genus synthesis of exp_5/count (run from the exp_5 directory)
#   @genus:root: 1> source ~/VLSI/rtlflow2/scripts/genus_count.tcl
read_libs $env(HOME)/.volare/sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
read_hdl -sv count.sv
elaborate count
check_design
create_clock -name clk -period 10 [get_ports clk]
syn_generic
syn_map
syn_opt
report_area
report_timing
report_qor
write_design -innovus
