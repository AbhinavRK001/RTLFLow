"""GUI plumbing shared by every tool shell (tempus, voltus, modus, pegasus, nclaunch …).

A shell class mixes in GuiMixin and sets `gui_module = "tempus_gui"`. It then
gets `gui_show` / `gui_hide`, busy/idle mirroring, the [GUI] command echo and
report capture — the same terminal ⇄ GUI contract innovus has.
"""

from __future__ import annotations

import queue
import threading

from .msg import C, colorize_backend_line, console, error, info, ok, warn
from .shell import command


class GuiMixin:
    gui_module: str = ""
    gui_label: str = "GUI"

    def _gui_init(self) -> None:
        self.gui = None
        self._gui_q: "queue.Queue[dict]" = queue.Queue()
        self._gui_worker: threading.Thread | None = None
        self._gui_tl = threading.local()

    # ── hooks for subclasses ──
    def gui_sync(self) -> None:
        """Send the full current state to a (re)connected GUI."""

    def gui_action(self, msg: dict) -> None:
        """Handle a non-command request from the GUI (e.g. data queries)."""

    def gui_args(self) -> list[str]:
        return ["--project", str(self.project.root)]

    # ── plumbing ──
    def gui_send(self, **msg) -> None:
        g = getattr(self, "gui", None)
        if g and g.is_open:
            g.send(**msg)

    def gui_open(self) -> bool:
        from ..gui.host import GuiHost, display_available
        if self.gui and self.gui.is_open:
            self.gui_send(t="raise")
            info(f"{self.gui_label} already open")
            return True
        if not display_available():
            warn(f"{self.msg_prefix}-GUI-001", "No display ($DISPLAY/$WAYLAND_DISPLAY) — continuing without GUI")
            return False
        self.gui = GuiHost(self.gui_module, self.project.logs / f"{self.tool}_gui.log", self._gui_message,
                           lambda code: console.write(f"{C.GRY}{self.tool}: GUI closed — gui_show reopens it{C.RESET}"))
        okk, why = self.gui.launch(self.gui_args())
        if not okk:
            if "no module named" in why.lower() and "tkinter" in why.lower():
                error(f"{self.msg_prefix}-GUI-002", "Python Tk is missing:  sudo apt install python3-tk")
            else:
                error(f"{self.msg_prefix}-GUI-003", f"GUI failed to start: {why}  (log: .rtlflow/logs/{self.tool}_gui.log)")
            return False
        if not self._gui_worker or not self._gui_worker.is_alive():
            self._gui_worker = threading.Thread(target=self._gui_loop, daemon=True, name=f"{self.tool}-gui")
            self._gui_worker.start()
        if not any(getattr(l, "_rf_gui", False) for l in console.listeners):
            def _l(text: str) -> None:
                self.gui_send(t="log", text=text)
            _l._rf_gui = True  # type: ignore[attr-defined]
            console.listeners.append(_l)
        ok(f"  {self.gui_label} open (pid {self.gui.proc.pid}) — same session as this shell")
        return True

    def gui_close(self) -> None:
        console.listeners[:] = [l for l in console.listeners if not getattr(l, "_rf_gui", False)]
        if self.gui:
            self.gui.close()

    def _gui_message(self, msg: dict) -> None:
        self._gui_q.put(msg)

    def _gui_loop(self) -> None:
        while True:
            msg = self._gui_q.get()
            t = msg.get("t")
            try:
                if t in ("ready", "request_state"):
                    with self.exec_lock:
                        self.gui_send(t="hello", app=self.tool, project=str(self.project.root))
                        self.gui_sync()
                elif t == "cmd":
                    line = msg.get("line", "").strip()
                    if not line:
                        continue
                    if line.split()[0] in ("exit", "quit", "exit_tool"):
                        console.write(f"{C.GRY}{self.tool}: 'exit' typed in the GUI closes the GUI only — "
                                      f"type exit in this terminal to end the session{C.RESET}")
                        self.gui_send(t="quit")
                        continue
                    console.write(f"{C.MAG}[GUI]{C.RESET} {C.BOLD}{line}{C.RESET}")
                    rep = bool(msg.get("report"))
                    if rep:
                        console.begin_capture()
                    okk = False
                    try:
                        okk = self.run_line(line, source="gui")
                    finally:
                        text = console.end_capture() if rep else ""
                    if rep:
                        for l in text.splitlines():
                            console.write(colorize_backend_line(l))
                        self.gui_send(t="report", title=msg.get("title") or line, text=text, ok=okk)
                    self.gui_send(t="result", id=msg.get("id"), ok=okk, line=line)
                elif t not in ("closing",):
                    with self.exec_lock:
                        self.gui_action(msg)
            except Exception as e:  # never kill the worker
                console.log_only(f"gui worker error: {e!r}")

    def run_line(self, line: str, source: str = "terminal") -> bool:  # type: ignore[override]
        tl = self._gui_tl
        top = not getattr(tl, "depth", 0)
        busy = top and line.strip() and not line.strip().startswith("#")
        tl.depth = getattr(tl, "depth", 0) + 1
        if busy:
            self.gui_send(t="busy", cmd=line.strip()[:120])
        okk = False
        try:
            okk = super().run_line(line, source)  # type: ignore[misc]
            return okk
        finally:
            tl.depth -= 1
            if busy:
                self.gui_send(t="idle", cmd=line.strip()[:120], ok=okk)

    # ── commands ──
    @command("gui_show", cat="GUI", aliases=["gui", "win"], usage="gui_show", help="Open this tool's GUI (same session)")
    def c_gui_show(self, args):
        if args and args[0].lower() in ("off", "0", "hide"):
            self.gui_close()
            return True
        return self.gui_open()

    @command("gui_hide", cat="GUI", help="Close the GUI (session keeps running)")
    def c_gui_hide(self, args):
        self.gui_close()
        info("GUI closed — gui_show reopens it")
        return True
