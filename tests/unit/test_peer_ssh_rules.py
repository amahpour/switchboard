"""The two SSH rules of the human side of broker.sock (DESIGN.md §27.5.7, built in M8a).

- A relay peer (the process on the broker socket is ssh, sshd, socat, nc, ...)
  never gets a human role: through any socket forward the broker's kernel peer
  is the relay, not the program behind it. No setting relaxes this.
- A remote-login ancestor (sshd, dropbear, mosh-server, ... anywhere above the
  caller, by program name) refuses human_cli and login unless
  ``[security] allow_ssh_cli``. It is named only to a chain that would otherwise
  be human; the caller's own argv (the message text) is never looked at.

Chains are injected (``chain_fn``, ``argv_fn``, ``tty_fn``), as the
``below_pytest`` technique of test_peer.py does for real processes, so the
verdict never depends on whatever runs pytest itself.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from itertools import takewhile
from pathlib import Path

import pytest

from switchboard.broker import proc
from switchboard.broker.peer import (
    Peer,
    PeerPolicy,
    ProcessPeerPolicy,
    relay_name,
    remote_login_name,
)
from switchboard.broker.proc import ProcInfo
from switchboard.broker.rpc import RpcError, RpcServer

CLI = "/opt/sb/bin/python3 -I -m switchboard say #build hi"
MAC_TAIL = [
    "-zsh",
    "login -pf alice",
    "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal",
    "/sbin/launchd",
]
LINUX_TAIL = [
    "/bin/bash",
    "/usr/libexec/gnome-terminal-server",
    "/usr/lib/systemd/systemd --user",
    "/sbin/init splash",
]
TMUX_TAIL = ["-bash", "tmux new -s work", "/sbin/init"]


def chain_of(argvs: list[str]) -> list[ProcInfo]:
    n = len(argvs)
    uid = os.getuid()
    return [
        ProcInfo(
            pid=1000 + i,
            ppid=(1001 + i) if i < n - 1 else 0,
            start=float(i + 1),
            uid=uid,
            comm=os.path.basename(a.split()[0]) if a else "",
        )
        for i, a in enumerate(argvs)
    ]


def policy(
    argvs: list[str], *, allow_ssh_cli: bool = False, tty: str | None = "ttys003", complete: bool = True
) -> tuple[ProcessPeerPolicy, Peer]:
    chain = chain_of(argvs)
    by_pid = {p.pid: a for p, a in zip(chain, argvs)}
    pol = ProcessPeerPolicy(
        chain_fn=lambda pid: (chain, complete),
        argv_fn=lambda ps: {p.pid: by_pid.get(p.pid, "") for p in ps},
        tty_fn=lambda pid: tty,
        allow_ssh_cli=allow_ssh_cli,
    )
    return pol, Peer(pid=1000, uid=os.getuid(), start=1.0)


RELAYS = {
    "ssh": "ssh -N -R /tmp/pi-side.sock:/home/alice/.switchboard/run/broker.sock alice@fpga-pi.local",
    "sshd-session: alice@notty": "sshd-session: alice@notty",
    "socat": "/usr/bin/socat UNIX-LISTEN:/tmp/x.sock,fork UNIX-CONNECT:/tmp/b.sock",
    "nc": "nc -U /tmp/b.sock",
    "ncat": "/usr/bin/ncat -U /tmp/b.sock",
    "autossh": "/usr/bin/autossh -M 0 -N -R /tmp/p.sock:/tmp/b.sock pi",
    "dbclient": "dbclient -R /tmp/p.sock:/tmp/b.sock pi",
}


@pytest.mark.parametrize("allow", [False, True], ids=["default", "allow_ssh_cli"])
@pytest.mark.parametrize("title", list(RELAYS))
def test_relay_peer_is_not_human(title: str, allow: bool) -> None:
    pol, peer = policy([RELAYS[title], *MAC_TAIL], allow_ssh_cli=allow)
    assert not pol.human_cli_allowed(peer)
    assert not pol.login_allowed(peer)
    assert not pol.human_allowed(peer)
    why = pol.refusal(peer)
    assert why is not None and relay_name(RELAYS[title]) in why


REMOTE_LOGINS = [
    "sshd: alice@pts/0",
    "sshd-session: alice [priv]",
    "/usr/sbin/sshd -D",
    "sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups",
    "dropbear",
    "mosh-server",
]


@pytest.mark.parametrize("title", REMOTE_LOGINS)
def test_remote_login_ancestor_is_not_human(title: str) -> None:
    pol, peer = policy([CLI, "-bash", title, "/sbin/init"])
    assert not pol.human_cli_allowed(peer)
    assert not pol.login_allowed(peer)
    why = pol.refusal(peer)
    assert why is not None and "ssh" in why and "allow_ssh_cli" in why


def test_remote_login_anywhere_above_the_caller() -> None:
    """Deep in the chain (tmux under an ssh login) counts as much as the direct parent."""
    chain = [
        CLI,
        "-zsh",
        "tmux: server",
        "sshd-session: alice@pts/3",
        "sshd-session: alice [priv]",
        "sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups",
        "/sbin/init",
    ]
    pol, peer = policy(chain)
    assert not pol.human_cli_allowed(peer)
    assert remote_login_name("/usr/libexec/sshd-session -R") == "sshd"  # before it sets its title


def test_allow_ssh_cli_admits_ssh_ancestry_never_a_relay_peer() -> None:
    for title in REMOTE_LOGINS:
        pol, peer = policy([CLI, "-bash", title, "/sbin/init"], allow_ssh_cli=True)
        assert pol.human_cli_allowed(peer), title
        assert pol.login_allowed(peer), title  # with a tty
        assert pol.refusal(peer) is None
    # a relay peer under an ssh login: still refused
    pol, peer = policy([RELAYS["socat"], "-bash", "sshd: alice@pts/0", "/sbin/init"], allow_ssh_cli=True)
    assert not pol.human_cli_allowed(peer) and not pol.login_allowed(peer)
    assert "socat" in (pol.refusal(peer) or "")
    # and an agent anywhere above still refuses, whatever allow_ssh_cli says
    pol, peer = policy(
        [CLI, "-bash", "/opt/homebrew/bin/claude", "sshd: alice@pts/0", "/sbin/init"], allow_ssh_cli=True
    )
    assert not pol.human_cli_allowed(peer)


def test_login_refused_under_sshd_even_with_tty() -> None:
    chain = [
        CLI.replace(" say #build hi", " login"),
        "-bash",
        "sshd-session: alice@pts/0",
        "sshd-session: alice [priv]",
        "sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups",
        "/sbin/init",
    ]
    pol, peer = policy(chain, tty="pts/0")
    assert not pol.login_allowed(peer)
    assert not pol.human_cli_allowed(peer)
    pol, peer = policy(chain, tty="pts/0", allow_ssh_cli=True)
    assert pol.login_allowed(peer)
    pol, peer = policy(chain, tty=None, allow_ssh_cli=True)
    assert pol.human_cli_allowed(peer) and not pol.login_allowed(peer)  # the tty rule still applies


@pytest.mark.parametrize("tail", [MAC_TAIL, LINUX_TAIL, TMUX_TAIL], ids=["macos", "linux", "tmux"])
def test_local_terminal_chain_still_human(tail: list[str]) -> None:
    pol, peer = policy([CLI, *tail])
    assert pol.human_cli_allowed(peer)
    assert pol.login_allowed(peer)
    assert pol.refusal(peer) is None
    pol, peer = policy([CLI, *tail], tty=None)
    assert pol.human_cli_allowed(peer) and not pol.login_allowed(peer)
    # the earlier rules are unchanged: an incomplete chain or an agent above is refused, without an ssh reason
    pol, peer = policy([CLI, *tail], complete=False)
    assert not pol.human_cli_allowed(peer) and pol.refusal(peer) is None
    pol, peer = policy([CLI, "/opt/homebrew/bin/claude", *tail])
    assert not pol.human_cli_allowed(peer) and pol.refusal(peer) is None


@pytest.mark.parametrize(
    "text",
    [
        "I restarted /usr/sbin/sshd",
        "is /usr/bin/mosh-server installed",
        "see /usr/sbin/dropbear -F",
        "sshd: alice@pts/0",
        "use /usr/bin/ssh",
    ],
)
@pytest.mark.parametrize("tail", [MAC_TAIL, LINUX_TAIL], ids=["macos", "linux"])
def test_message_text_is_never_a_remote_login(tail: list[str], text: str) -> None:
    """A human at a local terminal writing about sshd is still the human: only the
    program names of the ancestors count, never arguments, the caller's or a wrapper's."""
    cli = f"/opt/sb/bin/python3 -I -m switchboard say #build {text}"
    pol, peer = policy([cli, *tail])
    assert pol.human_cli_allowed(peer) and pol.refusal(peer) is None
    # `uv run switchboard say ...`: the uv process above the CLI carries the text too
    pol, peer = policy([cli, f"/opt/homebrew/bin/uv run switchboard say #build {text}", *tail])
    assert pol.human_cli_allowed(peer) and pol.refusal(peer) is None
    pol, peer = policy([cli, f"/bin/bash -c switchboard say #build {text}", *tail])
    assert pol.human_cli_allowed(peer) and pol.refusal(peer) is None


def test_agent_under_ssh_gets_the_agent_message_not_the_setting() -> None:
    """An agent the owner started inside an ssh login is refused as an agent: telling it
    to set allow_ssh_cli would not help it and would weaken the rule for every shell key."""
    chain = [
        CLI,
        "zsh -c switchboard cmd #build /budget 999",
        "/opt/homebrew/bin/claude",
        "-zsh",
        "sshd-session: alice@pts/3",
        "sshd-session: alice [priv]",
        "/sbin/launchd",
    ]
    for allow in (False, True):
        pol, peer = policy(chain, allow_ssh_cli=allow)
        assert not pol.human_cli_allowed(peer) and not pol.login_allowed(peer)
        assert pol.refusal(peer) is None, allow
    # an incomplete chain under ssh keeps its old message too
    pol, peer = policy([CLI, "-bash", "sshd: alice@pts/0", "/sbin/init"], complete=False)
    assert not pol.human_cli_allowed(peer) and pol.refusal(peer) is None
    # a relay peer is named whatever is above it (its reason suggests no setting)
    pol, peer = policy([RELAYS["ssh"], "-zsh", "/opt/homebrew/bin/claude", "-zsh", "/sbin/launchd"])
    why = pol.refusal(peer)
    assert why is not None and "relay" in why and "allow_ssh_cli" not in why
    # and the RPC layer then gives the agent message
    rpc = RpcServer.__new__(RpcServer)

    class C:
        mcp = None

    rpc.policy, peer = policy(chain)
    conn = C()
    conn.peer = peer  # type: ignore[attr-defined]
    with pytest.raises(RpcError) as e:
        rpc._authorize(conn, "human_cli", "human.say")  # type: ignore[arg-type]
    assert "agent" in e.value.message and "allow_ssh_cli" not in e.value.message


def test_refusal_message_names_the_reason() -> None:
    relay, peer_r = policy([RELAYS["ssh"], *MAC_TAIL])
    why = relay.refusal(peer_r)
    assert why is not None and "ssh" in why and "terminal on this machine" in why
    assert "allow_ssh_cli" not in why  # no setting relaxes the relay rule
    login, peer_l = policy([CLI, "-bash", "sshd: alice@pts/0", "/sbin/init"])
    why = login.refusal(peer_l)
    assert why is not None and "arrived through ssh" in why and "[security] allow_ssh_cli = true" in why
    assert "terminal on this machine" in why
    assert PeerPolicy().refusal(peer_l) is None  # the base policy has no reason to give

    # the RPC layer puts it in the forbidden message, for human_cli and login alike
    rpc = RpcServer.__new__(RpcServer)

    class C:
        mcp = None

    for pol, peer, want in ((relay, peer_r, "ssh"), (login, peer_l, "allow_ssh_cli")):
        rpc.policy = pol
        conn = C()
        conn.peer = peer  # type: ignore[attr-defined]
        for role, method in (("human_cli", "human.say"), ("login", "human.login_link")):
            with pytest.raises(RpcError) as e:
                rpc._authorize(conn, role, method)  # type: ignore[arg-type]
            assert e.value.code == "forbidden"
            assert e.value.message.startswith(f"{method} arrived through ssh") and want in e.value.message
    # an agent chain keeps its old message
    rpc.policy, peer = policy([CLI, "/opt/homebrew/bin/claude", *MAC_TAIL])
    conn = C()
    conn.peer = peer  # type: ignore[attr-defined]
    with pytest.raises(RpcError) as e:
        rpc._authorize(conn, "human_cli", "human.say")  # type: ignore[arg-type]
    assert "agent" in e.value.message and "ssh" not in e.value.message


@pytest.mark.parametrize(
    "argv,want",
    [
        ("ssh", "ssh"),
        ("/usr/bin/ssh -T host", "ssh"),
        ("sshd: alice@notty", "sshd"),
        ("sshd-session: alice@notty", "sshd-session"),
        ("/usr/sbin/sshd -D", "sshd"),
        ("/usr/libexec/sshd-session -R", "sshd-session"),
        ("netcat -U /tmp/b.sock", "netcat"),
        ("/usr/sbin/dropbear -F", "dropbear"),
        ("/usr/bin/python3 /tmp/yk-relay/ssh /tmp/l.sock /tmp/b.sock", "ssh"),  # a script named ssh
        ("/usr/bin/python3.13 -I /tmp/x/socat", "socat"),
        ("/bin/sh /tmp/x/nc", "nc"),
        ("ssh: /home/alice/.ssh/cm-alice@fpga-pi:22 [mux]", "ssh"),  # a ControlMaster serving -R forwards
        ("busybox nc -lk -U /tmp/x.sock", "nc"),
        (
            "/Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python /tmp/x/ssh",
            "ssh",
        ),
        # not relays
        ("/opt/sb/bin/python3 -I -m switchboard say #r look at /usr/bin/ssh now", None),
        ("/opt/sb/bin/python3 /opt/sb/bin/switchboard say #r ssh", None),
        ("ssh-agent -l", None),
        ("/usr/bin/ssh-keygen -F host", None),
        ("sshfs host: /mnt", None),
        ("python3: a retitled process", None),
        ("tmux: server", None),
        ("-zsh", None),
        ("", None),
    ],
)
def test_relay_name(argv: str, want: str | None) -> None:
    assert relay_name(argv) == want


@pytest.mark.parametrize(
    "argv,want",
    [
        ("sshd: alice@pts/0", "sshd"),
        ("sshd-session: alice [priv]", "sshd"),
        ("sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups", "sshd"),
        ("/usr/sbin/sshd -D", "sshd"),
        ("sshd", "sshd"),
        ("/usr/sbin/dropbear -F -R", "dropbear"),
        ("mosh-server new -s -c 256 -l LANG=en_US.UTF-8", "mosh-server"),
        ("/usr/bin/mosh-server", "mosh-server"),
        ("/usr/lib/openssh/sshd-session", "sshd"),
        ("/usr/sbin/tinysshd -v /etc/tinyssh/sshkeydir", "tinysshd"),
        ("/usr/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state", "tailscaled"),
        ("tailscaled be-child ssh --uid=1000", "tailscaled"),
        ("etserver --daemon", "etserver"),
        ("in.telnetd: 192.0.2.10", "telnetd"),
        ("/usr/sbin/telnetd -debug", "telnetd"),
        # not remote logins: arguments never count
        ("vim /etc/ssh/sshd_config", None),
        ("/opt/sb/bin/python3 -I -m switchboard say #r I restarted /usr/sbin/sshd", None),
        ("/opt/homebrew/bin/uv run switchboard say #r is /usr/bin/mosh-server installed", None),
        ("/bin/bash -c switchboard say #r see /usr/sbin/dropbear -F", None),
        ("sudo systemctl restart sshd", None),
        ("tmux: server", None),
        ("sshd_config_check", None),
        ("/usr/bin/ssh host", None),
        ("-zsh", None),
        ("", None),
    ],
)
def test_remote_login_name(argv: str, want: str | None) -> None:
    assert remote_login_name(argv) == want


# ---------------------------------------------------------------- real processes
@pytest.fixture
def listener() -> Iterator[tuple[socket.socket, str]]:
    d = tempfile.mkdtemp(prefix="yk-ssh-", dir="/tmp")
    path = os.path.join(d, "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(4)
    srv.settimeout(10)
    try:
        yield srv, path
    finally:
        srv.close()
        shutil.rmtree(d, ignore_errors=True)


CLIENT = (
    "import socket, sys, time\ns = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); time.sleep(3)\n"
)


def below_pytest(pid: int) -> tuple[list[ProcInfo], bool]:
    chain, _ = proc.ancestry_to_root(pid)
    return list(takewhile(lambda p: p.pid != os.getpid(), chain)), True


def test_real_process_named_ssh_is_a_relay_peer(listener: tuple[socket.socket, str], tmp_path: Path) -> None:
    """The fake_harness trick: a Python script copied to a path ending in /ssh."""
    srv, path = listener
    script = tmp_path / "ssh"
    script.write_text(CLIENT)
    child = subprocess.Popen(
        [sys.executable, str(script), path], stdin=subprocess.DEVNULL, start_new_session=True
    )
    try:
        conn, _ = srv.accept()
        peer = Peer.from_socket(conn)
        conn.close()
        pol = ProcessPeerPolicy(chain_fn=below_pytest)
        assert not pol.human_cli_allowed(peer)
        assert "ssh" in (pol.refusal(peer) or "")
        # the same client under another name is human (the control case)
        plain = tmp_path / "client.py"
        plain.write_text(CLIENT)
        other = subprocess.Popen(
            [sys.executable, str(plain), path], stdin=subprocess.DEVNULL, start_new_session=True
        )
        try:
            conn, _ = srv.accept()
            peer2 = Peer.from_socket(conn)
            conn.close()
            assert pol.human_cli_allowed(peer2) and pol.refusal(peer2) is None
        finally:
            other.kill()
            other.wait()
    finally:
        child.kill()
        child.wait()
