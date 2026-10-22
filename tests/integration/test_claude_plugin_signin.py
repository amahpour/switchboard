"""Claude Code's plugin when the agent signed in through the browser and no token is typed (#488,
DESIGN.md §49.11).

Claude Code keeps a browser sign-in's token to itself, so the plugin's scripts have none to send. Its MCP
server sends a header made by ``bin/headers`` (the sha256 of a listener key the script keeps in a 0600 file of
the plugin's data folder), the server learns the key over the signed-in connection, and ``bin/listen`` and
``bin/session`` present the key. These tests run the three scripts as Claude Code runs them (the POSIX ones:
native Windows has no key), against stand-in servers for each answer and against a broker for the whole way.
What the server does with a key is in ``test_listener_keys.py``."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import InProcBroker
from test_agent_plugins import (
    CLAUDE_PLUGIN,
    HELPER_COMMAND,
    SESSION_1,
    TOKEN,
    Gateway,
    Stub,
    listen_log,
    run_listen,
    run_session,
)
from test_agents_http import make_token
from test_agents_http_claude import SESSION_2
from test_agents_http_wake import parked, participant, until
from test_listener_keys import SignedIn

from switchboard.broker import agent_tokens

KEY = "sbl_" + "0123456789abcdef" * 4
KEY_RE = re.compile(r"sbl_[0-9a-f]{64}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
HEADER = "X-Switchboard-Listener"
UNKNOWN = '{"error": "unknown_key", "message": "the listener key was not accepted"}'
REVOKED = '{"error": "revoked", "message": "the listener key was not accepted"}'
WRONG = '{"error": "wrong_session", "message": "the listener key is for another session"}'
NO_TOKEN = ""  # nothing typed in the plugin's settings: the agent signed in through the browser
ME = os.getpid()  # Claude Code, as the scripts these tests start see it: the process that starts them


def run_headers(
    data: Path | str,
    *,
    root: Path = CLAUDE_PLUGIN,
    path: str | None = None,
    command: str = HELPER_COMMAND,
) -> subprocess.CompletedProcess[str]:
    """``bin/headers`` as Claude Code runs a headersHelper: the command in ``sh -c`` (so Claude Code, here the
    test, is the shell's parent), the plugin's root and data folder filled in. Claude Code gives it nothing
    else (not the plugin's settings, which is why a typed token goes in a header of its own)."""
    cmd = command.replace("${CLAUDE_PLUGIN_ROOT}", root.as_posix()).replace(
        "${CLAUDE_PLUGIN_DATA}", str(data)
    )
    env = {"PATH": path if path is not None else os.environ["PATH"]}
    sh = shutil.which("sh") or "/bin/sh"
    return subprocess.run([sh, "-c", cmd], env=env, capture_output=True, text=True, timeout=30)


def printed_hash(r: subprocess.CompletedProcess[str]) -> str:
    assert (r.returncode, r.stderr) == (0, ""), r
    [(name, h)] = json.loads(r.stdout).items()
    assert name == HEADER and HASH_RE.fullmatch(h), r.stdout
    return str(h)


def key_file(data: Path, owner: int = ME) -> Path:
    return data / f"listener-key-{owner}"


def put_key(data: Path, key: str = KEY, owner: int = ME) -> Path:
    """What ``bin/headers`` leaves: the key in a 0600 file named for Claude Code's pid."""
    data.mkdir(parents=True, exist_ok=True)
    f = key_file(data, owner)
    f.write_text(key + "\n")
    f.chmod(0o600)
    return f


# ------------------------------------------------------------------------------------ the helper
def test_the_helper_makes_a_key_for_this_claude_code_and_prints_only_its_hash(tmp_path: Path) -> None:
    """Run as Claude Code runs it, the helper keeps a new key (``sbl_`` and 64 hex) in a 0600 file named for
    Claude Code's pid and prints one header, the key's sha256, and nothing else: the key never leaves the
    machine in this output. A second run (the next connection) makes a new key in the same file and leaves no
    temporary file behind. Fails if the key is printed, is guessable or is readable by others."""
    data = tmp_path / "data"
    first = run_headers(data)
    h = printed_hash(first)
    f = key_file(data)
    key = f.read_text().strip()
    assert KEY_RE.fullmatch(key) and hashlib.sha256(key.encode()).hexdigest() == h
    assert key not in first.stdout and key not in first.stderr
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    again = run_headers(data)
    assert printed_hash(again) != h
    assert f.read_text().strip() != key and KEY_RE.fullmatch(f.read_text().strip())
    assert [p.name for p in data.iterdir()] == [f.name]


def test_the_helper_is_started_by_a_shell_or_directly_and_names_claude_codes_pid_both_ways(
    tmp_path: Path,
) -> None:
    """Claude Code runs a helper as ``sh -c <command>`` (seen in 2.1.307: claude, then the shell, then the
    script), and the file must carry the pid its command hooks get as ``CLAUDE_PID``, which is Claude Code's
    own. The script takes its parent's, or its parent's parent when that is a shell, so a Claude Code that
    one day starts it with no shell in between still names the same pid. Fails if the file is named for the
    shell."""
    data = tmp_path / "data"
    printed_hash(run_headers(data))
    assert key_file(data).is_file() and [p.name for p in data.iterdir()] == [f"listener-key-{ME}"]
    other = tmp_path / "direct"
    r = subprocess.run(
        [str(CLAUDE_PLUGIN / "bin" / "headers"), str(other)], capture_output=True, text=True, timeout=30
    )
    printed_hash(r)
    assert key_file(other).is_file()


def test_the_helper_removes_the_keys_of_claude_codes_that_have_gone(tmp_path: Path) -> None:
    """Each connection leaves a file, so the helper removes those of processes that no longer exist and
    keeps the live ones' and anything else in the folder. Fails if the folder fills with dead keys, or a
    live Claude Code's key is removed."""
    data = tmp_path / "data"
    data.mkdir()
    gone = subprocess.Popen(["sleep", "0"])
    gone.wait()
    alive = subprocess.Popen(["sleep", "60"])
    try:
        for pid in (gone.pid, alive.pid):
            put_key(data, owner=pid)
        (data / "listener-key-notapid").write_text("x")
        (data / "listen.log").write_text("x")
        printed_hash(run_headers(data))
        assert sorted(p.name for p in data.iterdir()) == sorted(
            [f"listener-key-{alive.pid}", f"listener-key-{ME}", "listener-key-notapid", "listen.log"]
        )
    finally:
        alive.kill()
        alive.wait()


def test_the_helper_replaces_a_link_planted_at_the_keys_name_and_never_follows_it(tmp_path: Path) -> None:
    """The key is written to a temporary file and renamed over its name, so a link someone planted there
    (to a file of the person's) is replaced and never written through. Fails if the file the link points
    at is changed."""
    data = tmp_path / "data"
    data.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("precious")
    key_file(data).symlink_to(victim)
    printed_hash(run_headers(data))
    assert victim.read_text() == "precious"
    f = key_file(data)
    assert not f.is_symlink() and KEY_RE.fullmatch(f.read_text().strip())


def test_the_helper_works_from_a_plugin_folder_with_spaces_and_an_apostrophe(tmp_path: Path) -> None:
    """The command is quoted for ``sh``, Git Bash and PowerShell alike, so a plugin folder or a data folder
    with a space or an apostrophe in its name is no trouble. Fails if either breaks the command."""
    root = tmp_path / "it's a plugin folder"
    shutil.copytree(CLAUDE_PLUGIN / "bin", root / "bin")
    data = tmp_path / "plugin data's folder"
    printed_hash(run_headers(data, root=root))
    assert key_file(data).is_file()


def tools_without(tmp_path: Path, *missing: str) -> str:
    """A PATH of links to the ordinary tools the helper uses, less the ones named."""
    bin_ = tmp_path / "tools"
    bin_.mkdir()
    for name in (
        "sh",
        "uname",
        "od",
        "tr",
        "cut",
        "sed",
        "mktemp",
        "mv",
        "chmod",
        "cat",
        "ps",
        "rm",
        "mkdir",
    ):
        if name not in missing and (found := shutil.which(name)):
            (bin_ / name).symlink_to(found)
    return str(bin_)


@pytest.mark.parametrize("why", ["unwritable", "relative", "none", "no hash tool", "windows"])
def test_the_helper_prints_an_empty_object_when_it_cant_make_a_key(tmp_path: Path, why: str) -> None:
    """Whatever stops it (a folder it can't write, a relative or no folder, a machine with no way to hash,
    native Windows, which has no key yet), it prints ``{}`` and exits 0, so the connection simply has no key
    and the scripts do nothing, as before: it never breaks the connection. Fails if it prints an error, a
    partial header or a code that makes Claude Code complain."""
    data = tmp_path / "data"
    data.mkdir()
    path: str | None = None
    arg: Path | str = data
    if why == "unwritable":
        data.chmod(0o500)
    elif why == "relative":
        arg = "relative/folder"
    elif why == "none":
        arg = ""
    elif why == "no hash tool":
        path = tools_without(tmp_path, "sha256sum", "shasum", "openssl")
        for hasher in ("sha256sum", "shasum", "openssl"):  # not even one found elsewhere on the machine
            assert shutil.which(hasher, path=path) is None
    else:
        stub = tmp_path / "stub"
        stub.mkdir()
        (stub / "uname").write_text("#!/bin/sh\necho MSYS_NT-10.0\n")
        (stub / "uname").chmod(0o755)
        path = f"{stub}:{os.environ['PATH']}"
    try:
        r = run_headers(arg, path=path)
    finally:
        data.chmod(0o700)
    assert (r.returncode, r.stdout, r.stderr) == (0, "{}\n", ""), r
    assert [p.name for p in data.iterdir()] == []


def test_the_command_for_the_helper_runs_in_sh_and_names_the_plugins_own_folders() -> None:
    """The headersHelper is one command string that means the same in ``sh``, Git Bash and PowerShell (the
    hooks' trick, #440): ``cd`` into ``bin``, run ``./headers`` with the data folder, and exit with its
    code. Fails if the command changes, or the script isn't one a shell can start."""
    assert (
        HELPER_COMMAND
        == 'cd "${CLAUDE_PLUGIN_ROOT}/bin"; ./headers "${CLAUDE_PLUGIN_DATA}"; exit $LASTEXITCODE'
    )
    assert os.access(CLAUDE_PLUGIN / "bin" / "headers", os.X_OK)


# ----------------------------------------------------------------------------------- the listener
def test_with_no_typed_token_the_listener_presents_the_key_and_a_typed_token_still_wins(
    tmp_path: Path,
) -> None:
    """Without a token the listener reads the key from its file and sends it as the bearer, for the one route
    it uses; with a token typed in the plugin's settings it sends that, exactly as before, key file or not.
    Fails if the key is used when a token is set, or isn't used when none is."""
    data = tmp_path / "data"
    put_key(data)
    stub = Stub([(200, "a message")])
    try:
        r = run_listen(None, NO_TOKEN, SESSION_1, url=stub.url, data=data)
    finally:
        stub.close()
    assert (r.returncode, r.stderr) == (2, "a message"), r
    [(method, path, headers, _)] = stub.requests
    assert (method, path) == ("POST", "/mcp/listen")
    assert headers["Authorization"] == f"Bearer {KEY}" and headers["X-Switchboard-Session"] == SESSION_1
    stub = Stub([(200, "a message")])
    try:
        r = run_listen(None, TOKEN, SESSION_1, url=stub.url, data=data)
    finally:
        stub.close()
    assert r.returncode == 2 and stub.requests[0][2]["Authorization"] == f"Bearer {TOKEN}"


def test_the_listener_keeps_the_key_and_the_token_off_every_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other processes can read a command line, so the credential reaches ``curl`` on its stdin (``-K -``) and
    in no argument: not the key, not a token. Checked by putting a ``curl`` in front of the real one that
    records what it was given. Fails if either credential is ever an argument."""
    shim = tmp_path / "shim"
    shim.mkdir()
    real = shutil.which("curl")
    assert real is not None
    log = tmp_path / "argv.log"
    (shim / "curl").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{log}"\nexec "{real}" "$@"\n')
    (shim / "curl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    data = tmp_path / "data"
    put_key(data)
    for token in (NO_TOKEN, TOKEN):
        stub = Stub([(200, "a message")])
        try:
            assert run_listen(None, token, SESSION_1, url=stub.url, data=data).returncode == 2
        finally:
            stub.close()
    seen = log.read_text()
    assert (
        "-K" in seen and KEY not in seen and TOKEN not in seen and "sbl_" not in seen and "sbat_" not in seen
    )


def test_a_listener_at_session_start_waits_for_the_helpers_key_file(tmp_path: Path) -> None:
    """Claude Code starts the SessionStart hooks and connects the server about together, so the helper may
    not have made the key yet. A listener at SessionStart waits for the file (up to a minute) and goes on
    with it. Fails if it gives up at once, as a Stop's does."""
    data = tmp_path / "data"
    data.mkdir()
    stub = Stub([(200, "woken before the first turn")])
    try:
        with ThreadPoolExecutor(1) as pool:
            run = pool.submit(
                run_listen,
                None,
                NO_TOKEN,
                SESSION_1,
                stub.url,
                None,
                None,
                data,
                None,
                None,
                "SessionStart",
                "resume",
            )
            assert not run.done()  # no key yet, so nothing has been asked of the server
            assert stub.requests == []
            put_key(data)
            r = run.result(30)
    finally:
        stub.close()
    assert (r.returncode, r.stderr) == (2, "woken before the first turn"), r


def test_a_listener_at_stop_does_not_wait_for_a_key_that_is_not_there(tmp_path: Path) -> None:
    """By the end of a turn the connection has long made its key, or there never will be one (an older
    Claude Code, a machine that can't make it), so a Stop's listener ends at once and says why in
    ``listen.log``, quietly: it asks nothing of the server and wakes nobody. Fails if it waits, asks, or
    wakes."""
    data = tmp_path / "data"
    data.mkdir()
    stub = Stub([(200, "never sent")])
    began = time.monotonic()
    try:
        r = run_listen(None, NO_TOKEN, SESSION_1, url=stub.url, data=data, grace_s=30)
    finally:
        stub.close()
    assert time.monotonic() - began < 10, "a Stop's listener waited for the key's file"
    assert (r.returncode, r.stderr, stub.requests) == (0, "", []), r
    [line] = listen_log(data)
    assert "status=nokey" in line and "no listener key" in line, line


@pytest.mark.parametrize(
    "held", ["sbl_short", "x" * 68, "sbl_" + "G" * 64, "sbl_" + "0" * 65, "sbat_" + "x" * 43, ""]
)
def test_a_key_file_that_doesnt_hold_a_key_is_no_key(tmp_path: Path, held: str) -> None:
    """The file is read as input: only ``sbl_`` and 64 hex are sent, and a file of anything else (a token
    someone put there, a half-written file) is no key at all, so nothing is asked of the server. Fails if
    whatever is in the file goes to the server."""
    data = tmp_path / "data"
    put_key(data, held)
    stub = Stub([(200, "never sent")])
    try:
        r = run_listen(None, NO_TOKEN, SESSION_1, url=stub.url, data=data)
    finally:
        stub.close()
    assert (r.returncode, r.stderr, stub.requests) == (0, "", []), r


def test_a_listener_at_session_start_asks_again_while_the_server_doesnt_know_the_key(tmp_path: Path) -> None:
    """The signed-in connection's first request, which tells the server about the key, can come a moment
    after a resumed session's listener (answered 401 ``unknown_key``): the listener asks again, as a first
    round each time, and goes on to wait and to wake when the server knows the key. Fails if one refusal
    ends it, or a retry passes for a later round."""
    data = tmp_path / "data"
    put_key(data)
    stub = Stub([(401, UNKNOWN), (401, UNKNOWN), (200, "now it is known")])
    try:
        r = run_listen(
            None, NO_TOKEN, SESSION_1, url=stub.url, data=data, event="SessionStart", source="resume"
        )
    finally:
        stub.close()
    assert (r.returncode, r.stderr) == (2, "now it is known"), r
    assert stub.again == ["0", "0", "0"]
    assert listen_log(data) == []


def test_a_listener_gives_up_quietly_on_a_key_the_server_never_learns(tmp_path: Path) -> None:
    """A person who never signed in has a key the server never hears of. At SessionStart the listener asks
    again for the grace it has (here 2 s, a minute in the plugin), then ends: logged, and no wake, because
    nobody asked this session to be woken, and a wake would start a turn in a session that did nothing.
    A Stop's listener doesn't ask twice. Fails if it wakes the model, never ends, or leaves no line."""
    data = tmp_path / "data"
    put_key(data)
    stub = Stub([(401, UNKNOWN)] * 50)
    try:
        r = run_listen(
            None,
            NO_TOKEN,
            SESSION_1,
            url=stub.url,
            data=data,
            event="SessionStart",
            source="startup",
            grace_s=2,
        )
        assert (r.returncode, r.stderr) == (0, ""), r
        assert 2 <= len(stub.requests) < 10
        [line] = listen_log(data)
        assert "status=401" in line and "unknown_key" in line, line
        stub.requests.clear()
        r = run_listen(None, NO_TOKEN, SESSION_1, url=stub.url, data=data)  # a Stop's
        assert (r.returncode, r.stderr, len(stub.requests)) == (0, "", 1), r
    finally:
        stub.close()


def test_a_revoked_sign_in_wakes_the_session_once_and_says_to_sign_in_again(tmp_path: Path) -> None:
    """The server says ``revoked`` when the sign-in the key belongs to was revoked. That is the person's
    doing and the session should hear it, once, as it does for a refused token: the status and the error
    word, never the rest of the body, and what to do (sign in again with /mcp). The key is in neither the log
    nor the wake. Fails if the session goes deaf without a word, is told about it at every Stop, or the key
    is printed."""
    data = tmp_path / "data"
    put_key(data)
    stub = Stub([(401, REVOKED)])
    try:
        r = run_listen(None, NO_TOKEN, SESSION_1, url=stub.url, data=data)
    finally:
        stub.close()
    assert r.returncode == 2, r
    assert "401 (revoked)" in r.stderr and "/mcp" in r.stderr and "Authenticate" in r.stderr, r.stderr
    assert "agent token" not in r.stderr and "was not accepted" not in r.stderr
    assert KEY not in r.stderr and KEY not in (data / "listen.log").read_text()
    stub = Stub([(401, REVOKED)])
    try:
        again = run_listen(None, NO_TOKEN, SESSION_1, url=stub.url, data=data)
    finally:
        stub.close()
    assert (again.returncode, again.stderr) == (0, ""), again  # the same reason: logged, not told again


def test_a_key_for_another_session_is_logged_and_not_told(tmp_path: Path) -> None:
    """A key is for one session, so a round for another (the key's session was ended by /clear, and this
    listener is the old one's) is refused ``wrong_session``. That is ordinary and wakes nobody, but it is
    logged. A 403 from a proxy in front of the server, with no such word, is told like any unexpected status.
    Fails if the model is woken for the first, or the second goes unsaid."""
    data = tmp_path / "data"
    put_key(data)
    stub = Stub([(403, WRONG)])
    try:
        r = run_listen(None, NO_TOKEN, SESSION_1, url=stub.url, data=data)
    finally:
        stub.close()
    assert (r.returncode, r.stderr) == (0, ""), r
    assert "wrong_session" in listen_log(data)[-1]
    stub = Stub([(403, "<html>Sign in to the guest network</html>")])
    try:
        r = run_listen(None, NO_TOKEN, SESSION_1, url=stub.url, data=data)
    finally:
        stub.close()
    assert r.returncode == 2 and "403" in r.stderr and "guest network" not in r.stderr, r


def test_a_token_is_still_told_to_check_the_settings_not_to_sign_in(tmp_path: Path) -> None:
    """The refused-token message is the one it was for a typed token. Fails if the sign-in wording reaches a
    setup that has a token to check."""
    data = tmp_path / "data"
    data.mkdir()
    stub = Stub([(401, '{"error": "unauthorized", "message": "an agent token is needed"}')])
    try:
        r = run_listen(None, TOKEN, SESSION_1, url=stub.url, data=data)
    finally:
        stub.close()
    assert r.returncode == 2 and "Check the plugin settings" in r.stderr and "Authenticate" not in r.stderr, r


def test_the_listener_takes_the_newest_key_each_round(tmp_path: Path) -> None:
    """A reconnect makes a new key in the same file; the listener reads the file at every round, so a
    listener that has waited through one still presents a key the server knows. Fails if it keeps the key it
    started with."""
    data = tmp_path / "data"
    put_key(data, KEY)
    newer = "sbl_" + "fedcba9876543210" * 4
    stub = Stub([(408, ""), (200, "the message")])
    try:
        with ThreadPoolExecutor(1) as pool:
            run = pool.submit(run_listen, None, NO_TOKEN, SESSION_1, stub.url, None, 10, data)
            until(lambda: len(stub.requests) == 1, timeout=30)
            put_key(data, newer)  # the listener sleeps a second after a round that ran out
            r = run.result(30)
    finally:
        stub.close()
    assert r.returncode == 2, r
    assert [req[2]["Authorization"] for req in stub.requests] == [f"Bearer {KEY}", f"Bearer {newer}"]


# --------------------------------------------------------------------------------- the session report
def test_the_session_script_presents_the_key_for_no_typed_token(tmp_path: Path) -> None:
    """Without a token the report of a session's start and end carries the key as the bearer, with the
    event and the session's id and nothing else; with a token typed, it sends that. Fails if the key isn't
    used where there is no token, is used where there is one, or anything but the usual fields are sent."""
    data = tmp_path / "data"
    put_key(data)
    for token, bearer in ((NO_TOKEN, KEY), (TOKEN, TOKEN)):
        stub = Stub([(200, "{}")])
        try:
            r = run_session(
                None, token, SESSION_1, "SessionEnd", url=stub.url, data=data, owner=ME, reason="logout"
            )
        finally:
            stub.close()
        assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), r
        [(method, path, headers, body)] = stub.requests
        assert (method, path) == ("POST", "/mcp/session")
        assert headers["Authorization"] == f"Bearer {bearer}"
        assert json.loads(body) == {
            "event": "SessionEnd",
            "session_id": SESSION_1,
            "source": "",
            "reason": "logout",
            "permission_mode": "",
        }


@pytest.mark.parametrize("held", [None, "sbl_short", "x" * 68, "sbat_" + "x" * 43])
def test_the_session_script_sends_nothing_without_a_key_it_can_use(tmp_path: Path, held: str | None) -> None:
    """No key file, or one that holds anything but a key, and no token: the script exits 0 quietly and asks
    nothing of the server, as it did with no token before. Fails if it sends a request, or complains."""
    data = tmp_path / "data"
    data.mkdir()
    if held is not None:
        put_key(data, held)
    stub = Stub([(200, "{}")])
    try:
        r = run_session(None, NO_TOKEN, SESSION_1, "SessionEnd", url=stub.url, data=data, owner=ME)
    finally:
        stub.close()
    assert (r.returncode, r.stdout, r.stderr, stub.requests) == (0, "", "", []), r


# ------------------------------------------------------------------------------- the whole way
@pytest.fixture
def build(broker: InProcBroker) -> InProcBroker:
    broker.call("room.create", {"name": "#build"})
    return broker


def signed_in(b: InProcBroker, data: Path, session: str = SESSION_1) -> tuple[SignedIn, str]:
    """A Claude Code whose helper just ran and whose connection is signed in: the connection carries the hash
    the helper printed, and its tokens' device is laptop."""
    token = make_token(b, "laptop")
    return SignedIn(b, token, session, header=printed_hash(run_headers(data))), token


def test_a_signed_in_claude_with_no_typed_token_is_woken_while_idle_and_goes_offline_when_it_quits(
    build: InProcBroker, tmp_path: Path
) -> None:
    """The whole way, with the real scripts and a broker: the helper's hash rides a signed-in connection, the
    session joins, and the listener, which has no token, wakes it with an @mention while it is idle; the
    session report then takes it offline. Fails if any link of the chain breaks: the claim, the key's
    session, the listener's door or the session route's."""
    data = tmp_path / "plugin-data"
    claude, _token = signed_in(build, data)
    assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
    claude.call("read", room="#build")
    pid = participant(build, "claude-1").id
    with ThreadPoolExecutor(1) as pool:
        waking = pool.submit(run_listen, build, NO_TOKEN, SESSION_1, data=data)
        until(lambda: parked(build, pid), timeout=30)
        build.call("human.say", {"room": "#build", "text": "@claude-1 wake up"})
        r = waking.result(60)
    assert r.returncode == 2 and "wake up" in r.stderr, r
    r = run_session(build, NO_TOKEN, SESSION_1, "SessionEnd", data=data, owner=ME, reason="logout")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), r
    until(lambda: participant(build, "claude-1").status == "offline")


