"""RTLFlow Launcher — the suite's front door (like Cadence's tool launcher).

    $ rtlflow -gui          or   rtlflow> gui

A window with one card per tool. Each card shows whether its engine was found
and what the project has done so far; "Launch" opens the tool's terminal shell
in a new terminal window with its GUI attached, "Terminal" opens the shell only.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import theme as T

try:
    from .. import VERSION, env
except Exception:  # pragma: no cover
    VERSION, env = "?", None

TOOLS = [
    # key, title, stage in flow, what it does, engines, commercial role, project key
    ("nclaunch", "NCLaunch", "1 · Simulate", "RTL simulation, waveforms", ["verilator", "iverilog"], "Xcelium / NCLaunch", "sim"),
    ("genus", "Genus", "2 · Synthesize", "Logic synthesis, schematic", ["yosys"], "Genus", "genus"),
    ("modus", "Modus", "3 · DFT / ATPG", "Scan insertion, stuck-at ATPG", ["yosys"], "Modus", "modus"),
    ("tempus", "Tempus", "4 · Timing", "Signoff STA, path analysis", ["sta", "openroad"], "Tempus", "tempus"),
    ("innovus", "Innovus", "5 · Implement", "Floorplan, place, CTS, route", ["openroad"], "Innovus", "innovus"),
    ("voltus", "Voltus", "6 · Power / IR", "Power, power map, IR drop", ["openroad"], "Voltus", "voltus"),
    ("pegasus", "Pegasus", "7 · Signoff", "DRC and LVS", ["klayout", "magic", "netgen"], "Pegasus", "pegasus"),
]
ACCENTS = {"nclaunch": "#58a6ff", "genus": "#d2a8ff", "modus": "#7ee2a0", "tempus": "#e3b341",
           "innovus": "#4c9dff", "voltus": "#ff7b72", "pegasus": "#39c5cf"}
TERMINALS = [("ptyxis", ["--new-window", "--"]), ("gnome-terminal", ["--"]), ("kgx", ["-e"]), ("konsole", ["-e"]),
             ("xfce4-terminal", ["-x"]), ("tilix", ["-e"]), ("alacritty", ["-e"]), ("kitty", []),
             ("wezterm", ["start", "--"]), ("xterm", ["-e"]), ("x-terminal-emulator", ["-e"])]


def find_terminal() -> tuple[str, list[str]] | None:
    pref = os.environ.get("RTLFLOW_TERMINAL")
    if pref:
        parts = shlex.split(pref)
        return parts[0], parts[1:]
    for exe, pre in TERMINALS:
        p = shutil.which(exe)
        if p:
            return p, pre
    return None


def rtlflow_cmd() -> list[str]:
    home = os.environ.get("RTLFLOW_HOME")
    if home and Path(home, "bin", "rtlflow").exists():
        return [str(Path(home, "bin", "rtlflow"))]
    return [sys.executable, "-m", "rtlflow"]


class Launcher:
    def __init__(self, root: tk.Tk, project: str):
        self.root = root
        self.project = Path(project).resolve()
        self.fonts = T.apply(root)
        root.title(f"RTLFlow EDA Suite {VERSION}")
        root.geometry("1180x760")
        root.minsize(900, 600)
        tools_found = {}
        if env:
            try:
                env.discover(versions=False)
                tools_found = {n: t for n, t in env.tool_status()}
            except Exception:
                pass
        self.found = tools_found
        # header
        hdr = tk.Frame(root, bg="#12151a", padx=22, pady=16)
        hdr.pack(fill="x")
        tk.Label(hdr, text="RTLFlow", bg="#12151a", fg="#ffffff", font=(self.fonts["ui"][0], 26, "bold")).pack(side="left")
        tk.Label(hdr, text=f"  EDA Suite {VERSION}", bg="#12151a", fg=T.FG_DIM, font=(self.fonts["ui"][0], 14)).pack(side="left", pady=(10, 0))
        right = tk.Frame(hdr, bg="#12151a")
        right.pack(side="right")
        tk.Label(right, text="PROJECT", bg="#12151a", fg=T.FG_DIM, font=self.fonts["small"]).pack(anchor="e")
        prow = tk.Frame(right, bg="#12151a")
        prow.pack(anchor="e")
        self.proj_lbl = tk.Label(prow, text="", bg="#12151a", fg=T.FG, font=self.fonts["mono"])
        self.proj_lbl.pack(side="left")
        ttk.Button(prow, text="Change...", style="Tool.TButton", command=self.pick_project).pack(side="left", padx=(8, 0))
        # cards
        body = ttk.Frame(root, padding=(16, 12))
        body.pack(fill="both", expand=True)
        self.cards = {}
        for i, tool in enumerate(TOOLS):
            card = self._card(body, tool)
            card.grid(row=i // 4, column=i % 4, sticky="nsew", padx=8, pady=8)
            body.columnconfigure(i % 4, weight=1)
        body.rowconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        # utilities
        util = tk.Frame(body, bg=T.BG2, padx=16, pady=14)
        util.grid(row=1, column=3, sticky="nsew", padx=8, pady=8)
        tk.Label(util, text="Utilities", bg=T.BG2, fg=T.FG, font=(self.fonts["ui"][0], 14, "bold")).pack(anchor="w")
        for text, cmd in (("rtlflow> shell", lambda: self.launch(None)), ("SimVision (waves)", lambda: self.launch("simvision", term=False)),
                          ("KLayout", lambda: self.run_detached(["klayout"])), ("Environment doctor", lambda: self.launch("doctor")),
                          ("User guide (README)", self.open_readme)):
            ttk.Button(util, text=text, command=cmd).pack(fill="x", pady=3)
        # status bar
        sb = ttk.Frame(root, style="Panel.TFrame")
        sb.pack(side="bottom", fill="x")
        term = find_terminal()
        self.st = ttk.Label(sb, text=f"Terminal: {Path(term[0]).name if term else 'none found (set RTLFLOW_TERMINAL)'}",
                            style="Status.TLabel")
        self.st.pack(side="left", fill="x", expand=True)
        self.refresh()

    def _card(self, parent, tool):
        key, title, stage, what, engines, role, pkey = tool
        acc = ACCENTS[key]
        f = tk.Frame(parent, bg=T.BG2, highlightthickness=1, highlightbackground=T.LINE, padx=16, pady=12)
        tk.Frame(f, bg=acc, height=3).pack(fill="x", pady=(0, 10))
        tk.Label(f, text=stage.upper(), bg=T.BG2, fg=acc, font=self.fonts["small"]).pack(anchor="w")
        tk.Label(f, text=title, bg=T.BG2, fg="#ffffff", font=(self.fonts["ui"][0], 18, "bold")).pack(anchor="w")
        tk.Label(f, text=what, bg=T.BG2, fg=T.FG_DIM, font=self.fonts["ui"], wraplength=220, justify="left").pack(anchor="w", pady=(2, 8))
        have = [e for e in engines if self.found.get(e) and self.found[e].ok]
        miss = [e for e in engines if e not in have]
        if key == "modus":
            have = ["RTLFlow ATPG"] + have
        erow = tk.Frame(f, bg=T.BG2)
        erow.pack(anchor="w")
        if have:
            tk.Label(erow, text="engines: " + ", ".join(have), bg=T.BG2, fg=T.OK, font=self.fonts["small"]).pack(side="left")
        if miss:
            tk.Label(erow, text=("  missing: " if have else "missing: ") + ", ".join(miss), bg=T.BG2, fg=T.ERR,
                     font=self.fonts["small"]).pack(side="left")
        st = tk.Label(f, text="", bg=T.BG2, fg="#9ecbff", font=self.fonts["small"], wraplength=230, justify="left")
        st.pack(anchor="w", pady=(4, 10))
        row = tk.Frame(f, bg=T.BG2)
        row.pack(fill="x", side="bottom")
        ttk.Button(row, text="Launch", style="Accent.TButton", command=lambda: self.launch(key, gui=True)).pack(side="left")
        ttk.Button(row, text="Terminal only", command=lambda: self.launch(key, gui=False)).pack(side="left", padx=6)
        self.cards[key] = (st, pkey)
        return f

    def refresh(self):
        self.proj_lbl.configure(text=str(self.project).replace(str(Path.home()), "~"))
        try:
            data = json.loads((self.project / ".rtlflow" / "project.json").read_text())
        except Exception:
            data = {}
        for key, (lbl, pkey) in self.cards.items():
            d = data.get(pkey) or {}
            txt = ""
            if key == "nclaunch" and d:
                txt = f"top {d.get('top') or '?'} · last run {d.get('result') or '-'}"
            elif key == "genus" and d:
                a = (d.get("area") or {}).get("area")
                txt = f"{d.get('top') or '?'} · {d.get('stage')}" + (f" · area {a:.1f}" if a else "")
            elif key == "innovus" and d:
                w = (d.get("timing") or {}).get("wns")
                txt = f"{d.get('top') or '?'} · {d.get('stage')}" + (f" · WNS {w}" if w not in (None, "NA") else "")
            elif key == "tempus" and d:
                w = (d.get("summary") or {}).get("wns")
                txt = f"{d.get('top') or '?'} from {d.get('source') or 'manual'}" + (f" · WNS {w:.3f}" if isinstance(w, (int, float)) else "")
            elif key == "modus" and d:
                txt = f"{d.get('top') or '?'} · coverage {d.get('coverage', 0):.1f}% · {d.get('patterns', 0)} patterns"
            elif key == "voltus" and d:
                p = (d.get("power") or {}).get("total")
                txt = f"{d.get('top') or '?'}" + (f" · {p * 1e3:.4f} mW" if p else "")
            elif key == "pegasus" and d:
                txt = f"DRC {d.get('drc_total') if d.get('drc_total') is not None else '-'} · LVS {d.get('lvs') or '-'}"
            lbl.configure(text=txt or "not run in this project")
        self.root.after(3000, self.refresh)

    def pick_project(self):
        d = filedialog.askdirectory(parent=self.root, initialdir=str(self.project))
        if d:
            self.project = Path(d).resolve()
            self.refresh()

    def launch(self, tool: str | None, gui: bool = True, term: bool = True):
        base = rtlflow_cmd()
        args = base + ([tool] if tool else [])
        if tool and gui and tool not in ("innovus", "simvision", "doctor"):
            args.append("-gui")
        if tool == "innovus" and not gui:
            args.append("-nowin")
        if not term:
            return self.run_detached(args)
        t = find_terminal()
        if not t:
            messagebox.showerror("No terminal", "No terminal emulator found.\nSet RTLFLOW_TERMINAL, e.g.\n"
                                                 "export RTLFLOW_TERMINAL='gnome-terminal --'", parent=self.root)
            return
        inner = f"cd {shlex.quote(str(self.project))} && {' '.join(shlex.quote(a) for a in args)}; " \
                f"echo; read -r -p '[{tool or 'rtlflow'} exited — press Enter to close]' _"
        cmd = [t[0], *t[1], "bash", "-c", inner]
        try:
            subprocess.Popen(cmd, cwd=str(self.project), start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.st.configure(text=f"Launched {tool or 'rtlflow'} in {Path(t[0]).name} · project {self.project.name}")
        except OSError as e:
            messagebox.showerror("Launch failed", str(e), parent=self.root)

    def run_detached(self, args):
        if args and not os.path.isabs(args[0]) and not shutil.which(args[0]) and env:
            p = env.find_tool(args[0])
            if p:
                args = [p] + args[1:]
        try:
            subprocess.Popen(args, cwd=str(self.project), start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            messagebox.showerror("Launch failed", str(e), parent=self.root)

    def open_readme(self):
        home = Path(os.environ.get("RTLFLOW_HOME") or Path(__file__).resolve().parents[2])
        for name in ("docs/RTLFlow_User_Guide.docx", "README.md"):
            p = home / name
            if p.exists():
                self.run_detached(["xdg-open", str(p)])
                return


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.getcwd())
    a, _ = ap.parse_known_args(argv)
    root = tk.Tk(className="RTLFlow")
    Launcher(root, a.project)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
