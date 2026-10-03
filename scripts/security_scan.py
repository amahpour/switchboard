"""Run the fast report-only security checks (DESIGN.md §36, issue #101)."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {"bandit": "1.9.4", "pip-audit": "2.10.1", "zizmor": "1.30.1"}


def run(*args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"{args[0]} could not scan: {result.stderr.strip() or result.stdout.strip()}")
    return result


def tool(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return run("uvx", "--from", f"{name}=={TOOLS[name]}", name, *args)


def bandit(out: Path) -> str:
    path = out / "bandit.json"
    tool("bandit", "-r", "src/switchboard", "-f", "json", "-o", str(path))
    rows = json.loads(path.read_text())["results"]
    return f"Bandit: {len(rows)} finding(s)"


def mermaid_version() -> str:
    source = (ROOT / "src/switchboard/web/static/diagram.js").read_text()
    match = re.search(r"vendor/mermaid/mermaid\.min\.js, (\d+\.\d+\.\d+)", source)
    if match is None:
        raise ValueError("vendored Mermaid version missing from diagram.js")
    return match.group(1)


def mermaid_osv(out: Path) -> str:
    version = mermaid_version()
    payload = json.dumps({"package": {"name": "mermaid", "ecosystem": "npm"}, "version": version})
    request = urllib.request.Request(
        "https://api.osv.dev/v1/query",
        data=payload.encode(),
        headers={"Content-Type": "application/json", "User-Agent": "switchboard-security-report"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    (out / "mermaid-osv.json").write_text(json.dumps({"version": version, **result}, indent=2) + "\n")
    return f"Mermaid {version} (OSV): {len(result.get('vulns', []))} advisory/advisories"


def dependencies(out: Path) -> list[str]:
    requirements = out / "requirements.txt"
    export = subprocess.run(
        (
            "uv",
            "export",
            "--frozen",
            "--format",
            "requirements.txt",
            "--all-groups",
            "--no-emit-project",
            "--no-header",
            "--no-hashes",
            "--output-file",
            str(requirements),
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if export.returncode:
        raise RuntimeError(f"uv could not export uv.lock: {export.stderr.strip()}")
    report = out / "pip-audit.json"
    tool("pip-audit", "-r", str(requirements), "--no-deps", "--format=json", "--output", str(report))
    rows = json.loads(report.read_text())["dependencies"]
    advisories = sum(len(row["vulns"]) for row in rows)
    return [
        f"pip-audit: {advisories} advisory/advisories in {len(rows)} locked packages",
        mermaid_osv(out),
    ]


def zizmor(out: Path) -> str:
    report = out / "zizmor.sarif"
    result = tool("zizmor", "--no-progress", "--format=sarif", ".github/workflows")
    report.write_text(result.stdout)
    rows = json.loads(result.stdout)["runs"][0]["results"]
    return f"zizmor: {len(rows)} finding(s)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("bandit", "dependencies", "zizmor"))
    parser.add_argument("--out", type=Path, help="keep JSON/SARIF reports here for an Actions artifact")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="switchboard-security-") as temporary:
        out = args.out or Path(temporary)
        out.mkdir(parents=True, exist_ok=True)
        lines = ["Security reports (findings are report-only):"]
        for name, scan in (("bandit", bandit), ("dependencies", dependencies), ("zizmor", zizmor)):
            if args.only is None or args.only == name:
                result = scan(out)
                lines.extend(result if isinstance(result, list) else [result])
        summary = "\n".join(lines) + "\n"
        print(summary, end="")
        if args.out:
            (out / "summary.txt").write_text(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
