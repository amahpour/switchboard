"""Shared machinery of ``switchboard uninstall`` (DESIGN.md §9.7).

``switchboard install`` is retired (0.38.0, #385: agents connect over HTTPS now, DESIGN.md §49), but a
machine that installed before still has switchboard's entries in its harnesses' configs, so
``switchboard uninstall <h|all>`` stays: ``unplan()`` per harness returns FileEdit and CommandEdit
items, and nothing is touched until the user confirms.

- It removes only switchboard's own entries, recognised the way install recognised its older ones (a hook
  command containing ``H="<home>/hooks/switchboard_hook-``, the MCP entry that runs
  ``-m switchboard mcp --home <home>``, the Codex marker block, Devin's nine allow names).
- The diff shows only those entries, through an allowlisted view (an MCP entry's ``command`` and ``args``,
  a hook's ``command``, ...; every other value ``***``, and secret-looking values masked), since the user
  may have edited them: other servers' env blocks are never printed.
- ``Apply? [y/N]``; with no TTY and no ``--yes`` it refuses. ``--dry-run`` only shows the diff.
- Each file is backed up to ``<file>.bak-switchboard-YYYYmmddHHMMSS`` (0600 unless the original was already
  private) and written atomically.
- Re-running is idempotent ("nothing to remove"). Missing files are skipped. An editable install of
  switchboard is not refused (removing entries can't make an agent's edits run).
- ``--purge-hooks`` also deletes the hook copies in ``<home>/hooks`` once no config runs one.
"""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from switchboard import DIST_NAME
from switchboard.colors import PLAIN, Paint, for_args

MASK_RE = re.compile(r"token|key|secret|pass|auth|env|url", re.IGNORECASE)


class InstallError(Exception):
    pass


@dataclass
class FileEdit:
    path: Path
    before: str | None
    after: str
    display: list[str]
    label: str
    mode: int = 0o600

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass
class CommandEdit:
    argv: list[str]
    display: str
    note: str = ""
    changed: bool = True


@dataclass
class Missing:
    """A config file uninstall would look at that isn't there (skipped)."""

    label: str
    changed: bool = False


@dataclass
class PurgeEdit:
    """``uninstall --purge-hooks``: remove switchboard's hook copies from ``<home>/hooks``."""

    label: str
    files: list[Path]
    blocked_by: list[str] = field(default_factory=list)
    problem: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.files) and not self.blocked_by and not self.problem


Edit = FileEdit | CommandEdit | Missing | PurgeEdit

HARNESSES = ("claude", "codex", "cursor", "devin")
# the user-level files install wrote hook commands into (``--purge-hooks`` checks them)
HOOK_CONFIGS: dict[str, tuple[str, ...]] = {
    "claude": (".claude", "settings.json"),
    "codex": (".codex", "hooks.json"),
    "cursor": (".cursor", "hooks.json"),
    "devin": (".config", "devin", "config.json"),
}
HOOK_COPY_RE = re.compile(r"switchboard_hook-[0-9a-f]{12}\.py")


