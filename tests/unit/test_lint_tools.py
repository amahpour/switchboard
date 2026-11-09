"""scripts/lint.py's pinned tools: a cached archive is checked, not trusted, and a failed download says so.

CI restores `.lint-tools/` between runs so a GitHub release outage doesn't turn every check red. That is
only safe if a restored archive is held to the same sha256 as a fresh download, and if a download that
does fail is not mistaken for a lint finding. CONTRIBUTING.md, "CI" (#667).
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import tarfile
import urllib.error
from pathlib import Path

import pytest

LINT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "lint.py"


@pytest.fixture
def lint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    spec = importlib.util.spec_from_file_location("lint_under_test", LINT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["lint_under_test"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "TOOLS", tmp_path / "tools")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module.sys, "platform", "linux")
    yield module
    sys.modules.pop("lint_under_test", None)


def archive_of(member: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def pin(lint, monkeypatch: pytest.MonkeyPatch, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setitem(
        lint.RELEASES,
        "faketool",
        ("v1", "example/faketool", {"linux-x86_64": ("faketool.tar.gz", digest, "faketool")}),
    )
    return digest


def test_kept_archive_with_the_pinned_digest_needs_no_download(lint, monkeypatch, capsys):
    data = archive_of("faketool", b"real binary")
    pin(lint, monkeypatch, data)
    lint.TOOLS.mkdir()
    (lint.TOOLS / "faketool.tar.gz").write_bytes(data)

    def no_network(*_a, **_k):
        raise AssertionError("a warm cache must not download")

    monkeypatch.setattr(lint.urllib.request, "urlopen", no_network)
    binary = lint.installed_tool("faketool")
    assert binary.read_bytes() == b"real binary"
    assert "no download" in capsys.readouterr().out


def test_a_cached_binary_is_never_trusted_without_its_archive(lint, monkeypatch):
    """A restored cache with a tampered binary or archive is replaced from a verified archive, not run."""
    data = archive_of("faketool", b"real binary")
    pin(lint, monkeypatch, data)
    lint.TOOLS.mkdir()
    (lint.TOOLS / "faketool-v1-linux-x86_64").write_bytes(b"evil")
    (lint.TOOLS / "faketool.tar.gz").write_bytes(b"tampered archive")

    class Response:
        def read(self) -> bytes:
            return data

    monkeypatch.setattr(lint.urllib.request, "urlopen", lambda *_a, **_k: Response())
    assert lint.installed_tool("faketool").read_bytes() == b"real binary"


def test_a_download_with_the_wrong_digest_fails(lint, monkeypatch):
    pin(lint, monkeypatch, archive_of("faketool", b"real binary"))

    class Response:
        def read(self) -> bytes:
            return archive_of("faketool", b"something else")

    monkeypatch.setattr(lint.urllib.request, "urlopen", lambda *_a, **_k: Response())
    with pytest.raises(ValueError, match="checksum mismatch"):
        lint.installed_tool("faketool")


def test_a_failed_download_says_it_is_not_a_lint_finding(lint, monkeypatch):
    pin(lint, monkeypatch, archive_of("faketool", b"real binary"))

    def outage(url, **_k):
        raise urllib.error.HTTPError(url, 504, "Gateway Timeout", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(lint.urllib.request, "urlopen", outage)
    with pytest.raises(SystemExit) as raised:
        lint.installed_tool("faketool")
    message = str(raised.value)
    assert "download failed" in message and "504" in message and "not a lint finding" in message
