"""Pegasus GUI — DRC violation browser with markers on the real layout, LVS result."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme as T
from .base import ToolGUI, run
from .layoutview import LayoutCanvas


class PegasusGUI(ToolGUI):
    app = "pegasus"
    title = "RTLFlow Pegasus Physical Verification"
    prompt = "pegasus>"

    def build(self):
        self.markers: list[dict] = []
        self.tool_button("Load Layout", lambda: self.send_cmd("read_layout"))
        self.tool_sep()
        self.tool_button("Run DRC (KLayout)", lambda: self.send_cmd("run_drc -engine klayout"), style="Accent.TButton")
        self.tool_button("Run DRC (Magic)", lambda: self.send_cmd("run_drc -engine magic"))
        self.tool_button("Run LVS", lambda: self.send_cmd("run_lvs"), style="Accent.TButton")
        self.tool_sep()
        self.tool_button("Open in KLayout", lambda: self.send_cmd("view_results"))
        self.tool_button("Fit", lambda: self.lay.fit())
        pw = ttk.PanedWindow(self.center, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.Frame(pw, style="Panel.TFrame", padding=8, width=330)
        pw.add(left, weight=0)
        self.badges = {}
        for key, lab in (("drc", "DRC"), ("lvs", "LVS")):
            box = tk.Frame(left, bg=T.BG3, padx=10, pady=6)
            box.pack(fill="x", pady=3)
            tk.Label(box, text=lab, bg=T.BG3, fg=T.FG_DIM, font=self.fonts["small"]).pack(anchor="w")
            v = tk.Label(box, text="not run", bg=T.BG3, fg=T.FG, font=(self.fonts["ui"][0], 14, "bold"))
            v.pack(anchor="w")
            self.badges[key] = v
        ttk.Label(left, text="VIOLATIONS", style="Dim.TLabel").pack(anchor="w", pady=(10, 2))
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_pick)
        nb = ttk.Notebook(pw)
        pw.add(nb, weight=5)
        self.lay = LayoutCanvas(nb, self.fonts)
        self.lay.empty_text = "Load a layout (innovus streamOut first)"
        nb.add(self.lay, text="Layout + markers")
        self.lvs_txt = self.text_tab("LVS report")
        self.lay.on_hover = lambda x, y: self.st_msg.configure(text=f"x {x:9.3f}  y {y:9.3f} µm", foreground=T.FG_DIM)

    def build_menus(self):
        self.menu("Verify", [("Load layout", lambda: self.send_cmd("read_layout")),
                             ("DRC (KLayout deck)", lambda: self.send_cmd("run_drc -engine klayout")),
                             ("DRC (Magic)", lambda: self.send_cmd("run_drc -engine magic")),
                             ("LVS (Magic + Netgen)", lambda: self.send_cmd("run_lvs")), None,
                             ("DRC report", lambda: self.send_cmd("report_drc", report=True)),
                             ("Status", lambda: self.send_cmd("status", report=True))])

    def on_message(self, m):
        t = m.get("t")
        if t == "design":
            self.set_stage(f"{m.get('top') or '-'} · {m.get('gds', '').split('/')[-1] or 'no GDS'}")
            if m.get("def_file"):
                self.lay.load(m["def_file"], m.get("lefs", []))
        elif t == "drc":
            self.markers = m.get("markers", [])
            tot = m.get("total")
            if tot is None:
                self.badges["drc"].configure(text="not run", fg=T.FG)
            else:
                self.badges["drc"].configure(text=f"{tot} violation(s)" if tot else "CLEAN",
                                             fg=T.ERR if tot else T.OK)
            self.tree.delete(*self.tree.get_children())
            by: dict = {}
            for i, mk in enumerate(self.markers):
                by.setdefault(mk["rule"], []).append(i)
            for rule, idx in sorted(by.items(), key=lambda kv: -len(kv[1])):
                r = self.tree.insert("", "end", text=f"{rule}  ({len(idx)})", open=False)
                for i in idx[:500]:
                    b = self.markers[i]["box"]
                    self.tree.insert(r, "end", iid=f"m{i}", text=f"({b[0]:.2f}, {b[1]:.2f})")
            self.lay.markers = [(*mk["box"], mk["rule"], T.ERR) for mk in self.markers]
            self.lay.schedule()
        elif t == "lvs":
            st = m.get("status") or ""
            self.badges["lvs"].configure(text=st.upper() if st else "not run",
                                         fg=T.OK if st.startswith("match") else (T.ERR if st else T.FG))
            if m.get("text"):
                self.set_text(self.lvs_txt, m["text"])

    def on_pick(self, e=None):
        s = self.tree.selection()
        if not s or not s[0].startswith("m"):
            return
        i = int(s[0][1:])
        b = self.markers[i]["box"]
        self.lay.hi_marker = i
        self.lay.zoom_to(*b, margin=3)
        self.flash(self.markers[i]["rule"], T.ERR)


def main(argv=None):
    return run(PegasusGUI, argv)


if __name__ == "__main__":
    raise SystemExit(main())
