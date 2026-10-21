"""The verbs retired in 0.38.0 (#385, DESIGN.md §49.8): ``switchboard remote``, ``satellite`` and ``join``
say so in one line and exit 2, with any arguments they used to take, and the rest of the CLI still
refuses an argument it doesn't know."""

from __future__ import annotations

import pytest

from switchboard import cli


@pytest.mark.parametrize(
    "argv",
    [
        ["remote"],
        ["remote", "add", "pi", "--host", "example.com"],
        ["remote", "accept", "--yes", "--authorized-keys", "/tmp/x"],
        ["satellite", "--name", "pi", "--test-mode"],
        ["join", "ABCD-EFGH", "--url", "https://example.com"],
    ],
)
def test_a_retired_verb_says_so_in_one_line_and_exits_2(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """A script, an ``authorized_keys`` line or an old harness entry that still calls a verb of the
    links gets one line (what was retired, where agents connect now) and exit 2, whatever arguments it
    passes. Fails if argparse's "invalid choice" or "unrecognized arguments" comes back, or if the
    verb does anything."""
    assert cli.main(argv) == 2
    out, err = capsys.readouterr()
    assert out == ""
    lines = err.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith(f"switchboard {argv[0]}: ")
    assert "retired in 0.38.0 (#385)" in lines[0] and "Settings → Agents" in lines[0]


def test_other_verbs_still_refuse_an_argument_they_do_not_know(capsys: pytest.CaptureFixture[str]) -> None:
    """Retired verbs take any argument, but only they: ``status --bogus`` is still a usage error.
    Fails if the leniency of ``parse_known_args`` leaks to every verb."""
    with pytest.raises(SystemExit) as e:
        cli.main(["status", "--bogus"])
    assert e.value.code == 2
    assert "unrecognized arguments: --bogus" in capsys.readouterr().err
