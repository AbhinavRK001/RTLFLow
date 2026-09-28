"""One-shot backend runners (Yosys, OpenSTA, Verilator, Icarus, KLayout, Magic, Netgen)."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import env
from ..core.msg import colorize_backend_line, console


@dataclass
class RunResult:
    rc: int
    lines: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    log: Path | None = None

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def run(cmd: list[str], *, cwd: Path | None = None, log: Path | None = None,
        show: Callable[[str], bool] | bool = False, timeout: float | None = None,
        stdin_text: str | None = None) -> RunResult:
    """Run *cmd*, stream output, return all lines.

    show=True prints everything, False prints nothing, or a predicate(line)->bool filter.
    """
    t0 = time.time()
    lines: list[str] = []
    logf = open(log, "w") if log else None
    try:
        p = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace",
                             stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                             env=dict(os.environ, TERM="dumb"))
    except FileNotFoundError as e:
        return RunResult(127, [f"{cmd[0]}: {e}"], 0.0, log)
    if stdin_text is not None:
        try:
            p.stdin.write(stdin_text); p.stdin.close()
        except OSError:
            pass
    try:
        for raw in p.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            if logf:
                logf.write(line + "\n")
            if show is True or (callable(show) and show(line)):
                console.write(colorize_backend_line(line))
        rc = p.wait(timeout=timeout)
    except KeyboardInterrupt:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
        rc = 130
        lines.append("** interrupted **")
    finally:
        if logf:
            logf.close()
    return RunResult(rc, lines, time.time() - t0, log)


def yosys(script: str, script_path: Path, log: Path, show=False) -> RunResult:
    exe = env.find_tool("yosys")
    if not exe:
        return RunResult(127, ["yosys not found (tools -rescan, or set RTLFLOW_YOSYS=/path/to/yosys)"])
    script_path.write_text(script)
    return run([exe, "-q", "-l", str(log), "-s", str(script_path)], cwd=script_path.parent, show=show)


def sta(script: str, script_path: Path, log: Path | None = None, show=False) -> RunResult:
    """Run an OpenSTA script with `sta`, or with `openroad` (which embeds OpenSTA)."""
    script_path.write_text(script + "\nexit\n")
    exe = env.find_tool("sta")
    if exe:
        return run([exe, "-no_init", "-no_splash", "-exit", str(script_path)],
                   cwd=script_path.parent, log=log, show=show)
    exe = env.find_tool("openroad")
    if exe:
        return run([exe, "-no_init", "-no_splash", "-exit", str(script_path)],
                   cwd=script_path.parent, log=log, show=show)
    return RunResult(127, ["Neither `sta` nor `openroad` found — cannot run timing analysis"])


def important(line: str) -> bool:
    s = line.lstrip()
    return (s.startswith(("ERROR", "Error", "[ERROR", "Warning", "WARNING", "[WARNING", "%Error", "%Warning"))
            or "error:" in s.lower()[:40])
