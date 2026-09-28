"""Console output, Cadence-style message formatting, and session log/cmd files.

Every tool shell (genus / innovus / nclaunch / rtlflow) writes two files the
way Cadence tools do:

    <tool>.logN   — everything printed to the terminal (tee'd)
    <tool>.cmdN   — every command the user (or the GUI) issued, replayable

They live in <project>/.rtlflow/logs/ so the project dir stays clean.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path

try:  # readline is optional (not present on some minimal Pythons)
    import readline  # noqa: F401
    _HAVE_RL = True
except Exception:  # pragma: no cover
    _HAVE_RL = False

_TTY = sys.stdout.isatty()
_COLOR = _TTY and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class C:
    """ANSI palette (empty strings when colour is disabled)."""
    RESET = "\x1b[0m" if _COLOR else ""
    BOLD = "\x1b[1m" if _COLOR else ""
    DIM = "\x1b[2m" if _COLOR else ""
    RED = "\x1b[38;5;203m" if _COLOR else ""
    YEL = "\x1b[38;5;221m" if _COLOR else ""
    GRN = "\x1b[38;5;114m" if _COLOR else ""
    CYA = "\x1b[38;5;80m" if _COLOR else ""
    BLU = "\x1b[38;5;75m" if _COLOR else ""
    MAG = "\x1b[38;5;176m" if _COLOR else ""
    GRY = "\x1b[38;5;245m" if _COLOR else ""
    WHT = "\x1b[38;5;255m" if _COLOR else ""


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


# Readline needs non-printing sequences wrapped in \001..\002 inside prompts.
def rl_safe(s: str) -> str:
    if not _COLOR:
        return s
    return _ANSI_RE.sub(lambda m: "\001" + m.group(0) + "\002", s)


class Console:
    """Thread-safe terminal writer with log tee and prompt redraw support."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._log = None
        self._cmd = None
        self.log_path: Path | None = None
        self.cmd_path: Path | None = None
        self.prompt_active = False
        self.prompt = ""
        self._captures: list[list[str]] = []
        self.listeners: list = []          # callables(text) — e.g. GUI transcript pane

    # -- log files --------------------------------------------------------
    def open_logs(self, log_dir: Path, tool: str) -> tuple[Path, Path]:
        """Open <tool>.logN / <tool>.cmdN with the next free N (Cadence style)."""
        self.close_logs()
        log_dir.mkdir(parents=True, exist_ok=True)
        n = 1
        while (log_dir / f"{tool}.log{n}").exists() or (log_dir / f"{tool}.cmd{n}").exists():
            n += 1
        self.log_path = log_dir / f"{tool}.log{n}"
        self.cmd_path = log_dir / f"{tool}.cmd{n}"
        self._log = open(self.log_path, "w", buffering=1, encoding="utf-8")
        self._cmd = open(self.cmd_path, "w", buffering=1, encoding="utf-8")
        stamp = time.strftime("%a %b %d %H:%M:%S %Y")
        self._cmd.write(f"#######################################################\n"
                        f"#  RTLFlow {tool} command log — {stamp}\n"
                        f"#######################################################\n")
        # stable symlinks <tool>.log / <tool>.cmd → latest
        for name, target in ((f"{tool}.log", self.log_path), (f"{tool}.cmd", self.cmd_path)):
            link = log_dir / name
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(target.name)
            except OSError:
                pass
        return self.log_path, self.cmd_path

    def close_logs(self) -> None:
        for f in (self._log, self._cmd):
            try:
                if f:
                    f.close()
            except Exception:
                pass
        self._log = self._cmd = None

    def record_cmd(self, line: str, source: str = "") -> None:
        if self._cmd:
            tag = f"   ;# from {source}" if source else ""
            self._cmd.write(line.rstrip() + tag + "\n")

    # -- writing ----------------------------------------------------------
    # -- capture (for `cmd > file` redirection) --------------------------
    def begin_capture(self) -> None:
        with self._lock:
            self._captures.append([])

    def end_capture(self) -> str:
        with self._lock:
            buf = self._captures.pop() if self._captures else []
        return "".join(buf)

    def write(self, text: str = "", end: str = "\n") -> None:
        with self._lock:
            if self._captures:
                self._captures[-1].append(strip_ansi(text) + end)
                return
            if self.prompt_active and _TTY:
                # erase current prompt line, print, then redraw prompt + buffer
                sys.stdout.write("\r\x1b[K")
                sys.stdout.write(text + end)
                buf = ""
                if _HAVE_RL:
                    try:
                        import readline as _rl
                        buf = _rl.get_line_buffer()
                    except Exception:
                        buf = ""
                sys.stdout.write(strip_ansi(self.prompt) if not _COLOR else self.prompt)
                sys.stdout.write(buf)
            else:
                sys.stdout.write(text + end)
            sys.stdout.flush()
            if self._log:
                self._log.write(strip_ansi(text) + end)
            for fn in list(self.listeners):
                try:
                    fn(text)
                except Exception:
                    pass

    def log_only(self, text: str) -> None:
        if self._log:
            self._log.write(strip_ansi(text) + "\n")


