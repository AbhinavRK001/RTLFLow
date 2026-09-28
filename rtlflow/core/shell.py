"""Base interactive shell shared by rtlflow / genus / innovus / nclaunch.

Gives every tool the same Cadence-flavoured behaviour:
  * numbered prompts           innovus 3>   @genus:root: 7>
  * Tcl-ish command syntax     braces, quotes, $vars, ;, \\-continuation, # comments
  * `set` / `puts` / `source` / `history` / `help` / `man` / `redirect`-style `> file`
  * <tool>.logN + <tool>.cmdN in .rtlflow/logs
  * readline history + tab completion (commands, then file paths)
  * one RLock so terminal and GUI commands never interleave on the backend
"""

from __future__ import annotations

import glob
import os
import shlex
import subprocess
import sys
import threading
import time
import traceback
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .msg import C, console, error, info, note, out, rl_safe, rule, warn

try:
    import readline
    _HAVE_RL = True
except Exception:  # pragma: no cover
    readline = None  # type: ignore
    _HAVE_RL = False


class ShellExit(Exception):
    pass


class CmdError(Exception):
    """Raised by commands to signal a user-level error (already reported or with message)."""

    def __init__(self, msg: str = "", msg_id: str = "") -> None:
        super().__init__(msg)
        self.msg_id = msg_id


@dataclass
class Command:
    name: str
    func: Callable
    category: str = "General"
    usage: str = ""
    help: str = ""
    aliases: list[str] = field(default_factory=list)
    hidden: bool = False


def command(name: str, *, cat: str = "General", usage: str = "", help: str = "",
            aliases: list[str] | None = None, hidden: bool = False):
    """Decorator marking a Shell method as a command."""
    def deco(fn):
        fn._rf_cmd = Command(name, fn, cat, usage, help, aliases or [], hidden)
        return fn
    return deco


# ─────────────────────────────── Tcl-ish lexer ───────────────────────────────

def split_commands(script: str) -> list[str]:
    """Split a script into complete commands (newline / ';'), respecting {} "" []."""
    cmds, cur = [], []
    depth_b = depth_k = 0
    in_q = False
    i = 0
    s = script.replace("\\\n", " ")
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            cur.append(s[i:i + 2]); i += 2; continue
        if ch == '"' and depth_b == 0:
            in_q = not in_q
        elif not in_q:
            if ch == "{":
                depth_b += 1
            elif ch == "}":
                depth_b = max(0, depth_b - 1)
            elif ch == "[" and depth_b == 0:
                depth_k += 1
            elif ch == "]" and depth_b == 0:
                depth_k = max(0, depth_k - 1)
        if ch in "\n;" and depth_b == 0 and depth_k == 0 and not in_q:
            c = "".join(cur).strip()
            if c:
                cmds.append(c)
            cur = []
        else:
            cur.append(ch)
        i += 1
    c = "".join(cur).strip()
    if c:
        cmds.append(c)
    # drop comments
    return [c for c in cmds if not c.lstrip().startswith("#")]


def is_complete(script: str) -> bool:
    depth_b = depth_k = 0
    in_q = False
    esc = False
    for ch in script:
        if esc:
            esc = False; continue
        if ch == "\\":
            esc = True; continue
        if ch == '"' and depth_b == 0:
            in_q = not in_q
        elif not in_q:
            if ch == "{": depth_b += 1
            elif ch == "}": depth_b -= 1
            elif ch == "[" and depth_b == 0: depth_k += 1
            elif ch == "]" and depth_b == 0: depth_k -= 1
    return depth_b <= 0 and depth_k <= 0 and not in_q and not script.rstrip().endswith("\\")


def tokenize(cmd: str, subst: Callable[[str], str]) -> list[str]:
    """Split a single command into words. {..} is literal, "..." and bare get $subst."""
    words: list[str] = []
    i, n = 0, len(cmd)
    while i < n:
        while i < n and cmd[i] in " \t":
            i += 1
        if i >= n:
            break
        ch = cmd[i]
        if ch == "{":
            depth, j = 1, i + 1
            while j < n and depth:
                if cmd[j] == "\\":
                    j += 2; continue
                if cmd[j] == "{": depth += 1
                elif cmd[j] == "}": depth -= 1
                j += 1
            words.append(cmd[i + 1:j - 1])
            i = j
        elif ch == '"':
            j = i + 1
            buf = []
            while j < n and cmd[j] != '"':
                if cmd[j] == "\\" and j + 1 < n:
                    buf.append(cmd[j + 1]); j += 2; continue
                buf.append(cmd[j]); j += 1
            words.append(subst("".join(buf)))
            i = j + 1
        else:
            j = i
            depth_k = 0
            while j < n and (cmd[j] not in " \t" or depth_k):
                if cmd[j] == "[": depth_k += 1
                elif cmd[j] == "]": depth_k = max(0, depth_k - 1)
                j += 1
            words.append(subst(cmd[i:j]))
            i = j
    return words


