"""A hosted relay trusts a named private CA without weakening TLS (DESIGN.md §54, #758)."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from switchboard.meeting.relay import BrokerClient, Secret, Unreachable, UsageError

KEY = Secret("sbc_" + "ab" * 32)


@pytest.fixture
async def private_server(tmp_path: Path) -> AsyncIterator[tuple[str, Path]]:
    """A localhost certificate signed by a new CA, never installed in the system store."""
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path, cert_path, key_path = (tmp_path / name for name in ("ca.pem", "server.pem", "key.pem"))
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    handlers: list[asyncio.Task[None]] = []

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.append(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            await reader.readexactly(2)  # the request body is {}
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 12\r\nConnection: close\r\n\r\n{"ok": true}')
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"https://localhost:{port}", ca_path
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.wait_for(asyncio.gather(*handlers), 5)


async def test_named_ca_reaches_private_broker_and_still_checks_hostname(
    private_server: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CA adds trust: the same server fails without it, works with it, and still rejects a wrong name.
    A certificate error must tell the person how to pass their CA, without repeating library error text."""
    url, ca_path = private_server
    monkeypatch.setenv("SWITCHBOARD_RELAY_CA_FILE", str(ca_path))
    client = BrokerClient(url, KEY)
    reply = await client.post("/x", {}, 5)
    assert (reply.status, reply.body) == (200, {"ok": True})
    with pytest.raises(Unreachable, match="--ca-file"):
        await BrokerClient(url.replace("localhost", "127.0.0.1"), KEY).post("/x", {}, 5)


async def test_certificate_error_explains_how_to_name_a_ca(
    private_server: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default context rejects a private issuer and gives a fixed, actionable diagnostic."""
    url, _ = private_server
    monkeypatch.delenv("SWITCHBOARD_RELAY_CA_FILE", raising=False)
    with pytest.raises(Unreachable, match="--ca-file") as error:
        await BrokerClient(url, KEY).post("/x", {}, 5)
    assert KEY.reveal() not in str(error.value)
    assert "localhost" not in str(error.value)


@pytest.mark.parametrize("kind", ["missing", "invalid"])
def test_unusable_ca_is_an_actionable_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """A broken mount or invalid PEM must stop setup, rather than silently using only system trust."""
    ca_path = tmp_path / "ca.pem"
    if kind == "invalid":
        ca_path.write_text("not a certificate")
    monkeypatch.setenv("SWITCHBOARD_RELAY_CA_FILE", str(ca_path))
    with pytest.raises(UsageError, match="CA file"):
        BrokerClient("https://example.com", KEY)
