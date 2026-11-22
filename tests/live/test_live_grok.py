"""Live Grok Build over HTTPS (DESIGN.md §12.4, §49.15). Opt-in: ``SWITCHBOARD_LIVE=grok uv run pytest -m live``.

One real ``grok`` TUI in a private tmux server, with this checkout's ``plugins/grok`` installed in a temp $HOME
(``harness/grok.py``), against a throwaway broker in a temp home. By default the model is the person's own Grok
login; ``SWITCHBOARD_GROK_BASE_URL=standin`` runs the scripted stand-in model instead (no login, nothing spent;
any other value is a chat-completions URL of your own). The scenarios are ``harness/scenarios.py``'s, in order,
and one of Grok's own: a session waiting on its approval prompt is not woken, and the person's Esc declines it.
The real ``~/.grok`` must not change.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from harness import scenarios, screens
from harness.grok import GrokRun
from harness.scenarios import nonce, record, say_text
from harness.world import skip_reason, wait_for

pytestmark = pytest.mark.live

_why = skip_reason("grok", "grok")
if _why:
    pytest.skip(_why, allow_module_level=True)


@pytest.fixture(scope="module")
def run() -> Iterator[GrokRun]:
    r = GrokRun("grok-1")
    try:
        r.start()
        yield r
    finally:
        r.stop()
    assert r.results["drift_fail"] == [], f"the user's own Grok config changed: {r.results['drift_fail']}"
    assert r.results["leftover_processes"] == [], "processes of the run outlived it"


def test_1_join_and_say(run: GrokRun) -> None:
    scenarios.join_and_say(run)


def test_2_woken_while_idle_by_a_mention(run: GrokRun) -> None:
    scenarios.idle_wake(run)


def test_3_busy_during_a_task_then_idle_and_a_mention_arrives_after_the_next_tool_call(run: GrokRun) -> None:
    scenarios.mid_task(run)
    run.wait_settled()


def test_4_waiting_on_an_approval_is_not_woken(run: GrokRun) -> None:
    """The person asks Grok, in Grok, for a shell command a rule makes it ask about: its approval prompt shows
    the session waiting-approval in Members; a mention then waits, with nothing offered, while the prompt is open (§7.4). The driver cancels
    the prompt with Ctrl+C, never approves it: the turn ends (Grok's StopCancelled) and the session is idle but
    not listening, its mention still queued. The person's next prompt starts a turn, and the mention reaches
    it at that turn's Stop."""
    assert run.joined, "the join scenario did not complete"
    run.wait_settled()
    run.type('run the shell command "touch approval-was-given"')

    def prompted() -> bool:
        return (run.member() or {}).get("status") == "waiting-approval" and screens.approval_open(
            "grok", run.tmux.capture(run.session)
        )

    # not run.wait: it declines a stray prompt, and this one is the scenario's
    wait_for(prompted, 120, "Grok's approval prompt", on_fail=run.dump)
    word = f"waited-{nonce()}"
    asked = run.mention(say_text(word))
    m = run.member()
    assert m is not None and m["status"] == "waiting-approval" and m["queued"] >= 1, m
    assert run.broker.delivery_of(asked, run.name)["bstate"] is None, "a message was offered at the prompt"
    run.decline()
    run.wait(lambda: (run.member() or {}).get("status") == "idle", 60, "the declined prompt to end the turn")
    assert not run.flag("approval-was-given"), "the command ran: the prompt was approved"
    m = run.member()
    assert m is not None and m["queued"] >= 1 and "type in Grok" in (m["parked_reason"] or ""), m
    run.type("Reply with one word: back.")
    answer = run.reply(word, after=asked, timeout=180)
    d = scenarios.delivery(run, asked)
    assert d["path"] in run.wake_paths, (d["path"], sorted(run.wake_paths), d)
    record(run, "approval", held=True, declined=True, path=d["path"], answer_id=answer["id"])


def test_5_quit_shows_offline(run: GrokRun) -> None:
    scenarios.quit_offline(run)
