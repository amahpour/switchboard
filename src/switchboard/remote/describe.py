"""One-line descriptions of a remote link's state, for ``switchboard status``,
``switchboard remote status`` and the notices (DESIGN.md §27.11). No I/O."""

from __future__ import annotations

from typing import Any

# blocked(reason): what to tell the owner (§27.4.7). A block never retries by itself.
BLOCK_HINTS = {
    "proto": "the satellite speaks another link protocol: install the same switchboard version on both machines",
    "name": "the satellite answers to another name: check satellite.toml on the remote",
    "shell_noise": "the remote's login shell prints text before switchboard starts: check ~/.bashrc there",
    "replaced": "another satellite took this link over (something else used the link key)",
    "local_broker": "a switchboard broker runs on the remote from the satellite's home: stop it there",
    "test_mode": "the satellite runs in test mode and this broker doesn't",
    "transport": "this switchboard version can't dial over ssh yet",
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
        return f"{name}: blocked: {reason}: {BLOCK_HINTS.get(reason or '', reason)}{tail}"
    return f"{name}: {state}{f' ({reason})' if reason else ''}{tail}"