@dataclass
class Plan:
    harness: str
    edits: list[Edit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    verb: str = "uninstall"
    title: str = ""

    @property
    def changed(self) -> bool:
        return any(e.changed for e in self.edits)

    @property
    def unchanged_text(self) -> str:
        return "nothing to remove"


# ------------------------------------------------------------------ recognising
def hook_ref(home: str) -> str:
    """The path prefix of every switchboard hook copy for ``home`` (any version).
    A plain substring: used where over-matching is the safe side (purge)."""
    return f"{home}/hooks/switchboard_hook-"


def is_switchboard_hook(command: Any, home: str) -> bool:
    """A hook command that switchboard wrote for this home (any hook version).

    Matched with the ``H="`` every hook command has had since M2, so a home
    whose path merely ends with this one (``/x/a/b`` vs ``/a/b``) isn't ours."""
    return isinstance(command, str) and f'H="{hook_ref(home)}' in command


def mentions_hook(text: str, home: str) -> bool:
    """Raw (JSON-escaped or not) text contains a switchboard hook command for ``home``."""
    return re.search(r'H=\\?"' + re.escape(hook_ref(home)), text) is not None


_OTHER_HOOK_HOME_RE = re.compile(r'H=\\?"([^"\\]+?)/hooks/switchboard_hook-[0-9a-f]{12}\.py')


def other_hook_homes(text: str, home: str) -> list[str]:
    """switchboard homes other than ``home`` that hook commands in ``text`` point at."""
    return sorted({m.group(1) for m in _OTHER_HOOK_HOME_RE.finditer(text)} - {home})


def runs_switchboard_mcp(entry: Any) -> bool:
    """Whether an MCP entry runs switchboard's MCP server
    (``<python> ... -m switchboard mcp ...``), for any home."""
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
        return False
    args = entry.get("args")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return False
    return any(args[i : i + 3] == ["-m", "switchboard", "mcp"] for i in range(len(args)))


def mcp_home(entry: Any) -> str | None:
    """The ``--home`` of an MCP entry that runs switchboard's MCP server
    (``<python> ... -m switchboard mcp ... --home H``), else None."""
    if not runs_switchboard_mcp(entry):
        return None
    args = entry["args"]
    try:
        return args[args.index("--home") + 1]
    except (ValueError, IndexError):
        return None


def safe_text(s: str) -> str:
    """``s`` for the terminal: non-printable characters (escape sequences, CR)
    shown escaped, so a value read from a config file can't rewrite the screen."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in s)


def shell_arg(s: str) -> str:
    """``s`` as one argument of a command the user may paste: shell-quoted, or
    the placeholder ``DIR`` if it has non-printable characters."""
    return shlex.quote(s) if s.isprintable() else "DIR"


def other_home_hint(harness: str, other: str) -> str:
    return f"run `switchboard uninstall {harness} --home {shell_arg(other)}` for it"


def foreign_mcp_note(where: str, entry: Any, harness: str) -> str:
    """Why an MCP server named switchboard was left alone."""
    other = mcp_home(entry)
    if other is not None:
        return (
            f"{where}: the switchboard MCP server there is for another switchboard home ({safe_text(other)});"
            f" left alone ({other_home_hint(harness, other)})"
        )
    return f"{where}: an MCP server named switchboard that isn't switchboard's; left alone"


def mask_args(args: list[Any]) -> list[Any]:
    """An argv for the diff: the value after a secret-looking flag
    (``--api-key X``) and of ``--token=X`` masked."""
    shown: list[Any] = []
    hide_next = False
    for a in args:
        if not isinstance(a, str) or hide_next:
            shown.append("***")
            hide_next = False
            continue
        flag, eq, _ = a.partition("=")
        if a.startswith("-") and eq and MASK_RE.search(flag):
            shown.append(f"{flag}=***")
            continue
        shown.append(a)
        hide_next = a.startswith("-") and not eq and MASK_RE.search(a) is not None
    return shown


def mcp_view(entry: Any) -> Any:
    """A removed MCP entry for the diff: its ``command`` and ``args`` (see
    ``mask_args``); every other key (``env``, ``headers``, ...) shows ``***``."""
    if not isinstance(entry, dict):
        return "***"
    return {
        k: (
            v
            if k == "command" and isinstance(v, str)
            else mask_args(v)
            if k == "args" and isinstance(v, list)
            else "***"
        )
        for k, v in entry.items()
    }


_HOOK_KEYS = frozenset({"matcher", "type", "command", "timeout", "loop_limit"})


def hook_view(obj: Any) -> Any:
    """A removed hook group or handler for the diff: the keys install wrote;
    any other key the user added shows ``***``."""
    if isinstance(obj, list):
        return [hook_view(v) for v in obj]
    if not isinstance(obj, dict):
        return obj
    return {k: (hook_view(v) if k == "hooks" else v if k in _HOOK_KEYS else "***") for k, v in obj.items()}


# ------------------------------------------------------------------- json
def mask(obj: Any) -> Any:
    """Values of secret-looking keys (a whole ``env``/``headers`` object too) as ``***``."""
    if isinstance(obj, dict):
        return {k: ("***" if MASK_RE.search(str(k)) else mask(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(v) for v in obj]
    return obj


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def load_json_obj(path: Path) -> tuple[str | None, dict[str, Any]]:
    text = read_text(path)
    if text is None or not text.strip():
        return text, {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise InstallError(f"{path} is not valid JSON ({e}); fix it by hand first") from None
    if not isinstance(data, dict):
        raise InstallError(f"{path} must contain a JSON object")
    return text, data


def dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def compact(obj: Any) -> str:
    return safe_text(json.dumps(mask(obj), ensure_ascii=False))


def remove_hook_groups(
    data: dict[str, Any], home: str, *, drop_empty_hooks: bool, positional: bool = False
) -> tuple[list[str], int]:
    """Undo install's hook groups (``hooks.<Event> = [{..., "hooks": [...]}]``).

    Every switchboard hook for this home is removed, in any event (older versions
    too). A group left with no hooks is dropped (install always made its own
    group), then an event array the removal emptied, and, if
    ``drop_empty_hooks``, a ``hooks`` object it emptied. A group, array or
    object that was already empty is left alone.

    ``positional`` (Codex, which keys hook trust by event:group:handler
    position): each of the user's groups or handlers that moves up gets a
    ``!`` line. Returns (display lines, how many of the user's hooks moved).
    """
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return [], 0
    lines: list[str] = []
    moved = 0
    for event in list(hooks):
        arr = hooks[event]
        if not isinstance(arr, list):
            continue
        kept: list[Any] = []
        removed = False
        shifts: list[str] = []
        for gi, g in enumerate(arr):
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                ours = [
                    hi
                    for hi, h in enumerate(g["hooks"])
                    if isinstance(h, dict) and is_switchboard_hook(h.get("command"), home)
                ]
                if ours:
                    removed = True
                    if len(ours) == len(g["hooks"]):
                        lines.append(f"  - hooks.{event}[{gi}]: {compact(hook_view(g))}")
                        continue
                    for hi in ours:
                        lines.append(
                            f"  - hooks.{event}[{gi}].hooks[{hi}]: {compact(hook_view(g['hooks'][hi]))}"
                        )
                    inner = [h for hi, h in enumerate(g["hooks"]) if hi not in ours]
                    if len(kept) == gi:  # the group keeps its index; later handlers move up
                        for new_hi, old_hi in enumerate(
                            hi for hi in range(len(g["hooks"])) if hi not in ours
                        ):
                            if new_hi != old_hi:
                                moved += 1
                                shifts.append(
                                    f"  ! hooks.{event}[{gi}].hooks[{old_hi}] (not switchboard's) moves to"
                                    f" hooks.{event}[{gi}].hooks[{new_hi}]"
                                )
                    g = {**g, "hooks": inner}
            if len(kept) != gi:
                moved += 1
                shifts.append(
                    f"  ! hooks.{event}[{gi}] (not switchboard's) moves to hooks.{event}[{len(kept)}]"
                )
            kept.append(g)
        if not removed:
            continue
        if positional:
            lines += shifts
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if lines and not hooks and drop_empty_hooks:
        del data["hooks"]
    return lines, (moved if positional else 0)


def json_removal(plan: Plan, path: Path, user_home: Path, home: str, remove: Any) -> None:
    """Add the FileEdit for removing switchboard's entries from one JSON file.

    ``remove(data) -> display lines`` edits ``data`` in place. A missing file
    is skipped; an unchanged file keeps its bytes. If switchboard's hook path
    still appears in the result (a shape uninstall doesn't know), a note says
    so rather than guessing."""
    label = tilde(path, user_home)
    before, data = load_json_obj(path)
    if before is None:
        plan.edits.append(Missing(label))
        return
    new = deep(data)
    lines = remove(new)
    after = dump_json(new) if lines else before
    plan.edits.append(FileEdit(path=path, before=before, after=after, display=lines, label=label))
    if mentions_hook(after, home):
        plan.notes.append(
            f"{label} still mentions switchboard's hooks somewhere uninstall doesn't recognise;"
            " remove them by hand"
        )
    others = other_hook_homes(after, home)
    if others:
        plan.notes.append(
            f"{label} has switchboard hooks for another switchboard home"
            f" ({', '.join(safe_text(o) for o in others)}); left alone"
            f" (run `switchboard uninstall {plan.harness} --home DIR` for those)"
        )


# ---------------------------------------------------------------- writing
def backup_path(path: Path, now: _dt.datetime | None = None) -> Path:
    stamp = (now or _dt.datetime.now()).strftime("%Y%m%d%H%M%S")
    return path.with_name(f"{path.name}.bak-switchboard-{stamp}")


def backup(path: Path) -> Path | None:
    """Copy ``path`` aside with its mode, or 0600 if the original is more open."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    dst = backup_path(path)
    n = 1
    while dst.exists():
        dst = dst.with_name(f"{backup_path(path).name}.{n}")
        n += 1
    mode = st.st_mode & 0o777
    if mode & 0o077:
        mode = 0o600
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as f, open(path, "rb") as src:
        shutil.copyfileobj(src, f)
    os.chmod(dst, mode)
    return dst


def atomic_write(path: Path, text: str, default_mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = default_mode
    # mkstemp: a fresh random name opened O_CREAT|O_EXCL (0600), so a planted
    # symlink at a predictable temp path can't redirect the write.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.switchboard-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------- checks
def editable_install() -> bool:
    """True if switchboard is installed from a source tree in editable mode."""
    try:
        from importlib.metadata import distribution

        raw = distribution(DIST_NAME).read_text("direct_url.json")
    except Exception:
        return False
    if not raw:
        return False
    try:
        info = json.loads(raw)
    except ValueError:
        return False
    return bool((info.get("dir_info") or {}).get("editable"))


def confirm(
    yes: bool, stdin: TextIO | None = None, stdout: TextIO | None = None, paint: Paint | None = None
) -> bool:
    if yes:
        return True
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if not (hasattr(stdin, "isatty") and stdin.isatty()):
        print("switchboard: no terminal to confirm on; re-run with --yes to apply", file=sys.stderr)
        return False
    stdout.write((paint or PLAIN).prompt("Apply? [y/N]") + " ")
    stdout.flush()
    return stdin.readline().strip().lower() in ("y", "yes")


def tilde(path: Path, user_home: Path) -> str:
    try:
        return "~/" + str(path.relative_to(user_home))
    except ValueError:
        return str(path)


def _diff_line(p: Paint, line: str) -> str:
    """One diff line for the terminal: ``safe_text`` first (a value read from a config
    file can't bring its own escape codes), then coloured by switchboard's marker."""
    line = safe_text(line)
    mark = line[:4]
    if mark == "  + ":
        return p.added(line)
    if mark == "  - ":
        return p.removed(line)
    if mark in ("  ~ ", "  ! "):  # moved or re-ordered: nothing lost, but worth a look
        return p.warn(line)
    return line


def _note(p: Paint, text: str) -> str:
    return p.warn("note:") + " " + p.dim(text)


def _status(p: Paint, text: str) -> str:
    """A run's status in the output and the summary: an error red, a skip dim."""
    if text.startswith("error"):
        return p.bad(text)
    if text.startswith("skipped"):
        return p.dim(text)
    return text


def render_plan(plan: Plan, out: TextIO, paint: Paint | None = None) -> None:
    """The diff for one plan. Colour (issue #28) marks only switchboard's framing:
    headers bold, added lines green, removed red, moved yellow, notes and "no
    changes" dim."""
    p = paint or PLAIN
    print(p.heading(f"{plan.title or f'switchboard {plan.verb} {plan.harness}'}:"), file=out)
    same = plan.unchanged_text
    any_change = False
    for e in plan.edits:
        if isinstance(e, Missing):
            print(p.dim(f"{e.label}: not there, skipped"), file=out)
        elif isinstance(e, PurgeEdit):
            if e.problem:
                print(p.warn(f"{e.label}: kept ({e.problem})"), file=out)
            elif e.blocked_by:
                print(p.dim(f"{e.label}: kept (still used by {', '.join(e.blocked_by)})"), file=out)
            elif not e.files:
                print(p.dim(f"{e.label}: no hook copies"), file=out)
            else:
                any_change = True
                print(
                    p.heading(
                        f"{e.label} (delete {len(e.files)} hook cop{'y' if len(e.files) == 1 else 'ies'}):"
                    ),
                    file=out,
                )
                for f in e.files:
                    print(_diff_line(p, f"  - {f.name}"), file=out)
        elif isinstance(e, FileEdit):
            if not e.changed:
                print(p.dim(f"{e.label}: {same}"), file=out)
                continue
            any_change = True
            verb = "create" if e.before is None else "update"
            print(p.heading(f"{e.label} ({verb}{'' if e.before is None else ', with a backup'}):"), file=out)
            for line in e.display:
                print(_diff_line(p, line), file=out)
        else:
            if not e.changed:
                print(p.dim(f"{safe_text(e.display)}: {same}"), file=out)
                continue
            any_change = True
            print(
                p.dim("$") + " " + p.bold(safe_text(e.display)) + (p.dim(f"   # {e.note}") if e.note else ""),
                file=out,
            )
    for n in plan.notes:
        print(_note(p, n), file=out)
    if not any_change:
        print(p.dim(same), file=out)


def apply_purge(e: PurgeEdit, out: TextIO, paint: Paint | None = None) -> None:
    n = 0
    for f in e.files:
        try:
            os.unlink(f)
            n += 1
        except FileNotFoundError:
            pass
    print(
        (paint or PLAIN).ok("deleted") + f" {n} hook cop{'y' if n == 1 else 'ies'} from {e.label}", file=out
    )


def apply_plan(plan: Plan, *, run_commands: bool, out: TextIO, paint: Paint | None = None) -> int:
    """File edits first (they can't fail half-way), then harness commands in
    order. A failed command stops the commands after it (an add after a failed
    remove would fail too) but never the file edits, and makes the result 1."""
    p = paint or PLAIN
    for e in plan.edits:
        if isinstance(e, FileEdit) and e.changed:
            b = backup(e.path)
            atomic_write(e.path, e.after, e.mode)
            print(p.ok("wrote") + f" {e.label}" + (f" (backup {b.name})" if b else ""), file=out)
        elif isinstance(e, PurgeEdit) and e.changed:
            apply_purge(e, out, p)
    for e in plan.edits:
        if not isinstance(e, CommandEdit) or not e.changed:
            continue
        if not run_commands:
            print(p.dim(f"skipped (--user-home): {safe_text(e.display)}"), file=out)
            continue
        try:
            r = subprocess.run(e.argv, capture_output=True, text=True)
            why = f"exit {r.returncode}" if r.returncode != 0 else ""
        except OSError as err:
            why = type(err).__name__
        if why:
            print(
                f"switchboard: `{' '.join(e.argv[:3])} ...` failed ({why}); any file edits"
                f" above were applied. Run it yourself:\n  {e.display}",
                file=sys.stderr,
            )
            return 1
        print(p.ok("ran:") + " " + p.dim(safe_text(e.display)), file=out)
    return 0


# -------------------------------------------------------------------- run
def _module(harness: str) -> Any:
    if harness == "claude":
        from switchboard.install import claude

        return claude
    if harness == "codex":
        from switchboard.install import codex

        return codex
    if harness == "cursor":
        from switchboard.install import cursor

        return cursor
    if harness == "devin":
        from switchboard.install import devin

        return devin
    raise InstallError(f"unknown harness {harness}")


@dataclass
class _Run:
    """One section of an uninstall run: a plan, or why there is none."""

    name: str
    plan: Plan | None = None
    status: str = ""
    error: bool = False
    rc: int | None = None


def _unchanged(plan: Plan) -> str:
    """A plan with nothing to do, for the summary (hook copies kept on purpose say why)."""
    for e in plan.edits:
        if isinstance(e, PurgeEdit) and e.problem:
            return f"kept ({e.problem})"
        if isinstance(e, PurgeEdit) and e.blocked_by:
            return f"kept (still used by {', '.join(e.blocked_by)})"
    return plan.unchanged_text


def _summary(
    runs: list[_Run],
    *,
    applied: bool,
    out: TextIO,
    run_commands: bool = True,
    paint: Paint | None = None,
) -> None:
    p = paint or PLAIN
    done = "removed"
    would = "would remove"
    print(p.heading("summary:"), file=out)
    for r in runs:
        if r.plan is None:
            s = _status(p, r.status)
        elif not r.plan.changed:
            s = p.dim(_unchanged(r.plan))
        elif not applied:
            s = p.warn(would)
        elif r.rc == 0:
            s = p.ok("deleted" if r.name == "hook copies" else done)
            if not run_commands and any(isinstance(e, CommandEdit) and e.changed for e in r.plan.edits):
                s += p.dim(" (files only: harness commands aren't run with --user-home)")
        elif any(isinstance(e, FileEdit) and e.changed for e in r.plan.edits):
            s = p.bad("files written, but a harness command failed (see above)")
        else:
            s = p.bad("a harness command failed (see above)")
        print(f"  {r.name}: {s}", file=out)


def _finish(
    runs: list[_Run],
    args: argparse.Namespace,
    *,
    many: bool,
    stdin: TextIO | None,
    out: TextIO,
    trailer: list[str] | None = None,
) -> int:
    """Render every section, then one confirmation, then apply each changed plan."""
    p = for_args(args, out)  # colour only when ``out`` is a terminal (or --color always)
    for r in runs:
        if r.plan is not None:
            render_plan(r.plan, out, p)
        else:
            print(p.heading(f"switchboard uninstall {r.name}:") + " " + _status(p, r.status), file=out)
    changed = any(r.plan is not None and r.plan.changed for r in runs)
    if changed:
        for n in trailer or []:
            print(_note(p, n), file=out)
    failed = any(r.error for r in runs)
    if args.dry_run or not changed:
        if many:
            _summary(runs, applied=False, out=out, paint=p)
        return 1 if failed else 0
    if not confirm(args.yes, stdin, out, p):
        print(p.warn("not applied"), file=out)
        return 1
    rc = 1 if failed else 0
    for r in runs:
        if r.plan is not None and r.plan.changed:
            r.rc = apply_plan(r.plan, run_commands=not args.user_home, out=out, paint=p)
            rc = rc or r.rc
    if many:
        _summary(runs, applied=True, out=out, run_commands=not args.user_home, paint=p)
    return rc


def hook_copies(hooks_dir: Path) -> tuple[list[Path], str]:
    """switchboard's hook copies in ``hooks_dir`` (regular files named
    ``switchboard_hook-<sha12>.py``), or why the directory can't be used."""
    try:
        st = os.lstat(hooks_dir)
    except FileNotFoundError:
        return [], ""
    except OSError as e:
        return [], f"can't read it: {e.strerror or e}"
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        return [], "not a directory you own"
    try:
        names = sorted(os.listdir(hooks_dir))
        files = [
            hooks_dir / n
            for n in names
            if HOOK_COPY_RE.fullmatch(n) and stat.S_ISREG(os.lstat(hooks_dir / n).st_mode)
        ]
    except OSError as e:
        return [], f"can't read it: {e.strerror or e}"
    return files, ""


def purge_plan(plans: list[Plan], user_home: Path, home: str) -> Plan:
    """``--purge-hooks``: delete the hook copies only if, after this run, none
    of the configs install wrote hooks into still runs one of them.

    With ``--user-home`` the real ``~``'s configs are checked too (read only):
    the switchboard home (``--home``, else SWITCHBOARD_HOME or ~/.switchboard) doesn't
    follow ``--user-home``, so its copies may be the ones the real configs run."""
    hooks_dir = Path(home) / "hooks"
    p = Plan("hook copies", verb="uninstall", title="switchboard uninstall --purge-hooks")
    after = {e.path: e.after for pl in plans for e in pl.edits if isinstance(e, FileEdit)}
    bases = [user_home]
    try:
        real = Path.home()
        if real.resolve() != user_home.resolve():
            bases.append(real)
    except (RuntimeError, OSError):
        pass
    users: list[str] = []
    for base in bases:
        for parts in HOOK_CONFIGS.values():
            f = base.joinpath(*parts)
            label = tilde(f, user_home) if base == user_home else f"{f} (your real ~)"
            if base == user_home and f in after:
                text = after[f]
            else:
                try:
                    text = f.read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                except (OSError, UnicodeDecodeError):
                    users.append(f"{label} (unreadable)")
                    continue
            if hook_ref(home) in text:
                users.append(label)
    files, problem = hook_copies(hooks_dir)
    p.edits.append(
        PurgeEdit(label=tilde(hooks_dir, user_home), files=files, blocked_by=users, problem=problem)
    )
    return p


def run_uninstall(args: argparse.Namespace, *, stdin: TextIO | None = None, out: TextIO | None = None) -> int:
    """``switchboard uninstall <h|all>``: remove only switchboard's own entries.

    No editable-install check (removing entries can't make an agent's edits
    run), no hook copy is written and the switchboard home is left alone."""
    from switchboard.paths import Paths

    out = out or sys.stdout
    many = args.harness == "all"
    home = str(Paths.from_home(getattr(args, "home", None)).home)
    user_home = Path(args.user_home).resolve() if args.user_home else Path.home()
    runs: list[_Run] = []
    for h in HARNESSES if many else (args.harness,):
        try:
            runs.append(_Run(h, plan=_module(h).unplan(user_home, home, run_commands=not args.user_home)))
        except InstallError as e:
            if not many:
                print(f"switchboard: {e}", file=sys.stderr)
                return 1
            runs.append(_Run(h, status=f"error: {e}", error=True))
    hooks_dir = Path(home) / "hooks"
    trailer: list[str] = []
    if getattr(args, "purge_hooks", False):
        runs.append(_Run("hook copies", plan=purge_plan([r.plan for r in runs if r.plan], user_home, home)))
    elif hook_copies(hooks_dir)[0]:
        trailer.append(
            f"switchboard's hook copies in {tilde(hooks_dir, user_home)} stay: they do nothing once no"
            " harness config runs them (`--purge-hooks` deletes them when none does)"
        )
    trailer.append(
        f"switchboard's own home ({tilde(Path(home), user_home)}: rooms, history, config) is not"
        " touched; `switchboard stop` stops the broker"
    )
    trailer.append(
        "agent sessions that are already running may keep switchboard's hooks and MCP server until"
        " they restart"
    )
    # a summary whenever there is more than one section (`all`, or a harness plus --purge-hooks)
    return _finish(runs, args, many=len(runs) > 1, stdin=stdin, out=out, trailer=trailer)


def deep(obj: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(obj)
