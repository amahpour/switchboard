"""The plugin-version claim (#789, DESIGN.md §49.13): what parses as a version, and how a parsed one is
compared with the version this release pins. Pure functions: no broker."""

from __future__ import annotations

import pytest

from switchboard import plugin_versions as pv


@pytest.mark.parametrize("raw", ["0.0.0", "0.16.22", " 1.2.3 ", "999999.999999.999999"])
def test_exactly_three_numbers_parse(raw: str) -> None:
    """A version is X.Y.Z of plain digits (whitespace at the ends is a proxy's, not the plugin's).
    Fails if a real plugin version is refused."""
    assert pv.parse(raw) == raw.strip()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        7,
        b"1.2.3",
        "",
        " ",
        "1.2",
        "1.2.3.4",
        "v1.2.3",
        "1.2.3-rc1",
        "1.2.3+build",
        "1.2.x",
        "-1.2.3",
        "01.2.3",
        "1.02.3",
        "1,2,3",
        "1.2.3\n4.5.6",
        "1000000.0.0",
        "١.٢.٣",  # Arabic-Indic digits: \d would take them
        "9" * 100 + ".0.0",
    ],
)
def test_anything_else_is_no_version(raw: object) -> None:
    """The header is a claim from outside: a string that isn't exactly that, or isn't a string, is None,
    including a number too large to compare sanely and non-ASCII digits. Fails if any gets through."""
    assert pv.parse(raw) is None


def test_a_value_longer_than_the_cap_is_no_version() -> None:
    """A value longer than the cap is refused whole, even if its start is a version: 1.2.3 followed by
    padding and junk is not 1.2.3. Fails if the tail is cut off and the head accepted."""
    assert pv.parse("1.2.3" + " " * 200 + "x") is None
    assert pv.parse("1.2.3" + " " * (pv.HEADER_MAX - 5)) == "1.2.3"
    assert pv.parse("1.2.3" + " " * (pv.HEADER_MAX - 4)) is None


def test_the_comparison_is_numeric_and_never_current_without_a_version() -> None:
    """0.16.9 is behind 0.16.10 (a string compare says otherwise), a newer or equal plugin is current, and
    nothing parsed is unknown, never current. Fails if the order is textual, or silence reads as current."""
    pinned = pv.PINNED["claude"]
    major, minor, patch = (int(x) for x in pinned.split("."))
    assert pv.check("claude", f"{major}.{minor}.{patch}").state == "current"  # type: ignore[union-attr]
    assert pv.check("claude", f"{major}.{minor}.{patch + 1}").state == "current"  # type: ignore[union-attr]
    assert pv.check("claude", f"{major + 1}.0.0").state == "current"  # type: ignore[union-attr]
    behind = pv.check("claude", f"{major}.{minor - 1}.{patch + 99}" if minor else f"0.0.{patch}")
    assert behind is not None and behind.state == "older" and behind.update == pv.UPDATE["claude"]
    assert pv.check("claude", None).state == "unknown"  # type: ignore[union-attr]
    assert pv.check("claude", None).note.endswith(pv.UPDATE["claude"])  # type: ignore[union-attr]


def test_a_numeric_not_textual_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pv, "PINNED", {"claude": "0.16.10", "codex": "0.8.4"})
    assert pv.check("claude", "0.16.9").state == "older"  # type: ignore[union-attr]
    assert pv.check("claude", "0.16.10").state == "current"  # type: ignore[union-attr]
    assert pv.check("claude", "0.9.99").state == "older"  # type: ignore[union-attr]


def test_a_harness_without_a_versioned_plugin_has_no_check() -> None:
    """Only Claude Code's and Codex's plugins report a version; the others have none to compare."""
    for harness in ("cursor", "devin", "opencode", "test", "unknown"):
        assert pv.check(harness, "1.0.0") is None and pv.check(harness, None) is None


def test_every_versioned_harness_has_its_own_update_command() -> None:
    """The note names the command that updates that harness's plugin, not another's."""
    assert set(pv.PINNED) == set(pv.UPDATE) == {"claude", "codex"}
    assert "claude plugin update" in pv.UPDATE["claude"] and "codex plugin" in pv.UPDATE["codex"]
    assert "codex" not in pv.UPDATE["claude"].lower() and "claude" not in pv.UPDATE["codex"].lower()


def test_a_codex_session_that_sent_nothing_is_not_nagged_but_a_claude_one_is() -> None:
    """Claude Code's helper always prints the version, so silence there means a plugin from before the
    header; Codex's helper is the person's opt-in, so silence there is the usual case and is not worth a
    note. Both are still unknown, never current. Fails if a token-only Codex is told to update, or a
    Claude Code that never said gets no note."""
    codex, claude = pv.check("codex", None), pv.check("claude", None)
    assert codex is not None and claude is not None
    assert (codex.state, codex.nags, codex.note) == ("unknown", False, "")
    assert (claude.state, claude.nags) == ("unknown", True) and claude.note
    old_codex = pv.check("codex", "0.0.1")
    assert old_codex is not None and old_codex.nags and pv.UPDATE["codex"] in old_codex.note
