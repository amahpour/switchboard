"""Scenarios as data (DESIGN.md §12.7). Test-only.

A scenario is a TOML file in ``tests/evals/scenarios/``: the room, the agents in it, what the person says
and the checks. A check is a ``graders`` function with its arguments, under an ``id`` that names it in the
results (so one function can serve two checks). ``load`` refuses a scenario that names a check, an
argument or an agent that doesn't exist, so a typo is found when the file is read and not after a run.
"""

from __future__ import annotations

import inspect
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .graders import GRADERS

SCENARIO_DIR = Path(__file__).resolve().parents[1] / "scenarios"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
ROOM_RE = re.compile(r"^#[a-z][a-z0-9-]{0,39}$")
MAX_SAY = 2000


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    say: str  # what the person posts in the room


@dataclass(frozen=True)
class Check:
    id: str  # the name in the results file
    grader: str  # a name in ``graders.GRADERS``
    params: dict[str, Any]
    step: int = 0  # the person's message it is about, by the order of the steps


@dataclass(frozen=True)
class Scenario:
    name: str
    about: str
    room: str
    agents: tuple[str, ...]
    steps: tuple[Step, ...]
    checks: tuple[Check, ...]


def parse(raw: dict[str, Any], source: str = "scenario") -> Scenario:
    """A scenario from the dict a TOML file makes. Every problem is a ``ScenarioError`` naming the source."""

    def bad(why: str) -> ScenarioError:
        return ScenarioError(f"{source}: {why}")

    unknown = set(raw) - {"name", "about", "room", "agents", "steps", "checks"}
    if unknown:
        raise bad(f"unknown keys {sorted(unknown)}")
    name, about, room = raw.get("name"), raw.get("about", ""), raw.get("room")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise bad("name must be lowercase words joined by hyphens")
    if not isinstance(about, str):
        raise bad("about must be text")
    if not isinstance(room, str) or not ROOM_RE.match(room):
        raise bad("room must be like #build")
    agents = raw.get("agents")
    if not isinstance(agents, list) or not 1 <= len(agents) <= 6:
        raise bad("agents must be a list of one to six screen names")
    if not all(isinstance(a, str) and AGENT_RE.match(a) for a in agents) or len(set(agents)) != len(agents):
        raise bad("agents must be different screen names (lowercase, digits, - and _)")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise bad("at least one [[steps]] is needed")
    steps = []
    for i, s in enumerate(steps_raw):
        if not isinstance(s, dict) or set(s) != {"say"} or not isinstance(s["say"], str):
            raise bad(f"step {i} must be a table with just 'say'")
        if not s["say"].strip() or len(s["say"]) > MAX_SAY:
            raise bad(f"step {i}: say must be 1 to {MAX_SAY} characters")
        steps.append(Step(s["say"]))
    checks_raw = raw.get("checks")
    if not isinstance(checks_raw, list) or not checks_raw:
        raise bad("at least one [[checks]] is needed")
    checks: list[Check] = []
    for c in checks_raw:
        if (
            not isinstance(c, dict)
            or not isinstance(c.get("id"), str)
            or not isinstance(c.get("grader"), str)
        ):
            raise bad("a check needs an id and a grader")
        cid, grader = c["id"], c["grader"]
        if not NAME_RE.match(cid.replace("_", "-")):
            raise bad(f"check id {cid!r} must be lowercase words joined by - or _")
        if any(x.id == cid for x in checks):
            raise bad(f"check id {cid!r} is used twice")
        if grader not in GRADERS:
            raise bad(f"check {cid}: no grader {grader!r} (have {sorted(GRADERS)})")
        step = c.get("step", 0)
        if isinstance(step, bool) or not isinstance(step, int) or not 0 <= step < len(steps):
            raise bad(f"check {cid}: step must be 0 to {len(steps) - 1}")
        params = {k: v for k, v in c.items() if k not in {"id", "grader", "step"}}
        try:
            inspect.signature(GRADERS[grader]).bind(None, 0, **params)
        except TypeError as e:
            raise bad(f"check {cid}: {e}") from None
        # an argument that would only fail after the run has been paid for fails here
        if "agents" in params:
            named = params["agents"]
            if not isinstance(named, list) or not named or not all(isinstance(a, str) for a in named):
                raise bad(f"check {cid}: agents must be a list of screen names")
            for a in named:
                if a not in agents:
                    raise bad(f"check {cid}: {a!r} is not one of the scenario's agents")
        if "n" in params and (
            isinstance(params["n"], bool)
            or not isinstance(params["n"], int)
            or not 0 <= params["n"] <= len(agents)
        ):
            raise bad(f"check {cid}: n must be a whole number from 0 to {len(agents)}")
        if "pattern" in params:
            try:
                re.compile(params["pattern"])
            except (re.error, TypeError) as e:
                raise bad(f"check {cid}: pattern is not a regular expression ({e})") from None
        checks.append(Check(cid, grader, params, step))
    return Scenario(name, about, room, tuple(agents), tuple(steps), tuple(checks))


def load(path: str | Path) -> Scenario:
    p = Path(path)
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ScenarioError(f"{p.name}: {e}") from None
    sc = parse(raw, p.name)
    if p.stem != sc.name:
        raise ScenarioError(f"{p.name}: the file must be named {sc.name}.toml")
    return sc


def load_all(directory: str | Path = SCENARIO_DIR) -> dict[str, Scenario]:
    out: dict[str, Scenario] = {}
    for p in sorted(Path(directory).glob("*.toml")):
        sc = load(p)
        out[sc.name] = sc
    return out
