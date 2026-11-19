"""The newest plugin against a real broker of the oldest release of its major (#850, DESIGN.md §49.13).

Opt-in: ``uv run pytest -m compat tests/compat``. It needs the release's git tag in this checkout
(``git fetch --tags``) and ``curl``; it needs no network and no Docker. The tag's ``src`` is extracted to a
temp folder and run on this interpreter (``PYTHONPATH``), as a hosted broker in a temp home (the live
tests' ``Broker``); a stand-in Claude Code session joins over ``/mcp`` and this checkout's ``bin/listen``
waits for it. The offline half, which CI runs, is ``tests/integration/test_plugin_server_compat.py``.
Everything that needs the tag, curl or the live harness is set up inside the test, so a default
collection of this module imports nothing of it."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.compat

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "compat" / "server-1.0.0.json"


def test_the_newest_plugin_is_woken_by_a_real_oldest_release() -> None:
    """A real ``v1.0.0`` broker (its own reported version is checked), this checkout's Claude Code plugin
    scripts. The listener parks, a person's message reaches it, and it exits 2 with the message. Fails when
    a change to the listen protocol leaves a plugin ahead of its server unable to wake a session, or when
    the broker that answered was not the oldest release."""
    release = json.loads(FIXTURE.read_text())["release"]
    tag = f"v{release}"
    if shutil.which("curl") is None:
        pytest.skip("the listener needs curl")
    if subprocess.run(["git", "-C", str(ROOT), "rev-parse", "-q", "--verify", f"refs/tags/{tag}"]).returncode:
        pytest.skip(f"{tag} is not in this checkout: git fetch --tags")
    for folder in (ROOT / "tests" / "live", ROOT / "tests" / "integration"):
        sys.path.insert(0, str(folder))  # the live tests' Broker, and the plugin tests' helpers
    from harness.world import Broker, poll
    from test_agent_plugins import run_listen
    from test_agents_http_claude import SESSION_1, Claude

    tmp = Path(tempfile.mkdtemp(prefix="sb-compat-"))
    (tmp / "h").mkdir()
    (tmp / "h" / ".curlrc").write_text("")  # curl reads this and goes no further than the test's home
    old = Broker("compat", hosted=True, src=tmp / "old" / "src")
    try:
        archive = tmp / "src.tar"
        subprocess.run(["git", "-C", str(ROOT), "archive", "-o", str(archive), tag, "src"], check=True)
        with tarfile.open(archive) as t:
            t.extractall(tmp / "old", filter="data")
        old.start()
        assert old.version == release, f"the broker that started reports {old.version!r}, not {release}"
        _id, token = old.make_token("laptop")
        claude = Claude(types.SimpleNamespace(base=old.url), token, SESSION_1)  # type: ignore[arg-type]
        assert claude.call("join", room=old.room, screen_name="claude-1")["ok"]
        claude.call("read", room=old.room)
        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(run_listen, None, token, SESSION_1, url=old.url, round_s=30, home=tmp / "h")
            assert poll(lambda: old.member("claude-1") is not None, 15)
            old.say("@claude-1 wake up")
            r = waiting.result(90)
        assert r.returncode == 2 and "wake up" in r.stderr, r
    finally:
        old.stop()
        old.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)