console = Console()


def out(text: str = "") -> None:
    console.write(text)


# ── Cadence-style message helpers ───────────────────────────────────────────

def error(msg_id: str, text: str) -> None:
    console.write(f"{C.RED}{C.BOLD}**ERROR:{C.RESET}{C.RED} ({msg_id}): {text}{C.RESET}")


def warn(msg_id: str, text: str) -> None:
    console.write(f"{C.YEL}**WARN: ({msg_id}): {text}{C.RESET}")


def info(text: str) -> None:
    console.write(f"{C.GRY}{text}{C.RESET}")


def note(text: str) -> None:
    console.write(f"{C.CYA}{text}{C.RESET}")


def ok(text: str) -> None:
    console.write(f"{C.GRN}{text}{C.RESET}")


def colorize_backend_line(line: str) -> str:
    """Colour OpenROAD / Yosys / OpenSTA output by severity."""
    if not _COLOR:
        return line
    s = line.lstrip()
    if s.startswith("[ERROR") or s.startswith("ERROR:") or s.startswith("Error:"):
        return f"{C.RED}{line}{C.RESET}"
    if s.startswith("[WARNING") or s.startswith("Warning:") or s.startswith("WARNING:"):
        return f"{C.YEL}{line}{C.RESET}"
    if s.startswith("[INFO"):
        return f"{C.GRY}{line}{C.RESET}"
    return line


def rule(title: str = "", width: int = 78, ch: str = "─") -> str:
    if not title:
        return f"{C.GRY}{ch * width}{C.RESET}"
    t = f" {title} "
    left = 3
    return f"{C.GRY}{ch * left}{C.RESET}{C.BOLD}{t}{C.RESET}{C.GRY}{ch * max(0, width - left - len(t))}{C.RESET}"


def table(rows: list[tuple], headers: tuple | None = None, indent: int = 2) -> str:
    """Render a Cadence-report-style +----+ table."""
    cols = len(headers) if headers else len(rows[0]) if rows else 0
    data = [tuple(str(x) for x in r) for r in rows]
    widths = [0] * cols
    for r in ([tuple(headers)] if headers else []) + data:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(strip_ansi(cell)))
    pad = " " * indent
    sep = pad + "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def fmt(r):
        cells = []
        for i, cell in enumerate(r):
            vis = len(strip_ansi(cell))
            cells.append(" " + cell + " " * (widths[i] - vis) + " ")
        return pad + "|" + "|".join(cells) + "|"

    lines = [sep]
    if headers:
        lines += [fmt(tuple(f"{C.BOLD}{h}{C.RESET}" for h in headers)), sep]
    lines += [fmt(r) for r in data]
    lines.append(sep)
    return "\n".join(lines)


def human_time(sec: float) -> str:
    if sec < 60:
        return f"{sec:.2f}s"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"
