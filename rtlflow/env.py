"""Tool & PDK discovery — the reason you never have to type `nix-shell`.

Search order per tool:
    1. $RTLFLOW_<TOOL>        explicit override (e.g. RTLFLOW_OPENROAD=/path/openroad)
    2. ~/.rtlflow/tools.conf  user overrides, one "tool = /abs/path" per line
    3. PATH
    4. /nix/store/*<hint>*/bin/<tool>   (newest wins) — OpenLane2's closure
    5. common user locations (~/.local/bin, /opt/*, /usr/local/bin, oss-cad-suite)

Results are cached in ~/.cache/rtlflow/tools.json and re-validated on start.
`tools -rescan` forces a fresh search.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path

HOME = Path.home()
CACHE = HOME / ".cache" / "rtlflow" / "tools.json"
USER_CONF = HOME / ".rtlflow" / "tools.conf"

# tool  -> (nix-store name hints, version args, role, commercial equivalent)
TOOLS: dict[str, tuple[tuple[str, ...], list[str] | None, str, str]] = {
    "yosys":     (("yosys",),            ["-V"],        "Synthesis",       "Genus / DC"),
    "openroad":  (("openroad",),         ["-version"],  "Implementation",  "Innovus / ICC2"),
    "sta":       (("opensta", "sta-"),   ["-version"],  "Static timing",   "Tempus / PrimeTime"),
    "klayout":   (("klayout",),          ["-v"],        "Layout / DRC",    "Virtuoso / Calibre"),
    "magic":     (("magic",),            ["--version"], "Layout / DRC",    "Virtuoso / Pegasus"),
    "netgen":    (("netgen",),           None,          "LVS",             "Calibre LVS / PVS"),
    "verilator": (("verilator",),        ["--version"], "Simulation",      "Xcelium / VCS"),
    "iverilog":  (("iverilog",),         ["-V"],        "Simulation",      "NC-Verilog"),
    "gtkwave":   (("gtkwave",),          ["--version"], "Waveforms",       "SimVision / Verdi"),
    "surfer":    (("surfer",),           ["--version"], "Waveforms",       "SimVision / Verdi"),
}
# Things that are not standalone binaries but capabilities of another tool
BUILTIN = {"openrcx": ("openroad", "Extraction", "Quantus / StarRC")}

REQUIRED = {"yosys", "openroad"}

_lock = threading.Lock()
_found: dict[str, "ToolInfo"] = {}
_pdk: "PDKInfo | None" = None


@dataclass
class ToolInfo:
    name: str
    path: str | None = None
    version: str = ""
    source: str = ""          # env / conf / PATH / nix / local

    @property
    def ok(self) -> bool:
        return bool(self.path)


@dataclass
class PDKInfo:
    root: str | None = None
    name: str | None = None
    stdcell: str | None = None
    extra: dict = field(default_factory=dict)

    # --- well-known files (open_pdks / volare / ciel layout) --------------
    @property
    def pdk_dir(self) -> Path | None:
        return Path(self.root) / self.name if self.root and self.name else None

    @property
    def libref(self) -> Path | None:
        d = self.pdk_dir
        return d / "libs.ref" / self.stdcell if d and self.stdcell else None

    def liberty(self, corner: str = "tt_025C_1v80") -> Path | None:
        lr = self.libref
        if not lr:
            return None
        cand = lr / "lib" / f"{self.stdcell}__{corner}.lib"
        if cand.exists():
            return cand
        libs = sorted((lr / "lib").glob("*.lib")) if (lr / "lib").is_dir() else []
        tt = [p for p in libs if "tt" in p.name]
        return (tt or libs or [None])[0]

    def cell_lef(self) -> Path | None:
        lr = self.libref
        if not lr:
            return None
        cand = lr / "lef" / f"{self.stdcell}.lef"
        if cand.exists():
            return cand
        lefs = sorted((lr / "lef").glob("*.lef")) if (lr / "lef").is_dir() else []
        return lefs[0] if lefs else None

    def tech_lef(self, corner: str = "nom") -> Path | None:
        lr = self.libref
        if not lr or not (lr / "techlef").is_dir():
            return None
        c = sorted((lr / "techlef").glob(f"*__{corner}.tlef")) or sorted((lr / "techlef").glob("*.tlef"))
        return c[0] if c else None

    def rcx_rules(self, corner: str = "nom") -> Path | None:
        d = self.pdk_dir
        if not d:
            return None
        ol = d / "libs.tech" / "openlane"
        c = sorted(ol.glob(f"rules.openrcx.*{corner}*")) if ol.is_dir() else []
        return c[0] if c else None

    def klayout_tech(self) -> Path | None:
        d = self.pdk_dir
        if not d:
            return None
        c = sorted((d / "libs.tech" / "klayout").rglob("*.lyt")) if (d / "libs.tech" / "klayout").is_dir() else []
        return c[0] if c else None

    def klayout_props(self) -> Path | None:
        d = self.pdk_dir
        if not d:
            return None
        c = sorted((d / "libs.tech" / "klayout").rglob("*.lyp")) if (d / "libs.tech" / "klayout").is_dir() else []
        return c[0] if c else None

    def cell_gds(self) -> Path | None:
        lr = self.libref
        if not lr or not (lr / "gds").is_dir():
            return None
        c = sorted((lr / "gds").glob("*.gds"))
        return c[0] if c else None


# ─────────────────────────────────────────────────────────────────────────────
def _is_exe(p: Path) -> bool:
    try:
        return p.is_file() and os.access(p, os.X_OK)
    except OSError:
        return False


def _read_user_conf() -> dict[str, str]:
    out: dict[str, str] = {}
    if USER_CONF.exists():
        for line in USER_CONF.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = (x.strip() for x in line.split("=", 1))
                out[k.lower()] = os.path.expanduser(v)
    return out


_nix_index: list[os.DirEntry] | None = None


def _nix_entries() -> list:
    global _nix_index
    if _nix_index is None:
        store = Path("/nix/store")
        try:
            _nix_index = [e for e in os.scandir(store) if e.is_dir(follow_symlinks=False)]
        except OSError:
            _nix_index = []
    return _nix_index


def _search_nix(tool: str, hints: tuple[str, ...]) -> str | None:
    best: tuple[float, str] | None = None
    for e in _nix_entries():
        n = e.name
        # store names look like <32-char-hash>-<pname>-<version>
        pname = n[33:] if len(n) > 33 else n
        if not any(pname.startswith(h) or f"-{h}" in pname for h in hints):
            continue
        cand = Path(e.path) / "bin" / tool
        if _is_exe(cand):
            try:
                mt = e.stat(follow_symlinks=False).st_mtime
            except OSError:
                mt = 0
            if best is None or mt > best[0]:
                best = (mt, str(cand))
    return best[1] if best else None


def _search_local(tool: str) -> str | None:
    cands = [
        HOME / ".local" / "bin" / tool,
        HOME / "bin" / tool,
        Path("/usr/local/bin") / tool,
        HOME / "oss-cad-suite" / "bin" / tool,
        Path("/opt/oss-cad-suite/bin") / tool,
        Path("/opt/openroad/bin") / tool,
        HOME / "OpenROAD-flow-scripts" / "tools" / "install" / "OpenROAD" / "bin" / tool,
        HOME / "VLSI" / "OpenROAD-flow-scripts" / "tools" / "install" / "OpenROAD" / "bin" / tool,
        HOME / ".nix-profile" / "bin" / tool,
    ]
    for c in cands:
        if _is_exe(c):
            return str(c)
    return None


def find_tool(tool: str, *, refresh: bool = False) -> str | None:
    """Return absolute path to *tool* or None."""
    with _lock:
        if not refresh and tool in _found and _found[tool].path and _is_exe(Path(_found[tool].path)):
            return _found[tool].path
    info = _locate(tool)
    with _lock:
        _found[tool] = info
    return info.path


def _locate(tool: str) -> ToolInfo:
    hints = TOOLS.get(tool, ((tool,), None, "", ""))[0]
    env = os.environ.get(f"RTLFLOW_{tool.upper()}")
    if env and _is_exe(Path(env)):
        return ToolInfo(tool, env, source="env")
    conf = _read_user_conf().get(tool)
    if conf and _is_exe(Path(conf)):
        return ToolInfo(tool, conf, source="conf")
    p = shutil.which(tool)
    if p:
        return ToolInfo(tool, str(Path(p)), source="PATH")
    n = _search_nix(tool, hints)
    if n:
        return ToolInfo(tool, n, source="nix")
    loc = _search_local(tool)
    if loc:
        return ToolInfo(tool, loc, source="local")
    return ToolInfo(tool, None)


def _probe_version(info: ToolInfo) -> None:
    args = TOOLS.get(info.name, ((), None, "", ""))[1]
    if not info.path or args is None:
        return
    try:
        r = subprocess.run([info.path, *args], capture_output=True, text=True, timeout=6,
                           stdin=subprocess.DEVNULL)
        txt = (r.stdout or r.stderr or "").strip().splitlines()
        line = next((l for l in txt if l.strip()), "")
        # compress to a short version token
        import re
        m = re.search(r"(v?\d+\.\d+(?:[.\-+][0-9A-Za-z_.\-+]+)?)", line)
        info.version = (m.group(1) if m else line)[:40]
    except Exception:
        info.version = ""


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def _save_cache() -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        data = {"tools": {k: asdict(v) for k, v in _found.items()},
                "pdk": asdict(_pdk) if _pdk else None}
        CACHE.write_text(json.dumps(data, indent=1))
    except OSError:
        pass


def discover(refresh: bool = False, versions: bool = True) -> dict[str, ToolInfo]:
    """Discover every known tool (cached)."""
    global _pdk
    cache = {} if refresh else _load_cache()
    cached_tools = cache.get("tools", {}) if cache else {}
    todo = []
    for t in TOOLS:
        c = cached_tools.get(t)
        env_override = os.environ.get(f"RTLFLOW_{t.upper()}")
        if c and c.get("path") and _is_exe(Path(c["path"])) and not env_override:
            _found[t] = ToolInfo(**c)
        else:
            todo.append(t)
    for t in todo:
        _found[t] = _locate(t)
    if versions:
        threads = [threading.Thread(target=_probe_version, args=(_found[t],), daemon=True)
                   for t in todo if _found[t].ok]
        for th in threads:
            th.start()
        for th in threads:
            th.join(8)
    _pdk = detect_pdk()
    _save_cache()
    return dict(_found)


def detect_pdk() -> PDKInfo:
    root = os.environ.get("PDK_ROOT")
    cands = [root] if root else []
    cands += [str(HOME / ".volare"), str(HOME / ".ciel"), "/usr/local/share/pdk", "/opt/pdk"]
    name_env = os.environ.get("PDK")
    for r in cands:
        if not r or not Path(r).is_dir():
            continue
        names = [name_env] if name_env else []
        names += ["sky130A", "sky130B", "gf180mcuD", "gf180mcuC", "ihp-sg13g2"]
        for n in names:
            if n and (Path(r) / n / "libs.ref").is_dir():
                std = os.environ.get("STD_CELL_LIBRARY") or _default_stdcell(Path(r) / n)
                return PDKInfo(r, n, std)
    return PDKInfo()


def _default_stdcell(pdk_dir: Path) -> str | None:
    pref = ["sky130_fd_sc_hd", "gf180mcu_fd_sc_mcu7t5v0", "sg13g2_stdcell"]
    libref = pdk_dir / "libs.ref"
    for p in pref:
        if (libref / p).is_dir():
            return p
    for d in sorted(libref.iterdir()) if libref.is_dir() else []:
        if (d / "lef").is_dir() and (d / "lib").is_dir():
            return d.name
    return None


def pdk() -> PDKInfo:
    global _pdk
    if _pdk is None:
        _pdk = detect_pdk()
    return _pdk


def setup_environment() -> None:
    """Export PDK vars and make discovered tool dirs reachable for child processes.

    Tool dirs are *appended* to PATH so system binaries (python, etc.) keep
    priority; nix store bin dirs only contain that one package's binaries.
    """
    p = pdk()
    if p.root:
        os.environ.setdefault("PDK_ROOT", p.root)
    if p.name:
        os.environ.setdefault("PDK", p.name)
    if p.stdcell:
        os.environ.setdefault("STD_CELL_LIBRARY", p.stdcell)
    path = os.environ.get("PATH", "").split(os.pathsep)
    for t in _found.values():
        if t.path:
            d = str(Path(t.path).parent)
            if d not in path:
                path.append(d)
    os.environ["PATH"] = os.pathsep.join(path)


def tool(name: str) -> ToolInfo:
    if name not in _found:
        find_tool(name)
    return _found.get(name, ToolInfo(name))


def tool_status() -> list[tuple[str, ToolInfo]]:
    return [(t, _found.get(t, ToolInfo(t))) for t in TOOLS]


def shorten(path: str | None, width: int = 58) -> str:
    if not path:
        return ""
    s = path.replace(str(HOME), "~")
    if s.startswith("/nix/store/") and len(s) > width:
        # /nix/store/<hash>-openroad-2024-..../bin/openroad → /nix/store/…-openroad-…/bin/openroad
        parts = s.split("/")
        pkg = parts[3]
        pkg = "…" + pkg[32:] if len(pkg) > 33 else pkg
        s = "/".join(parts[:3] + [pkg] + parts[4:])
    if len(s) > width:
        s = "…" + s[-(width - 1):]
    return s
