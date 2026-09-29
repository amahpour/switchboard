"""The ``switchboard`` command line (DESIGN.md §3).

Imports stay lazy so that quick verbs (say, tail, who) don't pay for FastAPI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

EXIT_OK = 0
EXIT_ERR = 1
EXIT_USAGE = 2
EXIT_DOWN = 3


def _paths(args: argparse.Namespace):
    from switchboard.paths import Paths

    return Paths.from_home(getattr(args, "home", None))


def _home_given(args: argparse.Namespace) -> bool:
    return getattr(args, "home", None) is not None


def _call(args: argparse.Namespace, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    from switchboard.mcp.client import call_sync

    return call_sync(_paths(args).sock, method, params or {}, timeout)


def _satellite_home(args: argparse.Namespace) -> bool:
    """This home is a satellite home, the far end of a remote link (DESIGN.md §27.3)."""
    from switchboard.remote.config import is_satellite_home

    return is_satellite_home(_paths(args))


def _desktop(args: argparse.Namespace) -> str:
    from switchboard.remote.config import satellite_desktop

    return satellite_desktop(_paths(args))


def on_desktop(args: argparse.Namespace) -> int:
    """A human verb on a satellite home: the broker, the web UI and the human's
    authority are on the desktop, never here."""
    print(f"switchboard: run this on the desktop ({_desktop(args)}): this is a satellite home;"
          " the broker and the web UI run there", file=sys.stderr)
    return EXIT_ERR


def _clean(text: str) -> str:
    from switchboard.envelope import clean

    return clean(text)


def _hhmmss(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def format_line(msg: dict[str, Any], paint: Any = None) -> str:
    """``[14:02:11] <alice> text`` (control characters stripped for the terminal).

    With a ``colors.Paint`` that is on, only switchboard's framing is coloured
    (issue #28): the time dim, the nick by who sent it (you bold, each agent its
    own colour, the system dim), join/leave lines and notices dim, warning notices
    red. Colour goes around the cleaned text and is never taken from it.
    """
    from switchboard.colors import PLAIN

    p = paint or PLAIN
    ts = p.dim(f"[{_hhmmss(msg.get('ts') or 0)}]")
    name = _clean(str(msg.get("from", "?")))
    text = _clean(str(msg.get("text", "")))
    kind = msg.get("kind", "chat")
    if kind == "join":
        line = f"{ts} " + p.dim(f"* {name} {text or 'joined'}")
    elif kind == "leave":
        line = f"{ts} " + p.dim(f"* {name} {text or 'left'}")
    elif kind == "notice":
        line = f"{ts} " + _notice(p, text, msg.get("level"))
    else:
        tag = p.dim(" (via cli)") if msg.get("via") == "cli" else ""
        line = f"{ts} <{p.nick(name, str(msg.get('sender_kind') or 'agent'))}>{tag} {text}"
    return line.replace("\n", "\n" + " " * 11)


def _notice(p: Any, text: str, level: Any) -> str:
    """A ``-!-`` notice line: red for a warning (as the web UI shows it), dim otherwise."""
    body = f"-!- {text}"
    return p.bad(body) if level == "warn" else p.dim(body)


def _state(p: Any, word: str) -> str:
    """A state word, coloured by what it says: up/ok/active green, blocked/failed red,
    off/offline dim, and anything that needs a look (down, paused, parked, needs
    enable, connecting) yellow."""
    key = word.lower().rstrip(":,")
    if key in ("up", "ok", "online", "active", "idle", "running", "clean"):
        return p.ok(word)
    if key in ("blocked", "failed", "fail", "error"):
        return p.bad(word)
    if key in ("off", "offline", "disabled", "starting"):
        return p.dim(word)
    return p.warn(word)


def _lead(p: Any, text: str) -> str:
    """``text`` with its first word coloured as a state (``up since 10:25``, ``ok (1 copy)``)."""
    word, sep, rest = text.partition(" ")
    return _state(p, word) + sep + rest


def _remote_line(p: Any, info: dict[str, Any]) -> str:
    """``describe(info)``, cleaned, with its state word coloured. The state is found
    after the remote's own name (remote names can't hold a colon, §27.2), so
    nothing the remote reported is ever matched or coloured."""
    from switchboard.remote.describe import describe

    line = _clean(describe(info))
    head = _clean(str(info.get("name", ""))) + ": "
    if line.startswith(head):
        rest = line[len(head):]
        for word in ("needs enable", "up", "down", "blocked", "disabled", str(info.get("state") or "")):
            if word and rest.startswith(word) and rest[len(word):len(word) + 1] in ("", " ", ":", ","):
                return head + _state(p, word) + rest[len(word):]
    return line


# ------------------------------------------------------------------ commands
def cmd_start(args: argparse.Namespace) -> int:
    from switchboard.broker import daemon
    from switchboard.config import ConfigError, load

    paths = _paths(args)
    if _satellite_home(args):
        print(f"switchboard: this is a satellite home: the broker runs on {_desktop(args)}, which dials this"
              " machine (`switchboard remote status` there)", file=sys.stderr)
        return EXIT_ERR
    if args.test_trust_uds and not args.test_mode:
        print("switchboard: --test-trust-uds needs --test-mode", file=sys.stderr)
        return EXIT_USAGE
    if args.test_mode:
        why = daemon.check_test_mode(paths, _home_given(args))
        if why:
            print(f"switchboard: {why}", file=sys.stderr)
            return EXIT_USAGE
    if args.foreground:
        try:
            cfg = load(paths)
        except ConfigError as e:
            print(f"switchboard: config error: {e}", file=sys.stderr)
            return EXIT_ERR
        return daemon.run_foreground(
            paths,
            cfg,
            port=args.port,
            test_mode=args.test_mode,
            test_trust_uds=args.test_trust_uds,
        )
    try:
        load(paths)
    except ConfigError as e:
        print(f"switchboard: config error: {e}", file=sys.stderr)
        return EXIT_ERR
    return daemon.start(
        paths, port=args.port, test_mode=args.test_mode, test_trust_uds=args.test_trust_uds
    )


def cmd_stop(args: argparse.Namespace) -> int:
    from switchboard.broker import daemon

    if _satellite_home(args):
        return on_desktop(args)
    return daemon.stop(_paths(args))


def cmd_satellite_status(args: argparse.Namespace) -> int:
    """``switchboard status`` on a satellite home: the satellite answers it, or no one does."""
    from switchboard.mcp.client import BrokerDown

    try:
        st = _call(args, "sys.status", timeout=5.0)
    except (BrokerDown, OSError, TimeoutError):
        st = None
    if st is None or st.get("role") != "satellite":
        print(f"link down: the desktop ({_desktop(args)}) dials this machine; on the desktop run"
              " `switchboard remote status`")
        return EXIT_DOWN
    if args.json:
        print(json.dumps(st, indent=2))
        return EXIT_OK
    print(f"switchboard satellite {st['version']} for {st['name']}: link {st['link']}, pid {st['pid']}")
    print(f"  desktop {st.get('desktop') or '?'} (switchboard {st.get('desktop_version') or '?'})")
    print(f"  rooms   {', '.join(st.get('rooms') or []) or 'none'}")
    print(f"  hooks   {st.get('hooks')}")
    if st.get("test_mode"):
        print("  TEST MODE")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    if _satellite_home(args):
        return cmd_satellite_status(args)
    st = _call(args, "sys.status")
    if args.json:
        print(json.dumps(st, indent=2))
        return EXIT_OK
    from switchboard.colors import for_args

    p = for_args(args)
    up = int(st.get("uptime_s", 0))
    print(f"switchboard {st['version']} {p.ok('running')}: pid {st['pid']}, up {up // 3600}h{up % 3600 // 60:02d}m")
    print(f"  web UI  {p.link(str(st['url']))}  ({st.get('web_clients', 0)} browser tab(s) connected)")
    print(f"  home    {st['home']}")
    print(f"  hooks   {_lead(p, str(st['hooks']))}")
    print(f"  codex   {_lead(p, str(st['codex_link']))}")
    if st.get("test_mode"):
        print("  " + p.warn("TEST MODE"))
    rooms = st.get("rooms", [])
    closed = st.get("closed_rooms", 0)
    if not rooms:
        print("  rooms   none open" if closed else "  rooms   none yet (create one in the web UI)")
    for r in rooms:
        state = p.warn(f"paused ({r['paused_reason']})") if r["paused"] else p.ok("active")
        limit = r.get("hop_limit")
        hops = (f"{r['hop_count']}, {p.warn('loop guard off')}" if limit == 0
                else f"{r['hop_count']}" if limit is None else f"{r['hop_count']}/{limit}")
        print(
            f"  {r['name']}: {state}, {r['members']} agent(s), budget "
            f"{r['budget_remaining']}/{r['budget_per_hour']}, hops {hops}"
        )
    if closed:
        print(f"  closed  {closed} room(s) (switchboard rooms --closed)")
    for info in st.get("remotes") or []:
        print(f"  remote  {_remote_line(p, info)}")
    return EXIT_OK


def cmd_login(args: argparse.Namespace) -> int:
    if _satellite_home(args):
        return on_desktop(args)
    res = _call(args, "human.login_link")
    print(f"Sign in (the link works once, for 5 minutes):\n  {res['url']}")
    if args.open:
        import webbrowser

        webbrowser.open(res["url"])
    return EXIT_OK


def cmd_logout(args: argparse.Namespace) -> int:
    if _satellite_home(args):
        return on_desktop(args)
    if not args.all:
        print("switchboard: use `switchboard logout --all` (or the Log out button in the web UI)", file=sys.stderr)
        return EXIT_USAGE
    res = _call(args, "human.logout_all")
    print(f"revoked {res['revoked']} web session(s)")
    return EXIT_OK


def _ymdhm(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts is not None else "at an unknown time"


def cmd_rooms(args: argparse.Namespace) -> int:
    if getattr(args, "rooms_cmd", None) == "delete":
        return cmd_rooms_delete(args)
    if args.closed:
        res = _call(args, "room.list", {"closed": True})
        if args.json:
            print(json.dumps(res["rooms"], indent=2))
            return EXIT_OK
        if not res["rooms"]:
            print("no closed rooms")
        for r in res["rooms"]:
            print(f"{r['name']}  was {r['display']}, closed {_ymdhm(r.get('closed_at'))}"
                  f" by {_clean(r.get('closed_by') or '?')}, {r['messages']} message(s)")
        return EXIT_OK
    res = _call(args, "room.list")
    if args.json:
        print(json.dumps(res["rooms"], indent=2))
        return EXIT_OK
    closed = res.get("closed", 0)
    if not res["rooms"]:
        print(f"no open rooms ({closed} closed: switchboard rooms --closed)" if closed
              else "no rooms yet (create one in the web UI)")
        return EXIT_OK
    for r in res["rooms"]:
        s = r["settings"]
        flag = "  [paused]" if s["paused"] else ""
        print(f"{r['name']}  {r['members']} agent(s){flag}")
    if closed:
        print(f"({closed} closed: switchboard rooms --closed)")
    return EXIT_OK


def _removed(c: dict[str, int]) -> str:
    return (f"{c['rooms']} room, {c['messages']} message(s), {c['memberships']} membership(s),"
            f" {c['deliveries']} delivery row(s), {c['batches']} batch(es), {c['events']} event(s)")


def cmd_rooms_delete(args: argparse.Namespace) -> int:
    """``switchboard rooms delete ROOM`` (DESIGN.md §28.6): the broker plans it (a dry run,
    refused while the room has members), you confirm, then the broker writes a checked
    backup and deletes the room pinned by the plan's id, name and creation time. The CLI never
    opens the database."""
    if _satellite_home(args):
        return on_desktop(args)
    from switchboard.install import common

    plan = _call(args, "room.delete", {"room": args.room, "dry_run": True}, timeout=30.0)
    print(f"switchboard rooms delete {plan['name']}:")
    if plan.get("state") == "closed":
        print(f"  {plan['name']}: was {plan['display']}, closed {_ymdhm(plan.get('closed_at'))}"
              f" by {_clean(plan.get('closed_by') or '?')}")
    else:
        print(f"  {plan['name']}: open, no agents, created {_ymdhm(plan.get('created_at'))}")
    print(f"  removes {_removed(plan['counts'])}")
    print(f"  a checked backup of the whole database is written first: {plan['backup']}")
    print("  this can't be undone, except by restoring that backup")
    if not common.confirm(args.yes):
        print("not applied")
        return EXIT_ERR
    # pinned to the room the plan showed: a reopen keeps its id, a re-create can reuse it
    pin = {"room_id": plan["room_id"], "name": plan["name"], "created_at": plan["created_at"]}
    res = _call(args, "room.delete", {"room": args.room, **pin}, timeout=120.0)
    print(f"deleted {res['name']}: {_removed(res['removed'])}")
    print(f"backup: {res['backup']} (0600, checked); it still holds the room: remove it once you no longer need it")
    return EXIT_OK


def cmd_create(args: argparse.Namespace) -> int:
    if _satellite_home(args):
        return on_desktop(args)
    res = _call(args, "room.create", {"name": args.room})
    print(f"created {res['room']['name']}")
    return EXIT_OK


def cmd_say(args: argparse.Namespace) -> int:
    if _satellite_home(args):
        return on_desktop(args)
    text = " ".join(args.text)
    if text == "-":
        text = sys.stdin.read()
    res = _call(args, "human.say", {"room": args.room, "text": text})
    if args.verbose:
        print(res["id"])
    return EXIT_OK


def command_text(words: list[str]) -> str:
    """The command ``switchboard cmd`` sends: its words joined by spaces. A word after the
    first that has a space in it was quoted in the shell, and is passed on in double quotes,
    so ``switchboard cmd '#build' /catchup codex-1 on "sprint cleanup"`` sends the topic in
    quotes; a word that has a double quote itself goes as it is. The whole command as one
    word (``'/catchup codex-1 on "sprint cleanup"'``) is passed on unchanged."""
    out = [w if i == 0 or not any(c.isspace() for c in w) or '"' in w else f'"{w}"'
           for i, w in enumerate(words)]
    return " ".join(out).strip()


def cmd_cmd(args: argparse.Namespace) -> int:
    if _satellite_home(args):
        return on_desktop(args)
    if not args.command:
        print("usage: switchboard cmd ROOM /command [args ...]", file=sys.stderr)
        return EXIT_USAGE
    text = command_text(args.command)
    if not text.startswith("/"):
        text = "/" + text
    res = _call(args, "human.command", {"room": args.room, "text": text})
    print(_clean(res.get("text", "")))
    return EXIT_OK if res.get("ok", True) else EXIT_ERR


def cmd_who(args: argparse.Namespace) -> int:
    from switchboard.models import tier_label

    res = _call(args, "room.who", {"room": args.room})
    if args.json:
        print(json.dumps(res, indent=2))
        return EXIT_OK
    from switchboard.colors import for_args

    p = for_args(args)
    members = res["members"]
    print(f"{p.bold(res['room'])}: {p.nick(res['human'], 'human')} (you, human) + {len(members)} agent(s)")
    for m in members:
        flags = []
        if m["approval_mode"] == "bypass":
            flags.append(p.bad("⚠"))
        elif m["approval_mode"] == "unknown":
            flags.append(p.warn("?"))
        if m["env_leak"]:
            flags.append(p.warn("env shared"))
        if m["held"]:
            flags.append(p.warn("held"))
        if m["queued"]:
            flags.append(f"{m['queued']} queued")
        if m["parked"]:
            flags.append(p.warn("parked — needs a poke"))
        if m.get("session"):
            flags.append(p.dim(f"session: {_clean(m['session'])}"))
        away = f'  away: "{_clean(m["away"])}"' if m.get("away") else ""
        tier = tier_label(m.get("tier"), m.get("tier_note"))
        name = p.nick(_clean(m["name"])) + (p.dim(f"@{_clean(m['host'])}") if m.get("host") else "")
        status = {"idle": p.ok, "waiting-approval": p.warn, "offline": p.dim, "starting": p.dim}.get(
            m["status"], str)(m["status"])
        print(f"  {name}  {m['harness']}  {status}  {tier}  {' '.join(flags)}{away}".rstrip())
    return EXIT_OK


def cmd_tail(args: argparse.Namespace) -> int:
    from switchboard.mcp.client import BrokerDown, Stream

    from switchboard.colors import for_args

    p = for_args(args)
    params: dict[str, Any] = {"room": args.room, "follow": not args.no_follow, "limit": args.lines}
    if args.after is not None:
        params["after"] = args.after
        params["limit"] = 500

    def emit(msg: dict[str, Any]) -> None:
        if args.json:
            print(json.dumps(msg, ensure_ascii=False), flush=True)
        else:
            print(format_line(msg, p), flush=True)

    with Stream(_paths(args).sock) as s:
        res = s.call("room.tail", params)
        last = 0
        for m in res.get("messages", []):
            emit(m)
            last = max(last, m["id"])
        more = bool(res.get("more"))
        while more:  # --after far back: page through the rest before following
            page = s.call("room.history", {"room": args.room, "after": last, "limit": 1000})["messages"]
            for m in page:
                emit(m)
                last = max(last, m["id"])
            more = len(page) == 1000
        if args.no_follow:
            return EXIT_OK
        try:
            for push in s.pushes():
                data = push.get("data") or {}
                if push.get("push") == "message":
                    m = data.get("msg") or {}
                    if m.get("id", 0) <= last:
                        continue  # at-least-once: skip anything already printed
                    last = m["id"]
                    emit(m)
                elif push.get("push") == "notice" and not args.json:
                    print(_notice(p, _clean(str(data.get("text", ""))), data.get("level")), flush=True)
        except BrokerDown:
            print("switchboard: the broker went away", file=sys.stderr)
            return EXIT_DOWN
        except KeyboardInterrupt:
            return EXIT_OK
    return EXIT_OK


def cmd_mcp(args: argparse.Namespace) -> int:
    from switchboard.mcp.server import main as mcp_main

    argv: list[str] = []
    if _home_given(args):
        argv += ["--home", args.home]
    if args.harness:
        argv += ["--harness", args.harness]
    if args.test_session:
        argv += ["--test-session", args.test_session]
    if args.ack:
        argv += ["--ack", args.ack]
    return mcp_main(argv)


def cmd_hook(args: argparse.Namespace) -> int:
    """Debug helper: exec the installed hook copy, exactly as a harness would."""
    from switchboard.paths import hook_sha12

    paths = _paths(args)
    copy = paths.hook_copy(hook_sha12())
    if not copy.exists():
        print(f"switchboard: no hook copy at {copy}; run `switchboard start` once", file=sys.stderr)
        return EXIT_OK
    argv = [sys.executable, "-I", "-S", str(copy), "--home", str(paths.home),
            "--harness", args.harness, "--event", args.event]
    if args.max_wait is not None:
        argv += ["--max-wait", str(args.max_wait)]
    os.execv(sys.executable, argv)
    return EXIT_OK  # pragma: no cover


def cmd_report(args: argparse.Namespace) -> int:
    """What happened in a room (DESIGN.md §12.6), read from the database (read-only)."""
    import sqlite3

    from switchboard import report

    now = time.time()
    try:
        since = report.parse_window(args.since, args.last, now)
        con = report.open_ro(_paths(args).db)
        try:
            rep = report.build(con, args.room, since=since, now=now)
        finally:
            con.close()
    except report.ReportError as e:
        print(f"switchboard: {e}", file=sys.stderr)
        return EXIT_ERR
    except sqlite3.Error as e:  # e.g. "database is locked" after the timeout, or an unexpected schema
        print(f"switchboard: can't read the switchboard database: {e}", file=sys.stderr)
        return EXIT_ERR
    if args.json:
        out = json.dumps(rep, indent=2) + "\n"
    else:
        from switchboard.colors import PLAIN, for_args

        # a file gets plain markdown; the terminal gets headings and fired rules coloured
        out = report.render_markdown(rep, paint=PLAIN if args.out else for_args(args))
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(out)
        except OSError as e:
            print(f"switchboard: can't write {args.out}: {e.strerror or e}", file=sys.stderr)
            return EXIT_ERR
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(out)
    return EXIT_OK


def _pairing(args: argparse.Namespace) -> int:
    """``switchboard remote add|accept|remove|doctor`` (DESIGN.md §27.5.8, §27.8)."""
    from pathlib import Path

    from switchboard.config import ConfigError, load
    from switchboard.install.common import InstallError
    from switchboard.remote import pairing

    paths = _paths(args)
    sat = _satellite_home(args)
    ak = Path(args.authorized_keys).expanduser() if getattr(args, "authorized_keys", None) else None
    try:
        if args.remote_cmd == "add":
            if sat:
                return on_desktop(args)
            return pairing.add(paths, args.name, args.dest, rooms=args.rooms, port=args.port,
                               harnesses=args.harnesses, ssh_config=args.ssh_config, known_hosts=args.known_hosts,
                               authorized_keys=ak, label=args.label)
        if args.remote_cmd == "accept":
            return pairing.accept(paths, args.token, from_=args.from_, ak_path=ak, yes=args.yes,
                                  allow_editable=args.allow_editable)
        if args.remote_cmd == "remove":
            if sat:
                return pairing.remove_remote(paths, args.name, ak_path=ak, yes=args.yes)
            return pairing.remove_desktop(paths, args.name, yes=args.yes,
                                          call=lambda m, p: _call(args, m, p, timeout=30.0))
        # doctor
        akp = ak or pairing.default_authorized_keys()
        if sat:
            findings = pairing.doctor_remote(paths, ak_path=akp, environ=os.environ,
                                             probe_desktop=args.probe_desktop,
                                             status=lambda: _satellite_status(paths))
            print(f"switchboard remote doctor (satellite home {paths.home}):")
        else:
            if args.probe_desktop is not None:
                print("switchboard: --probe-desktop is for a remote (satellite) home", file=sys.stderr)
                return EXIT_USAGE
            try:
                allow = load(paths).security.allow_ssh_cli
            except ConfigError as e:
                print(f"switchboard: config error: {e}", file=sys.stderr)
                return EXIT_ERR
            ssh_dir = Path(args.ssh_dir).expanduser() if args.ssh_dir else Path(os.path.expanduser("~")) / ".ssh"
            findings = pairing.doctor_desktop(paths, ak_path=akp, ssh_dir=ssh_dir, allow_ssh_cli=allow,
                                              status=lambda: _remote_status(args))
            print(f"switchboard remote doctor (desktop home {paths.home}):")
        from switchboard.colors import for_args

        return pairing.print_findings(findings, sys.stdout, paint=for_args(args))
    except (pairing.PairingError, InstallError) as e:
        print(f"switchboard remote {args.remote_cmd}: {e}", file=sys.stderr)
        return EXIT_ERR


def _satellite_status(paths: Any) -> dict[str, Any] | None:
    from switchboard.mcp.client import BrokerDown, RpcError, call_sync

    try:
        return call_sync(paths.sock, "sys.status", {}, 3.0)
    except (BrokerDown, RpcError, OSError, TimeoutError):
        return None


def _remote_status(args: argparse.Namespace) -> dict[str, Any] | None:
    from switchboard.mcp.client import BrokerDown, RpcError

    try:
        return _call(args, "remote.status", {}, timeout=5.0)
    except (BrokerDown, RpcError, OSError, TimeoutError):
        return None


def cmd_remote(args: argparse.Namespace) -> int:
    """``switchboard remote add|accept|enable|disable|status|remove|doctor`` (DESIGN.md
    §27.5.8, §27.8, §27.11). On a satellite home only ``accept``, ``remove`` and
    ``doctor`` run; the rest belong to the desktop."""
    from switchboard.colors import for_args

    if args.remote_cmd in ("add", "accept", "remove", "doctor"):
        return _pairing(args)
    if _satellite_home(args):
        return on_desktop(args)
    p = for_args(args)
    if args.remote_cmd == "enable":
        # the broker dials and waits up to 15 s for the link to come up or fail
        res = _call(args, "remote.enable", {"name": args.name}, timeout=30.0)
        print(_clean(res["text"]) if res.get("text") else _remote_line(p, res))
        return EXIT_OK if res.get("state") == "up" else EXIT_ERR
    if args.remote_cmd == "disable":
        res = _call(args, "remote.disable", {"name": args.name})
        print(_clean(res["text"]) if res.get("text") else _remote_line(p, res))
        return EXIT_OK
    params = {"name": args.name} if args.name else {}
    res = _call(args, "remote.status", params)
    if args.json:
        print(json.dumps(res, indent=2))
        return EXIT_OK
    if res.get("config_error"):
        print(f"{p.bad('remotes.toml:')} {_clean(res['config_error'])}")
    if not res.get("remotes"):
        print("no remotes (none in remotes.toml)")
    for info in res.get("remotes", []):
        print(_remote_line(p, info))
    return EXIT_OK


def cmd_satellite(args: argparse.Namespace) -> int:
    from switchboard.remote.satellite import main as satellite_main

    if args.test_mode and not _home_given(args):
        print("switchboard satellite: --test-mode needs an explicit --home", file=sys.stderr)
        return 2
    # the default home when none is given ($SWITCHBOARD_HOME or ~/.switchboard), never the cwd
    argv = ["--home", str(_paths(args).home), "--name", args.name]
    if args.test_mode:
        argv.append("--test-mode")
    return satellite_main(argv)


def cmd_install(args: argparse.Namespace) -> int:
    from switchboard.install import common

    return common.run_install(args)


def cmd_uninstall(args: argparse.Namespace) -> int:
    from switchboard.install import common

    return common.run_uninstall(args)


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    from switchboard.colors import MODES

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--home",
        default=argparse.SUPPRESS,
        help="switchboard home (default: $SWITCHBOARD_HOME or ~/.switchboard)",
    )
    # SUPPRESS, like --home, so the flag works before or after the verb (colors.for_args
    # reads a missing one as auto)
    common.add_argument(
        "--color",
        choices=MODES,
        default=argparse.SUPPRESS,
        help="colour the output: auto (a terminal, unless NO_COLOR is set; the default), always or never",
    )
    p = argparse.ArgumentParser(
        prog="switchboard",
        description="A local group chat where you and your coding agents talk and hand work to each other.",
        parents=[common],
    )
    p.add_argument("--version", action="version", version=_version())
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    s = sub.add_parser("start", parents=[common], help="start the broker (daemonizes)")
    s.add_argument("--foreground", action="store_true", help="run in this process")
    s.add_argument("--port", type=int, default=None, help="TCP port (default 7419; 0 = any)")
    s.add_argument("--test-mode", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--test-trust-uds", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("stop", parents=[common], help="stop the broker")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("status", parents=[common], help="show broker status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("login", parents=[common], help="print a one-time sign-in link for the web UI")
    s.add_argument("--open", action="store_true", help="also open it in your browser")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("logout", parents=[common], help="revoke web sessions")
    s.add_argument("--all", action="store_true", help="revoke every web session")
    s.set_defaults(func=cmd_logout)

    s = sub.add_parser("rooms", parents=[common], help="list rooms (--closed: closed ones); rooms delete ROOM")
    s.add_argument("--json", action="store_true")
    s.add_argument("--closed", action="store_true", help="list closed rooms (reopen them in the web UI)")
    rs = s.add_subparsers(dest="rooms_cmd", metavar="ACTION")  # optional: bare `rooms` lists
    d = rs.add_parser("delete", parents=[common], help="delete a room and its history for good (after a checked backup)")
    d.add_argument("room", help="'#build', or a closed room's full name '#build~closed-7' (quote it)")
    d.add_argument("--yes", action="store_true", help="apply without asking")
    s.set_defaults(func=cmd_rooms)

    s = sub.add_parser("create", parents=[common], help="create a room (needs the web session)")
    s.add_argument("room")
    s.set_defaults(func=cmd_create)

    s = sub.add_parser("say", parents=[common], help="post to a room as you (literal text)")
    s.add_argument("room")
    s.add_argument("text", nargs="+", help="message text ('-' reads stdin)")
    s.add_argument("-v", "--verbose", action="store_true", help="print the new message id")
    s.set_defaults(func=cmd_say)

    s = sub.add_parser("cmd", parents=[common], help="run a slash command, e.g. /pause")
    s.add_argument("room")
    # everything after the room is the command, dash words included (a /catchup note may
    # say "--from"); options such as --home go before the room
    s.add_argument("command", nargs=argparse.REMAINDER, help="the command and its arguments, e.g. /pause")
    s.set_defaults(func=cmd_cmd)

    s = sub.add_parser("tail", parents=[common], help="print and follow a room")
    s.add_argument("room")
    s.add_argument("--after", type=int, default=None, help="start after this message id")
    s.add_argument("-n", "--lines", type=int, default=20, help="backlog lines (default 20)")
    s.add_argument("--json", action="store_true", help="one JSON object per line")
    s.add_argument("--no-follow", action="store_true", help="print the backlog and exit")
    s.set_defaults(func=cmd_tail)

    s = sub.add_parser("who", parents=[common], help="list a room's members")
    s.add_argument("room")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_who)

    s = sub.add_parser("report", parents=[common],
                       help="latency, turns, posts vs passes and rules fired in a room (markdown or JSON)")
    s.add_argument("--room", required=True, help="the room, e.g. '#build'")
    s.add_argument("--since", default=None, help="start at this ISO time (default: the room's creation)")
    s.add_argument("--last", default=None, help="only the last N s/m/h/d, e.g. 2h")
    s.add_argument("--json", action="store_true", help="JSON instead of markdown")
    s.add_argument("--out", default=None, help="write to FILE instead of stdout")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("install", parents=[common],
                       help="register switchboard's MCP server and hooks with a harness (shows a diff first)")
    s.add_argument("harness", choices=["claude", "codex", "cursor", "devin", "all"],
                   help="a harness, or all (every harness whose CLI is on PATH, one confirmation)")
    s.add_argument("--dry-run", action="store_true", help="show the diff, write nothing")
    s.add_argument("--yes", action="store_true", help="apply without asking")
    s.add_argument("--print-args", action="store_true",
                   help="print per-launch flags/files as JSON (writes nothing)")
    s.add_argument("--workspace", default=None, help="with --print-args: the workspace dir")
    s.add_argument("--user-home", default=None, help="treat DIR as ~ (tests)")
    s.add_argument("--allow-editable", action="store_true",
                   help="allow an editable/source install of switchboard (agents could edit its code)")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("uninstall", parents=[common],
                       help="remove switchboard's own MCP server and hooks from a harness (shows a diff first)")
    s.add_argument("harness", choices=["claude", "codex", "cursor", "devin", "all"],
                   help="a harness, or all (the four in order, one confirmation)")
    s.add_argument("--dry-run", action="store_true", help="show the diff, write nothing")
    s.add_argument("--yes", action="store_true", help="apply without asking")
    s.add_argument("--user-home", default=None, help="treat DIR as ~ (tests)")
    s.add_argument("--purge-hooks", action="store_true",
                   help="also delete switchboard's hook copies in <home>/hooks if no harness config still runs them")
    s.set_defaults(func=cmd_uninstall)

    s = sub.add_parser("mcp", parents=[common], help="run the stdio MCP server (harnesses start this)")
    s.add_argument("--harness", choices=["test"], default=None, help=argparse.SUPPRESS)
    s.add_argument("--test-session", default=None, help=argparse.SUPPRESS)
    s.add_argument("--ack", choices=["next_call", "immediate", "never"], default=None,
                   help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("remote", parents=[common],
                       help="remote members over ssh: pair (add/accept), enable, disable, status, remove, doctor")
    rsub = s.add_subparsers(dest="remote_cmd", metavar="ACTION", required=True)
    r = rsub.add_parser("add", parents=[common],
                        help="desktop: pair a remote (link key, pinned host key, remotes.toml) and print its token")
    r.add_argument("name", help="the remote's name, e.g. fpga-pi")
    r.add_argument("dest", help="[user@]host, resolved once with `ssh -G`")
    r.add_argument("--rooms", action="append", default=None,
                   help="limit its members to these rooms (comma list, or repeat), e.g. '#fpga';"
                        " default: any room")
    r.add_argument("--port", type=int, default=None, help="ssh port (default: from your ssh config, else 22)")
    r.add_argument("--harnesses", action="append", default=None,
                   help="harnesses allowed there (comma list; default claude,codex,cursor,devin)")
    r.add_argument("--ssh-config", default=None, help="ssh config for `ssh -G` (default: yours)")
    r.add_argument("--known-hosts", default=None,
                   help="known_hosts file holding the remote's host key (default: your ssh config's)")
    r.add_argument("--authorized-keys", default=None,
                   help="this machine's authorized_keys, scanned for shell keys (default ~/.ssh/authorized_keys)")
    r.add_argument("--label", default=None, help="this machine's name in the token (default: its host name)")
    r = rsub.add_parser("accept", parents=[common],
                        help="remote: authorize the desktop's link key for the satellite only (shows the line first)")
    r.add_argument("token", help="the token `remote add` printed: 'switchboard-link v1 …'")
    r.add_argument("--from", dest="from_", default=None,
                   help="accept the key only from this address (the desktop's IP; comma list allowed)")
    r.add_argument("--authorized-keys", default=None, help="the file to write (default ~/.ssh/authorized_keys)")
    r.add_argument("--yes", action="store_true", help="apply without asking")
    # switchboard's own tests only (their venv is editable): refused outside a test home
    r.add_argument("--allow-editable", action="store_true", help=argparse.SUPPRESS)
    r = rsub.add_parser("remove", parents=[common],
                        help="unpair: on the desktop the link, members, key and table; on the remote its key line")
    r.add_argument("name")
    r.add_argument("--authorized-keys", default=None, help="remote: the file to edit (default ~/.ssh/authorized_keys)")
    r.add_argument("--yes", action="store_true", help="apply without asking")
    r = rsub.add_parser("doctor", parents=[common], help="check this machine's side of its remote links")
    r.add_argument("--authorized-keys", default=None, help="the authorized_keys to check (default ~/.ssh/…)")
    r.add_argument("--ssh-dir", default=None, help="desktop: where this machine's own public keys are (default ~/.ssh)")
    r.add_argument("--probe-desktop", default=None, metavar="DEST",
                   help="remote: also try `ssh DEST true` (a shell on the desktop from here is a warning)")
    r = rsub.add_parser("enable", parents=[common], help="consent to this remote's current config and dial it")
    r.add_argument("name")
    r = rsub.add_parser("disable", parents=[common], help="stop dialing a remote (its members go offline)")
    r.add_argument("name")
    r = rsub.add_parser("status", parents=[common], help="show every remote link, or one")
    r.add_argument("name", nargs="?", default=None)
    r.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_remote)

    # the far end of a remote link: started by sshd as the link key's forced command
    s = sub.add_parser("satellite", parents=[common])
    s.add_argument("--name", required=True)
    s.add_argument("--test-mode", action="store_true")
    s.set_defaults(func=cmd_satellite)

    s = sub.add_parser("hook", parents=[common], help="run the installed hook script (debugging)")
    s.add_argument("--harness", required=True, choices=["claude", "codex", "cursor", "devin"])
    s.add_argument("--event", required=True)
    s.add_argument("--max-wait", type=float, default=None)
    s.set_defaults(func=cmd_hook)
    return p


def _version() -> str:
    from switchboard import __version__

    return f"switchboard {__version__}"


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    from switchboard.mcp.client import BrokerDown, RpcError
    from switchboard.paths import UnsafePathError

    try:
        return int(args.func(args) or 0)
    except BrokerDown:
        print(
            "switchboard: the broker is not running (start it with `switchboard start`)",
            file=sys.stderr,
        )
        return EXIT_DOWN
    except RpcError as e:
        print(f"switchboard: {e.code}: {_clean(e.message)}", file=sys.stderr)
        return EXIT_ERR
    except UnsafePathError as e:
        print(f"switchboard: unsafe path: {e}", file=sys.stderr)
        return EXIT_ERR
    except PermissionError as e:
        print(f"switchboard: {e}", file=sys.stderr)
        return EXIT_ERR
    except TimeoutError as e:
        print(f"switchboard: {e}", file=sys.stderr)
        return EXIT_ERR
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
