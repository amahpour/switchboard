"""diagram.js's source clean-up, run for real in node (tests/diagram_harness.js; DESIGN.md §33).

Before Mermaid sees a diagram, each complete one-line HTML tag in it with an event-handler
attribute is shown as text by a zero width space right after its own `<` (issue #917): Mermaid's
DOMPurify parses each label in the page's own documents, and Chromium from 157 reports a handler
attribute to the CSP as soon as it is parsed, which the e2e tests count as a failure, though
nothing runs. Nothing is removed, so no diagram's structure changes, and no closing tag is
touched, since the scan can't tell which label a closer is in (#918's review). Skipped where node
is missing, except under SWITCHBOARD_REQUIRE_NODE=1 (CI), where that fails."""

from __future__ import annotations

import json
import random
import re
import subprocess
import time
from pathlib import Path

from conftest import node_for_tests

HARNESS = Path(__file__).resolve().parents[1] / "diagram_harness.js"
NODE, pytestmark = node_for_tests()


def clean(*sources: str) -> list[str]:
    assert NODE is not None
    r = subprocess.run(
        [NODE, str(HARNESS)], input=json.dumps(sources), capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


Z = "\u200b"  # the zero width space that makes a handler tag text


def test_a_handler_tag_is_shown_as_its_text() -> None:
    """Every spelling a browser parses as a handler attribute marks its tag, valued or not: a
    zero width space after its '<' makes it text, so nothing is parsed
    and Chromium 157 has nothing to report to the CSP, and the hostile-diagram e2e test passes."""
    got = clean(
        'A["<img src=x onerror=window.__pwned=1> <b>bold</b>"] --> B',
        "A[<img/onerror=a>]",
        "A[<IMG ONERROR=a>]",
        'A[<img title=">" onerror="a" alt=\'b\'>]',
        "A[<svg><g onload = 'a'></g></svg>]",
        "A[<b class=c onmouseover=a onfocus=b>x</B>]",
    )
    assert got == [
        f'A["<{Z}img src=x onerror=window.__pwned=1> <b>bold</b>"] --> B',
        f"A[<{Z}img/onerror=a>]",
        f"A[<{Z}IMG ONERROR=a>]",
        f'A[<{Z}img title=">" onerror="a" alt=\'b\'>]',
        f"A[<svg><{Z}g onload = 'a'></g></svg>]",
        f"A[<{Z}b class=c onmouseover=a onfocus=b>x</B>]",
    ]


def test_a_closing_tag_is_never_marked() -> None:
    """Only the candidate's own `<` gets a zero width space; its closing tag is left as written.
    At 53238e08 the first closing tag of the same name on the line was marked too, and `</` with a
    zero width space after it is a bogus comment to the HTML tokenizer, so it drew as nothing
    (#918's review, F3)."""
    assert clean('A["<p onclick=go>tap</p>"]', "A[<b onclick=x>a</b> <b>c</b>]") == [
        f'A["<{Z}p onclick=go>tap</p>"]',
        f"A[<{Z}b onclick=x>a</b> <b>c</b>]",
    ]


def test_a_closing_tag_in_the_next_label_is_left_as_written() -> None:
    """The review's F4 on #918: the scan reads A's `<b one="` as a candidate that ends at the first
    `>` in B's label, and at 53238e08 it then marked B's own `</b>`, which left B's `<b>` with no
    closer when Mermaid sanitized B's label on its own. The scan can't see label boundaries, so the
    only edit is one zero width space after A's `<`, and B's text is unchanged byte for byte."""
    b = '[" > threshold <b>inner</b> tail"]'
    src = f'flowchart TD\n  A["check x<b one="] --> B{b}'
    (got,) = clean(src)
    assert got.count(Z) == 1
    assert got == src.replace("x<b", f"x<{Z}b")
    assert got.endswith(f"B{b}")


def test_the_reviewers_case_keeps_both_nodes_and_the_edge() -> None:
    """The review's case on #918 (F1): the scan reads A's closing quote and B's opening quote as one
    attribute value, and removing that `one=...` attribute deleted `] --> B["`, so Mermaid drew one
    node and no edge. Nothing is removed now: the only change is a zero width space after `<`."""
    src = 'flowchart TD\n  A["check x<limit one="] --> B[" > threshold"]'
    assert clean(src) == [f'flowchart TD\n  A["check x<{Z}limit one="] --> B[" > threshold"]']


def test_everything_else_in_a_diagram_is_left_as_written() -> None:
    """Mermaid's own syntax, text that only looks like a tag, and a tag that doesn't close on its
    line are not touched, so a diagram draws exactly as before."""
    keep = (
        "flowchart TD\n  A --> B\n  A <--> C\n  one --> onward\n  A -- on hold --> B",
        "classDiagram\n  class Shape{\n    <<interface>>\n    onDraw()\n  }",
        "sequenceDiagram\n  A->>B: x < y and onerror=1\n  B-->>A: <b>bold</b>",
        'flowchart TD\n  A["<img src=x\n onerror=a>"]',  # the tag doesn't close on its line
        'flowchart TD\n  A["<img onerror="a\n">"]',  # nor does the value
        "graph LR\n  A[a < b] --> B[<x]",
        "flowchart TD\n  A[\"<img title='onerror=a' alt=on>\"]",  # \"on\" in a value is a value
        'flowchart TD\n  A["<p>x</p>"] --> B["<b class=c>y</b>"]',
    )
    assert clean(*keep) == list(keep)


def test_any_attribute_named_on_something_marks_its_tag() -> None:
    """The rule is by name, as the browser's is: an attribute whose name starts with "on" marks
    its tag, though DOMPurify would drop a name it doesn't know, such as `one`, anyway."""
    assert clean("A[\"<img one=1 title='x'>\"]") == [f"A[\"<{Z}img one=1 title='x'>\"]"]


def test_a_comparison_in_a_label_that_runs_into_an_arrow_is_left_as_written() -> None:
    """`a<b only` in a quoted label could read as a tag `<b` with an attribute `only"]`, closed by
    the edge's `-->`. It isn't a tag: nothing changes."""
    keep = (
        'flowchart TD\n  A["if a<b only"] --> B["done"]',
        "flowchart TD\n  A[x<y one] --> B",
        "flowchart TD\n  A(a<b on=1) --> B",
        "flowchart TD\n  A[a<b onx=1]-->B",
        'flowchart TD\n  A["n<m onwards"] -- on hold --> B["n>m"]',
        "sequenceDiagram\n  alice->>bob: i<j only, and onward -->> back",
    )
    assert clean(*keep) == list(keep)


# Pieces of the labels of ordinary diagrams: comparisons, quotes, arrows, entities, <br> and <b>
# tags, and words that start with "on", which can line up into a tag with an on… attribute (as
# `x<limit one="` does in #918's review), and a tag with a handler and its closer. Whatever they
# spell, only zero width spaces are added, each after a candidate's own `<`.
WORDS = ("one", "only", "on", "onward", "x", "total", "it's", "a = b", "(b)", "n:1", "#quot;", "one=", '"')
SIGNS = (" < ", " > ", "<=", ">=", "&lt;", "&gt;", "&amp;", "#lt;", "-->", "->>", "<br>", "<br/>")
TAGS = ("<b>bold</b>", "<i>it</i>", "<<interface>>", "</b>", "<b onclick=go>tap</b>")
OPEN = ("a<b", "x<y", "n<m", "x<limit")


def label(rng: random.Random) -> str:
    out: list[str] = []
    for _ in range(rng.randint(1, 6)):
        pool = WORDS + SIGNS + TAGS + OPEN
        out.append(rng.choice(pool))
    return rng.choice(("", " ")).join(out)


def flowchart(rng: random.Random) -> str:
    lines = [rng.choice(("flowchart TD", "flowchart LR", "graph LR"))]
    for n in range(rng.randint(1, 8)):
        a, b = f"N{n}", f"N{n + 1}"
        lines.append(
            "  "
            + rng.choice(
                (
                    f'{a}["{label(rng)}"] --> {b}["{label(rng)}"]',
                    f'{a}["{label(rng)}"]-->{b}',
                    f"{a} -->|{label(rng)}| {b}",
                    f"{a} -- {label(rng)} --> {b}",
                    f'{a}("{label(rng)}") ==> {b}{{"{label(rng)}"}}',
                    f"{a} -.-> {b}",
                )
            )
        )
    return "\n".join(lines)


def sequence(rng: random.Random) -> str:
    lines = ["sequenceDiagram"]
    for _ in range(rng.randint(1, 8)):
        lines.append(
            "  "
            + rng.choice(
                (
                    f"alice->>bob: {label(rng)}",
                    f"bob-->>alice: {label(rng)}",
                    f"Note over alice,bob: {label(rng)}",
                )
            )
        )
    return "\n".join(lines)


def test_any_diagram_is_its_input_with_only_zero_width_spaces_added() -> None:
    """Over many generated flowcharts and sequence diagrams, removing every zero width space from
    the output gives the input byte for byte: nothing is ever removed, since a removal can cross a
    label's boundary the scan can't see and change the diagram (#918's review, F1)."""
    rng = random.Random(918)
    sources = [rng.choice((flowchart, sequence))(rng) for _ in range(3000)]
    got = clean(*sources)
    changed = [(s, g) for s, g in zip(sources, got, strict=True) if g.replace(Z, "") != s]
    assert not changed, changed[:3]
    assert any(Z in g for g in got)  # the generator does make candidates
    for g in got:
        assert f"</{Z}" not in g, g  # a closing tag is never marked (#918's review, F3 and F4)
        for m in re.finditer(Z, g):
            assert CANDIDATE.match(g, m.start() - 1), (g, m.start())


# What follows a zero width space the clean-up put in: the '<' just before it, a tag name, and an
# attribute named on… before the next '<' or the line's end, which is where a candidate tag lies.
CANDIDATE = re.compile("<" + Z + r"[A-Za-z][^\s/>\n<]*[^\n<]*?[\s/](?i:on)[^\n<]*")


def test_a_diagram_with_no_candidate_comes_out_unchanged() -> None:
    """A diagram with no tag that has an on… attribute is passed on as written."""
    rng = random.Random(57)
    sources = [rng.choice((flowchart, sequence))(rng).replace("on", "in") for _ in range(500)]
    assert clean(*sources) == sources


def test_a_handler_in_an_ordinary_diagram_is_marked() -> None:
    """The same diagrams with a hostile tag in a label have that tag shown as text."""
    rng = random.Random(917)
    hostile = "<img src=x onerror=alert(1)>"
    sources = [f'{flowchart(rng)}\n  H["{label(rng)} {hostile}"] --> N0' for _ in range(300)]
    sources += [f"{sequence(rng)}\n  alice->>bob: {hostile} {label(rng)}" for _ in range(300)]
    for s, g in zip(sources, clean(*sources), strict=True):
        assert g.replace(Z, "") == s
        assert f"<{Z}img src=x onerror" in g


def test_a_very_long_line_is_read_once() -> None:
    """One 200 KB line of quoted attributes in a tag that never closes is read in one pass: the
    first clean-up looked for the line's end again after every quoted value, so a line of n
    attributes cost n times its length (#918's review). The harness counts the characters
    indexOf reads, which is where that rescan was; the clock bound is only a backstop."""
    assert NODE is not None
    line = 'flowchart TD\n  A["<img' + ' t="x"' * 33_000 + '"] --> B'
    started = time.monotonic()
    r = subprocess.run(
        [NODE, str(HARNESS), "--count"], input=json.dumps([line]), capture_output=True, text=True, timeout=60
    )
    elapsed = time.monotonic() - started
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["out"] == [line]
    assert got["read"] <= 4 * len(line), got["read"]  # one pass, not one per attribute (~10**9)
    assert elapsed < 30, elapsed
