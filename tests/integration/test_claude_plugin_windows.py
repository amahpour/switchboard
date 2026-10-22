"""Claude Code's plugin on native Windows (#440, DESIGN.md §49.5).

One ``hooks.json`` serves Linux, macOS and Windows. Claude Code runs a hook command in ``sh`` on
Linux and macOS, in Git Bash on Windows when Git for Windows is installed, and in PowerShell when it
isn't, so each command is written to mean the same in all three: ``cd`` to the plugin's ``bin``
folder, run ``./listen`` (or ``./session``), and exit with its code. There ``sh`` and Git Bash find the
shell script, which on Windows hands over to ``windows/listen.ps1``; PowerShell finds ``listen.cmd``,
which starts that script with the execution policy lifted for the one run.

These tests run what can run anywhere: the command strings in ``sh`` with a stand-in for each script,
the shell scripts' hand-over with stand-ins for ``uname`` and ``powershell.exe``, the PowerShell
scripts under ``pwsh`` where it is installed (``test_agent_plugins.py`` runs the same scenarios against
both implementations), and static checks on the files Windows reads. What only Windows can show
(Windows PowerShell 5.1, Git Bash, ``cmd.exe``, a real ``claude.exe``) was run natively for #440 and is
in its pull request."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_agent_plugins import (  # noqa: F401  (impl is a fixture)
    CLAUDE_PLUGIN,
    LISTEN_COMMAND,
    ROOT,
    SESSION_1,
    SESSION_COMMAND,
    Stub,
    claude_hooks,
    impl,
    run_listen,
    run_session,
)

BIN = CLAUDE_PLUGIN / "bin"
POWERSHELL_LINE = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File"
TOKEN = "sbat_" + "x" * 43


def command_in(root: Path, command: str) -> list[str]:
    """What Claude Code hands the shell for a hook command: the plugin's root filled in (on Windows with
    forward slashes), the shell started the way it does, so the command is parsed as the shell parses it."""
    return ["sh", "-c", command.replace("${CLAUDE_PLUGIN_ROOT}", root.as_posix())]


def test_every_command_hook_is_one_of_the_two_commands_all_three_shells_understand() -> None:
    """A hook command runs in sh, Git Bash or PowerShell depending on the machine, and a command that one
    of them can't parse fails every session there (PowerShell can't start a quoted path, sh can't parse
    its call operator). So the plugin has two command strings, both of which mean the same in all three:
    ``cd`` into ``bin``, run ``./listen`` or ``./session``, exit with its code. Fails if a command hook
    is added that is neither, or either string is changed."""
    assert SESSION_COMMAND == 'cd "${CLAUDE_PLUGIN_ROOT}/bin"; ./session; exit $LASTEXITCODE'
    assert LISTEN_COMMAND == 'cd "${CLAUDE_PLUGIN_ROOT}/bin"; ./listen; exit $LASTEXITCODE'
    found = [
        (event, h["command"])
        for event, groups in claude_hooks().items()
        for g in groups
        for h in g["hooks"]
        if h["type"] == "command"
    ]
    assert sorted(found) == sorted(
        [
            ("SessionStart", SESSION_COMMAND),
            ("SessionStart", LISTEN_COMMAND),
            ("Stop", LISTEN_COMMAND),
            ("SessionEnd", SESSION_COMMAND),
        ]
    )


@pytest.mark.parametrize("name,command", [("listen", LISTEN_COMMAND), ("session", SESSION_COMMAND)])
@pytest.mark.parametrize("code", [0, 1, 2])
def test_the_command_hands_the_script_its_input_and_returns_its_exit_code(
    tmp_path: Path, name: str, command: str, code: int
) -> None:
    """Claude Code wakes an idle session only on exit code 2 of the listener (``asyncRewake``), and reads
    the hook's input from its stdin. PowerShell reports the exit code of ``-Command`` as 1 whenever the
    last native command failed, which is why each command ends with ``exit $LASTEXITCODE`` (in sh that
    is ``exit`` with the last status). Run in sh from another folder, with a plugin root that has
    spaces and an apostrophe in its name: the script gets the input and Claude Code's environment, runs
    in ``bin``, and its code (0, 1 and 2) is the hook's. Fails if the command loses the code, the input
    or the plugin's folder."""
    root = tmp_path / "Jo's plugin dir"
    (root / "bin").mkdir(parents=True)
    script = root / "bin" / name
    script.write_text('#!/bin/sh\npwd > "$STUB_OUT.cwd"\ncat > "$STUB_OUT.stdin"\nexit "$STUB_CODE"\n')
    script.chmod(0o755)
    elsewhere = tmp_path / "project"
    elsewhere.mkdir()
    env = {"PATH": os.environ["PATH"], "STUB_OUT": str(tmp_path / "out"), "STUB_CODE": str(code)}
    r = subprocess.run(
        command_in(root, command),
        input='{"session_id": "x"}',
        cwd=elsewhere,
        env=env,
        capture_output=True,
        text=True,
    )
    assert (r.returncode, r.stdout, r.stderr) == (code, "", ""), r
    assert (tmp_path / "out.stdin").read_text() == '{"session_id": "x"}'
    assert Path((tmp_path / "out.cwd").read_text().strip()).resolve() == (root / "bin").resolve()


