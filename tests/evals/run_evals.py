"""Run the evals, and compare two runs (DESIGN.md §12.7, CONTRIBUTING.md "Evals"). Test-only.

    SWITCHBOARD_EVAL=claude uv run python tests/evals/run_evals.py run --arm baseline --k 5
    uv run python tests/evals/run_evals.py compare baseline-1.json first-answers.json --repeat baseline-2.json
    uv run python tests/evals/run_evals.py list

``run`` plays each scenario ``k`` times against real Claude Code agents (a few cents a trial) and writes one
results file. It starts a real CLI, so it runs only when ``SWITCHBOARD_EVAL=claude`` says so, and never in CI.
``compare`` reads files and costs nothing.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from evalkit import compare as compare_mod  # noqa: E402
from evalkit import results, scenario  # noqa: E402
from evalkit.trial import Limits, run_trial  # noqa: E402

OPT_IN = "SWITCHBOARD_EVAL"


def eval_set() -> set[str]:
    return {x.strip() for x in os.environ.get(OPT_IN, "").split(",") if x.strip()}


def opted_in() -> bool:
    return bool(eval_set() & {"claude", "all"})


def commit() -> str:
    r = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"], cwd=HERE, capture_output=True, text=True, timeout=10
    )
    return r.stdout.strip() or "unknown"


def cmd_list(args: argparse.Namespace) -> int:
    for name, sc in scenario.load_all().items():
        print(f"{name}: {sc.about}")
        print(f"  room {sc.room}, agents {', '.join(sc.agents)}")
        for i, st in enumerate(sc.steps):
            print(f"  person says ({i}): {st.say}")
        for c in sc.checks:
            print(f"  check {c.id}: {c.grader} {c.params}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    try:
        a, b = results.read(args.a), results.read(args.b)
        rep = results.read(args.repeat) if args.repeat else None
    except ValueError as e:
        print(f"evals: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(compare_mod.compare(a, b, rep))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from evalkit import claude_world

    if not opted_in():
        print(
            f"evals: set {OPT_IN}=claude to run the evals against Claude Code (it starts real CLIs)",
            file=sys.stderr,
        )
        return 2
    why = claude_world.skip_reason()
    if why:
        print(f"evals: {why}", file=sys.stderr)
        return 2
    every = scenario.load_all()
    names = (
        list(every)
        if args.scenarios == "all"
        else [x.strip() for x in args.scenarios.split(",") if x.strip()]
    )
    unknown = [n for n in names if n not in every]
    if unknown:
        print(f"evals: no scenario {unknown} (have {sorted(every)})", file=sys.stderr)
        return 2
    rules = (
        Path(args.rules_file).read_text(encoding="utf-8").strip() if args.rules_file else (args.rules or "")
    )
    if len(rules) > 2000:
        print("evals: a room's rules are at most 2000 characters", file=sys.stderr)
        return 2
    limits = Limits(token_cap=args.token_cap, settle_s=args.settle_s)
    started = time.monotonic()
    stamp = datetime.datetime.now(datetime.timezone.utc)
    out = Path(args.out or results.RESULTS_DIR) / f"{args.arm}-{stamp:%Y%m%dT%H%M%S}.json"
    from switchboard import __version__

    doc = results.new_doc(
        args.arm,
        {
            "started": stamp.isoformat(timespec="seconds"),
            "switchboard": {"version": __version__, "commit": commit()},
            "harness": {"name": "claude", "model": args.model, "cli": claude_world.cli_version()},
            "k": args.k,
            "rules": rules,
            "limits": dataclasses.asdict(limits),
            "max_cost_usd": args.max_cost,
        },
    )
    print(f"evals: arm {args.arm}, {len(names)} scenario(s) x {args.k}, model {args.model}, writing {out}")
    spent = 0.0
    stop = None
    for n in range(1, args.k + 1):  # trial by trial across the scenarios, so a drift in the CLI shows early
        for name in names:
            world = claude_world.ClaudeWorld(args.model)
            trial = run_trial(world, every[name], rules, limits, n=n)
            results.add_trial(doc, name, trial.as_dict())
            doc["wall_seconds"] = round(time.monotonic() - started)
            results.write(out, doc)
            print(_line(name, trial))
            spent += trial.metrics.get("tokens", {}).get("cost_usd") or 0.0
            if world.abort_reason:
                stop = f"stopped: {world.abort_reason}"
            elif spent > args.max_cost:
                stop = f"stopped: spent {spent:.3f} USD, over --max-cost {args.max_cost}"
            if stop:
                break
        if stop:
            break
    doc["wall_seconds"] = round(time.monotonic() - started)
    if stop:
        doc["stopped"] = stop
    results.write(out, doc)
    _summary(doc, spent)
    if stop:
        print(f"evals: {stop}", file=sys.stderr)
        return 1
    return 0


def _line(name: str, trial) -> str:
    if trial.status != "graded":
        e = trial.error or {}
        return f"  {name} #{trial.n}: HARNESS ERROR ({e.get('kind')}): {e.get('message', '')[:160]}"
    verdicts = " ".join(f"{cid}={'pass' if r['ok'] else 'FAIL'}" for cid, r in trial.checks.items())
    m = trial.metrics
    cost = m["tokens_after_ask"].get("cost_usd")
    return (
        f"  {name} #{trial.n}: {verdicts}  wakes={m['wakes']} passed={m['wakes_ended_in_pass']}"
        f" tokens={m['tokens_after_ask']['total']:,} cost={'n/a' if cost is None else cost}"
        f" {m['seconds']}s"
    )


def _summary(doc: dict, spent: float) -> None:
    print(f"\nevals: arm {doc['arm']} done in {doc.get('wall_seconds', 0)}s, {spent:.4f} USD over all trials")
    for name, sc in doc["scenarios"].items():
        s = sc["summary"]
        checks = "  ".join(f"{cid} {c['passed']}/{c['graded']}" for cid, c in s["checks"].items())
        print(f"  {name}: {checks}  (harness errors {s['harness_errors']}/{s['trials']})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="run_evals.py", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser(
        "run", help="play the scenarios against real Claude Code agents and write a results file"
    )
    s.add_argument("--arm", required=True, help="a name for what is being tried, e.g. baseline")
    s.add_argument("--scenarios", default="all", help="comma-separated scenario names (default: all)")
    s.add_argument("--k", type=int, default=5, help="trials per scenario (default 5)")
    s.add_argument(
        "--out", default=None, help="directory for the results file (default tests/evals/_results)"
    )
    s.add_argument("--rules", default=None, help="custom room rules for the room (#105): this arm's change")
    s.add_argument("--rules-file", default=None, help="the same, from a file")
    s.add_argument("--model", default="haiku", help="Claude Code's --model (default haiku, the cheapest)")
    s.add_argument(
        "--token-cap", type=int, default=Limits.token_cap, help="stop a trial past this many tokens"
    )
    s.add_argument(
        "--settle-s", type=float, default=Limits.settle_s, help="how long a room may take to settle"
    )
    s.add_argument(
        "--max-cost", type=float, default=1.0, help="stop the run past this many USD (default 1.00)"
    )
    s.set_defaults(func=cmd_run)
    s = sub.add_parser("compare", help="compare two results files (costs nothing)")
    s.add_argument("a", help="results file: before")
    s.add_argument("b", help="results file: after")
    s.add_argument(
        "--repeat", default=None, help="a second run of A's arm: the noise a change must be larger than"
    )
    s.set_defaults(func=cmd_compare)
    s = sub.add_parser("list", help="the scenarios and their checks")
    s.set_defaults(func=cmd_list)
    args = p.parse_args(argv)
    if getattr(args, "k", 1) < 1 or getattr(args, "k", 1) > 50:
        p.error("--k must be 1 to 50")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
