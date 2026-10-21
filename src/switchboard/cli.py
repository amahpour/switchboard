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


def _call(
    args: argparse.Namespace, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0
) -> dict[str, Any]:
    from switchboard.mcp.client import call_sync

    return call_sync(_paths(args).sock, method, params or {}, timeout)


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


# ------------------------------------------------------------------ commands
def cmd_start(args: argparse.Namespace) -> int:
    from switchboard.broker import daemon
    from switchboard.config import ConfigError, load

    paths = _paths(args)
    if args.test_trust_uds and not args.test_mode:
        print("switchboard: --test-trust-uds needs --test-mode", file=sys.stderr)
        return EXIT_USAGE
    if args.log_stdout and not args.foreground:
        print("switchboard: --log-stdout needs --foreground", file=sys.stderr)
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
            listen=args.listen,
            public_url=args.public_url,
            log_stdout=args.log_stdout,
            test_mode=args.test_mode,
            test_trust_uds=args.test_trust_uds,
        )
    try:
        cfg = load(paths)
    except ConfigError as e:
        print(f"switchboard: config error: {e}", file=sys.stderr)
        return EXIT_ERR
    # the broker checks these too; here a bad one fails now, not in the child's log
    try:
        daemon.web_settings(
            cfg.listen if args.listen is None else args.listen,
            cfg.public_url if args.public_url is None else args.public_url,
        )
    except ValueError as e:
        print(f"switchboard: {e}", file=sys.stderr)
        return EXIT_USAGE
    return daemon.start(
        paths,
        port=args.port,
        listen=args.listen,
        public_url=args.public_url,
        test_mode=args.test_mode,
        test_trust_uds=args.test_trust_uds,
    )


def cmd_stop(args: argparse.Namespace) -> int:
    from switchboard.broker import daemon

    return daemon.stop(_paths(args))


def cmd_status(args: argparse.Namespace) -> int:
    st = _call(args, "sys.status")
    if args.json:
        print(json.dumps(st, indent=2))
        return EXIT_OK
    from switchboard.colors import for_args

    p = for_args(args)
    up = int(st.get("uptime_s", 0))
    from switchboard.build_info import version_text

    print(
        f"{version_text(st['version'], st.get('commit'))} {p.ok('running')}: pid {st['pid']},"
        f" up {up // 3600}h{up % 3600 // 60:02d}m"
    )
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
        hops = (
            f"{r['hop_count']}, {p.warn('loop guard off')}"
            if limit == 0
            else f"{r['hop_count']}"
            if limit is None
            else f"{r['hop_count']}/{limit}"
        )
        print(
            f"  {r['name']}: {state}, {r['members']} agent(s), budget "
            f"{r['budget_remaining']}/{r['budget_per_hour']}, hops {hops}"
        )
    if closed:
        print(f"  closed  {closed} room(s) (switchboard rooms --closed)")
    return EXIT_OK


def cmd_login(args: argparse.Namespace) -> int:
    res = _call(args, "human.login_link")
    print(f"Sign in (the link works once, for 5 minutes):\n  {res['url']}")
    if args.open:
        import webbrowser

        webbrowser.open(res["url"])
    return EXIT_OK


