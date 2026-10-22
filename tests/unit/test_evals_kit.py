"""The evals' parts that need no broker and no model (DESIGN.md §12.7): scenarios as data, the results file,
``compare``, the token meter and the opt-in. The checks and the runner, against scripted agents, are
``tests/integration/test_evals_graders.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

EVALS = Path(__file__).resolve().parents[1] / "evals"
REPO = EVALS.parents[1]
sys.path.insert(0, str(EVALS))

import run_evals  # noqa: E402
from evalkit import claude_world, compare, results, scenario  # noqa: E402
from evalkit.record import Tokens  # noqa: E402

# ------------------------------------------------------------------ scenarios


def test_the_shipped_scenarios_load_and_name_real_checks() -> None:
    """The first slice's three scenarios (#480) read cleanly: three agents, a room, one message from the
    person, and only checks that exist. Fails if a file has a typo in a grader or an agent name."""
    every = scenario.load_all()
    assert sorted(every) == ["bystander", "nobody-named", "one-named"]
    for sc in every.values():
        assert sc.agents == ("alpha", "bravo", "charlie") and sc.room == "#build" and len(sc.steps) == 1
        assert sc.checks and all(c.step == 0 for c in sc.checks)


GOOD: dict[str, Any] = {
    "name": "t",
    "room": "#build",
    "agents": ["alpha", "bravo"],
    "steps": [{"say": "hi"}],
    "checks": [{"id": "c", "grader": "poster_count", "n": 1}],
}


@pytest.mark.parametrize(
    ("change", "complaint"),
    [
        ({"checks": [{"id": "c", "grader": "no_such_grader"}]}, "no grader"),
        ({"checks": [{"id": "c", "grader": "poster_count"}]}, "missing"),
        ({"checks": [{"id": "c", "grader": "poster_count", "n": 1, "extra": 1}]}, "unexpected"),
        ({"checks": [{"id": "c", "grader": "poster_count", "n": 9}]}, "n must be"),
        ({"checks": [{"id": "c", "grader": "agents_post", "agents": ["zed"]}]}, "not one of the scenario's"),
        ({"checks": [{"id": "c", "grader": "agents_post", "agents": "alpha"}]}, "list of screen names"),
        ({"checks": [{"id": "c", "grader": "a_post_matches", "pattern": "("}]}, "regular expression"),
        ({"checks": [{"id": "c", "grader": "poster_count", "n": 1, "step": 3}]}, "step must be"),
        (
            {
                "checks": [
                    {"id": "c", "grader": "poster_count", "n": 1},
                    {"id": "c", "grader": "poster_count", "n": 2},
                ]
            },
            "used twice",
        ),
        ({"steps": []}, "at least one"),
        ({"steps": [{"say": "hi", "wait": 1}]}, "just 'say'"),
        ({"agents": ["alpha", "alpha"]}, "different screen names"),
        ({"room": "build"}, "room must be"),
        ({"surprise": 1}, "unknown keys"),
    ],
)
def test_a_scenario_with_a_mistake_is_refused_when_it_is_read(change: dict[str, Any], complaint: str) -> None:
    """A typo in a grader, an argument, an agent or a step fails when the file is read, not after a run."""
    scenario.parse(GOOD)  # the base is fine
    with pytest.raises(scenario.ScenarioError, match=complaint):
        scenario.parse({**GOOD, **change})


def test_a_scenario_file_is_named_after_its_scenario(tmp_path: Path) -> None:
    (tmp_path / "other.toml").write_text(
        'name = "t"\nroom = "#build"\nagents = ["a1"]\n'
        '[[steps]]\nsay = "hi"\n[[checks]]\nid = "c"\ngrader = "poster_count"\nn = 1\n'
    )
    with pytest.raises(scenario.ScenarioError, match="must be named t.toml"):
        scenario.load(tmp_path / "other.toml")


# ---------------------------------------------------------------- the results


def trial(
    n: int, passed: dict[str, bool], tokens: int = 1000, wakes: int = 3, cost: float = 0.01
) -> dict[str, Any]:
    return {
        "n": n,
        "status": "graded",
        "checks": {cid: {"ok": ok, "detail": ""} for cid, ok in passed.items()},
        "metrics": {
            "tokens": {"total": tokens * 2, "cost_usd": cost * 2},
            "tokens_after_ask": {"total": tokens, "cost_usd": cost},
            "wakes": wakes,
            "wakes_ended_in_pass": 1,
            "posts": 1,
            "seconds": 40.0,
        },
    }


def errored(n: int, kind: str = "timeout") -> dict[str, Any]:
    return {
        "n": n,
        "status": "harness_error",
        "error": {"kind": kind, "message": "x"},
        "metrics": {"tokens": {"total": 5, "cost_usd": 0.02}},
    }


def test_summarize_counts_harness_errors_apart_from_failed_checks() -> None:
    """Passed over graded trials only: two passes, one fail and one timeout is 2/3 with one harness error, not
    2/4. Fails if an error is graded as a failed check, or its cost is left out of what was spent."""
    s = results.summarize([trial(1, {"c": True}), trial(2, {"c": True}), trial(3, {"c": False}), errored(4)])
    assert s["trials"] == 4 and s["graded"] == 3 and s["harness_errors"] == 1
    assert s["harness_error_kinds"] == {"timeout": 1}
    assert s["checks"] == {"c": {"passed": 2, "graded": 3}}
    assert s["means"]["tokens_after_ask"] == 1000 and s["means"]["wakes"] == 3
    assert s["cost_usd_all_trials"] == pytest.approx(0.02 * 3 + 0.02)


def test_a_results_file_round_trips_and_a_bad_one_is_refused(tmp_path: Path) -> None:
    doc = results.new_doc("baseline", {"k": 2})
    results.add_trial(doc, "nobody-named", trial(1, {"c": True}))
    results.add_trial(doc, "nobody-named", errored(2, "crash"))
    path = tmp_path / "out" / "r.json"
    results.write(path, doc)
    assert not list(path.parent.glob("*.tmp")), "the temporary file was left behind"
    back = results.read(path)
    assert back["arm"] == "baseline" and back["scenarios"]["nobody-named"]["summary"]["harness_errors"] == 1
    (tmp_path / "bad.json").write_text(json.dumps({"format": 99, "scenarios": {}}))
    with pytest.raises(ValueError, match="not a results file"):
        results.read(tmp_path / "bad.json")
    with pytest.raises(ValueError, match="not a results file"):
        results.read(tmp_path / "missing.json")


# ----------------------------------------------------------------- compare


def doc_of(arm: str, passes: int, k: int = 5, tokens: int = 1000, errors: int = 0) -> dict[str, Any]:
    d = results.new_doc(arm, {})
    for i in range(k):
        results.add_trial(d, "nobody-named", trial(i, {"exactly_one_answers": i < passes}, tokens=tokens))
    for i in range(errors):
        results.add_trial(d, "nobody-named", errored(100 + i))
    return d


def row(text: str, label: str) -> str:
    return next(ln for ln in text.splitlines() if ln.strip().startswith(label))


def test_compare_shows_each_check_as_before_arrow_after() -> None:
    """``4/5 -> 5/5``, per check, and the harness errors beside them (not as checks). Fails if the counts
    include the errored trials."""
    text = compare.compare(doc_of("base", 4, errors=1), doc_of("new", 5))
    line = row(text, "exactly_one_answers")
    assert "4/5 ->" in line.replace("  ", " ") and "5/5" in line
    assert "1/6" in row(text, "harness errors")


def test_a_change_is_real_only_if_larger_than_the_spread_between_two_baseline_runs() -> None:
    """A baseline that scored 2/5 and then 3/5 has a spread of 20 points: a move to 5/5 (+60) is real, a move
    to 3/5 (+20) is not, no move is 'same', and with no second baseline run nothing is called real. Fails if a
    change smaller than the noise is called real."""
    base, repeat = doc_of("base", 2), doc_of("base-2", 3)
    assert "REAL" in row(compare.compare(base, doc_of("new", 5), repeat), "exactly_one_answers")
    within = row(compare.compare(base, doc_of("new", 3), repeat), "exactly_one_answers")
    assert "within noise" in within and "REAL" not in within
    assert "same" in row(compare.compare(base, doc_of("new", 2), repeat), "exactly_one_answers")
    alone = compare.compare(base, doc_of("new", 5))
    assert "REAL" not in alone.replace("is called real", "") and "noise unknown" in alone
    assert "--repeat" in alone


def test_a_number_is_compared_against_its_baseline_spread_too() -> None:
    """Tokens after the ask: a baseline that moved 1000 -> 1100 between runs has a spread of 100, so a fall to
    500 is real and one to 950 is not."""
    base, repeat = doc_of("b", 3, tokens=1000), doc_of("b2", 3, tokens=1100)
    assert "REAL" in row(compare.compare(base, doc_of("n", 3, tokens=500), repeat), "tokens after the ask")
    small = row(compare.compare(base, doc_of("n", 3, tokens=950), repeat), "tokens after the ask")
    assert "within noise" in small


def test_compare_names_a_scenario_only_one_run_has() -> None:
    a, b = doc_of("a", 3), doc_of("b", 3)
    results.add_trial(b, "bystander", trial(0, {"charlie_posts_nothing": True}))
    assert "bystander: only in B" in compare.compare(a, b)


# ------------------------------------------------------------ the token meter
HAIKU = {
    "input_tokens": 2,
    "output_tokens": 4,
    "cache_read_input_tokens": 11461,
    "cache_creation_input_tokens": 30757,
    "cache_creation": {"ephemeral_1h_input_tokens": 30757, "ephemeral_5m_input_tokens": 0},
}


def test_a_call_is_priced_as_claude_codes_own_record_priced_it() -> None:
    """The usage of one real Haiku call (2 in, 4 out, 11,461 cache read, 30,757 written for an hour), which
    Claude Code itself recorded as 0.00626821 USD. Fails if the rates or the cache multipliers are wrong."""
    assert claude_world.price("claude-haiku-5-5", HAIKU) == pytest.approx(0.00626821, abs=1e-9)
    assert claude_world.price("claude-sonnet-5-5", HAIKU) == pytest.approx(0.00626821 * 20, abs=1e-8)
    assert claude_world.price("claude-some-new-model", HAIKU) is None


def line(**message: Any) -> bytes:
    return (json.dumps({"type": "assistant", "message": message}) + "\n").encode()


def test_the_meter_counts_a_message_once_and_reads_only_whole_new_lines(tmp_path: Path) -> None:
    """A message written as several lines (one per content block) counts once, at its largest output; a line
    still being written is left until it is whole; later reads add only what was appended. Fails if a
    message is counted per block, or half a line is parsed."""
    f = tmp_path / "s.jsonl"
    usage = {**HAIKU, "output_tokens": 4}
    f.write_bytes(line(id="m1", model="claude-haiku-5-5", usage=usage) + b'{"type": "user"}\n')
    meter = claude_world.TranscriptMeter(tmp_path)
    t = meter.read()
    assert (t.calls, t.output, t.cache_read, t.cache_write) == (1, 4, 11461, 30757)
    with open(f, "ab") as fh:  # the same message again, its output now final; then half a line
        fh.write(line(id="m1", model="claude-haiku-5-5", usage={**usage, "output_tokens": 90}))
        fh.write(line(id="m2", model="claude-haiku-5-5", usage={"input_tokens": 1, "output_tokens": 10}))
        fh.write(b'{"type": "assistant", "message": {"id": "m3", "usage": {"input_t')
    t = meter.read()
    assert t.calls == 2 and t.output == 100 and t.input == 3, t
    assert t.cost_usd is not None and t.cost_usd > 0.006
    with open(f, "ab") as fh:
        fh.write(b'okens": 7}}}\n{"type": "cost-state", "totalCostUSD": 0.5}\n')
    t = meter.read()
    assert t.calls == 3 and t.input == 10 and meter.cli_cost == 0.5
    assert claude_world.TranscriptMeter(tmp_path / "nothing-here").read() == Tokens()


def test_tokens_add_and_subtract_with_a_price_that_may_be_unknown() -> None:
    a, b = Tokens(10, 1, 100, 5, 2, 0.5), Tokens(1, 1, 1, 1, 1, None)
    assert (a + a).total == 232 and (a - a).total == 0 and (a - a).cost_usd == 0
    assert (a + b).cost_usd is None and a.as_dict()["total"] == 116


# ------------------------------------------------------ the real world's setup


def test_the_launch_line_asks_for_the_model_and_never_for_less_approval() -> None:
    """The eval's Claude Code is the live driver's (manual permission mode, user settings off) on the
    model the run names, and the typed prompt says to join and nothing about answering: how the room's
    own text makes an agent behave is what is measured. Fails if a bypass switch appears, or the prompt
    tells an agent to answer."""
    sys.path.insert(0, str(REPO / "tests" / "live"))
    from harness.world import Broker

    broker = Broker("claude", room="#lab")
    try:
        agent = claude_world.EvalClaude("alpha", broker, "sonnet")
        agent.settings = Path("/s.json")
        argv = agent.argv(False)
        assert argv[argv.index("--model") + 1] == "sonnet"
        assert argv[argv.index("--permission-mode") + 1] == "manual"
        assert argv[argv.index("--setting-sources") + 1] == "project,local"
        assert not [a for a in argv if "dangerously" in a or "bypass" in a.lower()]
        text = agent.join_text()
        assert 'room "#lab"' in text and 'screen_name "alpha"' in text
        assert "answer" not in text.lower() and "say" not in text.lower().split()
        assert agent.tmux.sock.startswith("sb-live-claude-alpha-"), "an agent shares no terminal server"
    finally:
        broker.cleanup()


# ------------------------------------------------------------------ the opt-in


def test_the_runner_refuses_without_the_opt_in_and_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``run`` starts real CLIs, so it needs ``SWITCHBOARD_EVAL=claude`` (or ``all``). Fails if it starts
    without being asked."""
    monkeypatch.delenv("SWITCHBOARD_EVAL", raising=False)
    assert run_evals.main(["run", "--arm", "x", "--k", "1"]) == 2
    assert "SWITCHBOARD_EVAL=claude" in capsys.readouterr().err
    monkeypatch.setenv("SWITCHBOARD_EVAL", "codex")
    assert run_evals.main(["run", "--arm", "x"]) == 2
    monkeypatch.setenv("SWITCHBOARD_EVAL", "claude, codex")
    assert run_evals.opted_in()


