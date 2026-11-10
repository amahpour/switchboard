"""SPIKE (#694): real Claude Code listening to a replayed call. Test-only, opt-in, real time.

    SWITCHBOARD_EVAL=claude ~/.local/bin/sb-suite uv run python tests/evals/listen_spike.py \
        --quiet 15 --hold 30 --k 3 --model haiku --out tests/evals/_results/listen-15-30.json

One agent (alpha) is briefed in the room, set to listen (``/listen alpha``), and then the lines of two
excerpts of ``tests/fixtures/calls/status_meeting.json`` are posted into the room as call lines, in real time
(silences cut to 10 s). The first excerpt holds the question the agent was briefed on (the upload limit),
the second a question that asks for a date. Lines go straight into the broker's database with the real
store (a throwaway home), as the bridge would post them; the broker's own tick wakes the agent.

What is read afterwards, from the database: the agent's posts and their timestamps, its passes and wakes, and
its tokens and cost from its own transcript.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from evalkit.claude_world import ClaudeWorld  # noqa: E402
from evalkit.record import read_record, sum_tokens  # noqa: E402
from evalkit.scenario import Scenario, Step  # noqa: E402
from evalkit.trial import HarnessError, Limits, _Guard, metrics_of, scrub  # noqa: E402
from listen_replay import script  # noqa: E402

from switchboard import db  # noqa: E402
from switchboard.clock import SystemClock  # noqa: E402
from switchboard.store import Store  # noqa: E402

ROOM = "#status"
AGENT = "alpha"
BRIEFING = (
    "@alpha I am leaving the weekly status call and sending you to listen in my place. Its lines will arrive "
    "in this room as call lines. Stay quiet and pass() unless you have something to add that I would want "
    "said. "
    "If anyone asks about the upload limit for attachments, the answer is 10 MB. Never commit to a date. "
    "Voice is on in this call: start a say() with @call to have it spoken aloud to the people on "
    "it. "
    "No need to answer this message."
)
UPLOAD = re.compile(r"\b10\s?MB\b", re.I)
DATEY = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|next week|next month"
    r"|end of (the )?(week|month)|by \w+day)\b",
    re.I,
)


def excerpts() -> list[tuple[float, str, str, str]]:
    """(seconds from the start of the replay, speaker, text, tag) for two stretches of the call."""
    lines = script(lag_s=4.0)
    key = next(u.index for u in lines if u.tag == "key")
    date = next(u.index for u in lines if u.tag == "date")
    picked = list(range(key - 12, key + 9)) + list(range(date - 6, date + 8))
    out: list[tuple[float, str, str, str]] = []
    t = 0.0
    prev: float | None = None
    last_idx = -2
    for i in picked:
        u = lines[i]
        if prev is not None and i == last_idx + 1:
            t += min(u.posted - prev, 10.0)
        elif prev is not None:
            t += 10.0  # the skipped stretch of the call
        out.append((t, u.speaker, u.text, u.tag))
        prev, last_idx = u.posted, i
    return out


class SpikeWorld(ClaudeWorld):
    """A ClaudeWorld whose broker has the settings under test in its config.toml."""

    def __init__(self, model: str, quiet_s: float, max_hold_s: float) -> None:
        super().__init__(model)
        self.quiet_s, self.max_hold_s = quiet_s, max_hold_s

    def start(self, scenario: Scenario, rules: str, limits: Limits) -> None:
        orig = Path.write_text
        extra = f"\n[delivery]\nquiet_s = {self.quiet_s}\nmax_hold_s = {self.max_hold_s}\n"

        def write_text(p: Path, data: str, *a: object, **k: object) -> int:
            if p.name == "config.toml":
                data += extra
            return orig(p, data, *a, **k)  # type: ignore[arg-type]

        with mock.patch.object(Path, "write_text", write_text):
            super().start(scenario, rules, limits)


def one_trial(n: int, model: str, quiet_s: float, max_hold_s: float) -> dict:
    sc = Scenario("listen-spike", "a briefed agent listens", ROOM, (AGENT,), (Step("x"),), ())
    lim = Limits(budget_per_hour=60, hop_limit=0, settle_s=240.0)
    world = SpikeWorld(model, quiet_s, max_hold_s)
    guard = _Guard(world, sc, lim)
    started = time.monotonic()
    try:
        world.start(sc, "", lim)
        guard.wait_settled("after the agent joined")
        before = sum_tokens(guard.tokens().values())
        brief_id = world.say(BRIEFING)
        guard.wait_settled("after the briefing")
        world.broker.command(f"/listen {AGENT}")
        guard.wait_settled("after /listen")
        after_brief = sum_tokens(guard.tokens().values())
        con = db.open_db(world.db_path)
        store = Store(con, SystemClock())
        room = store.get_room(ROOM)
        assert room is not None
        posted: list[tuple[float, str, int]] = []
        t0 = time.time()
        for at, who, text, tag in excerpts():
            wait = t0 + at - time.time()
            if wait > 0:
                time.sleep(wait)
            guard.check()
            msg = store.insert_message(
                room.id, sender_name=who, sender_kind="meeting", via="meeting", text=text
            )
            posted.append((msg.ts, tag, msg.id))
        # the last lines wait for the pause, and the agent's turn may follow
        time.sleep(min(max_hold_s + quiet_s + 10, 120))
        guard.wait_settled("after the call lines")
        con.close()
        final = guard.tokens()
        rec = read_record(world.db_path, ROOM, (AGENT,), world.human, (brief_id,), final)
        total = sum_tokens(final.values())
        m = metrics_of(rec, before, time.monotonic() - started, {})
        key_ts = next(ts for ts, tag, _ in posted if tag == "key")
        date_ts = next(ts for ts, tag, _ in posted if tag == "date")
        listen_ts = posted[0][0]
        posts = [x for x in rec.posts_after(brief_id) if x.ts >= listen_ts]
        up = [x for x in posts if UPLOAD.search(x.text) and x.ts >= key_ts]
        dated = [x for x in posts if DATEY.search(x.text)]
        early = [x for x in posts if x.ts < key_ts]
        wakes = [
            e for e in rec.events if e.kind == "offer" and e.ts >= listen_ts and (e.data or {}).get("counted")
        ]
        passes = [e for e in rec.events if e.kind == "pass" and e.ts >= listen_ts]
        return {
            "n": n,
            "status": "graded",
            "quiet_s": quiet_s,
            "max_hold_s": max_hold_s,
            "model": model,
            "wakes": len(wakes),
            "passes": len(passes),
            "posts": len(posts),
            "at_call_posts": sum(1 for x in posts if x.text.lstrip().startswith("@call")),
            "spoke_up_on_the_briefed_topic": bool(up),
            "latency_key_line_to_say_s": round(up[0].ts - key_ts, 1) if up else None,
            "posts_before_the_key_line": len(early),
            "posts_not_about_uploads": len([x for x in posts if not UPLOAD.search(x.text)]),
            "date_like_posts": [scrub(x.text, 160) for x in dated],
            "date_line_to_first_post_s": next(
                (round(x.ts - date_ts, 1) for x in posts if x.ts >= date_ts), None
            ),
            "post_texts": [scrub(x.text, 160) for x in posts],
            "model_calls_after_brief": (total - after_brief).calls,
            "tokens_after_brief": (total - after_brief).as_dict(),
            "tokens_total": total.as_dict(),
            "cost_usd_after_brief": (total - after_brief).cost_usd,
            "cost_usd_total": total.cost_usd,
            "per_agent": m["by_agent"],
            "seconds": round(time.monotonic() - started, 1),
        }
    except HarnessError as e:
        return {
            "n": n,
            "status": "harness_error",
            "kind": e.kind,
            "error": scrub(str(e)),
            "cost_usd_total": guard.peak.cost_usd,
        }
    except Exception as e:  # noqa: BLE001 - recorded as the run's error
        return {
            "n": n,
            "status": "harness_error",
            "kind": "error",
            "error": scrub(f"{type(e).__name__}: {e}"),
            "cost_usd_total": guard.peak.cost_usd,
        }
    finally:
        world.stop()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", type=float, required=True)
    ap.add_argument("--hold", type=float, required=True)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-cost", type=float, default=0.7)
    a = ap.parse_args()
    results: list[dict] = []
    spent = 0.0
    for n in range(1, a.k + 1):
        r = one_trial(n, a.model, a.quiet, a.hold)
        results.append(r)
        spent += float(r.get("cost_usd_total") or 0.0)
        print(
            json.dumps({k: v for k, v in r.items() if k not in ("post_texts", "tokens_total", "per_agent")}),
            flush=True,
        )
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(results, indent=1))
        if spent > a.max_cost:
            print(f"stopping: ${spent:.2f} spent, over the cap of ${a.max_cost:.2f}", flush=True)
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
