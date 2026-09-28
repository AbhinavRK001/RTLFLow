"""Project workspace (.rtlflow/) — shared state between genus, nclaunch, innovus.

    <project>/.rtlflow/
        project.json          top, files, libs, handoff pointers, stage history
        logs/                 genus.logN, innovus.cmdN ...
        runs/synth/           Yosys checkpoints, <top>_mapped.v, reports
        runs/sim/             Verilator/Icarus build + waves
        runs/pnr/             OpenROAD DEFs / DBs / reports, live.def for the GUI
        handoff/              <top>.invs_setup.tcl  (genus → innovus, like write_design -innovus)
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class Project:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or self._find_root(Path.cwd())).resolve()
        self.dir = self.root / ".rtlflow"
        self.data: dict = {}
        self.load()

    @staticmethod
    def _find_root(start: Path) -> Path:
        p = start.resolve()
        for cand in [p, *p.parents]:
            if (cand / ".rtlflow" / "project.json").exists():
                return cand
            if cand == Path.home():
                break
        return p

    # paths ---------------------------------------------------------------
    def path(self, *parts: str, mkdir: bool = True) -> Path:
        p = self.dir.joinpath(*parts)
        if mkdir:
            (p if not p.suffix else p.parent).mkdir(parents=True, exist_ok=True)
        return p

    @property
    def logs(self) -> Path:
        return self.path("logs")

    @property
    def synth(self) -> Path:
        return self.path("runs", "synth")

    @property
    def sim(self) -> Path:
        return self.path("runs", "sim")

    @property
    def pnr(self) -> Path:
        return self.path("runs", "pnr")

    @property
    def handoff(self) -> Path:
        return self.path("handoff")

    # state ---------------------------------------------------------------
    def load(self) -> None:
        f = self.dir / "project.json"
        try:
            self.data = json.loads(f.read_text())
        except Exception:
            self.data = {}

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        (self.dir / "project.json").write_text(json.dumps(self.data, indent=2))

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, **kw) -> None:
        self.data.update(kw)
        self.save()

    def stage(self, tool: str, stage: str, **info) -> None:
        hist = self.data.setdefault("history", [])
        hist.append({"tool": tool, "stage": stage, "time": time.strftime("%H:%M:%S"), **info})
        self.data["history"] = hist[-200:]
        self.save()

    def exists(self) -> bool:
        return (self.dir / "project.json").exists()

    def rel(self, p: str | Path) -> str:
        try:
            return str(Path(p).resolve().relative_to(self.root))
        except Exception:
            return str(p)
