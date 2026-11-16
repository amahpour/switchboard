"""A hosted relay trusts a named private CA without weakening TLS (DESIGN.md §54, #758)."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from switchboard.meeting.relay import BrokerClient, Secret, Unreachable, UsageError

KEY = Secret("sbc_" + "ab" * 32)


async def _respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Answer a request, closing the connection even when the client rejects its certificate."""
    try:
        await reader.readuntil(b"\r\n\r\n")
        await reader.readexactly(2)  # the request body is {}
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 12\r\nConnection: close\r\n\r\n{"ok": true}')
        await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        # truststore can reject the peer after TLS completes, before sending an HTTP request.
        pass
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


@pytest.mark.parametrize("reset", [False, True], ids=["eof", "reset"])
async def test_tls_server_handles_client_rejection(reset: bool) -> None:
    """Native trust checks can reject TLS after the server handshake, causing EOF or a reset before HTTP.
    Neither may escape the handler at teardown, and a reset while closing must still be awaited."""
    reader = asyncio.StreamReader()
    if reset:
        reader.set_exception(ConnectionResetError("client rejected the certificate"))
    else:
        reader.feed_eof()
    writer = Mock(spec=asyncio.StreamWriter)
    writer.wait_closed = AsyncMock(side_effect=ConnectionResetError("client closed"))
    await _respond(reader, writer)
    writer.close.assert_called_once_with()
    writer.wait_closed.assert_awaited_once_with()
    writer.write.assert_not_called()


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
        # Apple's native trust policy requires an explicit TLS server purpose.
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
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

    def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.append(asyncio.create_task(_respond(reader, writer)))

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