def test_a_resumed_signed_in_claude_is_woken_before_its_first_turn_though_its_key_is_not_known_yet(
    build: InProcBroker, tmp_path: Path
) -> None:
    """#413's case with a sign-in: the listener of a resumed session starts as the connection does, and its
    first request reaches the server before the connection's first has told it the key (a real 401
    ``unknown_key``). It asks again, and once the signed-in connection has spoken it parks and an @mention
    wakes it, with no turn typed. Fails if the first refusal ends the listener."""
    data = tmp_path / "plugin-data"
    header = printed_hash(run_headers(data))
    token = make_token(build, "laptop")
    gw = Gateway(
        build, lambda: True, cuts=0
    )  # in front of the broker, to see each request the listener makes
    try:
        with ThreadPoolExecutor(1) as pool:
            waking = pool.submit(
                run_listen,
                None,
                NO_TOKEN,
                SESSION_1,
                gw.url,
                None,
                None,
                data,
                None,
                None,
                "SessionStart",
                "resume",
            )
            until(
                lambda: len(gw.again) >= 1, timeout=30
            )  # it asked, and the broker said it doesn't know the key
            claude = SignedIn(build, token, SESSION_1, header=header)  # the connection's first requests
            assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
            claude.call("read", room="#build")
            pid = participant(build, "claude-1").id
            until(lambda: parked(build, pid), timeout=30)
            build.call("human.say", {"room": "#build", "text": "@claude-1 resumed and woken"})
            r = waking.result(60)
    finally:
        gw.close()
    assert r.returncode == 2 and "resumed and woken" in r.stderr, r
    assert len(gw.again) >= 2 and set(gw.again) == {"0"}, gw.again  # never past its first round
    assert listen_log(data) == []


