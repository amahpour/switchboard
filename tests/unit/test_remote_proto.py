"""The link protocol (DESIGN.md §27.4.4-§27.4.6): frames, the noise-tolerant hello, ages."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import pytest

from switchboard.broker import peer as P
from switchboard.broker.proc import ProcInfo
from switchboard.remote import proto

ATTEST = {"harness": "claude", "mcp": [1000004242, 1727000000.5], "agent": [1000004241, 1727000000.25],
          "evidence": "parent:claude+registry", "tier_note": None, "claude_socket": "/tmp/x/inbox.sock"}
CHAIN = [[1000000100, 1.0, "-"], [1000000099, 2.0, "claude"], [1000000001, 0.5, "?"]]

GOOD_S2B: dict[str, dict[str, Any]] = {
    "hello": proto.hello(version="0.3.0", name="fpga-pi", now=1790000000.0, hook_state="ok (1 copy)",
                         test_mode=False, harden="prctl"),
    "open": proto.open_(7),
    "req": proto.req(7, {"id": 1, "method": "mcp.hello", "params": {}}, {"attest": ATTEST}),
    "req_chain": proto.req(7, {"id": 2, "method": "hook.event", "params": {}}, {"chain": CHAIN}),
    "req_plain": proto.req(7, {"id": 3, "method": "agent.say", "params": {"text": "hi"}}),
    # the satellite's own report after its last-mile check dropped a push (M8d)
    "req_lastmile": proto.req(7, {"id": -1, "method": "mcp.posted",
                                  "params": {"batch_id": 4, "ok": False, "err": proto.STALE_STATUS}},
                              {"lastmile": True}),
    "close": proto.close(7),
    "alive": proto.alive(3, [(1000004242, 1727000000.5)]),
    "reg": {"t": "reg", "views": [[1000004241, 1.5, "idle", 12.0], [1000004243, 2.0, None, None]], "read_age": 0.01},
    "pong": proto.pong(9),
    "status": proto.status("ok (1 copy)"),
    "bye": proto.bye("replaced"),
}
GOOD_B2S: dict[str, dict[str, Any]] = {
    "welcome": proto.welcome(version="0.3.0", link="0123456789abcdef", rooms=["#fpga"], harnesses=["claude"],
                             limits={"max_conns": 64}),
    "refuse": proto.refuse("proto", "link protocol 2 is not 1"),
    "out": proto.out(7, {"id": 1, "result": {}}, {"pid": 1000004241, "start": 1.5, "want": "idle"}),
    "close": proto.close(7),
    "watch": proto.watch(3, [(1000004241, 1.5)], [(1000004241, 1.5, "/tmp/x/inbox.sock")]),
    "ping": proto.ping(9),
}


def mutate(frame: dict[str, Any], **changes: Any) -> dict[str, Any]:
    f = json.loads(json.dumps(frame))
    for k, v in changes.items():
        if v is KeyError:
            f.pop(k, None)
        else:
            f[k] = v
    return f


BAD_S2B: dict[str, dict[str, Any]] = {
    "unknown_type": {"t": "welcome"},  # a b->s type from the satellite
    "no_type": {"c": 1},
    "extra_field": mutate(GOOD_S2B["open"], extra=1),
    "bool_as_int": mutate(GOOD_S2B["open"], c=True),
    "float_as_int": mutate(GOOD_S2B["open"], c=1.0),
    "conn_zero": mutate(GOOD_S2B["open"], c=0),
    "conn_huge": mutate(GOOD_S2B["open"], c=2**31),
    "line_not_object": mutate(GOOD_S2B["req"], line=[1]),
    "facts_both": mutate(GOOD_S2B["req"], facts={"attest": ATTEST, "chain": CHAIN}),
    "facts_other": mutate(GOOD_S2B["req"], facts={"argv": ["claude"]}),
    "lastmile_false": mutate(GOOD_S2B["req_lastmile"], facts={"lastmile": False}),
    "lastmile_int": mutate(GOOD_S2B["req_lastmile"], facts={"lastmile": 1}),
    "lastmile_and_chain": mutate(GOOD_S2B["req_lastmile"], facts={"lastmile": True, "chain": CHAIN}),
    "attest_evidence": mutate(GOOD_S2B["req"], facts={"attest": {**ATTEST, "evidence": "trust me"}}),
    "attest_mismatch": mutate(GOOD_S2B["req"], facts={"attest": {**ATTEST, "harness": "codex"}}),
    "attest_pid_bool": mutate(GOOD_S2B["req"], facts={"attest": {**ATTEST, "mcp": [True, 1.0]}}),
    "attest_pid_neg": mutate(GOOD_S2B["req"], facts={"attest": {**ATTEST, "agent": [-5, 1.0]}}),
    "attest_socket_rel": mutate(GOOD_S2B["req"], facts={"attest": {**ATTEST, "claude_socket": "x.sock"}}),
    "attest_socket_long": mutate(GOOD_S2B["req"], facts={"attest": {**ATTEST, "claude_socket": "/" + "a" * 1023}}),
    "attest_socket_missing": mutate(GOOD_S2B["req"], facts={"attest": {**ATTEST, "claude_socket": None}}),
    "attest_socket_unverified": mutate(GOOD_S2B["req"], facts={"attest": {
        **ATTEST, "harness": "unknown", "evidence": "parent:claude,registry-mismatch"}}),
    "attest_note": mutate(GOOD_S2B["req"], facts={"attest": {**ATTEST, "tier_note": "vouched for"}}),
    "attest_extra": mutate(GOOD_S2B["req"], facts={"attest": {**ATTEST, "argv": "claude"}}),
    "chain_argv": mutate(GOOD_S2B["req_chain"], facts={"chain": [[1000000100, 1.0, "/usr/bin/claude"]]}),
    "chain_long": mutate(GOOD_S2B["req_chain"], facts={"chain": [[i + 1, 1.0, "-"] for i in range(9)]}),
    "chain_empty": mutate(GOOD_S2B["req_chain"], facts={"chain": []}),
    "alive_start_str": mutate(GOOD_S2B["alive"], dead=[[5, "1.0"]]),
    "alive_neg_start": mutate(GOOD_S2B["alive"], dead=[[5, -1.0]]),
    "reg_status_long": mutate(GOOD_S2B["reg"], views=[[5, 1.0, "x" * 33, None]]),
    "bye_why": mutate(GOOD_S2B["bye"], why="because"),
    "status_long": mutate(GOOD_S2B["status"], hook_state="x" * 201),
    "hello_harden": mutate(GOOD_S2B["hello"], harden="maybe"),
    "hello_name": mutate(GOOD_S2B["hello"], name="Bad Name"),
    "hello_missing": mutate(GOOD_S2B["hello"], hook_state=KeyError),
    "hello_now_bool": mutate(GOOD_S2B["hello"], now=True),
    "hello_proto_str": mutate(GOOD_S2B["hello"], proto="1"),
}


@pytest.mark.parametrize("name", sorted(GOOD_S2B))
def test_frames_validate_good_s2b(name: str) -> None:
    f = GOOD_S2B[name]
    got = proto.decode(proto.encode(f), "s2b")
    assert got["t"] == f["t"]
    if f["t"] not in proto.B2S:  # close goes both ways; nothing else does
        with pytest.raises(proto.FrameError):
            proto.decode(proto.encode(f), "b2s")


@pytest.mark.parametrize("name", sorted(GOOD_B2S))
def test_frames_validate_good_b2s(name: str) -> None:
    f = GOOD_B2S[name]
    assert proto.decode(proto.encode(f), "b2s")["t"] == f["t"]


@pytest.mark.parametrize("name", sorted(BAD_S2B))
def test_frames_validate(name: str) -> None:
    with pytest.raises(proto.FrameError):
        proto.validate(BAD_S2B[name], "s2b")


def test_frames_validate_nan_infinity_and_json() -> None:
    for text in (b'{"t":"pong","n":NaN}', b'{"t":"alive","n":1,"dead":[[5,Infinity]]}',
                 b'{"t":"hello","proto":1,"now":-Infinity}', b"not json", b"[1,2]", b'"x"'):
        with pytest.raises(proto.FrameError):
            proto.decode(text, "s2b")
    with pytest.raises(ValueError):
        proto.encode({"t": "pong", "n": math.nan})


def test_frames_validate_oversize() -> None:
    big = proto.req(1, {"id": 1, "method": "agent.say", "params": {"text": "x" * (proto.MAX_FRAME + 10)}})
    with pytest.raises(proto.FrameError) as ei:
        proto.encode(big)
    assert ei.value.code == "oversize"
    with pytest.raises(proto.FrameError) as ei:
        proto.decode(b"{" + b" " * (proto.MAX_FRAME + 5) + b"}", "s2b")
    assert ei.value.code == "oversize"
    # just under the limit passes (a 1 MiB client line fits with room to spare)
    ok = proto.req(1, {"id": 1, "method": "agent.say", "params": {"text": "x" * (1 << 20)}})
    assert proto.decode(proto.encode(ok), "s2b")["c"] == 1


def test_hello_of_another_proto_is_named_not_malformed() -> None:
    # a satellite of another protocol is refused by name (blocked(proto)), whatever its hello holds
    got = proto.validate({"t": "hello", "proto": 2, "version": "9.0.0", "whatever": [1]}, "s2b")
    assert got == {"t": "hello", "proto": 2, "version": "9.0.0"}


def test_evidence_vocabulary_is_what_verify_mcp_peer_says() -> None:
    """Every (harness, evidence, tier_note) verify_mcp_peer can produce passes check_attest."""
    me = ProcInfo(pid=4000, ppid=3000, start=10.0, uid=0)
    parent = ProcInfo(pid=3000, ppid=2000, start=9.0, uid=0)
    grand = ProcInfo(pid=2000, ppid=1, start=8.0, uid=0)
    seen = set()
    for claimed in ("test", "claude", "codex", "devin", "cursor", "unknown"):
        for argv in ("claude", "codex app-server", "devin acp", "cursor-agent", "bash", ""):
            ident = P.verify_mcp_peer(
                P.Peer(pid=4000, uid=0, start=10.0), claimed, claude_socket=None, sessions_dir="/nonexistent",
                test_mode=True, chain_fn=lambda pid, depth: [me, parent, grand],
                argv_fn=lambda procs, a=argv: {3000: a, 2000: a})
            a = {"harness": ident.harness, "mcp": [ident.mcp_pid, ident.mcp_start],
                 "agent": [ident.agent_pid, ident.agent_start], "evidence": ident.evidence,
                 "tier_note": ident.tier_note, "claude_socket": ident.claude_socket}
            proto.check_attest(a)
            seen.add(ident.evidence)
    # the registry match, with its socket
    proto.check_attest({"harness": "claude", "mcp": [4000, 10.0], "agent": [3000, 9.0],
                        "evidence": "parent:claude+registry", "tier_note": None, "claude_socket": "/s"})
    assert seen | {"parent:claude+registry"} == set(proto.EVIDENCE)


# ------------------------------------------------------------------ hello
async def _read(lines: list[bytes], **kw: Any) -> dict[str, Any]:
    r = asyncio.StreamReader()
    for ln in lines:
        r.feed_data(ln)
    r.feed_eof()
    return await proto.read_hello(r, **kw)


async def test_hello_skips_shell_noise() -> None:
    hello = proto.encode(GOOD_S2B["hello"])
    noise = [b"Welcome to fpga-pi!\n", b"\x1b[32mlast login: today\x1b[0m\n", b"{not json either}\n",
             b'{"t": "motd"}\n', b"\n"] * 12
    assert len(noise) <= proto.HELLO_MAX_LINES
    got = await _read(noise + [hello])
    assert got["t"] == "hello" and got["name"] == "fpga-pi"
    # a bye before any hello is returned too (a satellite that can't serve this home)
    assert (await _read([b"noise\n", proto.encode(proto.bye("local_broker"))]))["t"] == "bye"


async def test_noise_over_limit_is_shell_noise() -> None:
    hello = proto.encode(GOOD_S2B["hello"])
    with pytest.raises(proto.ShellNoise):
        await _read([b"x\n"] * (proto.HELLO_MAX_LINES + 1) + [hello])
    with pytest.raises(proto.ShellNoise):  # 64 KiB of noise in few lines
        await _read([b"y" * 40000 + b"\n", b"z" * 40000 + b"\n", hello])
    with pytest.raises(proto.LinkEOF):
        await _read([b"only noise\n"])
    r = asyncio.StreamReader()  # nothing at all within the timeout
    with pytest.raises(TimeoutError):
        await proto.read_hello(r, timeout=0.05)


def test_hello_scanner_counts() -> None:
    s = proto.HelloScanner(max_lines=2, max_bytes=100)
    assert s.feed(b"a\n") is None and s.feed(b"b\n") is None
    with pytest.raises(proto.ShellNoise):
        s.feed(b"c\n")


# -------------------------------------------------------------------- ages
@pytest.mark.parametrize("field", ["t_age", "t_post_age", "since_age", "read_age"])
def test_ages_rebased_and_clamped(field: str) -> None:
    lo, hi = proto.AGE_CLAMPS[field]
    recv = 1_790_000_000.0
    assert proto.rebase_field(field, 0.25, recv) == pytest.approx(recv - 0.25)
    assert proto.rebase_field(field, -3600.0, recv) == recv - lo  # a future time is "now"
    assert proto.rebase_field(field, hi + 3600.0, recv) == recv - hi
    for bad in (None, "1", True, math.nan, math.inf):
        assert proto.rebase_field(field, bad, recv) is None


def test_old_registry_since_not_clamped_to_30s() -> None:
    # an idle status from 5 minutes ago stays 5 minutes old: clamped short, it would look like
    # a fresh idle after the last hook, i.e. a false Esc-ended turn (§27.4.6)
    recv = 1_790_000_000.0
    assert proto.rebase_field("since_age", 300.0, recv) == recv - 300.0
    assert proto.rebase_field("t_age", 300.0, recv) == recv - 30.0


def test_request_times_become_ages_and_back() -> None:
    pi_now = 1_790_003_600.0  # an hour ahead: ages don't care
    params = {"harness": "claude", "event": "Stop", "t": pi_now - 0.4}
    sent = proto.to_ages("hook.event", params, pi_now)
    assert "t" not in sent and sent["t_age"] == pytest.approx(0.4)
    recv = 1_790_000_000.0
    got = proto.from_ages("hook.event", sent, recv)
    assert got["t"] == pytest.approx(recv - 0.4) and "t_age" not in got
    posted = proto.to_ages("mcp.posted", {"batch_id": 3, "ok": True, "t_post": pi_now - 1.0}, pi_now)
    assert proto.from_ages("mcp.posted", posted, recv)["t_post"] == pytest.approx(recv - 1.0)
    # a wall-clock value from the far side is never used, whatever the method
    assert "t" not in proto.from_ages("hook.event", {"t": 5.0}, recv)
    assert "t_post" not in proto.from_ages("mcp.posted", {"t_post": 5.0}, recv)
    assert proto.from_ages("agent.say", {"t": 5.0, "text": "x"}, recv) == {"text": "x"}


# ----------------------------------------------------------------- facts
def test_chain_facts_carry_no_argv() -> None:
    argvs = {1: "/usr/bin/claude --dangerous-flag --api-key sk-canary", 2: "bash -c 'cat ~/.ssh/id_ed25519'",
             3: "", 4: "/opt/devin/bin/devin --x acp"}
    verdicts = [proto.verdict(argvs[i], P.match_agent) for i in (1, 2, 3, 4)]
    assert verdicts == ["claude", "-", "?", "devin"]
    chain = proto.check_chain([[i, float(i), v] for i, v in zip((1, 2, 3, 4), verdicts, strict=True)])
    blob = json.dumps(chain)
    for secret in ("sk-canary", "ssh", "dangerous", "/usr/bin", "/opt"):
        assert secret not in blob
    # a verdict maps back to a canonical argv the broker's matcher reads the same way
    for v, argv in proto.VERDICT_ARGV.items():
        want = v if v in ("claude", "codex", "cursor", "devin") else None
        assert P.match_agent(argv) == want
        assert proto.verdict_argv(v) == argv


def test_request_frame_limit_matches_the_local_line_limit() -> None:
    """A remote request is no larger than a local one: the link's cap is the broker socket's
    line limit plus the envelope (the broker closes a link over it; the satellite refuses)."""
    from switchboard.broker import rpc
    from switchboard.remote import satellite

    assert proto.MAX_LINE == rpc.MAX_LINE == satellite.MAX_LINE
    assert proto.MAX_LINE < proto.MAX_REQ_FRAME < proto.MAX_FRAME
