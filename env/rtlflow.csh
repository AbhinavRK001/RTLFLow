# RTLFlow — optional C-shell setup (traditional EDA-style).  NOT required:
# bin/rtlflow discovers tools & PDK on its own.  Use this only if you want
# `genus` / `innovus` / `nclaunch` directly on PATH in csh/tcsh.
#     source ~/VLSI/rtlflow2/env/rtlflow.csh
if ( ! $?RTLFLOW_HOME ) setenv RTLFLOW_HOME "$HOME/VLSI/rtlflow2"
setenv PATH "${RTLFLOW_HOME}/bin:${PATH}"
if ( ! $?PDK_ROOT && -d "$HOME/.volare" ) setenv PDK_ROOT "$HOME/.volare"
if ( ! $?PDK ) setenv PDK sky130A
if ( ! $?STD_CELL_LIBRARY ) setenv STD_CELL_LIBRARY sky130_fd_sc_hd
