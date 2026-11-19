"""The newest plugin against a real broker of the oldest release of its major (#850, DESIGN.md §49.13).

Opt-in: ``uv run pytest -m compat tests/live/test_compat_oldest_release.py``. It needs the release's git
tag in this checkout (``git fetch --tags``) and ``curl``; it needs no network and no Docker. The tag's
``src`` is
extracted to a temp folder and run on this interpreter (``PYTHONPATH``), as a hosted broker in a temp home
(the live tests' ``Broker``); a stand-in Claude Code session joins over ``/mcp`` and this checkout's
``bin/listen`` waits for it. The offline half, which CI runs, is
``tests/integration/test_plugin_server_compat.py``."""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import tempfile
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from harness.world import Broker, poll

pytestmark = pytest.mark.compat

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "compat" / "server-1.0.0.json"
RELEASE = json.loads(FIXTURE.read_text())["release"]
TAG = f"v{RELEASE}"

if shutil.which("curl") is None:
    pytest.skip("the listener needs curl", allow_module_level=True)
if subprocess.run(["git", "-C", str(ROOT), "rev-parse", "-q", "--verify", f"refs/tags/{TAG}"]).returncode:
    pytest.skip(f"{TAG} is not in this checkout: git fetch --tags", allow_module_level=True)


def test_the_newest_plugin_is_woken_by_a_real_oldest_release() -> None:
    """A real ``v1.0.0`` broker, this checkout's Claude Code plugin scripts. The listener parks, a person's
    message reaches it, and it exits 2 with the message. Fails when a change to the listen protocol (or the
    session script's requests) leaves a plugin ahead of its server unable to wake a session."""
    import sys

    sys.path.insert(0, str(ROOT / "tests" / "integration"))
    from test_agent_plugins import run_listen
    from test_agents_http_claude import SESSION_1, Claude

    tmp = Path(tempfile.mkdtemp(prefix="sb-compat-"))
    try:
        archive = tmp / "src.tar"
        subprocess.run(["git", "-C", str(ROOT), "archive", "-o", str(archive), TAG, "src"], check=True)
        with tarfile.open(archive) as t:
            t.extractall(tmp / "old", filter="data")
        old = Broker("compat", hosted=True, src=tmp / "old" / "src").start()
        try:
            _id, token = old.make_token("laptop")
            claude = Claude(types.SimpleNamespace(base=old.url), token, SESSION_1)  # type: ignore[arg-type]
            assert claude.call("join", room=old.room, screen_name="claude-1")["ok"]
            claude.call("read", room=old.room)
            with ThreadPoolExecutor(1) as pool:
                waiting = pool.submit(run_listen, None, token, SESSION_1, url=old.url, round_s=30)
                assert poll(lambda: old.member("claude-1") is not None, 15)
                old.say("@claude-1 wake up")
                r = waiting.result(90)
            assert r.returncode == 2 and "wake up" in r.stderr, r
        finally:
            old.stop()
            old.cleanup()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
