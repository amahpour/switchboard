"""The upload line an agent gets must reach a file written under a home-relative path (#587).

The emitted word is run through a real shell with curl swapped for ``cat``: the shell is what
expands ``"$HOME"/'rest'`` at upload time, so the test proves the expansion (and the quoting)
rather than the upload itself.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from switchboard.files import share_line

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell for the ~ expansion")


def test_the_share_line_reaches_a_file_under_a_temp_home(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    work = home / "work"
    work.mkdir(parents=True)
    target = work / "build.log"
    target.write_text("payload")

    monkeypatch.setenv("HOME", str(home))
    line = share_line("build.log", "~/work/build.log", "http://switchboard.localhost:7419", "tok")

    # The word after `-T ` is what curl would upload; cat proves the shell found the file.
    word = line.split(" -T ", 1)[1].split(" ", 1)[0]
    r = subprocess.run(["sh", "-c", f"cat {word}"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "payload"
