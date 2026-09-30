"""The keys, signatures, handshake frames and pairing codes of a link a machine dials
(``remote/linkkey.py``, DESIGN.md §31.7)."""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

import pytest

from switchboard.remote import linkkey
from switchboard.remote.config import key_fingerprint

HOST = "sb.example.com"


def nonce(byte: int) -> bytes:
    return bytes([byte]) * linkkey.NONCE_BYTES


# ------------------------------------------------------------------ encoding
def test_base64url_is_strict() -> None:
    raw = bytes(range(32))
    s = linkkey.b64u(raw)
    assert "=" not in s and linkkey.unb64u(s, 32) == raw
    for bad in [s + "=", s[:-1] + "!", s + " ", 5, None, "x" * 300, s[:-2] + "+" + s[-1], s[:-2] + "/" + s[-1]]:
        with pytest.raises(ValueError):
            linkkey.unb64u(bad, 32)
    with pytest.raises(ValueError):
        linkkey.unb64u(linkkey.b64u(raw[:31]), 32)  # the wrong size


# ---------------------------------------------------------------------- keys
def test_a_key_file_is_private_and_its_fingerprint_is_opensshs(tmp_path: Path) -> None:
    p = tmp_path / "link" / linkkey.MACHINE_KEY
    key = linkkey.make_key(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    assert linkkey.pub_raw(linkkey.load_key(p)) == linkkey.pub_raw(key)
    pub = linkkey.pub_raw(key)
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + pub
    # the same as OpenSSH's fingerprint of the same public key (switchboard's own ssh helper computes it)
    assert linkkey.fingerprint(pub) == key_fingerprint("ssh-ed25519 " + base64.b64encode(blob).decode())
    assert linkkey.fingerprint(pub).startswith("SHA256:") and "=" not in linkkey.fingerprint(pub)
    # a new key replaces the old one; the broker's is made once and kept
    assert linkkey.pub_raw(linkkey.make_key(p)) != pub
    b = tmp_path / "link" / linkkey.BROKER_KEY
    first = linkkey.pub_raw(linkkey.load_or_make_key(b))
    assert linkkey.pub_raw(linkkey.load_or_make_key(b)) == first


def test_a_key_file_that_isnt_private_or_isnt_a_key_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "k"
    with pytest.raises(linkkey.KeyFileError, match="missing"):
        linkkey.load_key(p)
    linkkey.make_key(tmp_path / "link" / "k")
    good = tmp_path / "link" / "k"
    os.chmod(good, 0o640)
    with pytest.raises(linkkey.KeyFileError, match="private"):
        linkkey.load_key(good)
    os.chmod(good, 0o600)
    link = tmp_path / "link" / "alias"
    link.symlink_to(good)
    with pytest.raises(linkkey.KeyFileError):
        linkkey.load_key(link)  # never through a link
    junk = tmp_path / "link" / "junk"
    junk.write_bytes(b"not a key")
    os.chmod(junk, 0o600)
    with pytest.raises(linkkey.KeyFileError, match="not a key"):
        linkkey.load_key(junk)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    ecdsa = tmp_path / "link" / "ecdsa"
    ecdsa.write_bytes(ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(ecdsa, 0o600)
    with pytest.raises(linkkey.KeyFileError, match="Ed25519"):
        linkkey.load_key(ecdsa)


# ---------------------------------------------------------------- signatures
def test_a_signature_is_bound_to_its_role_the_host_and_both_nonces(tmp_path: Path) -> None:
    key = linkkey.make_key(tmp_path / "link" / "k")
    pub = linkkey.pub_raw(key)
    nm, nb = nonce(1), nonce(2)
    sig = linkkey.sign(key, "broker", HOST, nm, nb)
    assert linkkey.verify(pub, sig, "broker", HOST, nm, nb)
    assert not linkkey.verify(pub, sig, "machine", HOST, nm, nb)  # the other role's
    assert not linkkey.verify(pub, sig, "broker", "sb.example.org", nm, nb)  # another broker
    assert not linkkey.verify(pub, sig, "broker", HOST, nb, nm)  # the nonces swapped
    assert not linkkey.verify(pub, sig, "broker", HOST, nm, nonce(3))  # a later challenge
    other = linkkey.pub_raw(linkkey.make_key(tmp_path / "link" / "other"))
    assert not linkkey.verify(other, sig, "broker", HOST, nm, nb)
    tampered = linkkey.b64u(bytes([linkkey.unb64u(sig)[0] ^ 1]) + linkkey.unb64u(sig)[1:])
    assert not linkkey.verify(pub, tampered, "broker", HOST, nm, nb)
    assert not linkkey.verify(pub, "not a signature", "broker", HOST, nm, nb)
    with pytest.raises(ValueError):
        linkkey.signed_text("admin", HOST, nm, nb)
    with pytest.raises(ValueError):
        linkkey.signed_text("broker", HOST, b"short", nb)


# ----------------------------------------------------------- handshake frames
def test_handshake_frames_are_checked_strictly(tmp_path: Path) -> None:
    pub = linkkey.b64u(linkkey.pub_raw(linkkey.make_key(tmp_path / "link" / "k")))
    auth = {"t": "auth", "v": 1, "name": "work-laptop", "key": pub, "nm": linkkey.b64u(nonce(1))}
    assert linkkey.check_auth(auth)[0] == "work-laptop"
    for change, code in [({"v": 2}, "bad_version"), ({"name": "Work Laptop"}, "bad_name"), ({"key": "x"}, "bad_key"),
                         ({"nm": linkkey.b64u(b"x" * 8)}, "bad_key"), ({"extra": 1}, "bad_fields"),
                         ({"t": "proof"}, "bad_type")]:
        with pytest.raises(linkkey.HandshakeError) as e:
            linkkey.check_auth({**auth, **change})
        assert e.value.code == code, change
    with pytest.raises(linkkey.HandshakeError):
        linkkey.check_auth(["auth"])
    sig = linkkey.b64u(b"s" * 64)
    ch = {"t": "challenge", "nb": linkkey.b64u(nonce(2)), "bkey": pub, "sig": sig}
    assert linkkey.check_challenge(ch)[2] == sig
    with pytest.raises(linkkey.HandshakeError):
        linkkey.check_challenge({**ch, "sig": linkkey.b64u(b"s" * 63)})
    assert linkkey.check_proof({"t": "proof", "sig": sig}) == sig
    with pytest.raises(linkkey.HandshakeError):
        linkkey.check_proof({"t": "proof", "sig": sig, "more": True})
    assert linkkey.check_refuse(linkkey.refuse("removed", "gone")) == ("removed", "gone")
    with pytest.raises(linkkey.HandshakeError):
        linkkey.check_refuse(linkkey.refuse("because", "no"))  # not a reason the dialer knows
    assert linkkey.FINAL_REFUSALS <= linkkey.REFUSALS and "replaced" not in linkkey.FINAL_REFUSALS


# ------------------------------------------------------------ pairing codes
def test_pairing_codes() -> None:
    codes = {linkkey.make_code() for _ in range(200)}
    assert len(codes) == 200
    for c in codes:
        assert len(c) == 14 and c[4] == c[9] == "-"
        assert all(ch in linkkey.CROCKFORD for ch in c.replace("-", ""))
        assert linkkey.normalize_code(c) == c.replace("-", "")
    assert linkkey.normalize_code(" 7kq4-m2xd-9hva ") == "7KQ4M2XD9HVA"
    assert linkkey.normalize_code("7KQ4 M2XD 9HVA") == "7KQ4M2XD9HVA"
    assert linkkey.normalize_code("7KQ4-M2XD-9HVI") == "7KQ4M2XD9HV1"  # I, L and O read as 1, 1 and 0
    assert linkkey.normalize_code("OOOO-LLLL-IIII") == "000011111111"
    for bad in ["7KQ4-M2XD-9HV", "7KQ4-M2XD-9HVAA", "7KQ4-M2XD-9HVU", "", None, 7, "x" * 50, "7KQ4-M2XD-9HV!"]:
        assert linkkey.normalize_code(bad) is None, bad
    assert linkkey.code_hash("7KQ4M2XD9HVA") != linkkey.code_hash("7KQ4M2XD9HVB")
