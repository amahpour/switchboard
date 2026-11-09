"""Third-party container images are named with a digest (issue #660).

A tag can be moved onto other bytes. A digest cannot. The pin is
``name:tag@sha256:<64 hex>``, and the digest is the multi-platform index so a
Mac and a Windows machine resolve the same image.

Switchboard's own image stays tag-only. ``release.py`` writes
``ghcr.io/amahpour/switchboard:X.Y.Z`` in the release pull request, before the
release workflow has built that image, so the digest does not exist yet.
``switchboard:<tag>`` is an image this repo builds and does not publish.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from test_tree_scan import repo_texts

ROOT = Path(__file__).resolve().parents[2]
OWN = "ghcr.io/amahpour/switchboard:"
# Docker Hub library images this repo uses. ``node`` is not here: ``node:fs`` is a module import.
# A new library name in this set, written ``name:tag`` without a digest, fails the scan below.
LIBRARY = "alpine|busybox|caddy|debian|python"
TAG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{1,127}"  # at least two characters; ``joinly:v`` is a prefix
DIGEST = r"@sha256:[0-9a-f]{64}"
# A registry host (ghcr.io, quay.io, example.com), not a filesystem path. The last
# label is a registry TLD, so ``python3.12/site-packages/...:ro`` is not an image.
TLD = "aws|com|dev|io|net|org"
HOST = rf"[a-z0-9]+(?:[.-][a-z0-9]+)*\.(?:{TLD})(?::[0-9]{{2,5}})?"
NAME = r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
REF = re.compile(rf"(?<![A-Za-z0-9.@])(?P<ref>{HOST}/{NAME}:{TAG})(?P<digest>{DIGEST})?(?![A-Za-z0-9._-])")
LIBRARY_REF = re.compile(
    rf"(?<![A-Za-z0-9.@/])(?P<ref>(?:{LIBRARY}):{TAG})(?P<digest>{DIGEST})?(?![A-Za-z0-9._-])"
)
# ``image:`` and ``FROM`` also catch a library name the set above missed.
CONTEXT_REF = re.compile(
    rf"(?m)(?:image:|FROM)\s+[\"']?(?P<ref>[a-z0-9][a-z0-9._-]*:{TAG})(?P<digest>{DIGEST})?"
)
REPO_LINE = re.compile(r"(?m)^(?P<indent>\s*)repository:\s*(?P<repo>\S+)\s*$")
SHA = re.compile(r"^sha256:[0-9a-f]{64}$")


def _exempt(ref: str) -> bool:
    """Switchboard's published image, and a tag this repo builds locally."""
    if ref.startswith(OWN):
        return True
    return ref.split(":", 1)[0] == "switchboard"


def unpinned(text: str) -> list[tuple[int, str]]:
    """(line, reference) for every third-party image named without a digest."""
    hits: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()

    def add(line: int, ref: str) -> None:
        if (line, ref) not in seen:
            seen.add((line, ref))
            hits.append((line, ref))

    for rx in (REF, LIBRARY_REF, CONTEXT_REF):
        for m in rx.finditer(text):
            if m.group("digest") or _exempt(m.group("ref")):
                continue
            add(text.count("\n", 0, m.start()) + 1, m.group("ref"))
    for m in REPO_LINE.finditer(text):
        repo = m.group("repo")
        if repo == "ghcr.io/amahpour/switchboard" or "." not in repo.split("/", 1)[0]:
            continue
        rest = text[m.end() : m.end() + 600]
        block = rest.split("\n\n", 1)[0]
        pinned = False
        for line in block.splitlines():
            parts = line.strip().split()
            if parts and parts[0] == "digest:" and SHA.fullmatch(parts[-1]):
                pinned = True
        if not pinned:
            add(text.count("\n", 0, m.start()) + 1, repo)
    return hits


def test_a_third_party_image_is_named_with_its_digest() -> None:
    """Fails if a third-party image is a movable tag. Switchboard's own image is
    tag-only on purpose (DESIGN.md §30.5): the release pins it before it is built.
    """
    hits = [f"{rel}:{n}: {ref}" for rel, text in repo_texts() for n, ref in unpinned(text)]
    chart = ROOT / "deploy/helm/switchboard"
    image = yaml.safe_load((chart / "values.yaml").read_text())["meetings"]["image"]
    for key in ("digest", "cudaDigest"):
        if not SHA.fullmatch(str(image.get(key, ""))):
            hits.append(f"deploy/helm/switchboard/values.yaml: meetings.image.{key}")
    template = (chart / "templates/statefulset.yaml").read_text()
    broker = '{{ .Values.image.repository }}:{{ include "switchboard.imageTag" . }}'
    if broker not in template or broker + "@" in template:
        hits.append("deploy/helm/switchboard/templates/statefulset.yaml: broker image")
    if 'include "switchboard.joinlyDigest"' not in template:
        hits.append("deploy/helm/switchboard/templates/statefulset.yaml: joinly image")
    assert not hits, "third-party image without a digest:\n" + "\n".join(hits)


def test_the_digest_check_ignores_switchboards_own_image_and_catches_a_bare_tag() -> None:
    """The scan is the guard. Fails if it misses a tag-only third-party name, or if it
    demands a digest for the image the release pins before that image exists.
    """
    bare = "caddy:" + "2.10-alpine"
    pinned = bare + "@sha256:" + "ab" * 32
    own = "ghcr.io/amahpour/switchboard:" + "1.2.3"
    local = "switchboard:" + "relay-test"
    other = "ghcr.io/example/tool:" + "1.0"
    split = "repository: " + "ghcr.io/example/tool\n    tag: 1\n"
    assert unpinned(bare) == [(1, bare)]
    assert unpinned("FROM " + bare) == [(1, bare)]
    assert unpinned(pinned) == []
    assert unpinned("image: " + own) == []
    assert unpinned("image: " + local) == []
    assert unpinned(other) == [(1, other)]
    assert unpinned(split) == [(1, "ghcr.io/example/tool")]
    assert unpinned(split + "    digest: sha256:" + "cd" * 32) == []
