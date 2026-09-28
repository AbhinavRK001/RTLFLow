"""Persistent OpenROAD session — the single physical-design database.

One `openroad` process lives for the whole innovus session. Terminal commands
and GUI actions are both funnelled through `run()`, serialised by the shell's
exec lock, so there is exactly ONE database.

Protocol (robust to any script content, multi-line, braces, quotes):

    set ::__rf_rc [catch {uplevel #0 [binary decode base64 <b64>]} ::__rf_msg]
    puts "\\n<<RF_END <id> $::__rf_rc [binary encode base64 $::__rf_msg]>>"; flush stdout

Everything the tool prints before the sentinel is streamed live (so you see
detailed_route progress as it happens) and also returned in Result.lines.
OpenROAD's utl::error throws a Tcl error, so an `[ERROR XXX-nnnn]` always
comes back as rc != 0 — the shell/GUI never report a failed stage as done.
"""

from __future__ import annotations

import base64
import os
import queue
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_SENT = re.compile(r"<<RF_END (\d+) (-?\d+) ([A-Za-z0-9+/=]*)>>")
_PROMPT = re.compile(r"^(?:openroad> ?)+")


@dataclass
class Result:
    rc: int
    value: str = ""                       # Tcl result on success, error message on failure
    lines: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def errors(self) -> list[str]:
        return [l for l in self.lines if "[ERROR" in l or l.startswith("Error")]


class BackendDead(Exception):
    pass


class OpenROADSession:
    def __init__(self, exe: str, cwd: Path, on_line: Callable[[str], None] | None = None,
                 threads: int | None = None) -> None:
        self.exe = exe
        self.cwd = Path(cwd)
        self.on_line = on_line
        self.threads = threads or max(1, (os.cpu_count() or 2))
        self.proc: subprocess.Popen | None = None
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._id = 0
        self._active_id: int | None = None
        self._active_stream = True
        self._collect: list[str] = []
        self._pending_blank = False
        self.version = ""
        self.dead_reason = ""

    # lifecycle -------------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout: float = 60.0) -> None:
        env = dict(os.environ)
        env["TERM"] = "dumb"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [self.exe, "-no_init", "-no_splash"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=str(self.cwd), env=env, text=True, bufsize=1, errors="replace",
            start_new_session=True,   # Ctrl-C in the terminal must not kill the DB
        )
        self._reader = threading.Thread(target=self._read_loop, name="openroad-reader", daemon=True)
        self._reader.start()
        r = self.run("fconfigure stdout -buffering line\n"
                     "catch {fconfigure stderr -buffering line}\n"
                     "set ::__rf_version unknown\n"
                     "if {[catch {set ::__rf_version [ord::openroad_version]}]} {catch {set ::__rf_version [sta::version]}}\n"
                     f"catch {{set_thread_count {self.threads}}}\n"
                     "set ::__rf_version",
                     stream=False, timeout=timeout)
        if not r.ok and not self.alive:
            raise BackendDead(self.dead_reason or "OpenROAD failed to start:\n" + r.text)
        self.version = r.value.strip() if r.ok else ""

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            if self.alive:
                self.proc.stdin.write("exit\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
        except Exception:
            pass
        if self.alive:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except Exception:
                self.proc.kill()
        self.proc = None

    def kill(self) -> None:
        if self.proc and self.alive:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except Exception:
                self.proc.kill()

    # io --------------------------------------------------------------------
    def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = _PROMPT.sub("", raw.rstrip("\n"))
            m = _SENT.search(line)
            if m:
                pre = line[:m.start()]
                if pre.strip():
                    self._emit(pre)
                self._pending_blank = False
                try:
                    msg = base64.b64decode(m.group(3)).decode("utf-8", "replace")
                except Exception:
                    msg = ""
                self._q.put(("end", int(m.group(1)), int(m.group(2)), msg))
                continue
            if line == "":
                if self._pending_blank:
                    self._emit("")
                self._pending_blank = True
                continue
            if self._pending_blank:
                self._emit("")
                self._pending_blank = False
            self._emit(line)
        code = None
        if self.proc:
            try:
                code = self.proc.wait(timeout=2)
            except Exception:
                code = self.proc.poll()
        self.dead_reason = f"OpenROAD process exited (code {code})"
        self._q.put(("dead", -1, -1, self.dead_reason))

    def _emit(self, line: str) -> None:
        self._collect.append(line)
        if self._active_stream and self.on_line:
            try:
                self.on_line(line)
            except Exception:
                pass

    def run(self, script: str, *, stream: bool = True, timeout: float | None = None) -> Result:
        """Evaluate a Tcl script in the session. Blocks until done."""
        if not self.alive:
            raise BackendDead(self.dead_reason or "OpenROAD session is not running")
        self._id += 1
        rid = self._id
        self._collect = []
        self._active_stream = stream
        b64 = base64.b64encode(script.encode()).decode()
        payload = (f"set ::__rf_rc [catch {{uplevel #0 [binary decode base64 {b64}]}} ::__rf_msg]; "
                   f"puts \"\\n<<RF_END {rid} $::__rf_rc [binary encode base64 [encoding convertto utf-8 $::__rf_msg]]>>\"; "
                   f"flush stdout\n")
        t0 = time.time()
        try:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise BackendDead(f"OpenROAD pipe closed: {e}")
        interrupts = 0
        last_int = 0.0
        while True:
            try:
                kind, i, rc, msg = self._q.get(timeout=0.25 if timeout is None else min(0.25, timeout))
            except queue.Empty:
                if timeout is not None and time.time() - t0 > timeout:
                    return Result(-2, f"timeout after {timeout:.0f}s", list(self._collect), time.time() - t0)
                continue
            except KeyboardInterrupt:
                now = time.time()
                interrupts = interrupts + 1 if now - last_int < 3 else 1
                last_int = now
                if interrupts >= 2:
                    self.kill()
                    raise BackendDead("OpenROAD session killed by user (Ctrl-C twice)")
                if self.on_line:
                    self.on_line("**WARN: OpenROAD is busy. Press Ctrl-C again within 3s to KILL the "
                                 "session (the design database will be lost).")
                continue
            if kind == "dead":
                raise BackendDead(msg)
            if i != rid:
                continue   # stale sentinel from an abandoned (timed-out) call
            self._active_stream = True
            return Result(rc, msg, list(self._collect), time.time() - t0)

    def query(self, script: str, default: str = "") -> str:
        """Quiet evaluation returning the Tcl result (or default on error)."""
        try:
            r = self.run(script, stream=False)
        except BackendDead:
            return default
        return r.value if r.ok else default
