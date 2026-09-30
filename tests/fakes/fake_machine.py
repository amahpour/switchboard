"""A machine that dials in, for the web UI's tests and pictures (DESIGN.md §31.8).

``TestMachine`` is a temp home that pairs with a code as ``switchboard remote join`` does (a
new Ed25519 key, ``POST /link/pair``, a ``satellite.toml`` that dials, the broker's key pinned),
but says what the test chooses about itself: the facts are never this machine's host name or
OS. ``start()`` runs the real dialer there (``switchboard start --foreground --test-mode``), which
dials the broker's ``/link`` and runs the satellite, as the integration tests' machine does
(``fakes.fake_dialin``). Its satellite reports pids shifted by ``PID_SHIFT``.
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from conftest import child_env
from fakes.fake_link import PID_SHIFT, make_pi_home
from switchboard import __version__
from switchboard.paths import Paths
from switchboard.remote import linkkey
from switchboard.remote.config import write_satellite_conf

FACTS = {"hostname": "work-laptop", "os": "macOS 15.6", "arch": "arm64", "version": __version__,
         "harnesses": ["claude", "codex"]}


class TestMachine:
    __test__ = False  # not a test class, whatever its name

    def __init__(self, origin: str, facts: dict[str, Any] | None = None) -> None:
        self.origin = origin
        self.facts = dict(FACTS if facts is None else facts)
        self.home: Path = make_pi_home(satellite=False)
        self.paths = Paths.from_home(self.home)
        self.proc: subprocess.Popen[bytes] | None = None
        self.fingerprint = ""
        self.name = ""

    def pair(self, code: str) -> dict[str, Any]:
        """Pair with ``code``, as `remote join` does; returns the broker's answer."""
        key = linkkey.make_key(self.home / "link" / linkkey.MACHINE_KEY)
        pub = linkkey.pub_raw(key)
        self.fingerprint = linkkey.fingerprint(pub)
        r = httpx.post(self.origin + linkkey.PAIR_PATH, json={"code": code, "key": linkkey.b64u(pub),
                                                              "facts": self.facts}, timeout=10)
        r.raise_for_status()
        data = r.json()
        assert data["fingerprint"] == self.fingerprint, data
        self.name = data["name"]
        write_satellite_conf(self.paths, self.name, desktop="broker", key_fp=self.fingerprint,
                             broker_url=self.origin, broker_key=data["broker_key"])
        return data

    def start(self) -> subprocess.Popen[bytes]:
        out = open(self.home / "dialer-test.out", "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "switchboard", "--home", str(self.home), "start",
                                      "--foreground", "--test-mode"],
                                     env=child_env(SWITCHBOARD_TEST_PID_SHIFT=str(PID_SHIFT)),
                                     stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
        out.close()
        return self.proc

    def output(self) -> str:
        try:
            return (self.home / "dialer-test.out").read_text(errors="replace")
        except OSError:
            return ""

    def stop(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)

    def close(self) -> None:
        self.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def __enter__(self) -> "TestMachine":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