def stub_windows_tools(tmp_path: Path, uname: str) -> dict[str, str]:
    """Stand-ins on PATH for what a Windows machine adds: ``uname`` saying which system this is, and
    ``powershell.exe`` recording its arguments and stdin and exiting 2. The environment for a script."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "uname").write_text(f"#!/bin/sh\necho {uname}\n")
    (stubs / "powershell.exe").write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do printf \'%s\\n\' "$a"; done > "$STUB_OUT.argv"\n'
        'cat > "$STUB_OUT.stdin"\n'
        "exit 2\n"
    )
    for f in stubs.iterdir():
        f.chmod(0o755)
    return {
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "STUB_OUT": str(tmp_path / "out"),
        "CLAUDE_PLUGIN_OPTION_TOKEN": TOKEN,
    }


def plugin_copy(tmp_path: Path) -> Path:
    """The plugin's ``bin`` folder as a plugin cache would hold it, in a folder with a space in its name."""
    root = tmp_path / "plugin dir"
    shutil.copytree(BIN, root / "bin")
    return root


@pytest.mark.parametrize("name", ["listen", "session"])
def test_git_bashs_script_hands_over_to_powershell_on_windows_and_only_there(
    tmp_path: Path, name: str
) -> None:
    """Git for Windows' sh can't do the listener's job: its ``kill -0`` doesn't see a Windows pid, so the
    shell script ends at once and the session is never woken (seen on a native Windows Claude Code), and
    the session script is no faster there. So on Git Bash, MSYS or Cygwin (``uname -s``) the script runs
    ``windows/<name>.ps1`` in Windows PowerShell with the execution policy lifted for that run, passing
    on its stdin and its exit code, and no argument is anything but the script's path. Elsewhere it
    doesn't touch PowerShell, and where the PowerShell script isn't beside it (hooks set up by hand) it
    runs as the shell script it is. Fails if Linux or macOS starts PowerShell, Windows doesn't, or the
    token reaches a command line."""
    root = plugin_copy(tmp_path)
    payload = json.dumps({"session_id": SESSION_1, "hook_event_name": "Stop"})
    command = command_in(root, LISTEN_COMMAND if name == "listen" else SESSION_COMMAND)

    env = stub_windows_tools(tmp_path, "MINGW64_NT-10.0-26100")
    r = subprocess.run(command, input=payload, env=env, capture_output=True, text=True)
    assert (r.returncode, r.stdout, r.stderr) == (2, "", ""), r  # PowerShell's code is the hook's
    argv = (tmp_path / "out.argv").read_text().splitlines()
    assert argv == [
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        f"./windows/{name}.ps1",
    ]
    assert (tmp_path / "out.stdin").read_text() == payload  # the hook's input is still unread
    assert TOKEN not in " ".join(argv)

    for kind in ("Linux", "Darwin"):
        again = tmp_path / kind
        again.mkdir()
        env = stub_windows_tools(again, kind)
        env.pop("CLAUDE_PLUGIN_OPTION_TOKEN")  # no settings: the shell script ends quietly
        r = subprocess.run(command, input=payload, env=env, capture_output=True, text=True)
        assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), (kind, r)
        assert not (again / "out.argv").exists(), kind

    by_hand = tmp_path / "by hand"
    by_hand.mkdir()
    (root / "bin" / "windows" / f"{name}.ps1").unlink()
    env = stub_windows_tools(by_hand, "MSYS_NT-10.0")
    env.pop("CLAUDE_PLUGIN_OPTION_TOKEN")
    r = subprocess.run(command, input=payload, env=env, capture_output=True, text=True)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), r
    assert not (by_hand / "out.argv").exists()


def test_powershell_finds_the_cmd_file_for_the_command_and_nothing_else() -> None:
    """In PowerShell (the hook shell on a Windows without Git), ``./listen`` is resolved like a typed
    command: a ``.ps1`` beside it wins over ``.cmd``, and PowerShell's default policy on Windows refuses
    every ``.ps1``; a ``.bat``, ``.com`` or ``.exe`` would win over the ``.cmd`` too. So ``bin`` holds the
    shell scripts and the three ``.cmd`` files, and the PowerShell scripts are in ``bin/windows``. Fails
    if a file that would be found first lands in ``bin``."""
    assert sorted(p.name for p in BIN.iterdir()) == [
        "headers",
        "headers.cmd",
        "listen",
        "listen.cmd",
        "session",
        "session.cmd",
        "windows",
    ]
    assert sorted(p.name for p in (BIN / "windows").iterdir()) == ["listen.ps1", "session.ps1"]