def test_a_key_made_for_one_claude_code_wakes_no_other_session(build: InProcBroker, tmp_path: Path) -> None:
    """The key a Claude Code made is for its session: a listener of that Claude Code for any other session
    (the pid's file is the same) is refused and wakes nobody, and says so in the log. Fails if the key works
    for a session its connection never named."""
    data = tmp_path / "plugin-data"
    claude, _token = signed_in(build, data)
    assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
    r = run_listen(build, NO_TOKEN, SESSION_2, data=data)
    assert (r.returncode, r.stderr) == (0, ""), r
    assert "status=403" in listen_log(data)[-1] and "wrong_session" in listen_log(data)[-1]


def test_a_revoked_sign_in_tells_the_idle_session_through_its_listener(
    build: InProcBroker, tmp_path: Path
) -> None:
    """The person revokes the sign-in in Settings. The idle session's listener, which holds only a key, is
    refused ``revoked`` and wakes the session once to say so. Fails if the session goes deaf quietly."""
    data = tmp_path / "plugin-data"
    token = build.on_loop(agent_tokens.make, build.state, None, "laptop", "alice")
    claude = SignedIn(build, token["token"], SESSION_1, header=printed_hash(run_headers(data)))
    assert claude.call("join", room="#build", screen_name="claude-1")["ok"]
    build.on_loop(agent_tokens.revoke, build.state, None, token["id"], "alice")
    r = run_listen(build, NO_TOKEN, SESSION_1, data=data)
    assert r.returncode == 2 and "401 (revoked)" in r.stderr and "Authenticate" in r.stderr, r
