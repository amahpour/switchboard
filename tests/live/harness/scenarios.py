"""What every harness is asked to do over HTTPS (DESIGN.md §12.4, §49). Test-only.

Each function is one scenario against a ``Run``, in the order a person meets them: the agent joins and
says something; it is woken while idle by an @mention; it hears an @mention in the middle of a task, after
its next tool call; a subagent's tool calls take nothing meant for the session; it quits and shows as
offline; it resumes and is reachable again; a revoked token ends it. They run in order on one session,
because each leaves it where the next starts. The messages come from the person through the web API, the
same way the page posts them; the agent is only ever typed to for its first prompt, a resume that needs
one, and a subagent task. A scenario fails with the screen, the broker's log and the members row.

The bugs these exist to catch were each found by hand: a resumed session that nobody could wake until its
first turn (#412), a listener that ended without a word (#436), a subagent's tool calls taking the
session's messages (#446), a killed Devin shown busy for good (#448), a Codex Stop hook that failed after
five minutes (#449).
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from .run import Run
from .world import ROOM, poll


def nonce() -> str:
    return secrets.token_hex(3)


def say_text(word: str) -> str:
    return f'call say with the text "{word}"'


def record(run: Run, name: str, **fields: Any) -> None:
    run.results["scenarios"][name] = fields


def latency(run: Run, asked: int, answer: dict[str, Any]) -> float:
    """Seconds from the person's message to the agent's answer, on the broker's clock."""
    asked_ts = next(m["ts"] for m in run.broker.messages(asked - 1) if m["id"] == asked)
    return round(answer["ts"] - asked_ts, 1)


def delivery(run: Run, message_id: int) -> dict[str, Any]:
    """How the message reached the agent: the delivery's state and its batch's path and kind, once the batch
    is confirmed (the agent acted on it, or a hook acknowledged it)."""
    last: dict[str, Any] = {}

    def row() -> dict[str, Any] | None:
        d = run.broker.delivery_of(message_id, run.name)
        if d is not None:
            last.update(dict(d))
        return dict(d) if d is not None and d["bstate"] == "confirmed" else None

    try:
        return run.wait(row, 30, f"a confirmed delivery of message {message_id} to {run.name}")
    except AssertionError as e:
        raise AssertionError(f"{e}\nthe last delivery row seen: {last}") from None


# ----------------------------------------------------------------------- join
def join_and_say(run: Run) -> None:
    """It joins a room over HTTPS and says something; Members shows it on its device, with its tier."""
    run.launch()
    run.after_launch()
    run.type(run.join_prompt())
    run.wait(lambda: run.member() is not None, 180, f"{run.name} to join {ROOM}")
    m = run.member()
    assert m is not None
    assert (m["harness"], m["tier"], m["host"]) == (run.harness, run.tier, run.device), m
    hello = run.reply(f"hello from {run.name}", after=0)
    assert hello["via"] == "mcp" and hello["host"] == run.device, hello
    run.joined = True
    run.wait_settled(180)
    record(run, "join", tier=m["tier"], host=m["host"], approval_mode=run.member()["approval_mode"])


# ---------------------------------------------------------------------- waking
def idle_wake(run: Run) -> None:
    """An @mention wakes it between turns, with nothing typed to it: it is delivered at the Stop hook, the
    listener or the wait() it is held in (§49.4), and it answers in the room."""
    assert run.joined, "the join scenario did not complete"
    run.wait_settled()
    word = f"pong-{nonce()}"
    asked = run.mention(say_text(word))
    answer = run.reply(word, after=asked)
    d = delivery(run, asked)
    assert d["path"] in run.wake_paths, (d["path"], "expected one of", sorted(run.wake_paths), d)
    record(run, "idle_wake", path=d["path"], seconds=latency(run, asked, answer))


def mid_task(run: Run) -> None:
    """An @mention that arrives while a tool call runs reaches the agent after its next tool call, not at its
    next turn: held shell command, mention, release. What is checked is the delivery switchboard records
    (the path, and that it was acknowledged), because whether a cheap model then acts on a message that comes
    as hook context is the model's: whether it answered is reported, not asserted."""
    assert run.joined, "the join scenario did not complete"
    run.wait_settled()
    word, done = f"mid-{nonce()}", f"task-{nonce()}"
    task = run.mention(
        'run the shell command "sh hold.sh mid" and wait for it to finish; call no other tool while it runs.'
        f" When it has finished, {say_text(done)}."
    )
    run.wait(lambda: run.flag("mid.started"), 180, "the held shell command to start")
    m = run.member()
    assert m is not None and m["status"] == "busy", m
    asked = run.mention(f"Another thing: {say_text(word)}.")
    run.wait(lambda: (run.member() or {}).get("queued", 0) >= 1, 30, "the mention to wait for the busy agent")
    run.release("mid")
    run.reply(done, after=task)  # the held command ended and the agent went on
    d = delivery(run, asked)
    assert d["path"] in run.mid_paths, (d["path"], "expected one of", sorted(run.mid_paths), d)
    answered = poll(lambda: run.broker.said_by(run.name, word, after=asked), 20)
    record(run, "mid_task", path=d["path"], acknowledged=True, answered=answered)


