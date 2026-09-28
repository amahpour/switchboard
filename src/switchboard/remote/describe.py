"""One-line descriptions of a remote link's state, for ``switchboard status``,
``switchboard remote status`` and the notices (DESIGN.md §27.11). No I/O."""

from __future__ import annotations

from typing import Any

# blocked(reason): what to tell the owner (§27.4.7). A block never retries by itself.
# "<name>" is replaced by the remote's name.
BLOCK_HINTS = {
    "proto": "the satellite speaks another link protocol: install the same switchboard version on both machines",
    "name": "the satellite answers to another name: check satellite.toml on the remote",
    "shell_noise": "the remote's login shell prints text before switchboard starts: check ~/.bashrc there",
    "replaced": "another satellite took this link over (something else used the link key)",
    "local_broker": "a switchboard broker runs on the remote from the satellite's home: stop it there",
    "test_mode": "the satellite runs in test mode and this broker doesn't",
    "host_key": "the remote's host key is not the one pinned at `remote add` (it was reinstalled, or something"
                " is in the middle): compare fingerprints on that machine; to pin its new key run"
                " `switchboard remote remove <name>`, `remote add` and `remote accept` again",
    "auth": "the remote refused the link key: run `switchboard remote accept` there with the token"
            " `remote add` printed, and check ~/.ssh/authorized_keys there",
    "files": "remotes/<name>/ lacks its link key or pinned host key, or they aren't private:"
             " run `switchboard remote remove <name>`, `remote add` and `remote accept` again",
    "ssh_bin": "/usr/bin/ssh is missing, or not owned by root: switchboard dials only through the system's"
               " OpenSSH client",
    "negotiate": "this machine's ssh and the remote's sshd share no key-exchange, cipher or host-key algorithm"
                 " (one of them is very old or locked down): update OpenSSH on the older side",
    "command": "the remote couldn't run the forced command (switchboard moved or was uninstalled there):"
               " run `switchboard remote accept` there again",
    "satellite": "the satellite refused to start on the remote (the detail says why): run"
                 " `switchboard remote doctor` there",
    "exposed": "another process on the remote held the link's stdio when the satellite started (a process"
               " of that user raced it, or the login shell left one behind): check that machine",
}


def fmt_ms(ms: float) -> str:
    """2.1 below 10 ms, 37 above."""
    return f"{ms:.1f}" if ms < 10 else f"{ms:.0f}"


def describe(info: dict[str, Any]) -> str:
    """One line for ``switchboard status`` and ``remote status``."""
    name, state, reason = info["name"], info["state"], info.get("reason")
    members = info.get("members") or []
    tail = f", members {', '.join(members)}" if members else ""
    if state == "up":
        rtt = f"{fmt_ms(info['rtt_ms'])} ms" if info.get("rtt_ms") is not None else "rtt ?"
        return f"{name}: up {rtt}, satellite {info.get('version') or '?'}{tail}"
    if state == "disabled" and reason == "config_changed":
        return f"{name}: needs enable (config changed): run `switchboard remote enable {name}`{tail}"
    if state == "disabled" and reason == "not_enabled":
        return f"{name}: needs enable: run `switchboard remote enable {name}`{tail}"
    if state == "disabled" and reason == "disabled":
        return f"{name}: disabled (`switchboard remote enable {name}` dials it again){tail}"
    if state == "down":
        retry = info.get("retry_in_s")
        r = f" (retry in {retry:.0f} s)" if retry is not None else ""
        d = f" [{info['detail']}]" if info.get("detail") else ""
        return f"{name}: down: {reason or '?'}{r}{d}{tail}"
    if state == "blocked":
        hint = str(BLOCK_HINTS.get(reason or "", reason)).replace("<name>", name)
        d = f" [{info['detail']}]" if info.get("detail") else ""
        return f"{name}: blocked: {reason}: {hint}{d}{tail}"
    return f"{name}: {state}{f' ({reason})' if reason else ''}{tail}"
