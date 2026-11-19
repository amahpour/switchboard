"""A webhook receiver for the webhook tests (#723, DESIGN.md §62): an HTTPS server on 127.0.0.1 with a
self-signed certificate for ``hook.example.com``, that records every POST it gets and answers with the status
the test sets (a 302 sends a Location the test can see was not followed). ``context`` trusts its certificate,
for the notifier's client side."""

from __future__ import annotations

import datetime
import json
import ssl
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

HOST = "hook.example.com"


class TlsHook:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []  # {"path", "body"} for each POST
        self.hits: list[str] = []  # every path requested, of any method
        self.status = 200
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
            .sign(key, hashes.SHA256())
        )
        self._dir = tempfile.TemporaryDirectory()
        d = Path(self._dir.name)
        (d / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (d / "k.pem").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(d / "c.pem", d / "k.pem")
        self.context = ssl.create_default_context(cafile=str(d / "c.pem"))
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                outer.hits.append(self.path)
                outer.posts.append({"path": self.path, "body": json.loads(raw or b"null")})
                self.send_response(outer.status)
                if 300 <= outer.status < 400:
                    self.send_header("Location", "/followed")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:
                outer.hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a: Any) -> None:  # silent
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.socket = server_ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self._t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._t.start()

    @property
    def url(self) -> str:
        """The address a person would paste: a made-up secret path and query."""
        return f"https://{HOST}:{self.port}/services/T000/B000/secretsecret?token=s3cr3t"

    def resolve(self, host: str, port: int) -> list[str]:
        if host == HOST:
            return ["127.0.0.1"]
        return [host] if host.replace(".", "").isdigit() else []  # an address as typed resolves to itself

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._dir.cleanup()
