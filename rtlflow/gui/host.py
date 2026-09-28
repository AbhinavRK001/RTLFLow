"""Shell-side owner of a GUI window process (innovus layout GUI, genus GUI)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .ipc import Channel

PKG_ROOT = Path(__file__).resolve().parents[2]      # dir containing the `rtlflow` package


def display_available() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def tk_available(python: str = sys.executable) -> tuple[bool, str]:
    try:
        r = subprocess.run([python, "-c", "import tkinter; tkinter.Tcl(); print(tkinter.TkVersion)"],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else ""


class GuiHost:
    """Launches `python -m rtlflow.gui.<module> --fd N` and talks to it."""

    def __init__(self, module: str, log_path: Path, on_message: Callable[[dict], None],
                 on_close: Callable[[int], None] | None = None) -> None:
        self.module = module
        self.log_path = log_path
        self.on_message = on_message
        self.on_close = on_close
        self.proc: subprocess.Popen | None = None
        self.chan: Channel | None = None
        self._closing = False

    @property
    def is_open(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self.chan is not None and not self.chan.closed

    def launch(self, args: list[str] | None = None, wait_ready: float = 12.0) -> tuple[bool, str]:
        if self.is_open:
            return True, "already open"
        if not display_available():
            return False, "no display (DISPLAY / WAYLAND_DISPLAY unset)"
        parent, child = socket.socketpair()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PKG_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        # Tk runs through XWayland on GNOME; make sure it doesn't try native Wayland toolkits
        env.setdefault("GDK_BACKEND", "x11")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = open(self.log_path, "w")
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", f"rtlflow.gui.{self.module}", "--fd", str(child.fileno()), *(args or [])],
                pass_fds=(child.fileno(),), env=env, stdin=subprocess.DEVNULL,
                stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            child.close()
            logf.close()
        self.chan = Channel(parent)
        ready = threading.Event()

        def handler(msg: dict) -> None:
            if msg.get("t") == "ready":
                ready.set()
            self.on_message(msg)

        self.chan.start_reader(handler, self._closed, name=f"{self.module}-ipc")
        t0 = time.time()
        while time.time() - t0 < wait_ready:
            if ready.wait(0.1):
                return True, "ok"
            if self.proc.poll() is not None:
                break
        if self.proc.poll() is not None:
            tail = ""
            try:
                tail = self.log_path.read_text().strip().splitlines()[-1]
            except Exception:
                pass
            return False, tail or f"GUI exited with code {self.proc.returncode}"
        return True, "slow start"

    def _closed(self) -> None:
        code = self.proc.poll() if self.proc else 0
        if self.on_close and not self._closing:
            self.on_close(code if code is not None else 0)

    def send(self, **msg) -> bool:
        return bool(self.chan and self.chan.send(**msg))

    def close(self) -> None:
        self._closing = True
        if self.chan:
            self.chan.send(t="quit")
            time.sleep(0.1)
            self.chan.close()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
        self.proc = None
        self.chan = None
        self._closing = False
