"""NCLaunch GUI — source manager, compile / elaborate / simulate buttons and a
built-in SimVision-style waveform viewer, on the same nclaunch session."""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, ttk

from . import theme as T
from .base import ToolGUI, run
from .waveview import WaveView


class NCLaunchGUI(ToolGUI):
    app = "nclaunch"
    title = "RTLFlow NCLaunch"
    prompt = "nclaunch>"

    def build(self):
        self.files: list[dict] = []
        self.roles: dict[str, str] = {}
        self.top = tk.StringVar()
        self.engine = tk.StringVar(value="verilator")
        self.tool_button("Add files...", self.add_files)
        self.tool_button("Refresh", lambda: self.request(t="request_state"))
        self.tool_sep()
        self.tool_button("Compile (ncvlog)", lambda: self.apply_then("compile"))
        self.tool_button("Elaborate (ncelab)", lambda: self.apply_then("elaborate"))
        self.tool_button("Simulate (ncsim)", lambda: self.apply_then("simulate"), style="Accent.TButton")
        self.tool_sep()
        self.tool_button("SimVision (GTKWave)", lambda: self.send_cmd("simvision"))
        pw = ttk.PanedWindow(self.center, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.Frame(pw, style="Panel.TFrame", padding=8, width=380)
        pw.add(left, weight=0)
        ttk.Label(left, text="SOURCES  (right-click to set role)", style="Dim.TLabel").pack(anchor="w")
        cols = ("role", "module")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings", height=14)
        self.tree.heading("#0", text="File")
        self.tree.heading("role", text="Role")
        self.tree.heading("module", text="Module")
        self.tree.column("#0", width=150)
        self.tree.column("role", width=70, anchor="center")
        self.tree.column("module", width=110)
        self.tree.tag_configure("design", foreground="#9ecbff")
        self.tree.tag_configure("tb", foreground="#d2a8ff")
        self.tree.tag_configure("none", foreground=T.FG_DIM)
        self.tree.pack(fill="both", expand=True, pady=(4, 6))
        self.tree.bind("<Button-3>", self._ctx)
        self.tree.bind("<Double-1>", lambda e: self._cycle())
        rb = ttk.Frame(left, style="Panel.TFrame")
        rb.pack(fill="x")
        for lab, role in (("Design", "design"), ("Testbench", "tb"), ("Exclude", "")):
            ttk.Button(rb, text=lab, style="Tool.TButton", command=lambda r=role: self._set_role(r)).pack(side="left")
        f2 = ttk.Frame(left, style="Panel.TFrame")
        f2.pack(fill="x", pady=(10, 0))
        ttk.Label(f2, text="Simulation top", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.top_cb = ttk.Combobox(f2, textvariable=self.top, width=18)
        self.top_cb.grid(row=0, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(f2, text="Engine", style="Panel.TLabel").grid(row=1, column=0, sticky="w")
        eb = ttk.Frame(f2, style="Panel.TFrame")
        eb.grid(row=1, column=1, sticky="w", padx=6)
        for e in ("verilator", "icarus"):
            ttk.Radiobutton(eb, text=e, value=e, variable=self.engine).pack(side="left")
        self.result = tk.Label(left, text="no run yet", bg=T.BG3, fg=T.FG, font=(self.fonts["ui"][0], 13, "bold"), pady=6)
        self.result.pack(fill="x", pady=(12, 0))
        self.wave = WaveView(pw, self.fonts)
        pw.add(self.wave, weight=5)
        self.menu_ctx = tk.Menu(self.root, tearoff=False)
        for lab, role in (("Design", "design"), ("Testbench", "tb"), ("Exclude", "")):
            self.menu_ctx.add_command(label=f"Mark as {lab}", command=lambda r=role: self._set_role(r))

    def build_menus(self):
        self.menu("File", [("Add HDL files...", self.add_files), ("Open waveform (VCD)...", self.open_vcd)])
        self.menu("Simulation", [("Compile", lambda: self.apply_then("compile")),
                                 ("Elaborate", lambda: self.apply_then("elaborate")),
                                 ("Simulate", lambda: self.apply_then("simulate")),
                                 ("Status", lambda: self.send_cmd("status", report=True))])
        self.menu("Waves", [("Add all signals", self.wave.add_all), ("Clear", self.wave.clear),
                            ("Open in GTKWave", lambda: self.send_cmd("simvision"))])

    def _files(self):
        return [f"{{{f}}}" for f, r in self.roles.items() if r]

    def add_files(self):
        fns = filedialog.askopenfilenames(parent=self.root, initialdir=self.project,
                                          filetypes=[("HDL", "*.sv *.v *.svh *.vh"), ("All", "*")])
        for f in fns:
            self.files.append({"path": f, "role": "", "guess": "design", "top": ""})
            self.roles[f] = "design"
        self._fill()

    def open_vcd(self):
        fn = filedialog.askopenfilename(parent=self.root, initialdir=self.project, filetypes=[("VCD", "*.vcd"), ("All", "*")])
        if fn:
            self.wave.load(fn)

    def apply_then(self, cmd):
        self.request(t="set_roles", roles=self.roles, top=self.top.get().strip(), engine=self.engine.get())
        self.send_cmd(cmd)

    def _ctx(self, e):
        it = self.tree.identify_row(e.y)
        if it:
            self.tree.selection_set(it)
            self.menu_ctx.tk_popup(e.x_root, e.y_root)

    def _cycle(self):
        for it in self.tree.selection():
            cur = self.roles.get(it, "")
            self.roles[it] = {"design": "tb", "tb": "", "": "design"}[cur]
        self._fill()

    def _set_role(self, role):
        for it in self.tree.selection():
            self.roles[it] = role
        self._fill()

    def _fill(self):
        self.tree.delete(*self.tree.get_children())
        mods = []
        for f in self.files:
            p = f["path"]
            r = self.roles.get(p, "")
            self.tree.insert("", "end", iid=p, text=os.path.basename(p), values=(r or "-", f.get("top", "")),
                             tags=(r or "none",))
            if r == "tb" and f.get("top"):
                mods.append(f["top"])
        self.top_cb.configure(values=mods)
        if not self.top.get() and mods:
            self.top.set(mods[0])

    def on_message(self, m):
        t = m.get("t")
        if t == "sim_state":
            self.files = m.get("files", [])
            any_role = any(f["role"] for f in self.files)
            self.roles = {f["path"]: (f["role"] if any_role else f["guess"]) for f in self.files}
            self._fill()
            if m.get("top"):
                self.top.set(m["top"])
            if m.get("engine"):
                self.engine.set(m["engine"])
            res = m.get("result") or ""
            self.result.configure(text=f"last run: {res}" if res else "no run yet",
                                  fg=T.OK if res == "PASS" else (T.ERR if res == "FAIL" else T.FG))
            self.set_stage(f"top {m.get('top') or '?'} · {m.get('engine')}" + (" · built" if m.get("built") else ""))
            if m.get("wave") and os.path.exists(m["wave"]) and not self.wave.vcd:
                self.wave.load(m["wave"])
        elif t == "wave":
            if self.wave.load(m["file"]):
                self.flash(f"Waveform loaded: {os.path.basename(m['file'])}", T.OK)


def main(argv=None):
    return run(NCLaunchGUI, argv)


if __name__ == "__main__":
    raise SystemExit(main())