def test_compare_runs_from_the_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for name, passes in (("a", 2), ("a2", 3), ("b", 5)):
        results.write(tmp_path / f"{name}.json", doc_of(name, passes))
    args = [str(tmp_path / "a.json"), str(tmp_path / "b.json")]
    assert run_evals.main(["compare", *args, "--repeat", str(tmp_path / "a2.json")]) == 0
    assert "REAL" in capsys.readouterr().out
    assert run_evals.main(["compare", args[0], str(tmp_path / "nope.json")]) == 2


def test_a_plain_pytest_skips_the_eval_module() -> None:
    """With no ``SWITCHBOARD_EVAL`` the eval module is collected and skipped with the line that says how to
    run it, and nothing starts; the ``eval`` marker is declared and deselected by default, like ``live``.
    Fails if the module runs unasked, or if the marker is dropped from the default deselection."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWITCHBOARD", "CLAUDE"))}
    cmd = [sys.executable, "-m", "pytest", "tests/evals", "-q", "-rs", "-p", "no:cacheprovider"]
    for extra in ([], ["-m", "eval"]):
        r = subprocess.run([*cmd, *extra], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
        out = r.stdout + r.stderr
        assert r.returncode in (0, 5), out  # 5: no test was collected, the module skipped itself
        assert "1 skipped" in out and "SWITCHBOARD_EVAL=claude" in out and "passed" not in out, out
    opts = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert "not eval" in opts["addopts"] and any(m.startswith("eval:") for m in opts["markers"])