def start_subagent(run: Run, tries: int = 3) -> int:
    """Get a subagent running a held shell command (``sub1.started``). A cheap model sometimes declines to
    start one ("the tool isn't available", seen with Codex's), which is the model's and not what is being
    tested, so the task is asked again, up to ``tries`` times; how many it took is in the results."""
    for attempt in range(1, tries + 1):
        run.start_subagent()
        try:
            run.wait(lambda: run.flag("sub1.started"), 90, "the subagent's first held shell command to start")
            return attempt
        except AssertionError:
            if attempt == tries:
                raise
            run.wait_settled(60)
    raise AssertionError("unreachable")


def subagent(run: Run) -> None:
    """A subagent's tool calls take nothing meant for the session (#446, #465). The session ended its turn
    and listens; a background subagent finishes one shell command (its hooks run, carrying the session's id)
    and has a second in flight. The session still shows idle, an @mention wakes it, and it answers before the
    subagent is released. Without the fix the subagent's hook turns the session busy and ends its listener,
    and the message is handed to the subagent instead."""
    assert run.joined, "the join scenario did not complete"
    run.wait_settled()
    attempts = start_subagent(run)
    run.wait_settled(120)  # the session ended its turn, and listens
    run.release("sub1")  # the subagent's tool call ends: its hooks run
    run.wait(lambda: run.flag("sub2.started"), 120, "the subagent's second held shell command to start")
    run.wait_settled(60)  # its hooks did not turn the session busy, nor end its listener
    word = f"parent-{nonce()}"
    asked = run.mention(say_text(word))
    answer = run.reply(word, after=asked, timeout=90)
    assert not run.flag("sub2.go"), "the subagent was released before the session answered"
    d = delivery(run, asked)
    assert d["path"] in run.wake_paths, (d["path"], "expected one of", sorted(run.wake_paths), d)
    run.release("sub2")
    run.wait_subagent_done()  # the subagent finishes and the session takes its turn
    record(run, "subagent", path=d["path"], seconds=latency(run, asked, answer), attempts=attempts)


# ------------------------------------------------------------------ quit, resume
def quit_offline(run: Run) -> None:
    """Quitting the CLI shows it offline in Members, still a member of the room (DESIGN §49.2)."""
    assert run.joined, "the join scenario did not complete"
    run.wait_settled()
    run.quit()
    run.wait(lambda: (run.member() or {}).get("status") == "offline", 60, f"{run.name} to show offline")
    run.last_screen = run.wait_exited(60)
    assert run.member() is not None, "the member left the room when it quit"
    record(run, "quit", status="offline")


def resume(run: Run, label: str = "resume") -> None:
    """The same session resumed comes back, and an @mention wakes it. Where the harness reports a resume
    itself (Claude Code's SessionStart, #412) nothing is typed before the mention; the others are given
    their first prompt, as a person resuming one would."""
    assert run.joined, "the join scenario did not complete"
    typed = run.typed
    run.launch(resume=True)
    run.after_resume()
    run.wait(lambda: (run.member() or {}).get("status") != "offline", 120, f"{run.name} to come back")
    run.wait_settled(180)
    word = f"back-{nonce()}"
    asked = run.mention(say_text(word))
    answer = run.reply(word, after=asked)
    d = delivery(run, asked)
    assert d["path"] in run.wake_paths, (d["path"], "expected one of", sorted(run.wake_paths), d)
    if not run.resume_needs_prompt:
        assert run.typed == typed, "a prompt was typed after the resume"
    record(
        run, label, path=d["path"], typed_after_resume=run.typed - typed, seconds=latency(run, asked, answer)
    )


def killed_shows_offline(run: Run) -> None:
    """A session that is killed, not quit (a closed terminal: SIGHUP), runs no SessionEnd hook, and still ends
    up offline in Members within about two minutes, still a member (#448); resumed, it comes back."""
    assert run.joined, "the join scenario did not complete"
    run.wait_settled()
    started = time.time()
    run.tmux.kill_session(run.session)
    run.wait(
        lambda: (run.member() or {}).get("status") == "offline", 240, f"killed {run.name} to show offline"
    )
    gone = round(time.time() - started)
    assert run.member() is not None, "the member left the room when it was killed"
    assert gone <= 200, f"it took {gone}s to show offline"
    record(run, "killed", seconds_to_offline=gone)
    resume(run, label="resume_after_kill")


def cursor_lists_tools(run: Any) -> None:
    """Needs no model: Cursor connects to the broker over HTTP with the agent token and lists its tools, and
    the broker saw the token used."""
    run.enable_server()
    tools = run.listed_tools()
    assert {"switchboard_join", "switchboard_say", "switchboard_wait"} <= set(tools), tools
    used = [t for t in run.broker.tokens() if t["device"] == run.device]
    assert used and used[0]["last_used_at"] is not None, used
    record(run, "connect", tools=len(tools))


def revoked_token(run: Run) -> None:
    """Revoking the device's token ends its members at once (DESIGN §49.2); it shows in the room."""
    assert run.joined, "the join scenario did not complete"
    started = time.time()
    run.broker.revoke_token(run.token_id)
    run.wait(lambda: run.member() is None, 30, f"{run.name} to leave when its token is revoked")
    notes = [m["text"] for m in run.broker.messages() if m["kind"] == "leave" and m["from"] == run.name]
    assert notes and "revoked" in notes[-1], notes
    record(run, "revoked", seconds=round(time.time() - started, 1), note=notes[-1])