def parse_opts(args: list[str], flags: set[str] | None = None,
               valued: dict[str, int | str] | None = None,
               prefix_ok: bool = True) -> tuple[dict, list[str]]:
    """Cadence-style option parser.

    flags:  {"-sv", "-nowin"}                     → opts["-sv"] = True
    valued: {"-top": 1, "-lef": "+", "-r": 3}     → 1 value, 1+ until next -opt, or N values
    Unique prefixes are accepted (-util → -utilization) like Tcl commands do.
    """
    flags = flags or set()
    valued = valued or {}
    known = list(flags) + list(valued)
    opts: dict = {}
    pos: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        key = None
        if a.startswith("-") and len(a) > 1 and not _is_number(a):
            if a in known:
                key = a
            elif prefix_ok:
                m = [k for k in known if k.startswith(a)]
                if len(m) == 1:
                    key = m[0]
            if key is None:
                raise CmdError(f"Unknown option '{a}'. Valid options: {' '.join(sorted(known)) or '(none)'}")
        if key is None:
            pos.append(a); i += 1; continue
        if key in flags:
            opts[key] = True; i += 1; continue
        spec = valued[key]
        if spec == "+":
            vals = []
            i += 1
            while i < len(args) and not (args[i].startswith("-") and not _is_number(args[i])):
                vals.extend(args[i].split()); i += 1
            if not vals:
                raise CmdError(f"Option {key} requires a value")
            opts[key] = vals
        else:
            k = int(spec)
            vals = args[i + 1:i + 1 + k]
            # allow "-d {10 10 10 10}" as a single braced token
            if k > 1 and len(vals) >= 1 and len(vals[0].split()) == k:
                vals = vals[0].split()
                i += 2
            else:
                if len(vals) < k:
                    raise CmdError(f"Option {key} requires {k} value(s)")
                i += 1 + k
            opts[key] = vals[0] if k == 1 else vals
    return opts, pos


def _is_number(s: str) -> bool:
    try:
        float(s); return True
    except ValueError:
        return False


def expand_files(items: list[str], base: Path | None = None) -> list[str]:
    """Expand braces/space lists, ~, $VARS and globs; keep order, de-dup."""
    out_files: list[str] = []
    for it in items:
        for w in it.split():
            w = os.path.expandvars(os.path.expanduser(w))
            if base and not os.path.isabs(w):
                w = str(base / w)
            hits = sorted(glob.glob(w)) if any(c in w for c in "*?[") else [w]
            for h in hits:
                if h not in out_files:
                    out_files.append(h)
    return out_files


# ─────────────────────────────────── Shell ───────────────────────────────────