@pytest.mark.parametrize("name", ["listen", "session"])
def test_the_cmd_file_starts_the_powershell_script_and_returns_its_code(name: str) -> None:
    """``listen.cmd`` and ``session.cmd`` are what PowerShell runs for ``./listen`` and ``./session``. Each
    starts the script of the same name in Windows PowerShell 5.1 (the one every Windows has), with the
    profile skipped and the execution policy lifted for that run only (Claude Code's own documentation
    does the same for its PowerShell hooks), and ends with the script's exit code, which is how an exit 2
    wakes the session. The arguments are the script's path, from the file's own folder. CRLF line endings,
    which ``cmd.exe`` wants. Fails if the code is lost, a flag is dropped, or the path depends on the
    folder Claude Code runs in."""
    raw = (BIN / f"{name}.cmd").read_bytes()
    assert raw.isascii() and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), "ASCII, CRLF only"
    lines = raw.decode().splitlines()
    assert lines[0] == "@echo off"
    assert [ln for ln in lines if not ln.lower().startswith("rem ")][1:] == [
        f'{POWERSHELL_LINE} "%~dp0windows\\{name}.ps1"',
        "exit /b %ERRORLEVEL%",
    ]


def code_lines(path: Path) -> list[str]:
    """A script's lines without its comments."""
    out = []
    for line in path.read_text().splitlines():
        line = re.sub(r"(^|\s)#.*$", "", line).rstrip()
        if line:
            out.append(line)
    return out


@pytest.mark.parametrize("name", ["listen", "session"])
def test_the_powershell_scripts_run_on_windows_powershell_5_1(name: str) -> None:
    """Windows PowerShell 5.1 ships with Windows, so the scripts must run there: no syntax that only
    PowerShell 7 has (``&&``, ``||``, ``??``, ``?.``), and only ASCII, because 5.1 reads a file without a
    byte-order mark in the machine's ANSI code page and would garble the rest. (They ran under 5.1
    itself for #440; this keeps a later edit from breaking that unnoticed.) Fails on a PowerShell 7-only
    operator or a character outside ASCII."""
    path = BIN / "windows" / f"{name}.ps1"
    text = path.read_text()
    assert text.isascii(), [ch for ch in text if not ch.isascii()]
    for seven_only in ("&&", "||", "??", "?.", "?["):
        assert seven_only not in "\n".join(code_lines(path)), seven_only
    assert "Set-StrictMode" not in text and "$ErrorActionPreference = 'Stop'" in text
    # whatever goes wrong, never an exit code that would read as a wake
    assert re.search(r"^trap \{.*exit 0 \}$", text, re.M)


@pytest.mark.parametrize("name", ["listen", "session"])
def test_the_powershell_scripts_keep_the_token_off_every_command_line(name: str) -> None:
    """As the shell scripts keep it on curl's stdin: the token goes to the server in a request header
    built inside the script, and is never an argument of any process, where others on the machine could
    read it. So the script starts no process and runs no text as code, uses no other HTTP tool, and
    mentions the token only to read it, check it is there and put it in the header. A redirect is not
    followed (curl doesn't follow one either), which would send it wherever the server points. Fails if
    the script can start a process, or the token appears anywhere else."""
    lines = code_lines(BIN / "windows" / f"{name}.ps1")
    code = "\n".join(lines)
    for forbidden in (
        "Start-Process",
        "ArgumentList",
        "Invoke-Expression",
        "iex ",
        "Invoke-Command",
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "curl",
        "cmd /c",
        "[Diagnostics.Process]::Start",
    ):
        assert forbidden not in code, forbidden
    assert not any(ln.lstrip().startswith("&") for ln in lines), "no call operator"
    mentions = [ln.strip() for ln in lines if "$token" in ln]
    for ln in mentions:
        assert (
            ln.startswith("$token = First $env:CLAUDE_PLUGIN_OPTION_TOKEN $env:SWITCHBOARD_AGENT_TOKEN")
            or ln == "if (-not $url -or -not $token) { exit 0 }"
            or '"Bearer $token"' in ln
        ), ln
    assert any('"Bearer $token"' in ln for ln in mentions)
    assert "$handler.AllowAutoRedirect = $false" in code


