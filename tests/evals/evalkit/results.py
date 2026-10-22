"""The file a run writes, and what is counted from it (DESIGN.md §12.7). Test-only.

One JSON file per run: the arm's name, what was run (the CLI, the model, the room's custom rules, the
limits), and for each scenario its trials. A trial is either ``graded`` (each check passed or failed) or a
``harness_error`` (the run went wrong, no check is graded). ``summarize`` is the only place the counts
are made, from the trials, so a file that is edited by hand or built by another tool reads the same.
Results stay outside git: ``tests/evals/_results/`` is ignored.
"""

from __future__ import annotations

import json
import os
import statistics
from pathlib import Path
from typing import Any

FORMAT = 1
RESULTS_DIR = Path(__file__).resolve().parents[1] / "_results"

# the per-trial numbers compare reports, with a label and whether fewer is better
METRICS: tuple[tuple[str, str], ...] = (
    ("tokens_after_ask", "tokens after the ask"),
    ("cost_usd", "cost after the ask, USD"),
    ("wakes", "wakes"),
    ("wakes_ended_in_pass", "wakes that ended in pass()"),
    ("posts", "agent posts"),
    ("seconds", "seconds to settle"),
)


def metric_of(trial: dict[str, Any], name: str) -> float | None:
    m = trial.get("metrics", {})
    if name == "tokens_after_ask":
        return m.get("tokens_after_ask", {}).get("total")
    if name == "cost_usd":
        return m.get("tokens_after_ask", {}).get("cost_usd")
    v = m.get(name)
    return v if isinstance(v, (int, float)) else None


def summarize(trials: list[dict[str, Any]]) -> dict[str, Any]:
    """The counts of one scenario's trials: how many were graded, how many were harness errors (apart),
    per check how many of the graded trials passed, and the mean of each number over the graded trials."""
    graded = [t for t in trials if t.get("status") == "graded"]
    errors = [t for t in trials if t.get("status") == "harness_error"]
    checks: dict[str, dict[str, int]] = {}
    for t in graded:
        for cid, r in t.get("checks", {}).items():
            c = checks.setdefault(cid, {"passed": 0, "graded": 0})
            c["graded"] += 1
            c["passed"] += 1 if r.get("ok") else 0
    kinds: dict[str, int] = {}
    for t in errors:
        k = (t.get("error") or {}).get("kind", "error")
        kinds[k] = kinds.get(k, 0) + 1
    means: dict[str, float] = {}
    for name, _label in METRICS:
        xs = [v for t in graded if (v := metric_of(t, name)) is not None]
        if xs:
            means[name] = round(statistics.fmean(xs), 4)
    spent = 0.0
    for t in trials:
        c = t.get("metrics", {}).get("tokens", {}).get("cost_usd")
        spent += c if isinstance(c, (int, float)) else 0.0
    return {
        "trials": len(trials),
        "graded": len(graded),
        "harness_errors": len(errors),
        "harness_error_kinds": kinds,
        "checks": checks,
        "means": means,
        "cost_usd_all_trials": round(spent, 4),
    }


def new_doc(arm: str, info: dict[str, Any]) -> dict[str, Any]:
    return {"format": FORMAT, "arm": arm, **info, "scenarios": {}}


def add_trial(doc: dict[str, Any], scenario: str, trial: dict[str, Any]) -> None:
    sc = doc["scenarios"].setdefault(scenario, {"trials": []})
    sc["trials"].append(trial)
    sc["summary"] = summarize(sc["trials"])


def write(path: str | Path, doc: dict[str, Any]) -> None:
    """Write the file whole through a temporary name, so a run stopped halfway leaves its finished trials."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    os.replace(tmp, p)


def read(path: str | Path) -> dict[str, Any]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{path}: not a results file ({e})") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or not isinstance(doc.get("scenarios"), dict):
        raise ValueError(f"{path}: not a results file of format {FORMAT}")
    for sc in doc["scenarios"].values():
        sc["summary"] = summarize(sc.get("trials", []))
    return doc
