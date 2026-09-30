"""A hosted broker and a machine that dials it, for the tests of DESIGN.md §31.7.

``DialIn`` starts a test-mode broker in this process behind ``http://localhost:<port>`` (a
public URL that is a secure context, so passkeys work, and that every process here resolves),
claims it with a software passkey (``fakes.fake_owner``), and makes a second temp home for the
machine, with the test marker and a Claude sessions dir of its own. ``switchboard remote join``
and the dialer (``switchboard start --foreground``) run there as real processes, over plain
``ws://``, as M8's exec transport runs the satellite in a second home. The dialer's satellite
reports pids shifted by ``PID_SHIFT``, so no probe of this machine's process table can match a
member of that "machine".
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from conftest import InProcBroker, child_env, make_tmp_home
from fakes.fake_authenticator import SoftAuthenticator
from fakes.fake_link import PID_SHIFT, make_pi_home
from fakes.fake_owner import Browser, claim, claim_link
from switchboard.broker.auth import WebOrigin
from switchboard.paths import Paths


class DialIn:
    def __init__(self, *, cfg: Any = None, clock: Any = None):
        self.desk = make_tmp_home()
        self.machine = make_pi_home(satellite=False)
        self.machine_paths = Paths.from_home(self.machine)
        self.env_extra = {"SWITCHBOARD_TEST_PID_SHIFT": str(PID_SHIFT)}
        self.b = InProcBroker(self.desk, cfg, clock=clock,
                              web_origin=lambda port: WebOrigin.parse(f"http://localhost:{port}"))
        self.dialers: list[subprocess.Popen[bytes]] = []
        self.owner: Browser | None = None
        self.auth: SoftAuthenticator | None = None

    # -------------------------------------------------------------- set-up
    def start(self, *, claim_it: bool = True) -> "DialIn":
        self.b.start()
        o = self.b.state.web_origin
        self.origin, self.host = o.origin, o.host
        self.owner = Browser(self.b, self.host, self.origin)
        self.auth = SoftAuthenticator("localhost", self.origin)
        if claim_it:
            token = claim_link(self.b).split("#t=", 1)[1]
            claim(self.b, self.owner, self.auth, token)
        return self

    def close(self) -> None:
        for p in self.dialers:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(5)
        if self.owner is not None:
            self.owner.close()
        self.b.stop()
        shutil.rmtree(self.desk, ignore_errors=True)
        shutil.rmtree(self.machine, ignore_errors=True)

    def __enter__(self) -> "DialIn":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------- the owner
    def api(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        assert self.owner is not None
        r = self.owner.get(path) if method == "GET" else self.owner.post(path, body or {})
        return r

    def code(self, name: str = "work-laptop") -> str:
        r = self.api("POST", "/api/machines/pair", {"name": name})
        assert r.status_code == 200, r.text
        return str(r.json()["code"])

    def approve(self, name: str = "work-laptop") -> dict[str, Any]:
        r = self.api("POST", f"/api/machines/{name}/approve")
        assert r.status_code == 200, r.text
        return dict(r.json())

    def remove(self, name: str = "work-laptop") -> dict[str, Any]:
        r = self.api("POST", f"/api/machines/{name}/remove")
        assert r.status_code == 200, r.text
        return dict(r.json())

    def machines(self) -> list[dict[str, Any]]:
        r = self.api("GET", "/api/machines")
        assert r.status_code == 200, r.text
        return list(r.json()["machines"])

    def machine_info(self, name: str = "work-laptop") -> dict[str, Any] | None:
        return next((m for m in self.machines() if m["name"] == name), None)

    def wait_machine(self, pred: Any, name: str = "work-laptop", timeout: float = 20.0,
                     what: str = "") -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        m = None
        while time.monotonic() < deadline:
            m = self.machine_info(name)
            if m is not None and pred(m):
                return m
            time.sleep(0.05)
        raise AssertionError(f"machine {name} never got {what or 'there'}: {m}")

    # ---------------------------------------------------------- the machine
    def cli(self, *args: str, home: Path | None = None, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "switchboard", "--home", str(home or self.machine), *args],
                              env=child_env(**self.env_extra), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, start_new_session=True)

    def join(self, code: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.cli("remote", "join", self.origin, code, "--test-mode", "--no-start", *extra)

    def start_dialer(self, home: Path | None = None) -> subprocess.Popen[bytes]:
        home = home or self.machine
        out = open(home / "dialer-test.out", "ab")
        p = subprocess.Popen([sys.executable, "-m", "switchboard", "--home", str(home), "start", "--foreground",
                              "--test-mode"], env=child_env(**self.env_extra), stdin=subprocess.DEVNULL, stdout=out,
                             stderr=out, start_new_session=True)
        out.close()
        self.dialers.append(p)
        return p

    def dialer_output(self, home: Path | None = None) -> str:
        try:
            return ((home or self.machine) / "dialer-test.out").read_text(errors="replace")
        except OSError:
            return ""

    def paired(self, name: str = "work-laptop") -> subprocess.CompletedProcess[str]:
        """A code, then `remote join` with it (the machine pending)."""
        r = self.join(self.code(name))
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def linked(self, name: str = "work-laptop") -> subprocess.Popen[bytes]:
        """Paired, approved and dialed in: the link up."""
        self.paired(name)
        self.approve(name)
        p = self.start_dialer()
        self.wait_machine(lambda m: m["state"] == "up", name, what="up")
        return p

    @property
    def sock(self) -> Path:
        return self.machine_paths.sock
