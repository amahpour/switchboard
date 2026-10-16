"""The Kubernetes manifest keeps an agent's wait open (#384, DESIGN.md §49.4; docs/DEPLOY.md).

Claude Code's listener waits in silence at ``/mcp/listen`` for one round at a time, and
ingress-nginx closes a quiet request after 60 seconds unless its Ingress says otherwise.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_ingress_outlasts_a_round_of_the_listener() -> None:
    """The Ingress's read and send timeouts are longer than one round of the listener (its curl
    allows the round and 30 s more). Fails if either annotation is missing or shorter, which would
    cut every wait at ingress-nginx's 60-second default."""
    script = (ROOT / "plugins" / "claude" / "bin" / "listen").read_text()
    m = re.search(r"round=\$\{SWITCHBOARD_LISTEN_S:-(\d+)\}", script)
    assert m, "the listener's round"
    round_s = int(m.group(1))
    manifest = (ROOT / "deploy" / "kubernetes" / "switchboard.yaml").read_text()
    for name in ("proxy-read-timeout", "proxy-send-timeout"):
        found = re.search(rf'nginx\.ingress\.kubernetes\.io/{name}: "(\d+)"', manifest)
        assert found, name
        assert int(found.group(1)) >= round_s + 30, (name, found.group(1), round_s)
