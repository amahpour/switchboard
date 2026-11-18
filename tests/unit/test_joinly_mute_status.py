"""The joinly patch reads the call page's own mute button and does not click it (#853).

joinly is not installed here, so the pure helper is read out of the file the container runs and exercised
alone, the same way the share helper is. The names are the ones joinly's own mute and unmute already look
for: Zoom's "unmute my microphone", Meet's "turn on microphone", Teams' "Unmute". A host's "Mute all" is a
different control and must not be read as the bot's own button.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from switchboard.broker.commands import HELP_TEXT, CommandError, parse_command

SERVER = Path(__file__).resolve().parents[2] / "deploy" / "joinly" / "server.py"


@pytest.fixture(scope="module")
def muted_from_labels() -> Any:
    """``muted_from_labels``, compiled alone from the patch. Fails if the helper is missing from the file
    the bot runs."""
    tree = ast.parse(SERVER.read_text())
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "muted_from_labels"]
    assert keep, "muted_from_labels is not in deploy/joinly/server.py"
    scope: dict[str, Any] = {}
    exec(compile(ast.Module(keep, []), str(SERVER), "exec"), scope)  # noqa: S102
    return scope["muted_from_labels"]


@pytest.mark.parametrize(
    "labels,muted",
    [
        (["unmute my microphone"], True),  # Zoom, after Mute all
        (["Unmute my microphone"], True),
        (["mute my microphone"], False),
        (["Turn on microphone"], True),  # Meet
        (["turn off mic"], False),
        (["Unmute"], True),  # Teams
        (["Mute"], False),
        (["Mute all participants", "unmute my microphone"], True),  # the host's control is not the bot's
        (["Unmute", "mute my microphone"], False),  # someone else, then the bot's own open mic
        (["Unmute", "turn off microphone"], False),  # Meet: the bot's own button wins
        (["Unmute", "Mute"], None),  # Teams: both states, so the bot's own button is unknown
        (["Mute all"], None),
        (["Participants", "Chat"], None),  # the toolbar, and no mute button read
        ([], None),
        ("unmute my microphone", None),  # not a list: unknown, never a claim
        ([None, 3, "unmute my microphone"], True),
    ],
)
def test_the_bots_own_button_says_whether_it_is_muted(
    muted_from_labels: Any, labels: Any, muted: bool | None
) -> None:
    """True only for the button that would unmute the bot. Fails if Mute all is taken for the bot's
    own button, or an unknown page is reported as muted."""
    assert muted_from_labels(labels) is muted


def test_the_page_read_keeps_the_bots_own_button() -> None:
    """Unrelated buttons must not crowd out the bot's own mute button. Fails if the page read stops
    after the first buttons, or drops the names joinly clicks."""
    source = SERVER.read_text()
    start = source.index('_MUTE_LABELS_JS = """')
    script = source[start : source.index('"""', start + len('_MUTE_LABELS_JS = """'))]
    assert "unmute my microphone" in script and "turn on mic" in script
    assert "own.length" in script


def test_the_tool_reads_names_and_does_not_click() -> None:
    """The page is asked for button names. A click would change the call. Fails if the tool clicks, or if it
    is missing from the file the bot runs."""
    source = SERVER.read_text()
    assert "mute_status" in source
    tree = ast.parse(source)
    tools = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert [a.arg for a in tools["mute_status"].args.args] == ["ctx"]
    body = ast.get_source_segment(source, tools["mute_status"]) or ""
    assert ".click(" not in body and "unmute(" not in body


def test_help_lists_unmute_and_an_extra_word_is_refused() -> None:
    """``/meeting unmute`` is a person's command, and a second word is not a guess. Fails if help omits it,
    or the usage no longer names it."""
    assert "/meeting unmute" in HELP_TEXT
    with pytest.raises(CommandError) as e:
        parse_command("/meeting unmute later")
    assert e.value.code == "bad_request" and "unmute" in e.value.message
