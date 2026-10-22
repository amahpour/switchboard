"""Compare two runs (DESIGN.md §12.7). Test-only.

``compare A B`` prints each check as ``4/5 -> 5/5`` and the change in tokens, wakes and time. A change
is called real only if it is larger than the spread between two runs of the same baseline, so the
baseline is run twice first and its second run is given as ``--repeat``. With no repeat there is no
estimate of the noise, and nothing is called real. A harness error is not a failed check: the rates
are of the trials that were graded, and the errors are counted beside them.
"""

from __future__ import annotations

from typing import Any

from .results import METRICS, summarize


def _rate(c: dict[str, int] | None) -> float | None:
    return c["passed"] / c["graded"] if c and c["graded"] else None


def _frac(c: dict[str, int] | None) -> str:
    return f"{c['passed']}/{c['graded']}" if c and c["graded"] else "n/a"


def _verdict(change: float | None, spread: float | None) -> str:
    if change is None:
        return "n/a"
    if change == 0:
        return "same"
    if spread is None:
        return "noise unknown"
    return "REAL" if abs(change) > spread else "within noise"


def _num(x: float | None, name: str) -> str:
    if x is None:
        return "n/a"
    if name == "cost_usd":
        return f"{x:.4f}"
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:.1f}"


def _label(doc: dict[str, Any]) -> str:
    return f"{doc.get('arm', '?')}"


def compare(a: dict[str, Any], b: dict[str, Any], repeat: dict[str, Any] | None = None) -> str:
    """The report, as text, for results documents ``a`` (before) and ``b`` (after); ``repeat`` is a second run
    of ``a``'s arm, which gives the noise."""
    out = [
        f"A = {_label(a)}   B = {_label(b)}"
        + (f"   noise from {_label(repeat)}, a second run of A" if repeat else "")
    ]
    if repeat is None:
        out.append("no second run of A given (--repeat): the noise is unknown, so no change is called real")
    names = [n for n in a["scenarios"] if n in b["scenarios"]]
    for n in a["scenarios"]:
        if n not in b["scenarios"]:
            out.append(f"\n{n}: only in A, not compared")
    for n in b["scenarios"]:
        if n not in a["scenarios"]:
            out.append(f"\n{n}: only in B, not compared")
    for n in names:
        sa, sb = summarize(a["scenarios"][n]["trials"]), summarize(b["scenarios"][n]["trials"])
        sr = summarize(repeat["scenarios"][n]["trials"]) if repeat and n in repeat["scenarios"] else None
        out.append(f"\n{n}")
        out.append(f"  {'check':<28}{'A':>7}    {'B':>7}   {'change':>10}   {'noise':>9}   verdict")
        ids = list(dict.fromkeys([*sa["checks"], *sb["checks"]]))
        for cid in ids:
            ra, rb = _rate(sa["checks"].get(cid)), _rate(sb["checks"].get(cid))
            rr = _rate(sr["checks"].get(cid)) if sr else None
            change = None if ra is None or rb is None else (rb - ra) * 100
            spread = None if ra is None or rr is None else abs(ra - rr) * 100
            out.append(
                f"  {cid:<28}{_frac(sa['checks'].get(cid)):>7} -> {_frac(sb['checks'].get(cid)):>7}"
                f"   {_pts(change):>10}   {_pts(spread, plus=False):>9}   {_verdict(change, spread)}"
            )
        ea = f"{sa['harness_errors']}/{sa['trials']}"
        eb = f"{sb['harness_errors']}/{sb['trials']}"
        out.append(f"  {'harness errors (not graded)':<28}{ea:>7} -> {eb:>7}")
        head = "per graded trial (mean)"
        out.append(f"  {head:<28}{'A':>7}    {'B':>7}   {'change':>10}   {'noise':>9}   verdict")
        for key, label in METRICS:
            ma, mb = sa["means"].get(key), sb["means"].get(key)
            mr = sr["means"].get(key) if sr else None
            change = None if ma is None or mb is None else mb - ma
            spread = None if ma is None or mr is None else abs(ma - mr)
            pct = "" if not change or not ma else f" ({change / ma * 100:+.0f}%)"
            out.append(
                f"  {label:<28}{_num(ma, key):>7} -> {_num(mb, key):>7}"
                f"   {_signed(change, key):>10}   {_num(spread, key):>9}   {_verdict(change, spread)}{pct}"
            )
    return "\n".join(out) + "\n"


def _pts(x: float | None, plus: bool = True) -> str:
    if x is None:
        return "n/a"
    return ("0 pts" if round(x) == 0 else f"{x:+.0f} pts") if plus else f"{x:.0f} pts"


def _signed(x: float | None, name: str) -> str:
    if x is None:
        return "n/a"
    s = _num(abs(x), name)
    return f"{'-' if x < 0 else '+'}{s}" if x else "0"
