"""Run the pinned local lint tools and reject findings added after issue #103."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".lint-baseline.json"
TOOLS = ROOT / ".lint-tools"
RELEASES = {
    "shellcheck": (
        "v0.11.0",
        "koalaman/shellcheck",
        {
            "linux-x86_64": (
                "shellcheck-v0.11.0.linux.x86_64.tar.gz",
                "b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6",
                "shellcheck-v0.11.0/shellcheck",
            ),
            "darwin-arm64": (
                "shellcheck-v0.11.0.darwin.aarch64.tar.gz",
                "339b930feb1ea764467013cc1f72d09cd6b869ebf1013296ba9055ab2ffbd26f",
                "shellcheck-v0.11.0/shellcheck",
            ),
            "darwin-x86_64": (
                "shellcheck-v0.11.0.darwin.x86_64.tar.gz",
                "c2c15e08df0e8fbc374c335b230a7ee958c313fa5714817a59aa59f1aa594f51",
                "shellcheck-v0.11.0/shellcheck",
            ),
        },
    ),
    "actionlint": (
        "v1.7.12",
        "rhysd/actionlint",
        {
            "linux-x86_64": (
                "actionlint_1.7.12_linux_amd64.tar.gz",
                "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
                "actionlint",
            ),
            "darwin-arm64": (
                "actionlint_1.7.12_darwin_arm64.tar.gz",
                "aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f",
                "actionlint",
            ),
            "darwin-x86_64": (
                "actionlint_1.7.12_darwin_amd64.tar.gz",
                "5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644",
                "actionlint",
            ),
        },
    ),
}


def run(
    *args: str,
    check: bool = True,
    capture_output: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, text=True, check=check, capture_output=capture_output, env=env)


def installed_tool(name: str) -> Path:
    system = "darwin" if sys.platform == "darwin" else "linux"
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    version, repository, releases = RELEASES[name]
    filename, digest, member = releases[f"{system}-{machine}"]
    binary = TOOLS / f"{name}-{version}-{system}-{machine}"
    if binary.exists():
        return binary
    TOOLS.mkdir(exist_ok=True)
    archive = TOOLS / filename
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        url = f"https://github.com/{repository}/releases/download/{version}/{filename}"
        archive.write_bytes(urllib.request.urlopen(url, timeout=60).read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError(f"checksum mismatch: {filename}")
    with tarfile.open(archive) as tar:
        source = tar.extractfile(member)
        if source is None:
            raise ValueError(f"missing archive member: {member}")
        binary.write_bytes(source.read())
    binary.chmod(0o755)
    return binary


def source_hash(path: str, row: int) -> str:
    lines = (ROOT / path).read_text().splitlines()
    line = lines[row - 1].strip() if 0 < row <= len(lines) else ""
    return hashlib.sha256(line.encode()).hexdigest()[:16]


def findings() -> dict[str, list[list[str]]]:
    ruff = run(
        "uv",
        "run",
        "--frozen",
        "ruff",
        "check",
        "--output-format",
        "json",
        "src/switchboard",
        "tests",
        ".github/scripts",
        "scripts",
        check=False,
        capture_output=True,
    )
    if ruff.returncode not in (0, 1):
        raise RuntimeError(ruff.stderr)
    ruff_rows = []
    for item in json.loads(ruff.stdout):
        path = str(Path(item["filename"]).relative_to(ROOT))
        row = item["location"]["row"]
        ruff_rows.append([path, item["code"], item["message"], source_hash(path, row)])

    mypy = run(
        "uv",
        "run",
        "--frozen",
        "mypy",
        "--no-incremental",
        "src/switchboard",
        check=False,
        capture_output=True,
    )
    if mypy.returncode not in (0, 1):
        raise RuntimeError(mypy.stderr or mypy.stdout)
    mypy_rows = []
    pattern = re.compile(r"^(.+?):(\d+): error: (.+?)  \[([^]]+)\]$")
    for line in mypy.stdout.splitlines():
        if " error:" not in line:
            continue
        match = pattern.match(line)
        if match is None:
            raise ValueError(f"unparsed mypy error: {line}")
        path, row, message, code = match.groups()
        mypy_rows.append([path, code, message, source_hash(path, int(row))])
    return {"ruff": sorted(ruff_rows), "mypy": sorted(mypy_rows)}


def compare(actual: dict[str, list[list[str]]], base: str) -> bool:
    expected = json.loads(BASELINE.read_text())
    good = True
    for tool, rows in actual.items():
        added = list((Counter(map(tuple, rows)) - Counter(map(tuple, expected[tool]))).elements())
        print(f"{tool}: {len(rows)} existing findings, {len(added)} new")
        for row in added[:30]:
            print("  NEW", *row[:3])
        good &= not added
    old = run("git", "show", f"{base}:.lint-baseline.json", check=False, capture_output=True)
    if old.returncode == 0:
        former = json.loads(old.stdout)
        for tool in ("ruff", "mypy"):
            growth = Counter(map(tuple, expected[tool])) - Counter(map(tuple, former[tool]))
            if growth:
                print(f"{tool}: baseline grew by {sum(growth.values())} findings")
                good = False
    return good


def changed_python(base: str) -> list[str]:
    paths = set(run("git", "diff", "--name-only", f"{base}...HEAD", capture_output=True).stdout.splitlines())
    paths.update(run("git", "diff", "--name-only", capture_output=True).stdout.splitlines())
    paths.update(
        run("git", "ls-files", "--others", "--exclude-standard", capture_output=True).stdout.splitlines()
    )
    return sorted(p for p in paths if p.endswith(".py") and (ROOT / p).is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="fix JavaScript and changed Python files")
    parser.add_argument(
        "--base", default="origin/main", help="base commit for changed files and baseline ratchet"
    )
    args = parser.parse_args()
    changed = changed_python(args.base)
    if args.fix and changed:
        run("uv", "run", "--frozen", "ruff", "check", "--fix", *changed, check=False)
    if changed:
        command = ("uv", "run", "--frozen", "ruff", "format", *([] if args.fix else ["--check"]), *changed)
        format_ok = run(*command, check=False).returncode == 0
    else:
        format_ok = True
    print(f"ruff format: {len(changed)} changed Python files")
    run("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund")
    js_ok = run("npm", "run", "lint:js", *(["--", "--fix"] if args.fix else []), check=False).returncode == 0
    ok = compare(findings(), args.base) and format_ok and js_ok
    shellcheck = installed_tool("shellcheck")
    actionlint = installed_tool("actionlint")
    scripts = run("git", "ls-files", "*.sh", capture_output=True).stdout.splitlines()
    ok &= run(str(shellcheck), "-x", *scripts, check=False).returncode == 0
    env = {**os.environ, "PATH": f"{TOOLS}:{os.environ['PATH']}"}
    # actionlint looks up shellcheck by its ordinary name.
    link = TOOLS / "shellcheck"
    if not link.exists():
        link.symlink_to(shellcheck.name)
    ok &= run(str(actionlint), "-color", check=False, env=env).returncode == 0
    print("shellcheck and actionlint: clean" if ok else "lint failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
