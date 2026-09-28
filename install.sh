#!/usr/bin/env bash
# RTLFlow installer — links the launcher into ~/.local/bin and checks the environment.
#   ./install.sh                  → rtlflow on PATH
#   ./install.sh --cadence-names  → also genus / innovus / nclaunch / tempus / modus / voltus / pegasus / xrun / simvision
set -e
HERE="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
chmod +x "$HERE/bin/rtlflow"
TOOLS="genus innovus nclaunch xrun simvision tempus modus voltus pegasus"
for t in $TOOLS; do ln -sf rtlflow "$HERE/bin/$t"; done

ln -sf "$HERE/bin/rtlflow" "$BIN/rtlflow"
echo "  linked  $BIN/rtlflow -> $HERE/bin/rtlflow"
if [ "${1:-}" = "--cadence-names" ]; then
  for t in $TOOLS; do
    ln -sf "$HERE/bin/rtlflow" "$BIN/$t"; echo "  linked  $BIN/$t"
  done
fi

# desktop entry: "RTLFlow" in the GNOME app grid opens the launcher
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cat > "$APPS/rtlflow.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=RTLFlow EDA Suite
Comment=Launcher for nclaunch, genus, modus, tempus, innovus, voltus, pegasus
Exec=$HERE/bin/rtlflow -gui
Terminal=false
Categories=Development;Electronics;
DESK
echo "  desktop entry  $APPS/rtlflow.desktop  (search 'RTLFlow' in Activities)"

PY=/usr/bin/python3; command -v "$PY" >/dev/null || PY="$(command -v python3)"
echo "  python  $("$PY" --version 2>&1)  ($PY)"
if "$PY" -c "import tkinter; tkinter.Tcl()" 2>/dev/null; then
  echo "  tk      OK (GUIs enabled)"
else
  echo "  tk      MISSING -> GUIs disabled. Fix:  sudo apt install python3-tk"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) echo "  NOTE: add $BIN to PATH (e.g. in ~/.bashrc: export PATH=\"\$HOME/.local/bin:\$PATH\")";; esac

# clear stale tool cache from older versions, then discover once
rm -f "$HOME/.cache/rtlflow/tools.json"
PYTHONPATH="$HERE" "$PY" -m rtlflow tools
echo
echo "Done. Run:  rtlflow      (no nix-shell needed)"
