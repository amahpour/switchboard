"""The Kubernetes manifest keeps an agent's wait open (#384, DESIGN.md §49.4; docs/DEPLOY.md), and starts on
a stock cluster (#595).

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


def test_the_pod_takes_no_service_link_variables() -> None:
    """#595: the Service is named switchboard, so without this Kubernetes puts
    SWITCHBOARD_PORT=tcp://<cluster-ip>:7419 in the pod, over the image's SWITCHBOARD_PORT=7419, and the
    broker exits on it. Fails if the pod spec doesn't turn service links off while the Service keeps that
    name."""
    manifest = (ROOT / "deploy" / "kubernetes" / "switchboard.yaml").read_text()
    assert re.search(r"kind: Service\nmetadata:\n  name: switchboard\n", manifest)
    assert re.search(r"\n      enableServiceLinks: false\n", manifest)


def test_a_service_link_port_is_named_as_one() -> None:
    """#595: a deployment that names its own Service switchboard, without the manifest's fix, gets
    SWITCHBOARD_PORT=tcp://...; the error says where that came from and what to set. Fails if it only
    says the value isn't a number. Any other bad port keeps the plain message."""
    import argparse

    import pytest

    from switchboard.cli import _port_number

    with pytest.raises(argparse.ArgumentTypeError, match="enableServiceLinks: false"):
        _port_number("tcp://10.96.0.12:7419")
    with pytest.raises(argparse.ArgumentTypeError, match=r"is not a port number \(0\.\.65535\)"):
        _port_number("seventy")
    assert _port_number("7419") == 7419