def cmd_logout(args: argparse.Namespace) -> int:
    if not args.all:
        print(
            "switchboard: use `switchboard logout --all` (or the Log out button in the web UI)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    res = _call(args, "human.logout_all")
    print(f"revoked {res['revoked']} web session(s)")
    return EXIT_OK


def _ymdhm(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts is not None else "at an unknown time"


def cmd_rooms(args: argparse.Namespace) -> int:
    if getattr(args, "rooms_cmd", None) == "delete":
        return cmd_rooms_delete(args)
    if getattr(args, "rooms_cmd", None) == "rules":
        return cmd_rooms_rules(args)
    if args.closed:
        res = _call(args, "room.list", {"closed": True})
        if args.json:
            print(json.dumps(res["rooms"], indent=2))
            return EXIT_OK
        if not res["rooms"]:
            print("no closed rooms")
        for r in res["rooms"]:
            print(
                f"{r['name']}  was {r['display']}, closed {_ymdhm(r.get('closed_at'))}"
                f" by {_clean(r.get('closed_by') or '?')}, {r['messages']} message(s)"
            )
        return EXIT_OK
    res = _call(args, "room.list")
    if args.json:
        print(json.dumps(res["rooms"], indent=2))
        return EXIT_OK
    closed = res.get("closed", 0)
    if not res["rooms"]:
        print(
            f"no open rooms ({closed} closed: switchboard rooms --closed)"
            if closed
            else "no rooms yet (create one in the web UI)"
        )
        return EXIT_OK
    for r in res["rooms"]:
        s = r["settings"]
        flag = "  [paused]" if s["paused"] else ""
        print(f"{r['name']}  {r['members']} agent(s){flag}")
    if closed:
        print(f"({closed} closed: switchboard rooms --closed)")
    return EXIT_OK


def cmd_rooms_rules(args: argparse.Namespace) -> int:
    """Read or set a room's rules through the broker's human CLI method (§35)."""
    params = {"room": args.room}
    if args.set_text is not None:
        params["text"] = sys.stdin.read() if args.set_text == "-" else args.set_text
    result = _call(args, "room.rules", params)
    print(result["rules"] or "(no custom room rules)")
    return EXIT_OK


def _removed(c: dict[str, int]) -> str:
    return (
        f"{c['rooms']} room, {c['messages']} message(s), {c['memberships']} membership(s),"
        f" {c['deliveries']} delivery row(s), {c['batches']} batch(es), {c['events']} event(s)"
    )


def cmd_rooms_delete(args: argparse.Namespace) -> int:
    """``switchboard rooms delete ROOM`` (DESIGN.md §28.6): the broker plans it (a dry run,
    refused while the room has members), you confirm, then the broker writes a checked
    backup and deletes the room pinned by the plan's id, name and creation time. The CLI never
    opens the database."""
    from switchboard.install import common

    plan = _call(args, "room.delete", {"room": args.room, "dry_run": True}, timeout=30.0)
    print(f"switchboard rooms delete {plan['name']}:")
    if plan.get("state") == "closed":
        print(
            f"  {plan['name']}: was {plan['display']}, closed {_ymdhm(plan.get('closed_at'))}"
            f" by {_clean(plan.get('closed_by') or '?')}"
        )
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
    print(
        f"backup: {res['backup']} (0600, checked); it still holds the room:"
        " remove it once you no longer need it"
    )
    return EXIT_OK


def cmd_create(args: argparse.Namespace) -> int:
    res = _call(args, "room.create", {"name": args.room})
    print(f"created {res['room']['name']}")
    return EXIT_OK


def cmd_say(args: argparse.Namespace) -> int:
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
    out = [
        w if i == 0 or not any(c.isspace() for c in w) or '"' in w else f'"{w}"' for i, w in enumerate(words)
    ]
    return " ".join(out).strip()


def cmd_cmd(args: argparse.Namespace) -> int:
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
            m["status"], str
        )(m["status"])
        print(f"  {name}  {m['harness']}  {status}  {tier}  {' '.join(flags)}{away}".rstrip())
    return EXIT_OK


def cmd_tail(args: argparse.Namespace) -> int:
    from switchboard.colors import for_args
    from switchboard.mcp.client import BrokerDown, Stream

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
    argv = [
        sys.executable,
        "-I",
        "-S",
        str(copy),
        "--home",
        str(paths.home),
        "--harness",
        args.harness,
        "--event",
        args.event,
    ]
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


# Verbs that were retired (#385, DESIGN.md §49.8). Each says so in one line and exits 2, instead of
# argparse's "invalid choice": an old script, an old harness entry or an old ``switchboard update``
# that still calls one learns what happened and where to go.
HTTPS_WHERE = "Agents connect over HTTPS now: Settings → Agents in the web UI, and docs/USAGE.md"
RETIRED = {
    "install": (
        "registering an MCP server and hooks with a harness was retired in 0.38.0 (#385). "
        + HTTPS_WHERE
        + ". `switchboard uninstall <harness>` still removes what an old install wrote"
    ),
    "remote": "SSH remotes and the machines that dial in were retired in 0.38.0 (#385). " + HTTPS_WHERE,
    "satellite": "SSH remotes were retired in 0.38.0 (#385). " + HTTPS_WHERE,
    "join": "machines that dial in were retired in 0.38.0 (#385). " + HTTPS_WHERE,
}


def cmd_retired(args: argparse.Namespace) -> int:
    print(f"switchboard {args.cmd}: {RETIRED[args.cmd]}", file=sys.stderr)
    return EXIT_USAGE


def cmd_update(args: argparse.Namespace) -> int:
    """``switchboard update`` (DESIGN.md §45): upgrade this copy and say what still runs in the home."""
    import shutil

    from switchboard import __version__, update
    from switchboard.install import common

    paths = _paths(args)
    home = str(paths.home)
    try:
        inst = update.installer(sys.prefix, common.editable_install())
        tool = update.which(inst.kind)
    except update.UpdateError as e:
        print(f"switchboard: {e}", file=sys.stderr)
        return EXIT_ERR

    def state() -> update.Plan:
        from switchboard.mcp.client import ping

        return update.plan(broker_running=ping(paths.sock, timeout=1.0) is not None)

    # the same entry point, which the upgrade repoints at the new copy
    exe = sys.argv[0] if os.path.basename(sys.argv[0]) == "switchboard" else shutil.which("switchboard")
    try:
        return update.run(
            home=home,
            home_given=_home_given(args),
            version=args.to_version,
            current=__version__,
            inst=inst,
            tool=tool,
            exe=exe or "switchboard",
            state=state,
        )
    except update.UpdateError as e:
        print(f"switchboard: {e}", file=sys.stderr)
        return EXIT_ERR


def cmd_uninstall(args: argparse.Namespace) -> int:
    from switchboard.install import common

    return common.run_uninstall(args)


# -------------------------------------------------------------------- parser
def _port_number(text: str) -> int:
    """``start --port`` (or ``SWITCHBOARD_PORT``): a TCP port, 0 for any free one."""
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port number (0..65535)")
    return port


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
    p.add_argument("--version", action=_VersionAction)
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    s = sub.add_parser(
        "start",
        parents=[common],
        help="start the broker on this machine",
        description="Start the broker on this machine.",
    )
    s.add_argument("--foreground", action="store_true", help="run in this process")
    # a container sets --port, --listen and --public-url through the environment (docs/DEPLOY.md);
    # config.toml's `port`, `listen` and `public_url` otherwise
    s.add_argument(
        "--port",
        type=_port_number,
        default=os.environ.get("SWITCHBOARD_PORT") or None,
        help="TCP port (default 7419; 0 = any; env SWITCHBOARD_PORT)",
    )
    s.add_argument(
        "--listen",
        default=os.environ.get("SWITCHBOARD_LISTEN") or None,
        metavar="ADDR",
        help="listen on this IPv4 address (default 127.0.0.1; any other needs --public-url;"
        " env SWITCHBOARD_LISTEN)",
    )
    s.add_argument(
        "--public-url",
        default=os.environ.get("SWITCHBOARD_PUBLIC_URL") or None,
        metavar="URL",
        help="the https:// address browsers use, behind a proxy that terminates TLS"
        " (env SWITCHBOARD_PUBLIC_URL)",
    )
    s.add_argument(
        "--log-stdout",
        action="store_true",
        help="with --foreground: log to stdout instead of logs/broker.log (a container)",
    )
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

    s = sub.add_parser(
        "rooms", parents=[common], help="list rooms (--closed: closed ones); rooms delete ROOM"
    )
    s.add_argument("--json", action="store_true")
    s.add_argument("--closed", action="store_true", help="list closed rooms (reopen them in the web UI)")
    rs = s.add_subparsers(dest="rooms_cmd", metavar="ACTION")  # optional: bare `rooms` lists
    d = rs.add_parser(
        "delete", parents=[common], help="delete a room and its history for good (after a checked backup)"
    )
    d.add_argument("room", help="'#build', or a closed room's full name '#build~closed-7' (quote it)")
    d.add_argument("--yes", action="store_true", help="apply without asking")
    rules = rs.add_parser("rules", parents=[common], help="show or set a room's custom rules")
    rules.add_argument("room", help="room name, e.g. '#build'")
    rules.add_argument(
        "--set", dest="set_text", metavar="TEXT", help="set rules; '-' reads stdin; empty clears"
    )
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

    s = sub.add_parser(
        "report",
        parents=[common],
        help="latency, turns, posts vs passes and rules fired in a room (markdown or JSON)",
    )
    s.add_argument("--room", required=True, help="the room, e.g. '#build'")
    s.add_argument("--since", default=None, help="start at this ISO time (default: the room's creation)")
    s.add_argument("--last", default=None, help="only the last N s/m/h/d, e.g. 2h")
    s.add_argument("--json", action="store_true", help="JSON instead of markdown")
    s.add_argument("--out", default=None, help="write to FILE instead of stdout")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser(
        "update",
        parents=[common],
        help="upgrade switchboard and say what still runs the old version in this home",
    )
    s.add_argument(
        "--version", dest="to_version", metavar="X.Y.Z", default=None, help="a release (default: the newest)"
    )
    # update asks nothing now; a script that still passes --yes must not break
    s.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_update)

    s = sub.add_parser(
        "uninstall",
        parents=[common],
        help="remove switchboard's own MCP server and hooks from a harness (shows a diff first)",
    )
    s.add_argument(
        "harness",
        choices=["claude", "codex", "cursor", "devin", "all"],
        help="a harness, or all (the four in order, one confirmation)",
    )
    s.add_argument("--dry-run", action="store_true", help="show the diff, write nothing")
    s.add_argument("--yes", action="store_true", help="apply without asking")
    s.add_argument("--user-home", default=None, help="treat DIR as ~ (tests)")
    s.add_argument(
        "--purge-hooks",
        action="store_true",
        help="also delete switchboard's hook copies in <home>/hooks if no harness config still runs them",
    )
    s.set_defaults(func=cmd_uninstall)

    s = sub.add_parser("mcp", parents=[common], help="run the stdio MCP server (harnesses start this)")
    s.add_argument("--harness", choices=["test"], default=None, help=argparse.SUPPRESS)
    s.add_argument("--test-session", default=None, help=argparse.SUPPRESS)
    s.add_argument("--ack", choices=["next_call", "immediate", "never"], default=None, help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_mcp)

    for verb in ("install", "remote", "satellite", "join"):
        sub.add_parser(verb, parents=[common]).set_defaults(func=cmd_retired)

    s = sub.add_parser("hook", parents=[common], help="run the installed hook script (debugging)")
    s.add_argument("--harness", required=True, choices=["claude", "codex", "cursor", "devin"])
    s.add_argument("--event", required=True)
    s.add_argument("--max-wait", type=float, default=None)
    s.set_defaults(func=cmd_hook)
    return p


def _version() -> str:
    from switchboard import __version__, build_info

    return build_info.version_text(__version__, build_info.commit())


class _VersionAction(argparse.Action):
    """``--version``, worked out only when it's asked for: in a source checkout the commit comes
    from git, and building the parser for any other command shouldn't run it."""

    def __init__(self, option_strings: list[str], dest: str = argparse.SUPPRESS, **kw: Any) -> None:
        super().__init__(
            option_strings, dest, nargs=0, default=argparse.SUPPRESS, help="show the version and exit"
        )

    def __call__(
        self, parser: argparse.ArgumentParser, namespace: Any, values: Any, option_string: Any = None
    ) -> None:
        print(_version())
        parser.exit()


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if extra and args.cmd not in RETIRED:  # a retired verb takes any arguments it used to
        parser.error("unrecognized arguments: " + " ".join(extra))
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
