"""Run the pinned local lint tools and reject findings added after issue #103.

Ruff, mypy, ESLint, ShellCheck, actionlint, and Helm (#608) on the chart in deploy/helm/."""

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
# Findings pinned when a new rule lands, by file, rule and source. None are now, so there is no file;
# a missing baseline, here or at the base commit, pins nothing.
BASELINE = ROOT / ".lint-baseline.json"
NO_FINDINGS: dict[str, list[list[str]]] = {"ruff": [], "mypy": []}
TOOLS = ROOT / ".lint-tools"
ESLINT = ROOT / "scripts" / "eslint"
PYTHON_DIRS = ("src", "tests", "scripts", "docs/media")
CHART = ROOT / "deploy" / "helm" / "switchboard"
# The value sets the chart is linted with (a chart renders only with a publicUrl): the defaults, then
# the meeting bot, then the bot with the Zoom fix mounted. tests/unit/test_deploy_helm.py checks what each
# renders.
CHART_VALUES = (
    ("publicUrl=https://sb.example.com",),
    ("publicUrl=https://sb.example.com", "meetings.enabled=true"),
    (
        "publicUrl=https://sb.example.com",
        "meetings.enabled=true",
        "meetings.maxCalls=3",
        "meetings.zoomFix.configMap=joinly-zoom-fix",
    ),
)
# tool -> (version, where it comes from, per platform: archive, sha256, the file to take out of it).
# "where" is a GitHub repository (releases/download/<version>/<archive>), or a base URL.
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
    # Helm 4's line is the only one still getting minor releases (3.22 is the last of 3). Its checksums are
    # the ones in the release notes of v4.3.1 (https://github.com/helm/helm/releases/tag/v4.3.1).
    "helm": (
        "v4.3.1",
        "https://get.helm.sh",
        {
            "linux-x86_64": (
                "helm-v4.3.1-linux-amd64.tar.gz",
                "0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b",
                "linux-amd64/helm",
            ),
            "darwin-arm64": (
                "helm-v4.3.1-darwin-arm64.tar.gz",
                "6b4e61c8cd27d99e5a801e63ff25dd5c70b36875c2cd659d3e9ce6b76fc23198",
                "darwin-arm64/helm",
            ),
            "darwin-x86_64": (
                "helm-v4.3.1-darwin-amd64.tar.gz",
                "34bec4eba761ba5c16d1965ee6b8f5b0abe5993974a802274e2a4f0418893d89",
                "darwin-amd64/helm",
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
    version, source, releases = RELEASES[name]
    filename, digest, member = releases[f"{system}-{machine}"]
    binary = TOOLS / f"{name}-{version}-{system}-{machine}"
    if binary.exists():
        return binary
    TOOLS.mkdir(exist_ok=True)
    archive = TOOLS / filename
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        if source.startswith("https://"):
            url = f"{source}/{filename}"
        else:
            url = f"https://github.com/{source}/releases/download/{version}/{filename}"
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
    expected = json.loads(BASELINE.read_text()) if BASELINE.exists() else NO_FINDINGS
    good = True
    for tool, rows in actual.items():
        added = list((Counter(map(tuple, rows)) - Counter(map(tuple, expected[tool]))).elements())
        print(f"{tool}: {len(rows)} existing findings, {len(added)} new")
        for row in added[:30]:
            print("  NEW", *row[:3])
        good &= not added
    old = run("git", "show", f"{base}:.lint-baseline.json", check=False, capture_output=True)
    former = json.loads(old.stdout) if old.returncode == 0 else NO_FINDINGS
    for tool in ("ruff", "mypy"):  # the baseline may only shrink
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


def lint_chart(helm: Path) -> bool:
    """`helm lint --strict` on the chart with each value set, then the chart's own tests with this helm:
    what each render holds (tests/unit/test_deploy_helm.py). Those tests skip where there is no helm, so
    here they are made to find this one, and to fail without it."""
    ok = True
    for sets in CHART_VALUES:
        args = [arg for value in sets for arg in ("--set", value)]
        ok &= run(str(helm), "lint", "--strict", str(CHART), *args, check=False).returncode == 0
    env = {**os.environ, "SWITCHBOARD_HELM": str(helm), "SWITCHBOARD_REQUIRE_HELM": "1"}
    tests = (
        "uv",
        "run",
        "--frozen",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "tests/unit/test_deploy_helm.py",
    )
    ok &= run(*tests, check=False, env=env).returncode == 0
    print("helm chart: clean" if ok else "helm chart: failed")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="fix JavaScript and format Python files")
    parser.add_argument(
        "--base", default="origin/main", help="base commit for changed files and baseline ratchet"
    )
    args = parser.parse_args()
    changed = changed_python(args.base)
    if args.fix and changed:
        run("uv", "run", "--frozen", "ruff", "check", "--fix", *changed, check=False)
    command = ("uv", "run", "--frozen", "ruff", "format", *([] if args.fix else ["--check"]), *PYTHON_DIRS)
    format_ok = run(*command, check=False).returncode == 0
    print("ruff format: whole Python tree")
    npm = ("npm", "--prefix", str(ESLINT))  # ESLint and its config live in scripts/eslint/
    run(*npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund")
    js_ok = run(*npm, "run", "lint:js", *(["--", "--fix"] if args.fix else []), check=False).returncode == 0
    ok = compare(findings(), args.base) and format_ok and js_ok
    shellcheck = installed_tool("shellcheck")
    actionlint = installed_tool("actionlint")
    # shell scripts: *.sh, and the plugins' extensionless ones (bin/listen), not the Windows files beside
    # them (bin/listen.cmd, bin/windows/*.ps1), which are not shell
    not_shell = [":(exclude)*.cmd", ":(exclude)*.ps1"]
    scripts = run(
        "git", "ls-files", "*.sh", "plugins/*/bin/*", *not_shell, capture_output=True
    ).stdout.splitlines()
    ok &= run(str(shellcheck), "-x", *scripts, check=False).returncode == 0
    env = {**os.environ, "PATH": f"{TOOLS}:{os.environ['PATH']}"}
    # actionlint looks up shellcheck by its ordinary name.
    link = TOOLS / "shellcheck"
    if not link.exists():
        link.symlink_to(shellcheck.name)
    ok &= run(str(actionlint), "-color", check=False, env=env).returncode == 0
    print("shellcheck and actionlint: clean" if ok else "lint failed")
    ok &= lint_chart(installed_tool("helm"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
