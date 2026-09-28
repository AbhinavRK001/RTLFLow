# RTLFlow — optional bash/zsh setup. NOT required (bin/rtlflow self-discovers).
#     source ~/VLSI/rtlflow2/env/rtlflow.sh
export RTLFLOW_HOME="${RTLFLOW_HOME:-$HOME/VLSI/rtlflow2}"
case ":$PATH:" in *":$RTLFLOW_HOME/bin:"*) ;; *) export PATH="$RTLFLOW_HOME/bin:$PATH";; esac
[ -z "$PDK_ROOT" ] && [ -d "$HOME/.volare" ] && export PDK_ROOT="$HOME/.volare"
export PDK="${PDK:-sky130A}"
export STD_CELL_LIBRARY="${STD_CELL_LIBRARY:-sky130_fd_sc_hd}"