def test_the_scripts_are_checked_out_with_the_line_endings_they_need() -> None:
    """Git for Windows checks files out with CRLF endings unless told otherwise (``core.autocrlf=true``
    is its installer's default), and a shell script with CRLF endings fails in Git Bash. A plugin is
    installed by a ``git`` checkout on the user's machine, so ``.gitattributes`` pins the plugins'
    ``bin`` scripts to LF and the ``.cmd`` files to CRLF. Fails if either loses its rule."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    files = [
        "plugins/claude/bin/listen",
        "plugins/claude/bin/session",
        "plugins/claude/bin/windows/listen.ps1",
        "plugins/cursor/bin/hook",
        "plugins/devin/bin/hook",
        "plugins/claude/bin/listen.cmd",
        "plugins/claude/bin/session.cmd",
    ]
    out = subprocess.run(
        ["git", "-C", str(ROOT), "check-attr", "eol", "--", *files],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    eol = {ln.split(":")[0]: ln.rsplit(" ", 1)[1] for ln in out.splitlines()}
    assert {f: eol[f] for f in files} == {**{f: "lf" for f in files[:5]}, **{f: "crlf" for f in files[5:]}}


# ------------------------------------------------ the PowerShell script and the sh script, side by side
@pytest.mark.usefixtures("impl")
def test_a_wake_message_arrives_whole_whatever_it_says() -> None:
    """The message that wakes a session is other people's text, and has anything in it: accents, dashes,
    emoji. The listener passes the server's bytes to Claude Code as they came (UTF-8), not through the
    machine's code page, which on Windows is not UTF-8 and would turn them to gibberish. Fails if the
    text changes on its way to stderr."""
    text = "[switchboard] café — ☃ \U0001f680 über"
    stub = Stub([(200, text)])
    try:
        r = run_listen(None, TOKEN, SESSION_1, url=stub.url)
    finally:
        stub.close()
    assert (r.returncode, r.stderr) == (2, text), r


@pytest.mark.usefixtures("impl")
def test_a_redirect_is_not_followed_so_the_token_stays_with_the_server_named(tmp_path: Path) -> None:
    """A server (or a proxy in front of it) that answers with a redirect would, if the listener followed it,
    have the token sent to wherever it points. curl doesn't follow one unless told to, and the PowerShell
    script is told not to. The listener treats a redirect as any answer nobody expects: it stops, and wakes
    the session once to say so. Fails if the second server is ever asked."""
    elsewhere = Stub([(200, "never sent")])
    first = Stub([(302, "", {"Location": elsewhere.url + "/mcp/listen"})])
    try:
        r = run_listen(None, TOKEN, SESSION_1, url=first.url, data=tmp_path)
    finally:
        first.close()
        elsewhere.close()
    assert elsewhere.requests == [], elsewhere.requests
    assert r.returncode == 2 and "302" in r.stderr and TOKEN not in r.stderr, r


@pytest.mark.usefixtures("impl")
def test_the_session_script_sends_the_token_in_a_header_and_prints_the_servers_context() -> None:
    """``bin/session`` posts a small JSON object (the event, the session, the plain-word fields), the
    token only in the Authorization header, and prints the server's answer for Claude Code when there is
    one at SessionStart (``{}`` is nothing); it prints nothing at SessionEnd. Fails if the token travels
    anywhere else, a field that isn't a plain word is sent on, or the context is changed on the way."""
    context = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "caf\u00e9 \u2014 back",
            }
        },
        ensure_ascii=False,
    )
    stub = Stub([(200, context), (200, "{}"), (200, "{}")])
    try:
        start = run_session(
            None,
            TOKEN,
            SESSION_1,
            "SessionStart",
            url=stub.url,
            source="resume",
            permission_mode="de fault; rm",
        )
        quiet = run_session(None, TOKEN, SESSION_1, "SessionStart", url=stub.url, source="clear")
        end = run_session(None, TOKEN, SESSION_1, "SessionEnd", url=stub.url, reason="prompt_input_exit")
    finally:
        stub.close()
    assert (start.returncode, start.stdout.strip(), start.stderr) == (0, context, "")
    assert (quiet.returncode, quiet.stdout, quiet.stderr) == (0, "", "")
    assert (end.returncode, end.stdout, end.stderr) == (0, "", "")
    [one, two, three] = stub.requests
    assert (one[0], one[1]) == ("POST", "/mcp/session")
    assert one[2].get("Authorization") == f"Bearer {TOKEN}"
    assert json.loads(one[3]) == {
        "event": "SessionStart",
        "session_id": SESSION_1,
        "source": "resume",
        "reason": "",
        "permission_mode": "",  # not a plain word: left out, not escaped
    }
    assert (
        json.loads(three[3])["event"] == "SessionEnd"
        and json.loads(three[3])["reason"] == "prompt_input_exit"
    )
    assert all(TOKEN not in req[3].decode() and TOKEN not in req[1] for req in (one, two, three))
