"""Turn a live run's raw hook payloads into sanitized Claude fixtures (DESIGN.md §12.3).

    uv run python tests/live/harness/fixtures.py <run dir> [--out tests/fixtures/payloads/claude]

Input: ``<run>/raw.jsonl`` (the test-only recorder: every Claude hook payload
of the run) and ``<run>/params/claude.jsonl`` (what the broker received,
``SWITCHBOARD_RECORD_PAYLOADS``; used as a cross-check). Output: one JSON file per
fixture name, ``_unverified: false``, with ids replaced, paths rewritten to
``/ws`` (workspace), ``/ws/run`` (switchboard home) or ``~``, and the local user
name removed. ``test_fixtures_scan.py`` must pass on the result.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "src"))

from switchboard.hook import switchboard_hook as hk  # noqa: E402

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TOOLU_RE = re.compile(r"toolu_[A-Za-z0-9]+")
MAX_STR = 2000


class Sanitizer:
    def __init__(self, run: Path, ws: Path | None, home: str, user: str):
        self.uuids: dict[str, str] = {}
        self.toolus: dict[str, str] = {}
        subs: list[tuple[str, str]] = []
        for real, fake in ((str(run), "/ws/run"), (str(ws) if ws else "", "/ws")):
            if real:
                subs.append((real, fake))
                if real.startswith("/private/"):
                    subs.append((real[len("/private"):], fake))
        subs.append((home, "~"))
        subs.append(("/private/tmp", "/tmp"))
        self.subs = sorted(subs, key=lambda x: -len(x[0]))
        self.user = user

    def uuid(self, u: str) -> str:
        if u not in self.uuids:
            n = len(self.uuids)
            self.uuids[u] = f"00000000-0000-4000-8000-00000000c1{n:02x}"
        return self.uuids[u]

    def toolu(self, t: str) -> str:
        if t not in self.toolus:
            self.toolus[t] = f"toolu_fixture_c{len(self.toolus) + 1:03d}"
        return self.toolus[t]

    def text(self, s: str) -> str:
        for real, fake in self.subs:
            s = s.replace(real, fake)
        # the claude-<uid> style temp dir names carry the uid; the scratchpad layout the project path
        s = re.sub(r"/tmp/claude-\d+/[^\s\"']*?/scratchpad", "/tmp/scratch", s)
        s = re.sub(r"/tmp/cc-socks/\d+\.sock", "/tmp/cc-socks/1234.sock", s)
        if self.user and len(self.user) >= 3:
            s = re.sub(re.escape(self.user), "user", s, flags=re.IGNORECASE)
        s = UUID_RE.sub(lambda m: self.uuid(m.group(0)), s)
        s = TOOLU_RE.sub(lambda m: self.toolu(m.group(0)), s)
        if len(s) > MAX_STR:
            s = s[:MAX_STR] + "…"
        return s

    def value(self, v: Any, key: str = "") -> Any:
        if isinstance(v, dict):
            return {k: self.value(x, k) for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(x, key) for x in v]
        if isinstance(v, str):
            if key == "transcript_path":
                return "/ws/transcript.jsonl"
            if key == "cwd":
                return "/ws"
            return self.text(v)
        return v


def pick(events: list[dict[str, Any]], event: str, pred: Any = lambda p: True) -> dict[str, Any] | None:
    for e in events:
        p = e.get("payload") or {}
        if e.get("event") == event and p.get("hook_event_name") == event and pred(p):
            return p
    return None


def tool_is(name: str) -> Any:
    return lambda p: p.get("tool_name") == name


def mcp_tool(p: dict[str, Any]) -> bool:
    return str(p.get("tool_name", "")).startswith("mcp__switchboard__")


def is_inbox_prompt(p: dict[str, Any]) -> bool:
    return "[switchboard]" in str(p.get("prompt", "")) and "yk:b" in str(p.get("prompt", ""))


SELECT: list[tuple[str, str, Any]] = [
    ("SessionStart_startup", "SessionStart", lambda p: p.get("source") == "startup"),
    ("SessionStart_clear", "SessionStart", lambda p: p.get("source") == "clear"),
    ("SessionStart_resume", "SessionStart", lambda p: p.get("source") == "resume"),
    ("UserPromptSubmit", "UserPromptSubmit", lambda p: not is_inbox_prompt(p)),
    ("UserPromptSubmit_inbox", "UserPromptSubmit", is_inbox_prompt),
    ("PreToolUse_bash", "PreToolUse", tool_is("Bash")),
    ("PostToolUse_bash", "PostToolUse", lambda p: p.get("tool_name") == "Bash"
     and "echo" in json.dumps(p.get("tool_input"))),
    ("PostToolUse_mcp", "PostToolUse", mcp_tool),
    ("PostToolUseFailure_bash", "PostToolUseFailure", tool_is("Bash")),
    ("PostToolUseFailure_mcp_timeout", "PostToolUseFailure", mcp_tool),
    ("Stop", "Stop", lambda p: p.get("stop_hook_active") is False),
    ("SessionEnd_clear", "SessionEnd", lambda p: p.get("reason") == "clear"),
    ("SessionEnd_exit", "SessionEnd", lambda p: p.get("reason") != "clear"),
    ("Notification", "Notification", lambda p: p.get("notification_type") == "permission_prompt"),
    ("PermissionRequest", "PermissionRequest", tool_is("Bash")),
]


def build(run: Path, out: Path, version: str, ws: Path | None) -> list[str]:
    events = [json.loads(x) for x in (run / "raw.jsonl").read_text().splitlines() if x.strip()]
    san = Sanitizer(run, ws, str(Path.home()), os.environ.get("USER", ""))
    src = f"m3:live claude {version} (haiku, default mode)"
    written = []
    chosen: dict[str, dict[str, Any]] = {}
    for name, event, pred in SELECT:
        p = pick(events, event, pred)
        if p is None:
            print(f"  (not recorded: {name})")
            continue
        chosen[name] = p
    for name, p in chosen.items():
        doc = {"_source": src, "_unverified": False}
        doc.update(san.value(p))
        (out / f"{name}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        written.append(name)
    # derived: one documented value changed on a recorded payload
    derived = [
        ("PostToolUse_bypass", "PostToolUse_bash", {"permission_mode": "bypassPermissions"},
         "recorded PostToolUse_bash with permission_mode set to bypassPermissions (the value M0 saw in"
         " bypass sessions; M3 launches no bypass session)"),
        ("Stop_active", "Stop", {"stop_hook_active": True},
         "recorded Stop with stop_hook_active true (a chained Stop, FINDINGS §3 2.2; switchboard never"
         " blocks a Claude Stop, so the live run can't produce one)"),
    ]
    for name, base, over, note in derived:
        if base not in chosen:
            continue
        doc = {"_source": src, "_unverified": False, "_derived": note}
        doc.update(san.value({**chosen[base], **over}))
        (out / f"{name}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        written.append(name)
    cross_check(run, chosen)
    return written


def cross_check(run: Path, chosen: dict[str, dict[str, Any]]) -> None:
    """The broker's recorded params for each event carry the same keys the hook
    script derives from the recorded payload (live params vs replay)."""
    path = run / "params" / "claude.jsonl"
    if not path.exists():
        print("  (no broker params recorded)")
        return
    rec: dict[str, set[frozenset[str]]] = {}
    for line in path.read_text().splitlines():
        d = json.loads(line)
        keys = frozenset(k for k in d["params"] if k not in ("t", "max_wait_s"))
        rec.setdefault(d["event"], set()).add(keys)
    for name, p in chosen.items():
        ev = p["hook_event_name"]
        if ev not in hk.HANDLED["claude"]:
            continue
        mine = frozenset(k for k in hk.build_params(p, "claude", ev, 0.0, 1.0) if k not in ("t", "max_wait_s"))
        ok = mine in rec.get(ev, set())
        print(f"  params {name}: {'matches a live relay' if ok else 'NO live relay with these keys'} {sorted(mine)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("--out", type=Path, default=REPO / "tests" / "fixtures" / "payloads" / "claude")
    ap.add_argument("--ws", type=Path, default=None, help="the run's workspace dir (rewritten to /ws)")
    a = ap.parse_args()
    res = json.loads((a.run / "results.json").read_text()) if (a.run / "results.json").exists() else {}
    version = str(res.get("claude_version", "?")).split()[0]
    ws = a.ws or (Path(res["ws"]) if res.get("ws") else None)
    written = build(a.run, a.out, version, ws)
    print(f"wrote {len(written)} fixtures to {a.out}: {', '.join(written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