class Shell:
    tool = "rtlflow"
    msg_prefix = "RFS"
    history_name = ".rtlflow_history"
    no_timing: set = {"genus", "innovus", "nclaunch", "xrun", "simvision", "source", "exit"}

    def __init__(self) -> None:
        self.vars: dict[str, str] = {}
        self.counter = 1
        self.exec_lock = threading.RLock()
        self.running = True
        self.commands: dict[str, Command] = {}
        self.aliases: dict[str, str] = {}
        self._register()
        self.cwd_changed_hooks: list[Callable] = []

    # registry ------------------------------------------------------------
    def _register(self) -> None:
        for klass in reversed(type(self).__mro__):
            for attr, val in vars(klass).items():
                c = getattr(val, "_rf_cmd", None)
                if c:
                    # bind the decorated function itself: a subclass method that happens to share
                    # the Python name must not hijack another command (e.g. `clear`)
                    bound = types.MethodType(val, self)
                    cmd = Command(c.name, bound, c.category, c.usage, c.help, c.aliases, c.hidden)
                    self.commands[c.name] = cmd
                    for a in c.aliases:
                        self.aliases[a] = c.name

    def lookup(self, name: str) -> Command | None:
        name = self.aliases.get(name, name)
        return self.commands.get(name)

    # prompt --------------------------------------------------------------
    def prompt(self) -> str:
        return f"{C.BOLD}{self.tool} {self.counter}>{C.RESET} "

    # substitution ----------------------------------------------------------
    def subst(self, s: str) -> str:
        if "$" not in s:
            return s
        outp, i, n = [], 0, len(s)
        while i < n:
            if s[i] == "\\" and i + 1 < n and s[i + 1] == "$":
                outp.append("$"); i += 2; continue
            if s[i] == "$":
                if i + 1 < n and s[i + 1] == "{":
                    j = s.find("}", i + 2)
                    if j > 0:
                        outp.append(self._var(s[i + 2:j])); i = j + 1; continue
                j = i + 1
                while j < n and (s[j].isalnum() or s[j] in "_:"):
                    j += 1
                name = s[i + 1:j]
                if name.startswith("env(") or (j < n and s[j] == "(" and name == "env"):
                    k = s.find(")", j)
                    key = s[j + 1:k]
                    outp.append(os.environ.get(key, "")); i = k + 1; continue
                if name:
                    outp.append(self._var(name)); i = j; continue
            outp.append(s[i]); i += 1
        return "".join(outp)

    def _var(self, name: str) -> str:
        name = name.lstrip(":")
        if name in self.vars:
            return self.vars[name]
        if name in os.environ:
            return os.environ[name]
        return "$" + name  # leave unknown vars for backends (OpenROAD Tcl)

    # execution -------------------------------------------------------------
    def run_script(self, script: str, source: str = "terminal", echo: bool = False) -> bool:
        okall = True
        for cmd in split_commands(script):
            if echo:
                out(f"{C.GRY}{self.tool}> {cmd}{C.RESET}")
            if not self.run_line(cmd, source=source):
                okall = False
        return okall

    def run_line(self, line: str, source: str = "terminal") -> bool:
        """Execute one command. Returns True on success."""
        line = line.strip()
        if not line or line.startswith("#"):
            return True
        with self.exec_lock:
            console.record_cmd(line, "" if source == "terminal" else source)
            if line.startswith("!"):
                return self._sh(line[1:])
            # output redirection:  cmd args > file   /  >> file
            redirect = None
            for op in (">>", ">"):
                marker = f" {op} "
                if marker in line and not line.split()[0] in ("puts", "set", "expr"):
                    head, _, tgt = line.rpartition(marker)
                    if tgt.strip() and "{" not in tgt and "}" not in tgt:
                        redirect = (op, os.path.expanduser(tgt.strip()))
                        line = head.strip()
                        break
            if redirect:
                console.begin_capture()
            try:
                return self._dispatch(line, source)
            finally:
                if redirect:
                    text = console.end_capture()
                    op, fn = redirect
                    try:
                        Path(fn).parent.mkdir(parents=True, exist_ok=True)
                        with open(fn, "a" if op == ">>" else "w") as f:
                            f.write(text)
                        info(f"Report written to {fn}")
                    except OSError as e:
                        error(f"{self.msg_prefix}-IO-001", f"Cannot write {fn}: {e}")

    def _dispatch(self, line: str, source: str) -> bool:
        words = tokenize(line, self.subst)
        if not words:
            return True
        name, args = words[0], words[1:]
        cmd = self.lookup(name)
        t0 = time.time()
        try:
            if cmd is None:
                return self.default(name, args, line, source)
            res = cmd.func(args)
            return res is not False
        except CmdError as e:
            if str(e):
                error(e.msg_id or f"{self.msg_prefix}-CMD-001", f"{name}: {e}")
            return False
        except ShellExit:
            raise
        except KeyboardInterrupt:
            warn(f"{self.msg_prefix}-INT-001", f"{name} interrupted by user")
            return False
        except Exception as e:  # internal bug — never crash the shell
            error(f"{self.msg_prefix}-INT-999", f"Internal error in '{name}': {e}")
            console.log_only(traceback.format_exc())
            if os.environ.get("RTLFLOW_DEBUG"):
                traceback.print_exc()
            return False
        finally:
            dt = time.time() - t0
            if dt > 2.0 and cmd is not None and not cmd.hidden and cmd.name not in self.no_timing:
                info(f"{name} finished in {dt:.1f}s")

    def default(self, name: str, args: list[str], line: str, source: str) -> bool:
        close = [c for c in self.commands if c.lower().startswith(name.lower()[:4])][:5]
        hint = f"  Did you mean: {', '.join(close)}?" if close else "  Type 'help' for the command list."
        error(f"{self.msg_prefix}-CMD-404", f"Unknown command '{name}'.{hint}")
        return False

    def _sh(self, cmd: str) -> bool:
        r = subprocess.run(cmd, shell=True)
        return r.returncode == 0

    # main loop -------------------------------------------------------------
    def banner(self) -> None:
        pass

    def on_start(self) -> None:
        pass

    def on_exit(self) -> None:
        pass

    def _setup_readline(self) -> None:
        if not _HAVE_RL:
            return
        hist = Path.home() / self.history_name
        try:
            readline.read_history_file(hist)
        except OSError:
            pass
        readline.set_history_length(5000)
        readline.set_completer_delims(" \t\n{}\"'[];")
        readline.set_completer(self._complete)
        readline.parse_and_bind("tab: complete")
        self._hist_file = hist

    def _save_history(self) -> None:
        if _HAVE_RL and getattr(self, "_hist_file", None):
            try:
                readline.write_history_file(self._hist_file)
            except OSError:
                pass

    def _complete(self, text: str, state: int):
        try:
            buf = readline.get_line_buffer()
            first = len(buf.lstrip().split()) <= 1 and not buf.endswith(" ")
            if first:
                names = sorted(set(self.commands) | set(self.aliases))
                opts = [n for n in names if n.startswith(text)]
            else:
                t = os.path.expanduser(text)
                opts = []
                for p in glob.glob(t + "*"):
                    opts.append(p + ("/" if os.path.isdir(p) else ""))
                if not opts:
                    word = buf.split()[0]
                    cmd = self.lookup(word)
                    if cmd and cmd.usage:
                        opts = [w.strip("[]|") for w in cmd.usage.split() if w.strip("[]|").startswith(text) and w.strip("[]|").startswith("-")]
            return opts[state] if state < len(opts) else None
        except Exception:
            return None

    def read_command(self) -> str:
        """Read one complete (brace-balanced) command from the terminal."""
        prompt = self.prompt()
        console.prompt = prompt
        console.prompt_active = True
        try:
            line = input(rl_safe(prompt))
            while not is_complete(line):
                console.prompt = "> "
                line += "\n" + input("> ")
            return line
        finally:
            console.prompt_active = False

    def cmdloop(self) -> int:
        self._setup_readline()
        self.on_start()
        try:
            while self.running:
                try:
                    line = self.read_command()
                except KeyboardInterrupt:
                    console.write("")
                    continue
                except EOFError:
                    console.write("")
                    break
                if not line.strip():
                    continue
                console.log_only(f"{self.tool} {self.counter}> {line}")
                self.counter += 1
                try:
                    self.run_script(line)
                except ShellExit:
                    break
        finally:
            self._save_history()
            self.on_exit()
        return 0

    # ───────────────────────── common built-in commands ─────────────────────────
    @command("help", cat="Shell", usage="help [command|pattern]", help="List commands or show help for one")
    def c_help(self, args):
        if args:
            pat = args[0]
            cmd = self.lookup(pat)
            if cmd:
                return self.c_man([cmd.name])
            hits = [c for c in self.commands.values() if pat.lower() in c.name.lower() and not c.hidden]
            for c in sorted(hits, key=lambda c: c.name):
                out(f"  {C.BOLD}{c.name:<24}{C.RESET}{c.help}")
            if not hits:
                warn(f"{self.msg_prefix}-HLP-001", f"No command matches '{pat}'")
            return True
        cats: dict[str, list[Command]] = {}
        for c in self.commands.values():
            if not c.hidden:
                cats.setdefault(c.category, []).append(c)
        order = self.help_order() + sorted(k for k in cats if k not in self.help_order())
        for cat in order:
            if cat not in cats:
                continue
            out(rule(cat))
            for c in sorted(cats[cat], key=lambda c: c.name.lower()):
                al = f" {C.GRY}({', '.join(c.aliases)}){C.RESET}" if c.aliases else ""
                out(f"  {C.BOLD}{c.name:<22}{C.RESET}{c.help}{al}")
        out(f"\n  {C.GRY}man <command> for usage · any unknown line is an error unless the tool forwards it to its backend{C.RESET}")
        return True

    def help_order(self) -> list[str]:
        return []

    @command("man", cat="Shell", usage="man <command>", help="Show usage for a command")
    def c_man(self, args):
        if not args:
            raise CmdError("usage: man <command>")
        cmd = self.lookup(args[0])
        if not cmd:
            raise CmdError(f"No manual entry for {args[0]}")
        out(f"{C.BOLD}{cmd.name}{C.RESET}  —  {cmd.help}")
        if cmd.usage:
            out(f"\n  {C.CYA}Usage:{C.RESET} {cmd.usage}")
        doc = (cmd.func.__doc__ or "").strip()
        if doc:
            out("")
            for l in doc.splitlines():
                out("  " + l.strip())
        return True

    @command("set", cat="Shell", usage="set <var> [value]", help="Set or print a variable")
    def c_set(self, args):
        if not args:
            for k in sorted(self.vars):
                out(f"  {k} = {self.vars[k]}")
            return True
        if len(args) == 1:
            if args[0] not in self.vars:
                raise CmdError(f"can't read \"{args[0]}\": no such variable")
            out(self.vars[args[0]])
            return True
        self.vars[args[0]] = " ".join(args[1:])
        self.on_set(args[0], self.vars[args[0]])
        return True

    def on_set(self, name: str, value: str) -> None:
        pass

    @command("unset", cat="Shell", usage="unset <var>", help="Remove a variable", hidden=True)
    def c_unset(self, args):
        for a in args:
            self.vars.pop(a, None)
        return True

    @command("puts", cat="Shell", usage="puts <text>", help="Print text")
    def c_puts(self, args):
        if args and args[0] == "-nonewline":
            console.write(" ".join(args[1:]), end="")
        else:
            out(" ".join(args))
        return True

    @command("setenv", cat="Shell", usage="setenv NAME value", help="Set an environment variable")
    def c_setenv(self, args):
        if len(args) < 2:
            raise CmdError("usage: setenv NAME value")
        os.environ[args[0]] = " ".join(args[1:])
        return True

    @command("getenv", cat="Shell", usage="getenv NAME", help="Print an environment variable")
    def c_getenv(self, args):
        for a in args:
            out(os.environ.get(a, ""))
        return True

    @command("source", cat="Shell", usage="source [-echo] <file.tcl>", help="Run a script of commands")
    def c_source(self, args):
        opts, pos = parse_opts(args, flags={"-echo", "-verbose"})
        if not pos:
            raise CmdError("usage: source <file>")
        fn = Path(os.path.expanduser(pos[0]))
        if not fn.exists():
            raise CmdError(f"File not found: {fn}")
        note(f"Sourcing {fn}")
        ok_ = self.run_script(fn.read_text(), source=f"source {fn.name}",
                              echo=bool(opts.get("-echo") or opts.get("-verbose")) or True)
        if not ok_:
            warn(f"{self.msg_prefix}-SRC-001", f"{fn.name}: one or more commands failed")
        return ok_

    @command("history", cat="Shell", usage="history [n]", help="Show command history")
    def c_history(self, args):
        if not _HAVE_RL:
            raise CmdError("readline not available")
        n = readline.get_current_history_length()
        k = int(args[0]) if args else 30
        for i in range(max(1, n - k + 1), n + 1):
            out(f"  {i:5d}  {readline.get_history_item(i)}")
        return True

    @command("cd", cat="Shell", usage="cd [dir]", help="Change directory")
    def c_cd(self, args):
        d = Path(os.path.expanduser(args[0] if args else "~"))
        try:
            os.chdir(d)
        except OSError as e:
            raise CmdError(str(e))
        out(os.getcwd())
        for h in self.cwd_changed_hooks:
            h()
        return True

    @command("pwd", cat="Shell", help="Print working directory")
    def c_pwd(self, args):
        out(os.getcwd())
        return True

    @command("ls", cat="Shell", usage="ls [args]", help="List files")
    def c_ls(self, args):
        return self._sh("ls --color=auto " + " ".join(shlex.quote(a) for a in args))

    @command("sh", cat="Shell", usage="sh <command>   (or !command)", help="Run a Unix command")
    def c_sh(self, args):
        return self._sh(" ".join(args))

    @command("clear", cat="Shell", help="Clear the terminal", hidden=True)
    def c_clear(self, args):
        sys.stdout.write("\x1b[2J\x1b[H"); sys.stdout.flush()
        return True

    @command("exit", cat="Shell", help="Leave this tool", aliases=["quit", "exit_tool"])
    def c_exit(self, args):
        self.running = False
        raise ShellExit()
